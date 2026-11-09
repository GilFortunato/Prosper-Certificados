import hashlib
import hmac
import json
from io import BytesIO
from pathlib import PurePosixPath

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash

from . import data
from .formats import (
    BUILT_INS, SPREADSHEET_LIMIT, TEMPLATE_LIMIT, RenderError, normalize_key,
    parse_spreadsheet, parse_validation_spreadsheet, scalar_text, spreadsheet_model, template_fields,
)
from .portal import certificate_values, generate_pdf, pdf_download, rate_limit


admin = Blueprint("admin", __name__, url_prefix="/admin")

ISSUANCE_ENDPOINTS = {
    "admin.upload_template", "admin.upload_list", "admin.download_spreadsheet", "admin.preview", "admin.release",
}


@admin.before_request
def check_issuance_mode():
    if not current_app.config["ISSUANCE_ENABLED"] and request.endpoint in ISSUANCE_ENDPOINTS:
        abort(404)


def auth_fingerprint():
    return hashlib.sha256(current_app.config["ADMIN_PASSWORD_HASH"].encode()).hexdigest()


def authenticated():
    return hmac.compare_digest(session.get("admin", ""), auth_fingerprint())


@admin.route("/entrar", methods=["GET", "POST"])
def login():
    if authenticated():
        return redirect(url_for("admin.index"))
    if request.method == "GET":
        return render_template("login.html")

    limited = rate_limit("admin", 8, 600)
    if limited:
        return limited
    password = request.form.get("password", "")
    if len(password) > 128 or not check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], password):
        return render_template("login.html", error="A senha informada é inválida."), 401
    session.clear()
    session.permanent = True
    session["admin"] = auth_fingerprint()
    return redirect(url_for("admin.index"))


@admin.post("/sair")
def logout():
    session.clear()
    return redirect(url_for("portal.issue"))


@admin.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            details = formation_details()
        except data.InputError as error:
            return render_template("admin.html", formations=data.formations(), error=str(error)), 400
        formation_id = data.create_formation(*details)
        return redirect(url_for("admin.detail", formation_id=formation_id))
    return render_template("admin.html", formations=data.formations())


def formation_details():
    program = scalar_text(request.form.get("program", ""), 150)
    title = scalar_text(request.form.get("title", ""), 150)
    workload = scalar_text(request.form.get("workload", ""), 40)
    period = scalar_text(request.form.get("period", ""), 100)
    if not program or not title:
        raise data.InputError("Preencha o programa e o nome da formação.")
    return program, title, workload, period


def get_formation(formation_id):
    formation = data.formation(formation_id)
    if formation is None:
        abort(404)
    return formation


def custom_fields(formation):
    return {field: label for field, label in json.loads(formation["template_fields"]).items() if field not in BUILT_INS}


def read_upload(field, extension, limit):
    upload = request.files.get(field)
    filename = PurePosixPath((upload.filename or "").replace("\\", "/")).name if upload else ""
    if not filename.lower().endswith(extension):
        raise data.InputError(f"Selecione um arquivo {extension}.")
    content = upload.stream.read(limit + 1)
    if not content or len(content) > limit:
        raise data.InputError(f"O arquivo precisa ter conteúdo e até {limit // (1024 * 1024)} MB.")
    return filename[:180], content


def return_detail(formation_id, message, category="success"):
    flash(message, category)
    return redirect(url_for("admin.detail", formation_id=formation_id))


@admin.get("/formacoes/<int:formation_id>")
def detail(formation_id):
    formation = get_formation(formation_id)
    return render_template(
        "formation.html", formation=formation, fields=json.loads(formation["template_fields"]),
        custom_fields=custom_fields(formation),
    )


@admin.post("/formacoes/<int:formation_id>/validacao")
def upload_validation_list(formation_id):
    get_formation(formation_id)
    try:
        _, content = read_upload("validation_list", ".xlsx", SPREADSHEET_LIMIT)
        records = parse_validation_spreadsheet(
            content, request.form.get("issued_on", ""), request.form.get("skills", ""),
        )
        added, updated, skipped = data.register_certificates(formation_id, records)
    except data.InputError as error:
        return return_detail(formation_id, str(error), "error")
    return return_detail(
        formation_id, f"Importação concluída. Chaves novas ({added}), atualizadas ({updated}) e mantidas ({skipped}).",
    )


@admin.get("/formacoes/<int:formation_id>/validacao/planilha-modelo")
def download_validation_spreadsheet(formation_id):
    get_formation(formation_id)
    return download_xlsx(spreadsheet_model({}, for_validation=True), "modelo-validacao.xlsx")


