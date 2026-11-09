import re
import shutil
import stat
import subprocess
import unicodedata
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from datetime import date, datetime
from io import BytesIO
from itertools import accumulate
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from defusedxml.common import DefusedXmlException
from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .data import InputError, Participant, ValidationRecord


TEMPLATE_LIMIT = 12 * 1024 * 1024
SPREADSHEET_LIMIT = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 80
CPF = "cpf"
NAME = "nome"
KEY = "chave de validacao"
ISSUE_DATE = "data de emissao"
SKILLS = "habilidades"
BUILT_INS = {NAME, KEY, "programa", "formacao", "carga horaria", "periodo", ISSUE_DATE}
REQUIRED_TAGS = {NAME: "<<Nome>>", KEY: "<<Chave de validação>>"}
ALIASES = {
    "nome completo": NAME,
    "nome do participante": NAME,
    "participante": NAME,
    "cpf do participante": CPF,
    "cpf participante": CPF,
    "chave": KEY,
    "chave validadora": KEY,
    "chave validacao": KEY,
    "chave do certificado": KEY,
    "chave certificado": KEY,
    "codigo de validacao": KEY,
    "codigo de verificacao": KEY,
    "codigo do certificado": KEY,
    "curso": "formacao",
}
PLACEHOLDER = re.compile(r"<<([^<>\r\n]{1,80})>>")
VALID_CPF = re.compile(r"(?:[0-9]{11}|[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2})")
VALID_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{6,62}[A-Za-z0-9]")
SLIDE_XML = re.compile(r"ppt/slides/slide[0-9]+\.xml")
DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
MAIN_PART = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


class RenderError(RuntimeError):
    pass


def field_name(value):
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = " ".join(plain.split())
    return ALIASES.get(normalized, normalized)


def normalize_cpf(value):
    value = value.strip()
    if not VALID_CPF.fullmatch(value):
        raise InputError("Informe um CPF com 11 dígitos.")
    digits = value.replace(".", "").replace("-", "")
    if len(set(digits)) == 1:
        raise InputError("Informe um CPF válido.")
    for length in (9, 10):
        total = sum(int(digit) * weight for digit, weight in zip(digits, range(length + 1, 1, -1)))
        if (total * 10 % 11) % 10 != int(digits[length]):
            raise InputError("Informe um CPF válido.")
    return digits


def normalize_key(value):
    value = value.strip()
    if not VALID_KEY.fullmatch(value) or len(value.replace("-", "").replace("_", "")) < 8:
        raise InputError("Use uma chave de 8 a 64 caracteres, com pelo menos 8 letras ou números.")
    return value.upper()


def normalize_issue_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[0-9]{8}", value):
            value = f"{value[:2]}/{value[2:4]}/{value[4:]}"
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(value, pattern).date().isoformat()
            except ValueError:
                continue
    raise InputError("Informe uma data de emissão válida.")


def split_skills(value):
    return [item.strip() for item in re.split(r"[\r\n;|]", value) if item.strip()]


