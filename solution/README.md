# Assignment A solution

See the [project overview and quick start](../README.md) for the pipeline diagram and run commands.

| Module | Responsibility |
| --- | --- |
| [parsing.py](parsing.py) | Normalize and validate source fields |
| [store.py](store.py) | Import drops and persist leads and events |
| [schema.sql](schema.sql) | Define SQLite tables and enforce the append-only ledger |
| [reports.py](reports.py) | Resolve call history and write the five reports |
| [test_pipeline.py](test_pipeline.py) | Verify business rules and ingestion behavior |

[Design and results](../SUBMISSION.md) · [AI disclosure](../AI_USAGE.md)
