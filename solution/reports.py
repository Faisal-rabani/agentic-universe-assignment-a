"""Resolve the ledger against current leads and write the five report files."""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .parsing import IST
from .store import Counts, OVERRIDE, SET, load_dispositions, load_schemas

BUCKETS = (
    "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"
)
BATCH_HEADER = ("workflow_id", "batch_id", "total_leads", *BUCKETS)
DAILY_HEADER = (
    "workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"
)
LEAD_HEADER = (
    "workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
    "connected_calls", "last_call_at", "current_disposition", "ever_success"
)
REJECTION_HEADER = ("workflow_id", "batch_id", "row_number", "reason")


@dataclass
class Call:
    initiated: str | None = None
    connected: bool = False
    failed: bool = False
    disposition: tuple | None = None

    def apply(self, event):
        kind = event["event_type"]
        stamp = event["occurred_at"]
        if kind == "call.initiated":
            if self.initiated is None or stamp < self.initiated:
                self.initiated = stamp
        elif kind == "call.connected":
            self.connected = True
        elif kind == "call.telephony_failed":
            self.failed = True
        elif kind in (OVERRIDE, SET):
            candidate = (kind == OVERRIDE, stamp, event["event_id"], event["disposition_code"])
            if self.disposition is None or candidate > self.disposition:
                self.disposition = candidate

    @property
    def code(self):
        return self.disposition[-1] if self.disposition else ""


def ist_timestamp(utc):
    return datetime.fromisoformat(utc).astimezone(IST).isoformat()


def link_calls(db, leads, catalogue):
    call_owners = defaultdict(set)
    events = "SELECT * FROM ledger WHERE occurred_at <> ''"
    for event in db.execute(events):
        workflow = event["workflow_id"]
        identity = (workflow, event["phone_token"])
        if event["event_type"] != OVERRIDE and identity in leads:
            call_owners[workflow, event["call_id"]].add(identity)

    orphans = unknown = 0
    calls = {}
    for event in db.execute(events):
        workflow = event["workflow_id"]
        identity = (workflow, event["phone_token"])
        kind = event["event_type"]
        if kind == OVERRIDE:
            owners = call_owners.get((workflow, event["call_id"]), ())
            identity = next(iter(owners)) if len(owners) == 1 else None
        if identity not in leads:
            orphans += 1
            continue
        if kind.startswith("disposition.") and (workflow, event["disposition_code"]) not in catalogue:
            unknown += 1
            continue
        key = (identity, event["call_id"])
        if key not in calls:
            calls[key] = Call()
        calls[key].apply(event)

    by_lead = defaultdict(list)
    for (identity, call_id), call in calls.items():
        if call.initiated:
            by_lead[identity].append((call.initiated, call_id, call))
    return by_lead, orphans, unknown


def bucket_for(calls, terminal):
    if not calls:
        return "yet_to_reach_out"
    if terminal:
        return "completed"
    if all(call.failed for call in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def summaries(leads, by_lead, catalogue, references):
    batches = defaultdict(Counter)
    daily = defaultdict(Counter)
    lead_rows = []
    for identity, lead in leads.items():
        workflow, _ = identity
        calls = [call for _, _, call in sorted(by_lead.get(identity, ()), reverse=True)]
        terminal = ever_success = False
        successful_days = set()
        for call in calls:
            day = ist_timestamp(call.initiated)[:10]
            counts = daily[workflow, day]
            counts["calls"] += 1
            counts["connected"] += call.connected
            is_terminal, is_success = catalogue.get((workflow, call.code), (False, False))
            terminal |= is_terminal
            ever_success |= is_success
            if is_success:
                successful_days.add(day)
        for day in successful_days:
            daily[workflow, day]["success"] += 1

        bucket = bucket_for(calls, terminal)
        batch = lead["batch_id"]
        batches[workflow, batch][bucket] += 1
        values = json.loads(lead["fields"])
        lead_rows.append((
            workflow, values[references[workflow]], batch, lead["phone_masked"], bucket,
            len(calls), sum(call.connected for call in calls),
            ist_timestamp(calls[0].initiated) if calls else "",
            next((call.code for call in calls if call.code), ""),
            "true" if ever_success else "false",
        ))

    batch_rows = [
        (*key, sum(counts.values()), *(counts[bucket] for bucket in BUCKETS))
        for key, counts in sorted(batches.items())
    ]
    daily_rows = [
        (*key, counts["calls"], counts["connected"],
         f'{counts["connected"] / counts["calls"]:.4f}', counts["success"])
        for key, counts in sorted(daily.items())
    ]
    return batch_rows, daily_rows, sorted(lead_rows)


def read_reports(db):
    catalogue = load_dispositions()
    references = {
        workflow: next(field["name"] for field in fields if field.get("reference"))
        for workflow, fields in load_schemas().items()
    }
    db.execute("BEGIN")
    with db:
        leads = {(row["workflow_id"], row["phone_token"]): row
                 for row in db.execute("SELECT * FROM leads")}
        by_lead, orphans, unknown = link_calls(db, leads, catalogue)
        rejections = [tuple(row) for row in db.execute(
            "SELECT workflow_id, batch_id, row_number, reason FROM rejections "
            "ORDER BY workflow_id, batch_id, row_number, reason"
        )]
        totals = {
            name: db.execute(f"SELECT COALESCE(SUM({name}), 0) FROM drops").fetchone()[0]
            for name in Counts.__dataclass_fields__
        }

    totals.update(
        lead_rows_rejected=len(rejections),
        rejections_by_reason=dict(Counter(row[3] for row in rejections)),
        orphan_events=orphans,
        unknown_disposition_events=unknown,
    )
    batches, daily, lead_rows = summaries(leads, by_lead, catalogue, references)
    return {
        "batch_summary.csv": (BATCH_HEADER, batches),
        "daily_overview.csv": (DAILY_HEADER, daily),
        "lead_report.csv": (LEAD_HEADER, lead_rows),
        "rejections.csv": (REJECTION_HEADER, rejections),
    }, totals


def report(db, out, database_path):
    tables, quality = read_reports(db)
    names = [*tables, "data_quality.json"]
    for name in names:
        destination = out / name
        if destination.resolve() == database_path.resolve() or (
            destination.exists() and destination.samefile(database_path)
        ):
            raise ValueError("A report would overwrite the database")

    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".reports-", dir=out) as staging:
        stage = Path(staging)
        for name, (header, rows) in tables.items():
            with (stage / name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                writer.writerows(rows)
        (stage / "data_quality.json").write_text(
            json.dumps(quality, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for name in names:
            (stage / name).replace(out / name)