def normalize_skills(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise InputError("Informe as habilidades como texto de até 4.000 caracteres.")
    skills = {}
    for item in split_skills(value):
        label = scalar_text(item, 150)
        skills.setdefault(label.casefold(), label)
    if not skills or len(skills) > 30:
        raise InputError("Informe de 1 a 30 habilidades.")
    return "\n".join(skills.values())


def scalar_text(value, limit=350):
    text = " ".join(unicodedata.normalize("NFC", str(value)).split())
    if len(text) > limit:
        raise InputError(f"O texto excede {limit} caracteres.")
    if any(unicodedata.category(char) in {"Cc", "Cs"} or char in "\ufffe\uffff" for char in text):
        raise InputError("O texto tem caracteres inválidos.")
    return text


@contextmanager
def checked_package(content, max_expanded):
    try:
        with ZipFile(BytesIO(content)) as package:
            entries = package.infolist()
            if len(entries) > 1500 or sum(entry.file_size for entry in entries) > max_expanded:
                raise InputError("O conteúdo descompactado do arquivo excede o limite.")
            seen = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (
                    path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or name in seen or entry.flag_bits & 1 or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                ):
                    raise InputError("O arquivo do Office contém itens incompatíveis.")
                seen.add(name)
            yield package
    except (BadZipFile, OSError, KeyError, etree.XMLSyntaxError, DefusedXmlException) as error:
        raise InputError("Não conseguimos ler o arquivo do Office. Confira se ele abre e envie uma cópia.") from error


def xml_root(content):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise InputError("O arquivo contém XML com uma declaração incompatível.")
    return root


def text_groups(slide):
    for paragraph in slide.iter(f"{DRAWING}p"):
        nodes = []
        for item in paragraph:
            if item.tag == f"{DRAWING}br":
                if nodes:
                    yield nodes
                nodes = []
                continue
            if item.tag in (f"{DRAWING}r", f"{DRAWING}fld"):
                node = item.find(f"{DRAWING}t")
                if node is not None and node.text:
                    nodes.append(node)
        if nodes:
            yield nodes


def template_fields(content):
    fields = {}
    with checked_package(content, 72 * 1024 * 1024) as package:
        types = xml_root(package.read("[Content_Types].xml"))
        if not any(
            part.get("PartName") == "/ppt/presentation.xml" and part.get("ContentType") == MAIN_PART
            for part in types.iter(f"{CONTENT_TYPES}Override")
        ):
            raise InputError("Envie uma apresentação PowerPoint no formato .pptx.")
        presentation = xml_root(package.read("ppt/presentation.xml"))
        if presentation.tag != f"{PRESENTATION}presentation":
            raise InputError("Envie uma apresentação PowerPoint no formato .pptx.")
        for name in package.namelist():
            lower_name = name.casefold()
            if lower_name.startswith(("ppt/embeddings/", "ppt/activex/")) or "vbaproject" in lower_name:
                raise InputError("Salve o modelo sem macros ou objetos incorporados.")
            if name.endswith(".rels"):
                root = xml_root(package.read(name))
                for rel in root.iter(f"{RELATIONSHIPS}Relationship"):
                    if rel.get("TargetMode", "").lower() != "external":
                        continue
                    is_web_link = rel.get("Type", "").endswith("/hyperlink") and rel.get("Target", "").startswith("https://")
                    if not is_web_link:
                        raise InputError("Incorpore imagens e mídias ao PowerPoint antes de enviar o modelo.")
            if SLIDE_XML.fullmatch(name):
                slide = xml_root(package.read(name))
                if slide.get("show") in {"0", "false"}:
                    continue
                for nodes in text_groups(slide):
                    text = "".join(node.text for node in nodes)
                    for match in PLACEHOLDER.finditer(text):
                        label = scalar_text(match[1], 80)
                        field = field_name(label)
                        if not field:
                            raise InputError("Preencha o nome de cada campo entre os sinais << e >>.")
                        if field == CPF:
                            raise InputError("O CPF é usado para liberar o download. Remova o campo <<CPF>> do modelo.")
                        fields.setdefault(field, label)
    missing = [tag for field, tag in REQUIRED_TAGS.items() if field not in fields]
    if missing:
        raise InputError("Inclua estes campos em caixas de texto dos slides: " + ", ".join(missing) + ".")
    if len(fields.keys() - BUILT_INS) > MAX_COLUMNS - 3:
        raise InputError("O modelo excede o limite de campos personalizados da planilha.")
    return fields


def fill_text(nodes, values):
    parts = [node.text for node in nodes]
    ends = list(accumulate(map(len, parts)))
    starts = [0, *ends[:-1]]
    matches = list(PLACEHOLDER.finditer("".join(parts)))
    for match in reversed(matches):
        first = bisect_right(ends, match.start())
        last = bisect_left(ends, match.end())
        prefix = parts[first][:match.start() - starts[first]]
        suffix = parts[last][match.end() - starts[last]:]
        value = values[field_name(match[1])]
        parts[first:last + 1] = [prefix + value] + [""] * (last - first)
        parts[last] += suffix
    for node, part in zip(nodes, parts):
        node.text = part
        if part != part.strip():
            node.set(XML_SPACE, "preserve")
    return bool(matches)


def merge_template(content, values):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, "w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if SLIDE_XML.fullmatch(entry.filename):
                slide = xml_root(data)
                if slide.get("show") not in {"0", "false"}:
                    changed = [fill_text(nodes, values) for nodes in text_groups(slide)]
                    if any(changed):
                        data = etree.tostring(slide, encoding="UTF-8", xml_declaration=True)
            target.writestr(entry, data)
    return output.getvalue()


def render_pdf(content, values, soffice):
    executable = shutil.which(soffice)
    if not executable:
        raise RenderError("O conversor de PDF não está disponível.")
    with TemporaryDirectory(prefix="prosper-") as directory:
        workspace = Path(directory)
        deck = workspace / "certificado.pptx"
        pdf = workspace / "certificado.pdf"
        deck.write_bytes(merge_template(content, values))
        try:
            subprocess.run(
                [executable, f"-env:UserInstallation={(workspace / 'profile').as_uri()}",
                 "--headless", "--norestore", "--convert-to", "pdf:impress_pdf_Export",
                 "--outdir", directory, str(deck)],
                check=True, timeout=45, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"HOME": directory, "TMPDIR": directory, "LANG": "C.UTF-8", "PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
            if pdf.stat().st_size > 24 * 1024 * 1024:
                raise RenderError("O PDF excede 24 MB. Use imagens menores no PowerPoint.")
            content = pdf.read_bytes()
        except (OSError, subprocess.SubprocessError) as error:
            raise RenderError("O PowerPoint não pôde ser convertido em PDF.") from error
        if not content.startswith(b"%PDF-"):
            raise RenderError("O PowerPoint não pôde ser convertido em PDF.")
        return content


def raw_cell_value(cell):
    if cell.data_type in {"f", "e"}:
        raise InputError("Use valores fixos nas colunas importadas. Cole os resultados das fórmulas como valores.")
    return cell.value


def cell_value(cell, limit=350):
    value = raw_cell_value(cell)
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d/%m/%Y")
    if type(value) is int and re.fullmatch(r"0+", cell.number_format):
        return str(value).zfill(min(len(cell.number_format), limit))
    return scalar_text(value, limit)


def spreadsheet_columns(header, required, optional):
    allowed = {**required, **optional}
    indexes = {}
    for index, cell in enumerate(header):
        if not isinstance(cell.value, str):
            continue
        field = field_name(cell.value)
        if field not in allowed:
            continue
        if field in indexes:
            raise InputError(f"A coluna {allowed[field]} aparece mais de uma vez na primeira linha.")
        indexes[field] = index
    missing = [label for field, label in required.items() if field not in indexes]
    if missing:
        raise InputError("Inclua estas colunas na primeira linha: " + ", ".join(missing) + ".")
    return indexes


def parse_spreadsheet(content, custom_fields, digest):
    required = {NAME: "Nome", CPF: "CPF", KEY: "Chave de validação", **custom_fields}
    return read_spreadsheet(
        content, required, {},
        lambda number, row, indexes: parse_row(number, row, indexes, custom_fields, digest),
    )


def parse_validation_spreadsheet(content, default_date, default_skills):
    required = {NAME: "Nome", KEY: "Chave de validação"}
    optional = {ISSUE_DATE: "Data de emissão", SKILLS: "Habilidades"}
    return read_spreadsheet(
        content, required, optional,
        lambda number, row, indexes: parse_validation_row(number, row, indexes, default_date, default_skills),
    )


def read_spreadsheet(content, required, optional, read_record):
    with checked_package(content, 36 * 1024 * 1024):
        try:
            book = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        except (ValueError, InvalidFileException, TypeError, IndexError) as error:
            raise InputError("Envie uma planilha do Excel no formato .xlsx.") from error
        try:
            if not book.worksheets:
                raise InputError("A planilha não tem abas com dados.")
            sheet = book.worksheets[0]
            sheet.reset_dimensions()
            rows = sheet.iter_rows(max_col=MAX_COLUMNS)
            header = next(rows, ())
            indexes = spreadsheet_columns(header, required, optional)
            participants, seen_keys, seen_cpfs, errors = [], set(), set(), []
            for row_number, row in enumerate(rows, 2):
                if row_number > MAX_ROWS + 1:
                    raise InputError(f"Use até {MAX_ROWS} linhas após o cabeçalho.")
                if all(row[index].value in (None, "") for index in indexes.values()):
                    continue
                try:
                    participant = read_record(row_number, row, indexes)
                    if participant.validation_key in seen_keys:
                        raise InputError("A chave já aparece em outra linha da planilha.")
                    if CPF in required and participant.cpf_digest in seen_cpfs:
                        raise InputError("O CPF já aparece em outra linha da planilha.")
                except InputError as error:
                    errors.append(f"Linha {row_number} · {error}")
                    if len(errors) >= 8:
                        break
                    continue
                seen_keys.add(participant.validation_key)
                if CPF in required:
                    seen_cpfs.add(participant.cpf_digest)
                participants.append(participant)
            if errors:
                raise InputError("\n".join(errors))
            if not participants:
                raise InputError("A primeira aba precisa ter pelo menos um participante.")
            return participants
        finally:
            book.close()


def certificate_identity(row, indexes):
    name = cell_value(row[indexes[NAME]], 180)
    if not name:
        raise InputError("Preencha o nome do participante.")
    key_cell = row[indexes[KEY]]
    if key_cell.value is not None and key_cell.data_type != "s":
        raise InputError("Formate a coluna Chave de validação como texto antes de preencher.")
    return name, normalize_key(cell_value(key_cell, 64))


def parse_validation_row(row_number, row, indexes, default_date, default_skills):
    name, key = certificate_identity(row, indexes)
    issued_on = cell_or_default(row, indexes, ISSUE_DATE, default_date)
    skills = cell_or_default(row, indexes, SKILLS, default_skills)
    if issued_on == "":
        raise InputError("Preencha a data de emissão na planilha ou nos dados do lote.")
    if skills == "":
        raise InputError("Preencha as habilidades na planilha ou nos dados do lote.")
    return ValidationRecord(
        row_number, name, key, normalize_issue_date(issued_on), normalize_skills(skills),
    )


def cell_or_default(row, indexes, field, default):
    value = raw_cell_value(row[indexes[field]]) if field in indexes else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return default.strip()
    return value


def parse_row(row_number, row, indexes, custom_fields, digest):
    name, key = certificate_identity(row, indexes)
    cpf_cell = row[indexes[CPF]]
    cpf_text = cell_value(cpf_cell, 20)
    if type(cpf_cell.value) is int and 0 <= cpf_cell.value <= 99_999_999_999:
        cpf_text = str(cpf_cell.value).zfill(11)
    cpf = normalize_cpf(cpf_text)
    fields = {}
    for field, label in custom_fields.items():
        value = cell_value(row[indexes[field]])
        if not value:
            raise InputError(f"Preencha a coluna {label}.")
        fields[field] = value
    return Participant(row_number, name, digest(cpf), key, fields)


def spreadsheet_model(custom_fields, *, for_validation=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "Participantes"
    headers = ["Nome", "Chave de validação", "Data de emissão", "Habilidades"] if for_validation else [
        "Nome", "CPF", "Chave de validação", *custom_fields.values(),
    ]
    sheet.append(headers)
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.data_type = "s"
        column = sheet.column_dimensions[cell.column_letter]
        column.width = max(22, len(cell.value) + 6)
        if field_name(cell.value) in {CPF, KEY}:
            column.number_format = "@"
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()
