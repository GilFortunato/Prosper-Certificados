import hmac
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


@dataclass(frozen=True)
class ValidationRecord:
    row_number: int
    name: str
    validation_key: str
    issued_on: str
    skills: str


BRAZIL = ZoneInfo("America/Sao_Paulo")

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

CREATE TABLE IF NOT EXISTS validation_records (
    validation_key TEXT PRIMARY KEY NOT NULL,
    formation_id INTEGER NOT NULL REFERENCES formations (id),
    name TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    skills TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS validation_records_by_formation ON validation_records (formation_id);

CREATE VIEW IF NOT EXISTS verifiable_certificates AS
    SELECT validation_key, formation_id, name, revoked_at, issued_on, NULL AS issued_at, skills
    FROM validation_records
    UNION ALL
    SELECT p.validation_key, p.formation_id, p.name, p.revoked_at, NULL, p.issued_at,
           COALESCE(json_extract(p.fields, '$.habilidades'), '')
    FROM participants p
    WHERE p.issued_at IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM validation_records v WHERE v.validation_key = p.validation_key
    );

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

VALIDATION_COUNTS = """
    (SELECT count(*) FROM verifiable_certificates c
       WHERE c.formation_id = f.id AND c.revoked_at IS NULL) AS valid_keys,
    (SELECT count(*) FROM verifiable_certificates c
       WHERE c.formation_id = f.id AND c.revoked_at IS NOT NULL) AS revoked_keys
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


def issuance_day(issued_on, issued_at=None):
    if issued_on:
        return date.fromisoformat(issued_on)
    return datetime.fromisoformat(issued_at).astimezone(BRAZIL).date()


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
               count(p.issued_at) AS issued, count(p.revoked_at) AS revoked, {VALIDATION_COUNTS}
        FROM formations f LEFT JOIN participants p ON p.formation_id = f.id
        GROUP BY f.id ORDER BY f.id DESC
    """).fetchall()


def formation(formation_id):
    return connect().execute(f"""
        SELECT {FORMATION_COLUMNS}, count(p.id) AS participants,
               count(p.issued_at) AS issued, count(p.revoked_at) AS revoked, {VALIDATION_COUNTS}
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


def same_holder(record, formation_id, person):
    return record["formation_id"] == formation_id and record["name"].casefold() == person.name.casefold()


def register_certificates(formation_id, participants):
    added = updated = skipped = 0
    with transaction() as db:
        for person in participants:
            registered = db.execute(
                """SELECT formation_id, name, issued_on, skills, revoked_at FROM validation_records
                   WHERE validation_key = ?""",
                (person.validation_key,),
            ).fetchone()
            participant = db.execute(
                "SELECT formation_id, name, issued_at, revoked_at FROM participants WHERE validation_key = ?",
                (person.validation_key,),
            ).fetchone()
            for record in (registered, participant):
                if record and not same_holder(record, formation_id, person):
                    raise InputError(f"Linha {person.row_number} · Esta chave já pertence a outro cadastro.")
            if any(record and record["revoked_at"] for record in (registered, participant)):
                skipped += 1
                continue

            if participant and participant["issued_at"]:
                stored_day = issuance_day(registered["issued_on"] if registered else None, participant["issued_at"])
                if stored_day.isoformat() != person.issued_on:
                    raise InputError(f"Linha {person.row_number} · Use a data que consta no certificado emitido pelo portal.")

            if registered is None:
                db.execute(
                    """INSERT INTO validation_records (validation_key, formation_id, name, issued_on, skills)
                       VALUES (?, ?, ?, ?, ?)""",
                    (person.validation_key, formation_id, person.name, person.issued_on, person.skills),
                )
                added += 1
            elif (registered["name"], registered["issued_on"], registered["skills"]) != (
                person.name, person.issued_on, person.skills,
            ):
                db.execute(
                    "UPDATE validation_records SET name = ?, issued_on = ?, skills = ? WHERE validation_key = ?",
                    (person.name, person.issued_on, person.skills, person.validation_key),
                )
                updated += 1
            else:
                skipped += 1
    return added, updated, skipped


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
            registered = db.execute(
                "SELECT formation_id, name, revoked_at FROM validation_records WHERE validation_key = ?",
                (person.validation_key,),
            ).fetchone()
            if registered and not same_holder(registered, formation_id, person):
                raise InputError(f"Linha {person.row_number} · Esta chave já pertence a outro cadastro.")
            if registered and registered["revoked_at"]:
                raise InputError(f"Linha {person.row_number} · Esta chave foi revogada. Use uma nova chave.")
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


def revoke_certificate(formation_id, validation_key, *, allow_unissued):
    now = timestamp()
    with transaction() as db:
        registered = db.execute(
            """UPDATE validation_records SET revoked_at = ?
               WHERE formation_id = ? AND validation_key = ? AND revoked_at IS NULL""",
            (now, formation_id, validation_key),
        ).rowcount
        issued = db.execute(
            """UPDATE participants SET revoked_at = ?, pdf = NULL
               WHERE formation_id = ? AND validation_key = ? AND revoked_at IS NULL
                 AND (issued_at IS NOT NULL OR ?)""",
            (now, formation_id, validation_key, bool(registered) or allow_unissued),
        ).rowcount
        if not (registered or issued):
            raise InputError("Não encontramos uma chave ativa com esse código nesta formação.")


def eligible_participant(cpf, validation_key):
    return connect().execute(
        """SELECT p.id, p.name, p.validation_key, p.fields, p.issued_at, p.pdf, v.issued_on,
                  f.id AS formation_id, f.program, f.title, f.workload, f.period
           FROM participants p JOIN formations f ON f.id = p.formation_id
           LEFT JOIN validation_records v ON v.validation_key = p.validation_key
           WHERE p.validation_key = ? AND p.cpf_digest = ?
             AND p.revoked_at IS NULL AND f.released_at IS NOT NULL""",
        (validation_key, cpf_digest(cpf)),
    ).fetchone()


def record_issuance(participant_id, pdf, issued_at, rendered_day):
    with transaction() as db:
        registered = db.execute(
            """SELECT v.issued_on FROM participants p JOIN validation_records v
               ON v.validation_key = p.validation_key WHERE p.id = ?""",
            (participant_id,),
        ).fetchone()
        if registered and registered["issued_on"] != rendered_day:
            raise InputError("A data do certificado foi atualizada. Tente novamente.")
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
        """SELECT c.name, c.skills, c.issued_on, c.issued_at, c.revoked_at, f.program, f.title, f.workload, f.period
           FROM verifiable_certificates c JOIN formations f ON f.id = c.formation_id
           WHERE c.validation_key = ?""",
        (validation_key,),
    ).fetchone()
