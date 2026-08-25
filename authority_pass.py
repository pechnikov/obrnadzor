#!/usr/bin/env python3
"""Recover active licences through searches partitioned by licensing authority."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from control_pass import SCHEMA, discover_totals, download_pages, parse_options
from obrnadzor import (BASE_URL, Curl, Request, download_details, export_csv, local_now,
                       read_html)


def discover_authorities(curl: Curl, tempdir: Path) -> list[tuple[str, str]]:
    request = Request(0, f"{BASE_URL}/rlic/", tempdir / "authorities.html")
    if 0 not in curl.fetch([request], tempdir):
        raise RuntimeError("could not download the licensing-authority list")
    authorities = parse_options(read_html(request.output), "lo")
    if len(authorities) < 250:
        raise RuntimeError(f"unexpected licensing-authority list: {len(authorities)} entries")
    return authorities


def merge_rows(control, main) -> int:
    before = main.execute("SELECT count(*) FROM licenses").fetchone()[0]
    rows = control.execute(
        """SELECT license_id,min(list_name),min(registration_number),min(order_text),
                  min(validity),min(list_status) FROM page_rows GROUP BY license_id""")
    with main:
        main.executemany(
            """INSERT INTO licenses(id,list_name,registration_number,order_text,validity,list_status)
               VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               list_name=COALESCE(NULLIF(licenses.list_name,''),excluded.list_name),
               registration_number=COALESCE(NULLIF(licenses.registration_number,''),excluded.registration_number),
               order_text=COALESCE(NULLIF(licenses.order_text,''),excluded.order_text),
               validity=COALESCE(NULLIF(licenses.validity,''),excluded.validity),
               list_status=COALESCE(NULLIF(licenses.list_status,''),excluded.list_status)""",
            rows,
        )
    return main.execute("SELECT count(*) FROM licenses").fetchone()[0] - before


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-db", type=Path, default=Path("data/licenses.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("recovery"))
    parser.add_argument("--rate", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--rescan", action="store_true",
                        help="repeat all authority pages; IDs already in the main database are retained")
    parser.add_argument("--max-authorities", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--curl", default="curl.exe" if os.name == "nt" else "curl")
    args = parser.parse_args()
    if args.rate <= 0 or args.batch_size <= 0:
        parser.error("--rate and --batch-size must be positive")
    if not args.main_db.is_file():
        parser.error(f"main database not found: {args.main_db}")
    return args


def main() -> int:
    args = arguments()
    print(f"Started: {local_now()}", flush=True)
    curl_path = shutil.which(args.curl)
    if not curl_path:
        raise SystemExit(f"curl not found: {args.curl}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    database = args.output_dir / "authority.sqlite3"
    curl = Curl(curl_path, args.rate, args.output_dir / ".curl-cookies.txt", workers=1)
    with closing(sqlite3.connect(database)) as control, closing(
            sqlite3.connect(args.main_db)) as main, tempfile.TemporaryDirectory(
                prefix="obrnadzor-authority-") as temp:
        control.executescript(SCHEMA)
        new_ids = merge_rows(control, main)
        tempdir = Path(temp)
        authorities = discover_authorities(curl, tempdir)
        if args.max_authorities:
            authorities = authorities[:args.max_authorities]
        discover_totals(control, curl, authorities, args.batch_size, tempdir, "lo", "Authorities")
        total_pages = control.execute("SELECT sum(total_pages) FROM regions").fetchone()[0] or 0
        print(f"Authority request plan: {len(authorities):,} authorities, {total_pages:,} pages")
        if args.discover_only:
            print(f"Authority SQLite: {database}")
            return 0
        if args.rescan:
            with control:
                control.execute("DELETE FROM page_rows")
                control.execute("DELETE FROM pages")
        try:
            download_pages(control, curl, args.batch_size, tempdir, "lo", "Authority pages")
        finally:
            new_ids += merge_rows(control, main)
        rows, unique_ids = control.execute(
            "SELECT count(*),count(DISTINCT license_id) FROM page_rows").fetchone()
        total = main.execute("SELECT count(*) FROM licenses").fetchone()[0]
        baseline = main.execute("SELECT sum(row_count) FROM list_pages").fetchone()[0] or 0
        print(f"Authority coverage: rows={rows:,}, unique_ids={unique_ids:,}, "
              f"duplicates={rows - unique_ids:,}, new_ids={new_ids:,}, main_total={total:,}, "
              f"remaining_estimate={max(0, baseline - total):,}")
        download_details(main, curl, args.batch_size)
        export_csv(main, args.main_db.parent)
    print(f"Main SQLite: {args.main_db}")
    print(f"Authority SQLite: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
