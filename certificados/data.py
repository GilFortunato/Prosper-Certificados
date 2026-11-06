import hmac
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Participant:
    row_number: int
    name: str
    cpf_digest: bytes
    validation_key: str
    fields: dict[str, str]


SCHEMA = """
CREATE TABLE IF NOT EXISTS formations (
    id INTEGER PRIMARY KEY,
    program TEXT NOT NULL,
    title TEXT NOT NULL,
    workload TEXT NOT NULL,
    period TEXT NOT NULL,
    template BLOB,
    template_name TEXT,
    template_fields TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 0,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    formation_id INTEGER NOT NULL REFERENCES formations (id),
    name TEXT NOT NULL,
    cpf_digest BLOB NOT NULL,
    validation_key TEXT NOT NULL UNIQUE,
    fields TEXT NOT NULL,
    issued_at TEXT,
    pdf BLOB,
    revoked_at TEXT,
    CHECK ((pdf IS NOT NULL) = (issued_at IS NOT NULL AND revoked_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS active_cpf_per_formation
    ON participants (formation_id, cpf_digest) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS participants_by_formation ON participants (formation_id);

CREATE TABLE IF NOT EXISTS request_limits (
    scope TEXT NOT NULL,
    source BLOB NOT NULL,
    window INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, source, window)
);

CREATE INDEX IF NOT EXISTS request_limits_by_window ON request_limits (window);
"""

FORMATION_COLUMNS = """
    f.id, f.program, f.title, f.workload, f.period, f.template_name,
    f.template_fields, f.revision, f.released_at
"""


def connect():
    if "database" not in g:
        g.database = sqlite3.connect(current_app.config["DB_PATH"], timeout=10, isolation_level=None)
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys = ON")
    return g.database


def close(_error=None):
    database = g.pop("database", None)
    if database is not None:
        database.close()


def init_db():
    Path(current_app.config["DB_PATH"]).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = connect()
    database.execute("PRAGMA journal_mode = WAL")
    database.executescript(SCHEMA)


@contextmanager
def transaction():
    database = connect()
    database.execute("BEGIN IMMEDIATE")
    try:
        yield database
        database.commit()
    except BaseException:
        database.rollback()
        raise


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cpf_digest(cpf):
    return hmac.digest(current_app.config["CPF_KEY"], cpf.encode("ascii"), "sha256")


def allow_request(scope, source, max_hits, seconds):
    now = int(time.time())
    start = now - now % seconds
    source_digest = hmac.digest(current_app.secret_key, source.encode(), "sha256")
    with transaction() as db:
        db.execute("DELETE FROM request_limits WHERE window < ?", (now - 3600,))
        hits = db.execute(
            """INSERT INTO request_limits (scope, source, window, hits) VALUES (?, ?, ?, 1)
               ON CONFLICT (scope, source, window) DO UPDATE SET hits = min(hits + 1, ?)
               RETURNING hits""",
            (scope, source_digest, start, max_hits + 1),
        ).fetchone()["hits"]
    return 0 if hits <= max_hits else start + seconds - now


def formations():
    return connect().execute(f"""
        SELECT {FORMATION_COLUMNS}, count(p.id) AS participants,
               count(p.issued_at) AS issued, count(p.revoked_at) AS revoked
        FROM formations f LEFT JOIN participants p ON p.formation_id = f.id
        GROUP BY f.id ORDER BY f.id DESC
    """).fetchall()


def formation(formation_id):
    return connect().execute(f"""
        SELECT {FORMATION_COLUMNS}, count(p.id) AS participants,
               count(p.issued_at) AS issued, count(p.revoked_at) AS revoked
        FROM formations f LEFT JOIN participants p ON p.formation_id = f.id
        WHERE f.id = ? GROUP BY f.id
    """, (formation_id,)).fetchone()


def create_formation(program, title, workload, period):
    return connect().execute(
        "INSERT INTO formations (program, title, workload, period) VALUES (?, ?, ?, ?)",
        (program, title, workload, period),
    ).lastrowid


def update_formation(formation_id, program, title, workload, period):
    changed = connect().execute(
        """UPDATE formations SET program = ?, title = ?, workload = ?, period = ?, revision = revision + 1
           WHERE id = ? AND released_at IS NULL""",
        (program, title, workload, period, formation_id),
    ).rowcount
    if not changed:
        raise InputError("Os dados desta formação já constam nos certificados liberados.")


def load_template(formation_id):
    row = connect().execute("SELECT template FROM formations WHERE id = ?", (formation_id,)).fetchone()
    return row["template"] if row else None


def save_template(formation_id, content, name, fields):
    changed = connect().execute(
        """UPDATE formations SET template = ?, template_name = ?, template_fields = ?,
               revision = revision + 1 WHERE id = ? AND released_at IS NULL""",
        (content, name, json.dumps(fields, ensure_ascii=False), formation_id),
    ).rowcount
    if not changed:
        raise InputError("O modelo desta formação já está em uso. Cadastre outra formação para usar um novo modelo.")