@admin.post("/formacoes/<int:formation_id>/dados")
def update_details(formation_id):
    get_formation(formation_id)
    try:
        data.update_formation(formation_id, *formation_details())
    except data.InputError as error:
        return return_detail(formation_id, str(error), "error")
    return return_detail(formation_id, "Dados da formação atualizados.")


@admin.post("/formacoes/<int:formation_id>/modelo")
def upload_template(formation_id):
    formation = get_formation(formation_id)
    try:
        if formation["released_at"]:
            raise data.InputError("O modelo está em uso nesta formação.")
        filename, content = read_upload("template", ".pptx", TEMPLATE_LIMIT)
        fields = template_fields(content)
        data.save_template(formation_id, content, filename, fields)
    except data.InputError as error:
        return return_detail(formation_id, str(error), "error")
    return return_detail(formation_id, "Modelo PowerPoint salvo.")


@admin.post("/formacoes/<int:formation_id>/lista")
def upload_list(formation_id):
    formation = get_formation(formation_id)
    try:
        if not formation["template_name"]:
            raise data.InputError("Envie o modelo PowerPoint primeiro para identificar os campos da planilha.")
        _, content = read_upload("spreadsheet", ".xlsx", SPREADSHEET_LIMIT)
        people = parse_spreadsheet(content, custom_fields(formation), data.cpf_digest)
        added, skipped = data.save_participants(formation_id, formation["revision"], people)
    except data.InputError as error:
        return return_detail(formation_id, str(error), "error")
    message = f"{added} participante(s) importado(s). {skipped} já estavam na lista."
    return return_detail(formation_id, message)


@admin.get("/formacoes/<int:formation_id>/planilha-modelo")
def download_spreadsheet(formation_id):
    formation = get_formation(formation_id)
    return download_xlsx(spreadsheet_model(custom_fields(formation)), "modelo-participantes.xlsx")


def download_xlsx(content, filename):
    return send_file(
        BytesIO(content), as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_preview(formation, *, use_participant=False):
    if not formation["template"]:
        raise data.InputError("Envie um modelo PowerPoint para gerar a prévia.")
    fields = {field: f"Exemplo de {label}" for field, label in custom_fields(formation).items()}
    name, key = "Marina de Almeida dos Santos", "DEMO-H7Z4-Q5K9"
    if use_participant and formation["name"]:
        name = formation["name"]
        fields.update(json.loads(formation["fields"]))
    values = certificate_values(formation, name, key, fields, data.timestamp())
    return generate_pdf(formation["template"], values)


@admin.post("/formacoes/<int:formation_id>/previa")
def preview(formation_id):
    formation = data.preview_source(formation_id)
    if formation is None:
        abort(404)
    try:
        pdf = render_preview(formation)
    except (data.InputError, RenderError) as error:
        return return_detail(formation_id, str(error), "error")
    return pdf_download(pdf, "previa-certificado.pdf")


@admin.post("/formacoes/<int:formation_id>/liberar")
def release(formation_id):
    formation = data.preview_source(formation_id)
    if formation is None:
        abort(404)
    if formation["released_at"]:
        return return_detail(formation_id, "A autoemissão desta formação está liberada.")
    try:
        if not formation["name"] or not formation["template"]:
            raise data.InputError("Envie o PowerPoint e a lista Excel antes de liberar a emissão.")
        fields = json.loads(formation["template_fields"])
        if "carga horaria" in fields and not formation["workload"]:
            raise data.InputError("Preencha a carga horária nos dados da formação. O PowerPoint usa esse campo.")
        if "periodo" in fields and not formation["period"]:
            raise data.InputError("Preencha o período nos dados da formação. O PowerPoint usa esse campo.")
        render_preview(formation, use_participant=True)
        data.release_formation(formation_id, formation["revision"], BUILT_INS)
    except (data.InputError, RenderError) as error:
        return return_detail(formation_id, str(error), "error")
    return return_detail(formation_id, "Autoemissão liberada para os participantes da lista.")


@admin.post("/formacoes/<int:formation_id>/revogar")
def revoke(formation_id):
    formation = get_formation(formation_id)
    try:
        key = normalize_key(request.form.get("key", ""))
        allow_unissued = bool(current_app.config["ISSUANCE_ENABLED"] and formation["released_at"])
        data.revoke_certificate(formation_id, key, allow_unissued=allow_unissued)
    except data.InputError as error:
        return return_detail(formation_id, str(error), "error")
    return return_detail(formation_id, "Chave revogada.")
