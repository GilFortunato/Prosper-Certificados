import ipaddress
import json
from io import BytesIO

from flask import Blueprint, current_app, render_template, request, send_file

from . import brazil_date, data
from .formats import KEY, NAME, RenderError, normalize_cpf, normalize_key, render_pdf


portal = Blueprint("portal", __name__)
NOT_AVAILABLE = "Não encontramos um certificado liberado para esse CPF e essa chave. Confira os dados e tente novamente."


def source_address():
    raw = request.remote_addr or "unknown"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address)
    return str(address)


def rate_limit(scope, max_hits, seconds, source=None):
    retry_after = data.allow_request(scope, source or source_address(), max_hits, seconds)
    if retry_after:
        return (
            render_template(
                "error.html", title="Muitas tentativas",
                message=f"Aguarde {retry_after} segundos para tentar novamente.",
            ),
            429,
            {"Retry-After": str(retry_after)},
        )
    return None


def certificate_values(formation, name, key, fields, issued_at):
    return {
        **fields,
        NAME: name,
        KEY: key,
        "programa": formation["program"],
        "formacao": formation["title"],
        "carga horaria": formation["workload"],
        "periodo": formation["period"],
        "data de emissao": brazil_date(issued_at),
    }


def generate_pdf(template, values):
    slots = current_app.extensions["pdf_slots"]
    if not slots.acquire(blocking=False):
        raise RenderError("A fila de PDFs está ocupada. Aguarde alguns instantes e tente novamente.")
    try:
        return render_pdf(template, values, current_app.config["SOFFICE"])
    finally:
        slots.release()


def pdf_download(content, filename="certificado.pdf"):
    return send_file(
        BytesIO(content), mimetype="application/pdf", as_attachment=True, download_name=filename,
    )


@portal.route("/", methods=["GET", "POST"])
def issue():
    if request.method == "GET":
        return render_template("portal.html", mode="issue")

    limited = rate_limit("issue-ip", 60, 300)
    if limited:
        return limited
    try:
        cpf = normalize_cpf(request.form.get("cpf", ""))
        key = normalize_key(request.form.get("key", ""))
    except data.InputError as error:
        return render_template("portal.html", mode="issue", error=str(error)), 400

    limited = rate_limit("issue-key", 12, 600, source=key)
    if limited:
        return limited
    participant = data.eligible_participant(cpf, key)
    if participant is None:
        return render_template("portal.html", mode="issue", error=NOT_AVAILABLE), 404

    pdf = participant["pdf"]
    if pdf is None:
        issued_at = data.timestamp()
        values = certificate_values(
            participant, participant["name"], key, json.loads(participant["fields"]), issued_at,
        )
        try:
            pdf = generate_pdf(data.load_template(participant["formation_id"]), values)
        except RenderError as error:
            current_app.logger.warning("Falha na emissão de PDF: %s", error)
            return render_template(
                "portal.html", mode="issue",
                error="O PDF está temporariamente indisponível. Tente novamente em alguns instantes.",
            ), 503
        pdf = data.record_issuance(participant["id"], pdf, issued_at)

    if pdf is None:
        return render_template("portal.html", mode="issue", error=NOT_AVAILABLE), 404
    return pdf_download(pdf)


@portal.route("/validar", methods=["GET", "POST"])
def verify():
    if request.method == "GET":
        return render_template("portal.html", mode="verify")

    limited = rate_limit("verify", 90, 300)
    if limited:
        return limited
    try:
        key = normalize_key(request.form.get("key", ""))
    except data.InputError as error:
        return render_template("portal.html", mode="verify", error=str(error)), 400

    certificate = data.check_certificate(key)
    return render_template("portal.html", mode="verify", certificate=certificate, checked=True)
