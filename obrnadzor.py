#!/usr/bin/env python3
"""Download Rosobrnadzor licence data with curl and store it in SQLite/CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

BASE_URL = "https://islod.obrnadzor.gov.ru"
USER_AGENT = "obrnadzor-downloader/1.0 (+https://github.com/pechnikov/obrnadzor)"
REQUEST_HEADERS = (
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language: ru-RU,ru;q=0.9,en;q=0.5",
    "Referer: https://islod.obrnadzor.gov.ru/rlic/",
)
TIMEOUT_RETRY_SECONDS = 40
WORKERS = 3


def clean(text: str) -> str:
    return " ".join(text.replace("\ufeff", "").split())


class ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str | int]] = []
        self.pages: list[int] = []
        self.in_licenses = self.in_row = self.in_cell = False
        self.cells: list[str] = []
        self.cell: list[str] = []
        self.license_id: int | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tbody" and attributes.get("id") == "licenses":
            self.in_licenses = True
        elif self.in_licenses and tag == "tr":
            self.in_row = True
            self.cells, self.license_id = [], None
        elif self.in_row and tag == "td":
            self.in_cell, self.cell = True, []
        elif self.in_cell and tag == "a":
            match = re.fullmatch(r"/view/(\d+)", attributes.get("href", ""))
            if match:
                self.license_id = int(match.group(1))
        if tag == "a":
            match = re.search(r"search\((\d+)\)", attributes.get("onclick", ""))
            if match:
                self.pages.append(int(match.group(1)))

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.in_cell and tag == "td":
            self.cells.append(clean("".join(self.cell)))
            self.in_cell = False
        elif self.in_row and tag == "tr":
            if self.license_id is not None and len(self.cells) >= 5:
                self.rows.append({
                    "id": self.license_id, "list_name": self.cells[0],
                    "registration_number": self.cells[1], "order_text": self.cells[2],
                    "validity": self.cells[3], "list_status": self.cells[4],
                })
            self.in_row = False
        elif self.in_licenses and tag == "tbody":
            self.in_licenses = False


class DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.branches: list[dict[str, str | int]] = []
        self.label_depth = self.field_depth = 0
        self.label_text: list[str] = []
        self.field_text: list[str] = []
        self.pending_label: str | None = None
        self.in_branch_table = self.in_row = self.in_cell = False
        self.row_cells: list[str] = []
        self.cell_text: list[str] = []
        self.row_branch_id: int | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "label" and "form-label" in classes:
            self.label_depth, self.label_text = 1, []
        elif self.label_depth and tag == "label":
            self.label_depth += 1
        if tag == "div" and self.pending_label and {"form-field", "disabled"} <= classes:
            self.field_depth, self.field_text = 1, []
        elif self.field_depth and tag == "div":
            self.field_depth += 1
        if tag == "table" and "tbl-list" in classes:
            self.in_branch_table = True
        elif self.in_branch_table and tag == "tr":
            self.in_row, self.row_cells, self.row_branch_id = True, [], None
        elif self.in_row and tag == "td":
            self.in_cell, self.cell_text = True, []
        elif self.in_cell and tag == "a":
            match = re.fullmatch(r"/branch/(\d+)", attributes.get("href", ""))
            if match:
                self.row_branch_id = int(match.group(1))

    def handle_data(self, data):
        if self.label_depth:
            self.label_text.append(data)
        if self.field_depth:
            self.field_text.append(data)
        if self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag):
        if self.label_depth and tag == "label":
            self.label_depth -= 1
            if not self.label_depth:
                self.pending_label = clean("".join(self.label_text))
        if self.field_depth and tag == "div":
            self.field_depth -= 1
            if not self.field_depth and self.pending_label:
                self.fields[self.pending_label] = clean("".join(self.field_text))
                self.pending_label = None
        if self.in_cell and tag == "td":
            self.row_cells.append(clean("".join(self.cell_text)))
            self.in_cell = False
        elif self.in_row and tag == "tr":
            if self.row_branch_id is not None and self.row_cells:
                self.branches.append({
                    "id": self.row_branch_id, "name": self.row_cells[0],
                    "status": self.row_cells[1] if len(self.row_cells) > 1 else "",
                    "order_text": self.row_cells[2] if len(self.row_cells) > 2 else "",
                })
            self.in_row = False
        elif self.in_branch_table and tag == "table":
            self.in_branch_table = False


class ActivityParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, str]]]] = []
        self.in_table = self.in_row = False
        self.cell_tag: str | None = None
        self.cell_text: list[str] = []
        self.row: list[tuple[str, str]] = []
        self.table: list[list[tuple[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "table" and "table-filled" in classes:
            self.in_table, self.table = True, []
        elif self.in_table and tag == "tr":
            self.in_row, self.row = True, []
        elif self.in_row and tag in {"th", "td"}:
            self.cell_tag, self.cell_text = tag, []

    def handle_data(self, data):
        if self.cell_tag:
            self.cell_text.append(data)

    def handle_endtag(self, tag):
        if self.cell_tag == tag:
            self.row.append((tag, clean("".join(self.cell_text))))
            self.cell_tag = None
        elif self.in_row and tag == "tr":
            if any(value for _, value in self.row):
                self.table.append(self.row)
            self.in_row = False
        elif self.in_table and tag == "table":
            if self.table:
                self.tables.append(self.table)
            self.in_table = False

    def activities(self) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for rows in self.tables:
            category = rows[0][0][1] if rows and rows[0] else ""
            details: list[str] = []
            for row in rows[1:]:
                if row and all(tag == "th" for tag, _ in row):
                    continue
                values = [value for _, value in row if value]
                if values and re.fullmatch(r"\d+", values[0]):
                    values = values[1:]
                if values:
                    details.append(" — ".join(values))
            if category:
                result.extend((category, detail) for detail in details or [""])
        return list(dict.fromkeys(result))


def parse_list(source: str):
    parser = ListParser(); parser.feed(source)
    return parser.rows, max(parser.pages, default=None)


def parse_detail(source: str):
    parser = DetailParser(); parser.feed(source)
    return parser.fields, list({int(row["id"]): row for row in parser.branches}.values())


def parse_activities(source: str):
    parser = ActivityParser(); parser.feed(source)
    return parser.activities()


SCHEMA = """
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS list_pages (page INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, row_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS licenses (
 id INTEGER PRIMARY KEY, list_name TEXT NOT NULL DEFAULT '', registration_number TEXT NOT NULL DEFAULT '',
 order_text TEXT NOT NULL DEFAULT '', validity TEXT NOT NULL DEFAULT '', list_status TEXT NOT NULL DEFAULT '',
 ogrn TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
 licensing_authority TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '', short_name TEXT NOT NULL DEFAULT '',
 inn TEXT NOT NULL DEFAULT '', kpp TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
 changed_date TEXT NOT NULL DEFAULT '', detail_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS license_branches (
 branch_id INTEGER PRIMARY KEY, license_id INTEGER NOT NULL REFERENCES licenses(id), name TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT '', order_text TEXT NOT NULL DEFAULT '', fetched INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS branches_license_idx ON license_branches(license_id);
CREATE TABLE IF NOT EXISTS activities (
 branch_id INTEGER NOT NULL REFERENCES license_branches(branch_id), category TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '', PRIMARY KEY (branch_id, category, details));
"""


@dataclass(frozen=True)
class Request:
    key: int
    url: str
    output: Path
    data: str | None = None


class Curl:
    def __init__(self, executable: str, rate: int, cookie_file: Path,
                 cooldown: int = TIMEOUT_RETRY_SECONDS, stream=None, workers: int = WORKERS) -> None:
        self.executable, self.rate, self.workers = executable, rate, workers
        self.cooldown = cooldown
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.status_width = 0
        self.cookie_files = [cookie_file.with_name(f"{cookie_file.stem}-{number}{cookie_file.suffix}")
                             for number in range(1, workers + 1)]
        for path in self.cookie_files:
            path.touch(exist_ok=True)

    @staticmethod
    def _quoted(value: str | Path) -> str:
        return json.dumps(str(value).replace("\\", "/"), ensure_ascii=False)

    def _run(self, requests: list[Request], config: Path, rate=None, cookie_file=None,
             tick=None) -> tuple[set[int], bool]:
        rate = rate or self.rate
        cookie_file = cookie_file or self.cookie_files[0]
        lines: list[str] = []
        for index, request in enumerate(requests):
            lines += [
                f"url = {self._quoted(request.url)}", f"output = {self._quoted(request.output)}",
                "connect-timeout = 15", "max-time = 90", "fail-with-body", "remove-on-error",
                "compressed", f"user-agent = {self._quoted(USER_AGENT)}",
                f"cookie = {self._quoted(cookie_file)}",
                f"cookie-jar = {self._quoted(cookie_file)}",
                *(f"header = {self._quoted(header)}" for header in REQUEST_HEADERS),
            ]
            if request.data is not None:
                lines += ["request = POST", f"data = {self._quoted(request.data)}"]
            if index + 1 < len(requests):
                lines.append("next")
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [self.executable, "--silent", "--show-error", "--fail-early",
                   "--rate", f"{rate}/s", "--config", str(config)]
        process = subprocess.Popen(command, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while True:
            try:
                _, errors = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if tick:
                    tick()
        error_lines = [line for line in errors.splitlines() if "curl: (28)" not in line]
        if error_lines:
            print("\n".join(error_lines), file=sys.stderr)
        return ({request.key for request in requests if request.output.is_file()}, "curl: (28)" in errors)

    def _timeout_status(self, started: float, retries: int, suffix: str, force: bool = False) -> None:
        line = f"Timeouts       {format_duration(time.monotonic() - started)} | retries: {retries} | {suffix}"
        if self.interactive:
            self.status_width = max(self.status_width, len(line))
            print(f"\r{line:<{self.status_width}}", end="", file=self.stream, flush=True)
        elif force:
            print(line, file=self.stream, flush=True)

    def _recover(self, request: Request, config: Path) -> None:
        if self.interactive:
            print(file=self.stream)
        print(f"Timeout started: {local_now()}", file=self.stream, flush=True)
        started, retries = time.monotonic(), 0
        next_retry = started + self.cooldown
        while True:
            first_status = True
            while (remaining := next_retry - time.monotonic()) > 0:
                self._timeout_status(started, retries, f"next retry in {math.ceil(remaining)}s", force=first_status)
                first_status = False
                time.sleep(min(1, remaining))
            retries += 1
            attempt_started = time.monotonic()
            tick = lambda: self._timeout_status(started, retries, "retry in progress")
            downloaded, _ = self._run([request], config, self.rate, self.cookie_files[0], tick)
            if request.key in downloaded:
                message = f"Connection restored after {format_duration(time.monotonic() - started)}, retries: {retries}"
                if self.interactive:
                    print(f"\r{message:<{self.status_width}}", file=self.stream, flush=True)
                else:
                    print(message, file=self.stream, flush=True)
                return
            next_retry = attempt_started + self.cooldown

    def fetch(self, requests: list[Request], workdir: Path, tick=None) -> set[int]:
        pending = requests
        while pending:
            active = min(self.workers, self.rate, len(pending))
            base_rate, faster_workers = divmod(self.rate, active)
            groups = [pending[number::active] for number in range(active)]
            with ThreadPoolExecutor(max_workers=active) as pool:
                futures = [pool.submit(self._run, group, workdir / f"curl-{number + 1}.cfg",
                                       base_rate + (number < faster_workers), self.cookie_files[number],
                                       tick if number == 0 else None)
                           for number, group in enumerate(groups)]
                results = [future.result() for future in futures]
            downloaded = set().union(*(result[0] for result in results))
            timed_out = any(result[1] for result in results)
            pending = [request for request in pending if request.key not in downloaded]
            if not pending or not timed_out:
                break
            self._recover(pending[0], workdir / "curl-recovery.cfg")
            pending = [request for request in pending if not request.output.is_file()]
        return {request.key for request in requests if request.output.is_file()}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now() -> str:
    return datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))


def chunks(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--"
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class Progress:
    BAR_WIDTH = 20

    def __init__(self, label: str, total: int, completed: int = 0, stream=None) -> None:
        self.label, self.total = label, total
        self.completed = self.initial = completed
        self.started = time.monotonic()
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.closed = False
        self._render()

    def advance(self, count: int) -> None:
        self.completed += count
        self._render()

    def refresh(self) -> None:
        if self.interactive and not self.closed:
            self._render()

    def close(self) -> None:
        if self.interactive and not self.closed:
            print(file=self.stream, flush=True)
        self.closed = True

    def _render(self) -> None:
        ratio = self.completed / self.total if self.total else 1.0
        ratio = min(1.0, max(0.0, ratio))
        elapsed = max(time.monotonic() - self.started, 1e-9)
        current_run = self.completed - self.initial
        speed = current_run / elapsed
        remaining = max(0, self.total - self.completed)
        eta = 0 if not remaining else remaining / speed if speed else None
        filled = round(self.BAR_WIDTH * ratio)
        bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
        line = (f"{self.label:<14} [{bar}] {ratio:6.2%} "
                f"{self.completed:,}/{self.total:,} | {speed:5.1f} req/s | ETA {format_duration(eta)}")
        finished = self.completed >= self.total
        print(f"\r{line}" if self.interactive else line, end="\n" if not self.interactive or finished else "",
              file=self.stream, flush=True)
        self.closed = finished


def read_html(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def download_list(db, curl, scope, batch_size, max_pages, rescan):
    status = "6" if scope == "active" else ""
    with tempfile.TemporaryDirectory(prefix="obrnadzor-list-") as temp:
        tempdir = Path(temp)
        first = Request(1, f"{BASE_URL}/search", tempdir / "1.html", urlencode({"status": status, "p": 1}))
        curl.fetch([first], tempdir)
        if not first.output.exists():
            raise RuntimeError("curl did not download the first list page")
        rows, total_pages = parse_list(read_html(first.output))
        if not rows or not total_pages:
            raise RuntimeError("the first list page has an unexpected format")
        set_meta(db, "total_pages", total_pages); set_meta(db, "scope", scope); db.commit()
        target = min(total_pages, max_pages) if max_pages else total_pages
        done = set() if rescan else {r[0] for r in db.execute("SELECT page FROM list_pages WHERE page<=?", (target,))}
        pending = [page for page in range(1, target + 1) if page not in done]
        progress = Progress("List", target, target - len(pending))
        failures = 0
        for batch in chunks(pending, batch_size):
            requests = [Request(page, f"{BASE_URL}/search", tempdir / f"{page}.html",
                                urlencode({"status": status, "p": page})) for page in batch]
            downloaded = curl.fetch(requests, tempdir, progress.refresh)
            completed = 0
            with db:
                for request in requests:
                    if request.key not in downloaded:
                        failures += 1; continue
                    page_rows, _ = parse_list(read_html(request.output))
                    if not page_rows or len(page_rows) > 10:
                        failures += 1; continue
                    for row in page_rows:
                        db.execute("""INSERT INTO licenses(id,list_name,registration_number,order_text,validity,list_status)
                         VALUES(:id,:list_name,:registration_number,:order_text,:validity,:list_status)
                         ON CONFLICT(id) DO UPDATE SET list_name=excluded.list_name,
                         registration_number=excluded.registration_number,order_text=excluded.order_text,
                         validity=excluded.validity,list_status=excluded.list_status""", row)
                    db.execute("""INSERT INTO list_pages(page,fetched_at,row_count) VALUES(?,?,?)
                     ON CONFLICT(page) DO UPDATE SET fetched_at=excluded.fetched_at,row_count=excluded.row_count""",
                               (request.key, utc_now(), len(page_rows)))
                    request.output.unlink(missing_ok=True)
                    completed += 1
            progress.advance(completed)
        progress.close()
        if failures:
            raise RuntimeError(f"{failures} list pages failed; restart the same command to resume")


FIELD_MAP = {
    "ОГРН": "ogrn", "Решение о предоставлении": "order_text",
    "Текущий статус лицензии": "status",
    "Полное наименование организации (ФИО индивидуального предпринимателя)": "full_name",
    "Наименование органа, выдавшего лицензию": "licensing_authority", "Срок действия": "validity",
    "Субьект РФ": "region", "Сокращенное наименование организации": "short_name",
    "ИНН": "inn", "КПП": "kpp", "Регистрационный номер лицензии": "registration_number",
    "Место нахождения организации": "address", "Дата внесения изменений": "changed_date",
}


def download_details(db, curl, batch_size):
    ids = [r[0] for r in db.execute("SELECT id FROM licenses WHERE detail_done=0 ORDER BY id")]
    total = db.execute("SELECT count(*) FROM licenses").fetchone()[0]
    progress = Progress("Details", total, total - len(ids))
    failures = 0
    with tempfile.TemporaryDirectory(prefix="obrnadzor-details-") as temp:
        tempdir = Path(temp)
        for batch in chunks(ids, batch_size):
            requests = [Request(item, f"{BASE_URL}/view/{item}", tempdir / f"{item}.html") for item in batch]
            downloaded = curl.fetch(requests, tempdir, progress.refresh)
            completed = 0
            with db:
                for request in requests:
                    if request.key not in downloaded:
                        failures += 1; continue
                    fields, branches = parse_detail(read_html(request.output))
                    if "Текущий статус лицензии" not in fields or "ИНН" not in fields:
                        failures += 1; continue
                    values = {column: fields.get(label, "") for label, column in FIELD_MAP.items()}
                    assignments = ",".join(f"{column}=?" for column in values)
                    db.execute(f"UPDATE licenses SET {assignments},detail_done=1 WHERE id=?",
                               (*values.values(), request.key))
                    for branch in branches:
                        db.execute("""INSERT INTO license_branches(branch_id,license_id,name,status,order_text)
                         VALUES(?,?,?,?,?) ON CONFLICT(branch_id) DO UPDATE SET license_id=excluded.license_id,
                         name=excluded.name,status=excluded.status,order_text=excluded.order_text""",
                                   (branch["id"], request.key, branch["name"], branch["status"], branch["order_text"]))
                    request.output.unlink(missing_ok=True)
                    completed += 1
            progress.advance(completed)
        progress.close()
        if failures:
            raise RuntimeError(f"{failures} detail pages failed; restart the same command to resume")


def download_activities(db, curl, batch_size):
    ids = [r[0] for r in db.execute("SELECT branch_id FROM license_branches WHERE fetched=0 ORDER BY branch_id")]
    total = db.execute("SELECT count(*) FROM license_branches").fetchone()[0]
    progress = Progress("Activities", total, total - len(ids))
    failures = 0
    with tempfile.TemporaryDirectory(prefix="obrnadzor-activities-") as temp:
        tempdir = Path(temp)
        for batch in chunks(ids, batch_size):
            requests = [Request(item, f"{BASE_URL}/branch/{item}", tempdir / f"{item}.html") for item in batch]
            downloaded = curl.fetch(requests, tempdir, progress.refresh)
            completed = 0
            with db:
                for request in requests:
                    if request.key not in downloaded:
                        failures += 1; continue
                    source = read_html(request.output)
                    if "Полное наименование организации" not in source:
                        failures += 1; continue
                    db.execute("DELETE FROM activities WHERE branch_id=?", (request.key,))
                    db.executemany("INSERT OR IGNORE INTO activities(branch_id,category,details) VALUES(?,?,?)",
                                   [(request.key, *item) for item in parse_activities(source)])
                    db.execute("UPDATE license_branches SET fetched=1 WHERE branch_id=?", (request.key,))
                    request.output.unlink(missing_ok=True)
                    completed += 1
            progress.advance(completed)
        progress.close()
        if failures:
            raise RuntimeError(f"{failures} branch pages failed; restart the same command to resume")


CSV_COLUMNS = ["ID", "URL", "ИНН", "Название организации полное", "Название организации сокращенное",
               "Статус", "Рег.номер", "Приказ", "Срок действия", "КПП", "Адрес", "Дата изменений",
               "Виды деятельности"]


def atomic_csv(path, rows, columns):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream); writer.writerow(columns); writer.writerows(rows)
    os.replace(temporary, path)


def export_csv(db, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)

    def full_rows():
        query = """SELECT l.id,l.inn,COALESCE(NULLIF(l.full_name,''),l.list_name),l.short_name,
         COALESCE(NULLIF(l.status,''),l.list_status),l.registration_number,l.order_text,
         l.validity,l.kpp,l.address,l.changed_date,a.category,a.details FROM licenses l
         LEFT JOIN license_branches b ON b.license_id=l.id LEFT JOIN activities a ON a.branch_id=b.branch_id
         ORDER BY l.id,a.category,a.details"""
        current, activities = None, []
        for row in db.execute(query):
            if current is not None and row[0] != current[0]:
                yield [current[0], f"{BASE_URL}/view/{current[0]}", *current[1:], "; ".join(dict.fromkeys(activities))]
                activities = []
            if current is None or row[0] != current[0]:
                current = list(row[:11])
            if row[11]:
                activities.append(f"{row[11]}: {row[12]}" if row[12] else row[11])
        if current is not None:
            yield [current[0], f"{BASE_URL}/view/{current[0]}", *current[1:], "; ".join(dict.fromkeys(activities))]

    atomic_csv(output_dir / "licenses.csv", full_rows(), CSV_COLUMNS)
    minimal = db.execute("""SELECT DISTINCT inn,COALESCE(NULLIF(full_name,''),list_name) AS name FROM licenses
     WHERE COALESCE(NULLIF(status,''),list_status)='Действующая' AND inn<>''
     AND COALESCE(NULLIF(full_name,''),list_name)<>'' ORDER BY inn,name""")
    atomic_csv(output_dir / "active_inn_name.csv", minimal, ["ИНН", "Название"])
    print(f"CSV: {output_dir / 'licenses.csv'}\nCSV minimum: {output_dir / 'active_inn_name.csv'}")


def print_request_plan(db, rate, include_branches):
    pages = db.execute("SELECT count(*) FROM list_pages").fetchone()[0]
    licenses = db.execute("SELECT count(*) FROM licenses").fetchone()[0]
    branches = db.execute("SELECT count(*) FROM license_branches").fetchone()[0] if include_branches else 0
    total = pages + licenses + branches
    print(f"Request plan: {pages:,} list + {licenses:,} details + {branches:,} branches = {total:,}")
    print(f"Theoretical transfer time at {rate:g}/s: {total / rate / 3600:.2f} h")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--scope", choices=("active", "all"), default="active",
                        help="active licenses only; all is much larger")
    parser.add_argument("--minimal", action="store_true", help="skip activity branch pages")
    parser.add_argument("--rate", type=int, default=8, help="maximum sequential curl request starts per second")
    parser.add_argument("--batch-size", type=int, default=16, help="requests between progress updates")
    parser.add_argument("--max-pages", type=int, help="download only this many list pages (smoke test)")
    parser.add_argument("--rescan-list", action="store_true", help="refresh downloaded list pages")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--curl", default="curl.exe" if os.name == "nt" else "curl")
    args = parser.parse_args()
    if args.rate <= 0 or args.batch_size <= 0:
        parser.error("--rate and --batch-size must be positive")
    return args


def main():
    args = arguments()
    print(f"Started: {local_now()}", flush=True)
    curl_path = shutil.which(args.curl)
    if not curl_path:
        raise SystemExit(f"curl not found: {args.curl}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    database = args.output_dir / "licenses.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(SCHEMA)
        old_scope = get_meta(db, "scope")
        if old_scope and old_scope != args.scope and not args.export_only:
            raise SystemExit(f"database scope is {old_scope!r}; use another --output-dir for {args.scope!r}")
        if not args.export_only:
            curl = Curl(curl_path, args.rate, args.output_dir / ".curl-cookies.txt")
            started = time.monotonic()
            download_list(db, curl, args.scope, args.batch_size, args.max_pages, args.rescan_list)
            download_details(db, curl, args.batch_size)
            print_request_plan(db, args.rate, not args.minimal)
            if not args.minimal:
                download_activities(db, curl, args.batch_size)
            print(f"Download time: {(time.monotonic() - started) / 3600:.2f} h")
        export_csv(db, args.output_dir)
        print(f"SQLite: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
