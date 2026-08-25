#!/usr/bin/env python3
"""Check the active-license list by region without modifying the main database."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

from obrnadzor import (BASE_URL, Curl, Progress, Request, atomic_csv, clean, local_now,
                       parse_list, read_html, utc_now)


SCHEMA = """
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS regions (
 code TEXT PRIMARY KEY, name TEXT NOT NULL, total_pages INTEGER NOT NULL, discovered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages (
 region_code TEXT NOT NULL REFERENCES regions(code), page INTEGER NOT NULL,
 fetched_at TEXT NOT NULL, row_count INTEGER NOT NULL, PRIMARY KEY(region_code,page));
CREATE TABLE IF NOT EXISTS page_rows (
 region_code TEXT NOT NULL, page INTEGER NOT NULL, position INTEGER NOT NULL, license_id INTEGER NOT NULL,
 list_name TEXT NOT NULL, registration_number TEXT NOT NULL, order_text TEXT NOT NULL,
 validity TEXT NOT NULL, list_status TEXT NOT NULL,
 PRIMARY KEY(region_code,page,position), FOREIGN KEY(region_code,page) REFERENCES pages(region_code,page));
CREATE INDEX IF NOT EXISTS control_license_idx ON page_rows(license_id);
"""


class OptionParser(HTMLParser):
    def __init__(self, select_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.select_id = select_id
        self.in_select = self.in_option = False
        self.value = ""
        self.text: list[str] = []
        self.options: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "select" and attributes.get("id") == self.select_id:
            self.in_select = True
        elif self.in_select and tag == "option":
            self.in_option = True
            self.value, self.text = attributes.get("value", ""), []

    def handle_data(self, data):
        if self.in_option:
            self.text.append(data)

    def handle_endtag(self, tag):
        if self.in_option and tag == "option":
            if self.value:
                self.options.append((self.value, clean("".join(self.text))))
            self.in_option = False
        elif self.in_select and tag == "select":
            self.in_select = False


def parse_options(source: str, select_id: str) -> list[tuple[str, str]]:
    parser = OptionParser(select_id)
    parser.feed(source)
    return list(dict.fromkeys(parser.options))


def parse_regions(source: str) -> list[tuple[str, str]]:
    return parse_options(source, "region")


def save_page(db, region_code: str, page: int, rows: list[dict]) -> None:
    db.execute("DELETE FROM page_rows WHERE region_code=? AND page=?", (region_code, page))
    db.execute("""INSERT INTO pages(region_code,page,fetched_at,row_count) VALUES(?,?,?,?)
                ON CONFLICT(region_code,page) DO UPDATE SET
                fetched_at=excluded.fetched_at,row_count=excluded.row_count""",
               (region_code, page, utc_now(), len(rows)))
    db.executemany("""INSERT INTO page_rows(region_code,page,position,license_id,list_name,
                    registration_number,order_text,validity,list_status) VALUES(?,?,?,?,?,?,?,?,?)""",
                   [(region_code, page, position, row["id"], row["list_name"],
                     row["registration_number"], row["order_text"], row["validity"], row["list_status"])
                    for position, row in enumerate(rows, 1)])


def search_request(key: int, region_code: str, page: int, tempdir: Path,
                   filter_name: str = "region") -> Request:
    data = urlencode({"status": "6", filter_name: region_code, "p": page})
    return Request(key, f"{BASE_URL}/search", tempdir / f"{region_code}-{page}.html", data)


def discover_regions(curl: Curl, tempdir: Path) -> list[tuple[str, str]]:
    request = Request(0, f"{BASE_URL}/rlic/", tempdir / "regions.html")
    if 0 not in curl.fetch([request], tempdir):
        raise RuntimeError("could not download the region list")
    regions = parse_regions(read_html(request.output))
    if len(regions) < 80:
        raise RuntimeError(f"unexpected region list: {len(regions)} entries")
    return regions


def discover_totals(db, curl: Curl, regions: list[tuple[str, str]], batch_size: int,
                    tempdir: Path, filter_name: str = "region", label: str = "Regions") -> None:
    old_totals = dict(db.execute("SELECT code,total_pages FROM regions"))
    pending = [(number, code, name) for number, (code, name) in enumerate(regions, 1)]
    progress = Progress(label, len(regions), 0)
    failures = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        requests = [search_request(number, code, 1, tempdir, filter_name) for number, code, _ in batch]
        downloaded = curl.fetch(requests, tempdir, progress.refresh)
        completed = 0
        with db:
            for request, (_, code, name) in zip(requests, batch):
                if request.key not in downloaded:
                    failures += 1
                    continue
                source = read_html(request.output)
                rows, reported_pages = parse_list(source)
                if 'id="licenses"' not in source or len(rows) > 10:
                    failures += 1
                    continue
                total_pages = reported_pages or (1 if rows else 0)
                if old_totals.get(code) != total_pages:
                    db.execute("DELETE FROM page_rows WHERE region_code=?", (code,))
                    db.execute("DELETE FROM pages WHERE region_code=?", (code,))
                db.execute("""INSERT INTO regions(code,name,total_pages,discovered_at) VALUES(?,?,?,?)
                            ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                            total_pages=excluded.total_pages,discovered_at=excluded.discovered_at""",
                           (code, name, total_pages, utc_now()))
                if rows:
                    save_page(db, code, 1, rows)
                request.output.unlink(missing_ok=True)
                completed += 1
        progress.advance(completed)
    progress.close()
    if failures:
        raise RuntimeError(f"{failures} region discovery requests failed; restart to resume")


def download_pages(db, curl: Curl, batch_size: int, tempdir: Path,
                   filter_name: str = "region", label: str = "Regional pages") -> None:
    region_pages = list(db.execute("SELECT code,total_pages FROM regions ORDER BY code"))
    total = sum(row[1] for row in region_pages)
    completed = db.execute("SELECT count(*) FROM pages").fetchone()[0]
    progress = Progress(label, total, completed)
    failures = 0
    for code, total_pages in region_pages:
        done = {row[0] for row in db.execute("SELECT page FROM pages WHERE region_code=?", (code,))}
        pending = [page for page in range(1, total_pages + 1) if page not in done]
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            requests = [search_request(page, code, page, tempdir, filter_name) for page in batch]
            downloaded = curl.fetch(requests, tempdir, progress.refresh)
            batch_completed = 0
            with db:
                for request in requests:
                    if request.key not in downloaded:
                        failures += 1
                        continue
                    rows, reported_pages = parse_list(read_html(request.output))
                    expected_pages = {total_pages, total_pages - 1} if request.key == total_pages else {total_pages}
                    if (not rows or len(rows) > 10 or
                            (reported_pages is not None and reported_pages not in expected_pages)):
                        failures += 1
                        continue
                    save_page(db, code, request.key, rows)
                    request.output.unlink(missing_ok=True)
                    batch_completed += 1
            progress.advance(batch_completed)
    progress.close()
    if failures:
        raise RuntimeError(f"{failures} regional pages failed; restart to resume")
    actual = db.execute("SELECT count(*) FROM pages").fetchone()[0]
    if actual != total:
        raise RuntimeError(f"control pass is incomplete: {actual}/{total} pages")


def compare(db, main_database: Path, output_dir: Path,
            details_database: Path | None = None) -> dict[str, int]:
    path = main_database.resolve().as_posix()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as main:
        main_rows = {row[0]: row[1:] for row in main.execute(
            """SELECT id,inn,COALESCE(NULLIF(full_name,''),list_name),
               COALESCE(NULLIF(status,''),list_status),registration_number,region,detail_done
               FROM licenses""")}
    supplement_rows = {}
    if details_database and details_database.is_file():
        path = details_database.resolve().as_posix()
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as details:
            supplement_rows = {row[0]: row[1:] for row in details.execute(
                """SELECT id,inn,COALESCE(NULLIF(full_name,''),list_name),
                   COALESCE(NULLIF(status,''),list_status),registration_number,region,detail_done
                   FROM licenses WHERE detail_done=1""")}
    control_rows = {row[0]: row[1:] for row in db.execute(
        """SELECT license_id,min(list_name),min(registration_number),min(list_status),
           min(region_code),group_concat(DISTINCT region_code),count(*)
           FROM page_rows GROUP BY license_id""")}
    region_names = dict(db.execute("SELECT code,name FROM regions"))
    main_ids, control_ids = set(main_rows), set(control_rows)
    missing, main_only = control_ids - main_ids, main_ids - control_ids
    duplicates = list(db.execute(
        """SELECT license_id,count(*),group_concat(region_code || ':' || page || ':' || position)
           FROM page_rows GROUP BY license_id HAVING count(*)>1 ORDER BY count(*) DESC,license_id"""))

    atomic_csv(output_dir / "missing_from_main.csv",
               ([item, f"{BASE_URL}/view/{item}", control_rows[item][3],
                 region_names.get(control_rows[item][3], ""), control_rows[item][1],
                 control_rows[item][0], control_rows[item][5]] for item in sorted(missing)),
               ["ID", "URL", "Код субъекта", "Субъект РФ", "Рег.номер", "Название", "Вхождений"])
    atomic_csv(output_dir / "main_only.csv",
               ([item, f"{BASE_URL}/view/{item}", main_rows[item][3], main_rows[item][1]]
                for item in sorted(main_only)),
               ["ID", "URL", "Рег.номер", "Название"])
    atomic_csv(output_dir / "regional_duplicates.csv", duplicates,
               ["ID", "Вхождений", "Позиции субъект:страница:позиция"])

    inns_by_registration: dict[str, set[str]] = {}
    for rows in (main_rows, supplement_rows):
        for inn, _, _, registration_number, _, _ in rows.values():
            if inn and registration_number:
                inns_by_registration.setdefault(registration_number, set()).add(inn)

    def union_row(item: int) -> list:
        main_row, detail_row, control_row = main_rows.get(item), supplement_rows.get(item), control_rows.get(item)
        picked = lambda index: ((main_row[index] if main_row else "") or
                                (detail_row[index] if detail_row else ""))
        registration_number = picked(3) or (control_row[1] if control_row else "")
        matched = inns_by_registration.get(registration_number, set())
        card_inn = picked(0)
        inn = card_inn or (next(iter(matched)) if len(matched) == 1 else "")
        detail_done = bool((main_row and main_row[5]) or (detail_row and detail_row[5]))
        inn_source = ("карточка" if card_inn else "регистрационный номер" if inn else
                      "нет в карточке" if detail_done else "")
        codes = control_row[4].split(",") if control_row else []
        regions = "; ".join(region_names.get(code, code) for code in codes) or picked(4)
        source = "оба" if main_row and control_row else "основной" if main_row else "региональный"
        return [item, f"{BASE_URL}/view/{item}", inn,
                picked(1) or (control_row[0] if control_row else ""),
                picked(2) or (control_row[2] if control_row else ""),
                registration_number, regions,
                source, inn_source, "да" if detail_done else "нет"]

    union_columns = ["ID", "URL", "ИНН", "Название", "Статус", "Рег.номер", "Субъекты РФ",
                     "Источник записи", "Источник ИНН", "Карточка скачана"]
    union_ids = sorted(main_ids | control_ids)
    atomic_csv(output_dir / "union.csv", (union_row(item) for item in union_ids), union_columns)
    atomic_csv(output_dir / "union_missing_inn.csv",
               (row for item in union_ids if not (row := union_row(item))[2]), union_columns)
    union_with_inn = sum(bool(union_row(item)[2]) for item in union_ids)
    matched_inn = sum(union_row(item)[8] == "регистрационный номер" for item in union_ids)

    summary = {
        "regions": db.execute("SELECT count(*) FROM regions").fetchone()[0],
        "pages": db.execute("SELECT count(*) FROM pages").fetchone()[0],
        "rows": db.execute("SELECT count(*) FROM page_rows").fetchone()[0],
        "unique_ids": len(control_ids), "also_in_main": len(control_ids & main_ids),
        "missing_from_main": len(missing), "main_only": len(main_only),
        "duplicate_ids": len(duplicates), "union_ids": len(union_ids),
        "union_with_inn": union_with_inn, "union_missing_inn": len(union_ids) - union_with_inn,
        "inn_from_registration_number": matched_inn,
        "supplemental_details": len(supplement_rows),
        "supplemental_with_inn": sum(bool(row[0]) for row in supplement_rows.values()),
    }
    atomic_csv(output_dir / "summary.csv", summary.items(), ["Показатель", "Значение"])
    return summary


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-db", type=Path, default=Path("data/licenses.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("control"))
    parser.add_argument("--rate", type=int, default=1, help="additional sequential requests per second")
    parser.add_argument("--batch-size", type=int, default=16)
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
    database = args.output_dir / "control.sqlite3"
    curl = Curl(curl_path, args.rate, args.output_dir / ".curl-cookies.txt", workers=1)
    with closing(sqlite3.connect(database)) as db, tempfile.TemporaryDirectory(prefix="obrnadzor-control-") as temp:
        db.executescript(SCHEMA)
        tempdir = Path(temp)
        regions = discover_regions(curl, tempdir)
        discover_totals(db, curl, regions, args.batch_size, tempdir)
        download_pages(db, curl, args.batch_size, tempdir)
        summary = compare(db, args.main_db, args.output_dir)
    print("Control summary: " + ", ".join(f"{key}={value:,}" for key, value in summary.items()))
    print(f"Control SQLite: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
