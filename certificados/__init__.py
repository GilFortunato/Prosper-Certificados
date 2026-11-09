import hmac
import os
import secrets
from datetime import timedelta
from threading import BoundedSemaphore

from flask import Flask, abort, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import data
from .config import PASSWORD_METHOD, secret_from_env


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def create_app(config=None):
    from .admin import admin, authenticated
    from .portal import portal

    app = Flask(__name__)
    app.config.from_mapping(
        DB_PATH=os.environ.get("PROSPER_DB_PATH", "data/certificados.sqlite3"),
        ISSUANCE_ENABLED=os.environ.get("PROSPER_ENABLE_ISSUANCE", "0") == "1",
        SOFFICE=os.environ.get("PROSPER_SOFFICE", "soffice"),
        PROXY_HOPS=int(os.environ.get("PROSPER_PROXY_HOPS", "0")),
        MAX_CONTENT_LENGTH=13 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        MAX_FORM_PARTS=8,
        SESSION_COOKIE_NAME="prosper_certificados",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("PROSPER_SECURE_COOKIES", "1") != "0",
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
    )
    if config is None:
        app.config.update(
            SECRET_KEY=secret_from_env("PROSPER_SESSION_SECRET"),
            CPF_KEY=secret_from_env("PROSPER_CPF_KEY"),
            ADMIN_PASSWORD_HASH=os.environ.get("PROSPER_ADMIN_PASSWORD_HASH", ""),
        )
        if app.secret_key == app.config["CPF_KEY"]:
            raise RuntimeError("Configure segredos diferentes para a sessão e o CPF.")
        if not app.config["ADMIN_PASSWORD_HASH"].startswith(f"{PASSWORD_METHOD}$"):
            raise RuntimeError("Gere a senha administrativa com python -m certificados.config.")
    else:
        app.config.update(config)

    proxy_hops = app.config["PROXY_HOPS"]
    if proxy_hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops)
    app.extensions["pdf_slots"] = BoundedSemaphore(2)
    app.jinja_env.globals.update(csrf_token=csrf_token)

    @app.before_request
    def protect_forms():
        admin_route = request.blueprint == "admin" and request.endpoint != "admin.login"
        if admin_route and not authenticated():
            return redirect(url_for("admin.login"))
        if request.method == "POST":
            if not admin_route:
                request.max_content_length = 4096
            submitted = request.form.get("_csrf", "")
            expected = session.get("_csrf", "")
            if not expected or not hmac.compare_digest(submitted.encode(), expected.encode()):
                abort(400)

    @app.after_request
    def response_headers(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    messages = {
        400: ("Sessão do formulário expirada", "Volte à página anterior, atualize e tente novamente."),
        404: ("Página indisponível", "Confira o endereço ou volte à central de certificados."),
        413: ("Envio acima do limite", "O limite é 12 MB para o PowerPoint e 5 MB para a planilha."),
        500: ("Serviço indisponível", "Tente novamente em alguns instantes."),
    }

    def render_error(error):
        title, message = messages[error.code]
        return render_template("error.html", title=title, message=message), error.code

    for status in messages:
        app.register_error_handler(status, render_error)

    app.register_blueprint(portal)
    app.register_blueprint(admin)
    app.teardown_appcontext(data.close)
    with app.app_context():
        data.init_db()
    return app
