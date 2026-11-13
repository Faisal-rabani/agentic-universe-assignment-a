import csv
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .parsing import calendar_date, money, phone_number
from .reports import Call, bucket_for, report
from .store import connect, ingest

KEY = bytes(range(32))
WORKFLOW = "motor_renewal"


def event(event_id, kind, call_id="call1", time="2026-10-01T20:00:00Z",
          phone="+91 98765 43210", code=None):
    return dict(event_id=event_id, event_type=kind, workflow_id=WORKFLOW,
                call_id=call_id, occurred_at=time, phone=phone,
                payload={"disposition_code": code, "note": "Private raw payload"})


def lead_file(drop, batch, rows):
    path = drop / "leads" / WORKFLOW / f"{batch}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


def event_file(drop, events):
    directory = drop / "events"
    directory.mkdir(parents=True)
    with (directory / "events.jsonl").open("w", encoding="utf-8") as stream:
        for item in events:
            stream.write((item if isinstance(item, str) else json.dumps(item)) + "\n")


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class RulesTests(unittest.TestCase):
    def test_phone(self):
        for raw in ("09876543210", "91-98765-43210", "+91 (98765) 43210", "9876543210"):
            self.assertEqual(phone_number(raw), "+919876543210")
        for raw in ("1234567890", "98765", None, "019876543210"):
            self.assertIsNone(phone_number(raw))

    def test_dates_and_amounts(self):
        for raw in ("2026-12-01", "01/12/2026", "01-12-2026", "01-dEc-2026"):
            self.assertEqual(calendar_date(raw), "2026-12-01")
        self.assertIsNone(calendar_date("31/02/2026"))
        self.assertIsNone(calendar_date("01/Dec/2026"))
        self.assertEqual(money("Rs. 1,234.565"), "1234.57")
        self.assertIsNone(money("NaN"))
        self.assertIsNone(money("INR -1"))

    def test_bucket_priority(self):
        self.assertEqual(bucket_for([], False), "yet_to_reach_out")
        self.assertEqual(bucket_for([Call(failed=True)], True), "completed")
        self.assertEqual(bucket_for([Call(failed=True)], False), "in_progress_telephony_failed")
        self.assertEqual(bucket_for([Call(failed=True), Call()], False), "in_progress_dialed")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.first = self.root / "drop1"
        self.second = self.root / "drop2"
        self.db_path = self.root / "ledger.sqlite3"
        self.out = self.root / "reports"
        lead_file(self.first, "batch_001", [
            ["customer_name", "phone", "policy_number", "preferred_language", "premium"],
            ["Private First", "8765432199", "P1", "ta", "12.30"],
            ["", "", "", "", ""],
            ["", "123", "bad", "invalid", ""],
            ["Private Invalid", "123", "bad", "", ""],
        ])
        lead_file(self.second, "batch_002", [
            [" Insured__Name ", "Mobile No", "Policy No", "Language", "Premium"],
            ["Private Updated", "+91 8765432199", "P2", "invalid", ""],
            ["Private Late", "09876543210", "P3", "", ""],
        ])
        self.initiated = event("e1", "call.initiated")
        event_file(self.first, [
            event("e0", "disposition.override", time="2026-10-03 08:00:00",
                  phone=None, code="not_interested"),
            self.initiated,
            event("e2", "call.connected", time="2026-10-02 01:31:00"),
            event("e3", "disposition.set", time="2026-10-05 08:00:00", code="renewed"),
            event("e4", "disposition.set", code="unknown"),
            event("e5", "disposition.override", time="2026-10-03T04:00:00Z",
                  phone=None, code="wrong_number"),
            event("e6", "disposition.override", time="2026-10-03 08:01:00",
                  phone=None, code="dnd"),
            event("e7", "call.initiated", phone="9999999999"),
            event("e8", "call.connected", call_id="missing_initiation"),
            "{broken", " ",
        ])
        event_file(self.second, [self.initiated, "{}", "[]"])

    def test_full_pipeline(self):
        with closing(connect(self.db_path)) as db:
            self.assertEqual(ingest(db, KEY, [self.first]), 1)
            report(db, self.out, self.db_path)
            first_quality = json.loads((self.out / "data_quality.json").read_text())
            self.assertEqual(first_quality["orphan_events"], 9)
            self.assertEqual(first_quality["unknown_disposition_events"], 0)

            self.assertEqual(ingest(db, KEY, [self.second]), 1)
            report(db, self.out, self.db_path)
            lead, late = csv_rows(self.out / "lead_report.csv")
            self.assertEqual((lead["external_ref"], lead["batch_id"], lead["bucket"]),
                             ("P2", "batch_001", "yet_to_reach_out"))
            self.assertEqual(late, dict(
                workflow_id=WORKFLOW, external_ref="P3", batch_id="batch_002",
                phone_masked="+91******3210", bucket="completed", total_calls="1",
                connected_calls="1", last_call_at="2026-10-02T01:30:00+05:30",
                current_disposition="wrong_number", ever_success="false",
            ))
            self.assertEqual(csv_rows(self.out / "daily_overview.csv"), [dict(
                workflow_id=WORKFLOW, date="2026-10-02", total_calls="1",
                connected_calls="1", connectivity_rate="1.0000", success_leads="0",
            )])
            self.assertEqual([row["row_number"] for row in csv_rows(self.out / "rejections.csv")],
                             ["2", "3"])
            self.assertEqual(json.loads((self.out / "data_quality.json").read_text()), dict(
                lead_rows_total=5, lead_rows_rejected=2, duplicate_lead_rows_merged=1,
                field_warnings=1, events_total=13, malformed_events=3,
                duplicate_events_dropped=1, orphan_events=1, unknown_disposition_events=1,
                rejections_by_reason={"invalid_phone": 1, "missing_required:customer_name": 1},
            ))
            values = [json.loads(row[0]) for row in db.execute("SELECT fields FROM leads")]
            retained = next(item for item in values if item["policy_number"] == "P2")
            self.assertEqual(retained["preferred_language"], "ta")
            self.assertEqual(retained["premium_amount"], "12.30")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE ledger SET occurred_at=''")
            db.rollback()

            before = {path.name: path.read_bytes() for path in self.out.iterdir()}
            db_before = self.db_path.read_bytes()
            self.assertEqual(ingest(db, KEY, [self.first, self.second]), 0)
            self.assertEqual(db_before, self.db_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "key does not match"):
                ingest(db, b"x" * 32, [self.first])
            report(db, self.out, self.db_path)
            self.assertEqual(before, {path.name: path.read_bytes() for path in self.out.iterdir()})
            for secret in (b"Private First", b"Private Updated", b"Private Late",
                           b"919876543210", b"8765432199", b"Private raw payload"):
                self.assertNotIn(secret, db_before)
                self.assertTrue(all(secret not in data for data in before.values()))

        combined = self.root / "combined.sqlite3"
        with closing(connect(combined)) as db:
            self.assertEqual(ingest(db, KEY, [self.first, self.second]), 2)
            report(db, self.out, combined)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.out.iterdir()})

    def test_batch_rolls_back_when_a_later_drop_fails(self):
        with closing(connect(self.db_path)) as db:
            with self.assertRaises(ValueError):
                ingest(db, KEY, [self.first, self.root / "missing"])
            for table in ("drops", "leads", "ledger", "rejections", "metadata"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(ingest(db, KEY, [self.first]), 1)


if __name__ == "__main__":
    unittest.main()
