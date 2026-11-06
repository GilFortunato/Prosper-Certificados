import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from lxml import etree
from openpyxl import Workbook
from werkzeug.security import generate_password_hash

from certificados import create_app, data
from certificados.config import PASSWORD_METHOD
from certificados.formats import DRAWING, RenderError, merge_template, template_fields


def powerpoint():
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", """
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Override PartName="/ppt/presentation.xml"
                ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
            </Types>
        """)
        archive.writestr("ppt/presentation.xml", """
            <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>
        """)
        archive.writestr("ppt/slides/slide1.xml", """
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p>
                <a:r><a:rPr b="1"/><a:t>Certificado de &lt;&lt;No</a:t></a:r>
                <a:r><a:t>me&gt;&gt; · &lt;&lt;Turma&gt;&gt;</a:t></a:r>
              </a:p>
            </p:sld>
        """)
        archive.writestr("ppt/slides/slide2.xml", """
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p><a:r><a:t>&lt;&lt;chave de validação&gt;&gt; · &lt;&lt;Nome&gt;&gt;</a:t></a:r></a:p>
            </p:sld>
        """)
    return output.getvalue()


def spreadsheet(*rows):
    book = Workbook()
    book.active.append(["Nome completo", "CPF", "Chave Validadora", "Turma", "E-mail"])
    for row in rows:
        book.active.append(row)
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class FormatsTest(unittest.TestCase):
    def test_tags_are_replaced_across_runs_and_slides(self):
        template = powerpoint()
        self.assertEqual({"nome", "chave de validacao", "turma"}, set(template_fields(template)))
        merged = merge_template(
            template, {"nome": "Ana & Lívia", "turma": "B", "chave de validacao": "TESTE-289JD82K"},
        )
        with ZipFile(BytesIO(merged)) as result:
            front = etree.fromstring(result.read("ppt/slides/slide1.xml"))
            back = etree.fromstring(result.read("ppt/slides/slide2.xml"))
        self.assertEqual(front.find(f".//{DRAWING}rPr").get("b"), "1")
        self.assertEqual("".join(n.text or "" for n in front.iter(f"{DRAWING}t")), "Certificado de Ana & Lívia · B")
        self.assertEqual("".join(n.text for n in back.iter(f"{DRAWING}t")), "TESTE-289JD82K · Ana & Lívia")


class PortalTest(unittest.TestCase):
    PASSWORD = "senha de teste descartável"
    CPF = "01234567890"
    KEY = "DEMO-X4G6-9FR5"
    PDF = b"%PDF-1.7\nSynthetic test output"

    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash(cls.PASSWORD, method=PASSWORD_METHOD)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": str(Path(folder.name) / "certificados.sqlite3"),
            "SECRET_KEY": os.urandom(32),
            "CPF_KEY": os.urandom(32),
            "ADMIN_PASSWORD_HASH": self.password_hash,
            "SESSION_COOKIE_SECURE": False,
        })
        self.admin = self.app.test_client()
        self.visitor = self.app.test_client()
        self.admin.get("/admin/entrar")
        self.visitor.get("/")

    def post(self, client, path, fields=None, **kwargs):
        with client.session_transaction() as session:
            token = session["_csrf"]
        return client.post(path, data={"_csrf": token, **(fields or {})}, **kwargs)

    def sign_in(self):
        result = self.post(self.admin, "/admin/entrar", {"password": self.PASSWORD})
        self.assertEqual(result.status_code, 302)
        self.admin.get("/admin/")

    def create_formation(self, program="Programa Horizonte", title="Formação em IA"):
        result = self.post(
            self.admin, "/admin/", {"program": program, "title": title, "workload": "90 horas"},
        )
        self.assertEqual(result.status_code, 302)
        path = result.location
        result = self.post(
            self.admin, f"{path}/modelo", {"template": (BytesIO(powerpoint()), "modelo.pptx")},
            follow_redirects=True,
        )
        self.assertIn("Modelo PowerPoint salvo.", result.get_data(as_text=True))
        return path

    def import_list(self, path, *rows):
        return self.post(
            self.admin, f"{path}/lista", {"spreadsheet": (BytesIO(spreadsheet(*rows)), "lista.xlsx")},
            follow_redirects=True,
        ).get_data(as_text=True)

    def verify(self, key):
        return self.post(self.visitor, "/validar", {"key": key}).get_data(as_text=True)

    def issue(self, cpf, key):
        return self.post(self.visitor, "/", {"cpf": cpf, "key": key})

    def test_issue_and_validation_use_different_credentials(self):
        self.assertEqual(self.visitor.get("/admin/").status_code, 302)
        self.assertEqual(self.visitor.post("/admin/", data={"program": "X"}).status_code, 302)
        self.sign_in()
        self.assertEqual(self.admin.post("/admin/", data={"program": "X"}).status_code, 400)
        path = self.create_formation()
        message = self.import_list(path, ["Ana Fictícia", int(self.CPF), self.KEY, "B", "demo@example.com"])
        self.assertIn("1 participante(s) importado(s)", message)
        self.assertNotIn("Certificado válido", self.verify(self.KEY))

        with patch("certificados.portal.render_pdf", return_value=self.PDF) as converter:
            preview = self.post(self.admin, f"{path}/previa")
            self.assertEqual(preview.mimetype, "application/pdf")
            self.assertEqual(self.issue(self.CPF, self.KEY).status_code, 404)
            release = self.post(self.admin, f"{path}/liberar", follow_redirects=True)
            self.assertIn("Autoemissão liberada", release.get_data(as_text=True))
            self.assertNotIn("Certificado válido", self.verify(self.KEY))
            self.assertEqual(self.issue("11144477735", self.KEY).status_code, 404)
            issued = self.issue("012.345.678-90", self.KEY.lower())
            self.assertEqual(issued.data, self.PDF)
            self.assertEqual(issued.headers["Cache-Control"], "no-store")
            self.assertIn("attachment", issued.headers["Content-Disposition"])
            self.assertEqual(converter.call_args.args[1]["turma"], "B")
            self.assertEqual(self.issue(self.CPF, self.KEY).data, self.PDF)
            self.assertEqual(converter.call_count, 3)

        verified = self.verify(self.KEY)
        for public_value in ("Certificado válido", "Programa Horizonte", "Formação em IA", "90 horas"):
            self.assertIn(public_value, verified)
        for private_value in ("Ana Fictícia", self.CPF, "demo@example.com"):
            self.assertNotIn(private_value, verified)
        with self.app.app_context():
            row = data.connect().execute("SELECT cpf_digest, fields FROM participants").fetchone()
            self.assertEqual(len(row["cpf_digest"]), 32)
            self.assertEqual(json.loads(row["fields"]), {"turma": "B"})

        self.post(self.admin, f"{path}/revogar", {"key": self.KEY})
        self.assertIn("Certificado revogado", self.verify(self.KEY))
        self.assertEqual(self.issue(self.CPF, self.KEY).status_code, 404)
        message = self.import_list(path, ["Ana Fictícia", self.CPF, "DEMO-Q45F-PR72", "B", ""])
        self.assertIn("1 participante(s) importado(s)", message)

    def test_global_key_collision_rolls_back_the_whole_import(self):
        self.sign_in()
        first_path = self.create_formation()
        second_path = self.create_formation("Outro programa")
        person = ["Ana Fictícia", self.CPF, self.KEY, "B"]
        self.import_list(first_path, person)
        response = self.import_list(
            second_path,
            ["Bruna Fictícia", "11144477735", "DEMO-FM72-37XJ", "A"],
            ["Ana Fictícia", self.CPF, self.KEY.lower(), "B"],
        )
        self.assertIn("Linha 3 · Esta chave já pertence a outro cadastro", response)
        with self.app.app_context():
            self.assertEqual(data.connect().execute("SELECT count(*) FROM participants").fetchone()[0], 1)
        person[2] = "DEMO-H84A-37YP"
        self.assertIn("1 participante(s) importado(s)", self.import_list(second_path, person))

    def test_render_failure_does_not_activate_the_public_key(self):
        self.sign_in()
        path = self.create_formation()
        self.import_list(path, ["Ana Fictícia", self.CPF, self.KEY, "A"])
        with patch("certificados.portal.render_pdf", return_value=self.PDF):
            self.post(self.admin, f"{path}/liberar")
        with patch("certificados.portal.render_pdf", side_effect=RenderError("Falha simulada")):
            with self.assertLogs(self.app.logger, level="WARNING"):
                self.assertEqual(self.issue(self.CPF, self.KEY).status_code, 503)
        self.assertNotIn("Certificado válido", self.verify(self.KEY))
        with patch("certificados.portal.render_pdf", return_value=self.PDF):
            self.assertEqual(self.issue(self.CPF, self.KEY).status_code, 200)
        self.assertIn("Certificado válido", self.verify(self.KEY))


if __name__ == "__main__":
    unittest.main()
