# Submission

## Candidate

- **Name** — Candidate to confirm before submission.
- **Assignment chosen** — A, Interaction Ledger & Reporting Pipeline.
- **Time spent** — Candidate to record actual time spent reviewing, testing, and preparing the submission. AI assistance is disclosed separately.

## How to run

Run from the repository root with Python 3.11 or later. Assignment A uses the Python standard library and SQLite included with Python.

PowerShell:

```powershell
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
$env:LEDGER_HMAC_KEY = python -c "import secrets; print(secrets.token_hex(32))"
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports
python -m unittest discover -v
Remove-Item Env:LEDGER_HMAC_KEY
```

Bash:

```bash
python3 assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
export LEDGER_HMAC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 -m solution ingest data/drop_1
python3 -m solution ingest data/drop_2
python3 -m solution report --out reports
python3 -m unittest discover -v
unset LEDGER_HMAC_KEY
```

Those commands assume a fresh database. Keep the same key for later ingests into that database and store it separately in a secret manager if it needs to survive the current session. Reporting needs no key. Use `--db another.sqlite3` before the subcommand for an independent database. Pass several drops to one `ingest` invocation to import them in the given order in a single transaction.

## Design

SQLite stores batch counts in `drops`, validation failures in `rejections`, and one merged row per workflow and keyed phone identity in `leads`. The `ledger` stores the first occurrence of each event ID. It retains the fields needed to link events and derive the specified reports. SQLite triggers reject updates and deletes on ledger rows.

Phone identity is a workflow-scoped HMAC-SHA-256 token. Other schema-marked PII, such as the customer name, is represented by a separate HMAC token. These tokens support equality checks and do not permit recovery of the original values. The lead report uses the required masked phone number and external reference. Raw event payloads and raw rejected rows stay out of SQLite and the exports.

The report takes a consistent database snapshot. It matches the ledger to the current leads, finds call owners for phone-free overrides, and derives call and lead metrics. An event without a matching lead is excluded until a later drop supplies the missing lead. This approach keeps earlier ledger entries immutable and supports out-of-order delivery.

## Decisions and assumptions

| Decision | Rationale |
|---|---|
| A drop is identified by its relative filenames and file content digests. | Exact copies are safe to replay from another directory. Each drop is an immutable delivery. A revised drop is treated as new input and does not retract the previous one. |
| Prefer a schema field's canonical header over aliases, then use the listed alias order. | Gives deterministic behavior when multiple headers map to the same field. |
| Use half-up rounding for currency and validate comma grouping. | Makes half-cent handling explicit and rejects malformed amounts. |
| Scope call IDs to a workflow. A phone-free override stays quarantined when the call has more than one linked lead. | Prevents applying a correction to the wrong lead when input IDs conflict. |
| Pick the earliest initiation if several events initiate the same call. Break timestamp ties by event ID for dispositions and by call ID for call ordering. | Keeps reports stable across runs. |
| Treat invalid event timestamps or missing structural fields as malformed after duplicate detection when the event ID is usable. | A broken first occurrence still reserves its event ID. |
| Write CSV values exactly as specified, including external references. | The contract requires unmodified values. Use a CSV parser or import columns as text when inspecting untrusted deliveries in a spreadsheet. |

## Failure modes considered

- Duplicate drops and event IDs are handled independently. A wrong key is rejected before any drop is imported. Invalid required fields produce a reason with no raw lead values.
- Ingest uses a write transaction with bound SQL parameters. A failure rolls back the invocation, and a retry can safely start again. Input files are hashed again before the transaction commits to detect changes during ingest.
- Reports are staged and replaced one file at a time. An interruption during replacement can leave files from different runs. Re-run the report command to regenerate the complete set.
- The supplied generator creates synthetic fixtures. Source drops and contract-mandated report references and masked numbers remain sensitive if the pipeline is adapted to real data. The application does not manage OS permissions or key rotation.
- Reporting scans the ledger and materializes the call summaries in memory. Corrections and deletions of already-delivered drops are outside the current design.

## Testing

`python -m unittest discover -v` exercises phone, date and decimal validation, bucket priority, out-of-order overrides, IST day boundaries, late lead linkage, unknown dispositions, duplicate handling, rollback, report replay and PII checks against database and output bytes.

CLI runs on small generator seeds 42 and 137 produced all five reports. For each seed, incremental and combined ingest produced byte-identical reports. Replaying both drops left the database bytes identical. The employer's expected-output fixtures were unavailable. The large dataset was not benchmarked.

## Results

| Seed | Input event lines | Orphans after drop 1 | Orphans after both drops | Two ingests and two report runs |
|---|---:|---:|---:|---:|
| 42 | 8,745 | 1,123 | 462 | 1.38 s |
| 137 | 9,061 | 1,213 | 518 | 1.21 s |

Times are elapsed wall time on Windows with Python 3.13, including process startup and excluding fixture generation. The final orphan counts fell even as more input arrived, because the second delivery supplied previously missing leads.

## If I had another day

1. Benchmark the large dataset and profile report memory usage.
2. Add indexed or incrementally maintained report projections if the full scan becomes a bottleneck.
3. Add operational procedures for rotating the token key and publishing a whole report generation atomically.
