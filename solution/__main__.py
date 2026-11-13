"""Run with python -m solution from the repository root."""

import argparse
import csv
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .reports import report
from .store import connect, ingest


def secret_key():
    value = os.environ.get("LEDGER_HMAC_KEY", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("Set LEDGER_HMAC_KEY to a securely generated 32-byte key in hex")
    return bytes.fromhex(value)


def main():
    parser = argparse.ArgumentParser(description="Interaction ledger and reporting pipeline")
    parser.add_argument("--db", type=Path, default=Path("ledger.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("ingest", help="Import drops in the specified order")
    load.add_argument("drops", type=Path, nargs="+")
    export = commands.add_parser("report", help="Write the five required reports")
    export.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "ingest":
            key = secret_key()
            with closing(connect(args.db)) as db:
                count = ingest(db, key, args.drops)
            print(f"Imported {count} of {len(args.drops)} drop(s).")
        else:
            if not args.db.is_file():
                raise ValueError("Ingest data into the database before reporting")
            with closing(connect(args.db)) as db:
                report(db, args.out, args.db)
            print("Wrote five reports.")
    except UnicodeError:
        parser.exit(1, "Error: Input contains invalid text encoding.\n")
    except (ValueError, OSError, sqlite3.Error, csv.Error) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
