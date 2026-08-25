#!/usr/bin/env python3
"""Add regional licenses and their detail cards to the main database."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from control_pass import compare
from obrnadzor import Curl, SCHEMA, download_details, export_csv, local_now


LICENSE_COLUMNS = (
    "id", "list_name", "registration_number", "order_text", "validity", "list_status",
    "ogrn", "status", "full_name", "licensing_authority", "region", "short_name", "inn",
    "kpp", "address", "changed_date", "detail_done",
)


def merge_details(main, details_database: Path) -> int:
    """Import an old fill_inn detail cache into the authoritative database."""
    if not details_database.is_file() or details_database.resolve() == Path(
            main.execute("PRAGMA database_list").fetchone()[2]).resolve():
        return 0
    columns = ",".join(LICENSE_COLUMNS)
    placeholders = ",".join("?" for _ in LICENSE_COLUMNS)
    updates = ",".join(f"{column}=excluded.{column}" for column in LICENSE_COLUMNS[1:])
    imported_ids: set[int] = set()
    with closing(sqlite3.connect(
            f"file:{details_database.resolve().as_posix()}?mode=ro", uri=True)) as details:
        with main:
            for row in details.execute(f"SELECT {columns} FROM licenses WHERE detail_done=1"):
                changed = main.execute(f"""INSERT INTO licenses({columns}) VALUES({placeholders})
                                        ON CONFLICT(id) DO UPDATE SET {updates}
                                        WHERE licenses.detail_done=0""", row).rowcount
                if changed:
                    imported_ids.add(row[0])
            main.executemany(
                """INSERT INTO license_branches(branch_id,license_id,name,status,order_text,fetched)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(branch_id) DO UPDATE SET
                   license_id=excluded.license_id,name=excluded.name,status=excluded.status,
                   order_text=excluded.order_text,
                   fetched=max(license_branches.fetched,excluded.fetched)""",
                (row for row in details.execute(
                    "SELECT branch_id,license_id,name,status,order_text,fetched FROM license_branches")
                 if row[1] in imported_ids),
            )
            main.executemany(
                "INSERT OR IGNORE INTO activities(branch_id,category,details) VALUES(?,?,?)",
                details.execute("SELECT branch_id,category,details FROM activities"),
            )
    return len(imported_ids)


def enqueue_missing(main, control_database: Path) -> tuple[int, int]:
    with closing(sqlite3.connect(f"file:{control_database.resolve().as_posix()}?mode=ro", uri=True)) as control:
        control_rows = {row[0]: row[1:] for row in control.execute(
            """SELECT license_id,min(list_name),min(registration_number),min(order_text),
               min(validity),min(list_status) FROM page_rows GROUP BY license_id""")}

    known_ids = {row[0] for row in main.execute("SELECT id FROM licenses")}
    with main:
        main.executemany(
            """INSERT OR IGNORE INTO licenses(id,list_name,registration_number,order_text,
               validity,list_status) VALUES(?,?,?,?,?,?)""",
            ((item, *row) for item, row in control_rows.items() if item not in known_ids),
        )
    total = main.execute("SELECT count(*) FROM licenses").fetchone()[0]
    pending = main.execute("SELECT count(*) FROM licenses WHERE detail_done=0").fetchone()[0]
    return total, pending


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-db", type=Path, default=Path("data/licenses.sqlite3"))
    parser.add_argument("--control-db", type=Path, default=Path("control/control.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("control"))
    parser.add_argument("--details-db", type=Path, default=Path("control/inn.sqlite3"),
                        help="old supplemental database to import, if it exists")
    parser.add_argument("--rate", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--curl", default="curl.exe" if os.name == "nt" else "curl")
    args = parser.parse_args()
    if args.rate <= 0 or args.batch_size <= 0:
        parser.error("--rate and --batch-size must be positive")
    for path in (args.main_db, args.control_db):
        if not path.is_file():
            parser.error(f"database not found: {path}")
    return args


def main() -> int:
    args = arguments()
    print(f"Started: {local_now()}", flush=True)
    curl_path = shutil.which(args.curl)
    if not curl_path:
        raise SystemExit(f"curl not found: {args.curl}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    curl = Curl(curl_path, args.rate, args.output_dir / ".inn-cookies.txt")
    with closing(sqlite3.connect(args.main_db)) as main:
        main.executescript(SCHEMA)
        imported = merge_details(main, args.details_db)
        total, pending = enqueue_missing(main, args.control_db)
        print(f"Imported from old supplemental database: {imported:,}")
        print(f"INN detail queue: {pending:,} pending / {total:,} total")
        print(f"Theoretical transfer time at {args.rate}/s: {pending / args.rate / 3600:.2f} h")
        download_details(main, curl, args.batch_size)
        export_csv(main, args.main_db.parent)
    with closing(sqlite3.connect(args.control_db)) as control:
        summary = compare(control, args.main_db, args.output_dir)
    print("Union summary: " + ", ".join(f"{key}={value:,}" for key, value in summary.items()))
    print(f"Main SQLite: {args.main_db}")
    print(f"Union CSV: {args.output_dir / 'union.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
