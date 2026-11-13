"""Persist validated leads and a sanitized append-only event ledger."""

import csv
import hashlib
import hmac
import json
import sqlite3
from dataclasses import astuple, dataclass
from pathlib import Path

from .parsing import event_timestamp, header_key, parse_lead, phone_number

SCHEMAS = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
OVERRIDE = "disposition.override"
SET = "disposition.set"


@dataclass
class Counts:
    lead_rows_total: int = 0
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0


def token(key, purpose, *parts):
    message = json.dumps([purpose, *parts], ensure_ascii=True).encode("ascii")
    return hmac.digest(key, message, "sha256").hex()


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    return db


def load_schemas():
    schemas = {}
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[schema["workflow_id"]] = schema["fields"]
    return schemas


def load_dispositions():
    with (SCHEMAS / "dispositions.csv").open(encoding="utf-8", newline="") as stream:
        return {
            (row["workflow_id"], row["code"]): (
                row["is_terminal"] == "true", row["is_success"] == "true"
            )
            for row in csv.DictReader(stream)
        }


def input_paths(drop, schemas):
    if not drop.is_dir():
        raise ValueError("Each input drop must be an existing directory")
    lead_dir, event_dir = drop / "leads", drop / "events"
    if not lead_dir.is_dir() and not event_dir.is_dir():
        raise ValueError("An input drop needs a leads or events directory")
    lead_paths = sorted(lead_dir.glob("*/*.csv"), key=lambda path: (path.name, path.parent.name))
    if any(path.parent.name not in schemas for path in lead_paths):
        raise ValueError("A lead file refers to an unknown workflow")
    return lead_paths, sorted(event_dir.glob("*.jsonl"))


def drop_fingerprint(key, drop, paths):
    files = []
    for path in paths:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append([path.relative_to(drop).as_posix(), digest])
    return token(key, "drop", files)


def ingest_leads(db, key, path, fields, fingerprint, counts):
    workflow = path.parent.name
    batch = path.stem
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        headers = {}
        for index, header in enumerate(next(reader, [])):
            headers.setdefault(header_key(header), index)
        positions = [
            next((headers[name] for name in map(header_key, [field["name"], *field.get("aliases", [])])
                  if name in headers), None)
            for field in fields
        ]
        rows = (row for row in reader if any(cell.strip() for cell in row))
        for number, row in enumerate(rows, 1):
            counts.lead_rows_total += 1
            values, warnings, reason = parse_lead(row, fields, positions)
            if reason:
                db.execute("INSERT INTO rejections VALUES (?, ?, ?, ?, ?)",
                           (fingerprint, workflow, batch, number, reason))
                continue
            counts.field_warnings += warnings
            store_lead(db, key, workflow, batch, fields, values, counts)


def store_lead(db, key, workflow, batch, fields, values, counts):
    phone = values.pop("phone")
    phone_token = token(key, "phone", workflow, phone)
    identity = (workflow, phone_token)
    previous = db.execute("SELECT fields FROM leads WHERE workflow_id=? AND phone_token=?",
                          identity).fetchone()
    merged = json.loads(previous["fields"]) if previous else {}
    for field in fields:
        name = field["name"]
        if name in values:
            value = values[name]
            merged[name] = token(key, "pii", workflow, name, value) if field.get("pii") else value
        elif name not in merged and "default" in field:
            value = field["default"]
            merged[name] = token(key, "pii", workflow, name, value) if field.get("pii") else value
    data = json.dumps(merged, ensure_ascii=True, sort_keys=True)
    if previous:
        counts.duplicate_lead_rows_merged += 1
        db.execute("UPDATE leads SET fields=? WHERE workflow_id=? AND phone_token=?",
                   (data, *identity))
    else:
        db.execute("INSERT INTO leads VALUES (?, ?, ?, ?, ?)",
                   (*identity, batch, "+91******" + phone[-4:], data))


def read_event(line):
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    return event if isinstance(event_id, str) and event_id.strip() else None


def utf8_string(value):
    return (isinstance(value, str) and bool(value.strip())
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value))


def event_values(event, key):
    workflow, call_id, kind = (event.get(name) for name in ("workflow_id", "call_id", "event_type"))
    if not all(map(utf8_string, (event["event_id"], workflow, call_id, kind))):
        return None
    try:
        occurred_at = event_timestamp(event.get("occurred_at"))
    except (TypeError, ValueError, OverflowError):
        return None
    phone = phone_number(event.get("phone"))
    phone_token = token(key, "phone", workflow, phone) if phone and kind != OVERRIDE else None
    payload = event.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    code = code if kind.startswith("disposition.") and utf8_string(code) else None
    return workflow, call_id, phone_token, kind, occurred_at, code


def ingest_events(db, key, path, fingerprint, counts):
    with path.open("rb") as stream:
        for line in stream:
            if not line.strip():
                continue
            counts.events_total += 1
            event = read_event(line)
            if event is None or not utf8_string(event["event_id"]):
                counts.malformed_events += 1
                continue
            event_id = event["event_id"]
            values = event_values(event, key)
            if values is None:
                # Reserve the ID even for a structurally invalid first occurrence.
                values = ("", "", None, "", "", None)
            result = db.execute(
                "INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
                (event_id, fingerprint, *values),
            )
            if not result.rowcount:
                counts.duplicate_events_dropped += 1
            elif not values[4]:
                counts.malformed_events += 1


def ingest(db, key, drops):
    schemas = load_schemas()
    verifier = token(key, "key-check")
    imported = 0
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO metadata VALUES ('key_check', ?) ON CONFLICT DO NOTHING",
                   (verifier,))
        stored = db.execute("SELECT value FROM metadata WHERE name='key_check'").fetchone()[0]
        if not hmac.compare_digest(verifier, stored):
            raise ValueError("The key does not match this database")
        for drop in drops:
            lead_paths, event_paths = input_paths(drop, schemas)
            paths = [*lead_paths, *event_paths]
            fingerprint = drop_fingerprint(key, drop, paths)
            if db.execute("SELECT 1 FROM drops WHERE fingerprint=?", (fingerprint,)).fetchone():
                continue
            counts = Counts()
            for path in lead_paths:
                ingest_leads(db, key, path, schemas[path.parent.name], fingerprint, counts)
            for path in event_paths:
                ingest_events(db, key, path, fingerprint, counts)
            if drop_fingerprint(key, drop, paths) != fingerprint:
                raise ValueError("An input file changed during ingest")
            db.execute("INSERT INTO drops VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (fingerprint, *astuple(counts)))
            imported += 1
    return imported