def save_participants(formation_id, version, participants):
    added = skipped = 0
    with transaction() as db:
        current = formation(formation_id)
        if current["revision"] != version:
            raise InputError("A formação mudou durante a importação. Envie a planilha novamente.")
        if current["released_at"] is None:
            db.execute("DELETE FROM participants WHERE formation_id = ?", (formation_id,))

        for person in participants:
            serialized = json.dumps(person.fields, ensure_ascii=False, sort_keys=True)
            existing = db.execute(
                """SELECT formation_id, name, cpf_digest, fields FROM participants
                   WHERE validation_key = ?""",
                (person.validation_key,),
            ).fetchone()
            if existing:
                if tuple(existing) == (formation_id, person.name, person.cpf_digest, serialized):
                    skipped += 1
                    continue
                raise InputError(f"Linha {person.row_number} · Esta chave já pertence a outro cadastro.")
            try:
                db.execute(
                    """INSERT INTO participants (formation_id, name, cpf_digest, validation_key, fields)
                       VALUES (?, ?, ?, ?, ?)""",
                    (formation_id, person.name, person.cpf_digest, person.validation_key, serialized),
                )
            except sqlite3.IntegrityError as error:
                raise InputError(
                    f"Linha {person.row_number} · Este CPF já tem uma chave ativa nesta formação."
                ) from error
            added += 1
        db.execute("UPDATE formations SET revision = revision + 1 WHERE id = ?", (formation_id,))
    return added, skipped


def preview_source(formation_id):
    return connect().execute(
        f"""SELECT {FORMATION_COLUMNS}, f.template, p.name, p.fields
            FROM formations f LEFT JOIN participants p ON p.id = (
                SELECT id FROM participants WHERE formation_id = f.id AND revoked_at IS NULL
                ORDER BY length(name) DESC, id LIMIT 1
            ) WHERE f.id = ?""",
        (formation_id,),
    ).fetchone()


def release_formation(formation_id, version, required_fields):
    with transaction() as db:
        current = formation(formation_id)
        if current["released_at"]:
            return
        if current["revision"] != version:
            raise InputError("A formação mudou durante a prévia. Confira os arquivos e tente liberar novamente.")
        if not current["template_name"] or not current["participants"]:
            raise InputError("Envie o PowerPoint e a lista Excel antes de liberar a emissão.")

        fields = json.loads(current["template_fields"])
        for participant in db.execute("SELECT fields FROM participants WHERE formation_id = ?", (formation_id,)):
            values = json.loads(participant["fields"])
            if any(not values.get(field) for field in fields.keys() - required_fields):
                raise InputError("O modelo tem campos novos. Importe a lista Excel atualizada antes de liberar.")

        db.execute(
            "UPDATE formations SET released_at = ?, revision = revision + 1 WHERE id = ?",
            (timestamp(), formation_id),
        )


def revoke_participant(formation_id, validation_key):
    changed = connect().execute(
        """UPDATE participants SET revoked_at = ?, pdf = NULL
           WHERE formation_id = ? AND validation_key = ? AND revoked_at IS NULL""",
        (timestamp(), formation_id, validation_key),
    ).rowcount
    if not changed:
        raise InputError("Não encontramos uma chave ativa com esse código nesta formação.")


def eligible_participant(cpf, validation_key):
    return connect().execute(
        """SELECT p.id, p.name, p.validation_key, p.fields, p.issued_at, p.pdf,
                  f.id AS formation_id, f.program, f.title, f.workload, f.period
           FROM participants p JOIN formations f ON f.id = p.formation_id
           WHERE p.validation_key = ? AND p.cpf_digest = ?
             AND p.revoked_at IS NULL AND f.released_at IS NOT NULL""",
        (validation_key, cpf_digest(cpf)),
    ).fetchone()


def record_issuance(participant_id, pdf, issued_at):
    with transaction() as db:
        db.execute(
            """UPDATE participants SET pdf = ?, issued_at = ?
               WHERE id = ? AND issued_at IS NULL AND revoked_at IS NULL""",
            (pdf, issued_at, participant_id),
        )
        row = db.execute(
            "SELECT pdf FROM participants WHERE id = ? AND revoked_at IS NULL",
            (participant_id,),
        ).fetchone()
        return row["pdf"] if row else None


def check_certificate(validation_key):
    return connect().execute(
        """SELECT p.issued_at, p.revoked_at, f.program, f.title, f.workload, f.period
           FROM participants p JOIN formations f ON f.id = p.formation_id
           WHERE p.validation_key = ? AND p.issued_at IS NOT NULL""",
        (validation_key,),
    ).fetchone()
