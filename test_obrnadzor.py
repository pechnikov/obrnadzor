import io
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from authority_pass import merge_rows
from control_pass import (SCHEMA as CONTROL_SCHEMA, compare, discover_totals, download_pages,
                          parse_options, parse_regions, save_page)
from fill_inn import enqueue_missing, merge_details
from obrnadzor import (SCHEMA as MAIN_SCHEMA, Curl, Progress, Request, format_duration,
                       export_csv, parse_activities, parse_detail, parse_list)


class ParserTests(unittest.TestCase):
    def test_list(self):
        source = '''<tbody id="licenses"><tr><td><a href="/view/38300">ООО «ТРЕНД»</a></td>
        <td>Л035</td><td>Приказ №1</td><td>Бессрочная</td><td>Действующая</td></tr></tbody>
        <a onclick="return pageObject.search(13628)">last</a>'''
        rows, pages = parse_list(source)
        self.assertEqual(13628, pages)
        self.assertEqual(38300, rows[0]["id"])
        self.assertEqual("ООО «ТРЕНД»", rows[0]["list_name"])

    def test_detail(self):
        source = '''<label class="form-label">ИНН</label>
        <div class="form-field disabled">7714920616</div>
        <label class="form-label">Текущий статус лицензии</label>
        <div class="form-field disabled">Действующая</div>
        <table class="tbl-list"><tr><td><a href="/branch/238805">ООО «ТРЕНД»</a></td>
        <td>Действует</td><td>Приказ №1</td></tr></table>'''
        fields, branches = parse_detail(source)
        self.assertEqual("7714920616", fields["ИНН"])
        self.assertEqual(238805, branches[0]["id"])

    def test_activities(self):
        source = '''<table class="table-filled"><thead><tr><th colspan="2">Общее образование</th></tr>
        <tr><th>№</th><th>Уровень образования</th></tr></thead>
        <tbody><tr><td>1</td><td>Начальное общее образование</td></tr></tbody></table>'''
        self.assertEqual(
            [("Общее образование", "Начальное общее образование")],
            parse_activities(source),
        )

    def test_branch_without_active_programs(self):
        self.assertEqual([], parse_activities("<table><tr><td>Нет действующих ОП</td></tr></table>"))

    def test_progress(self):
        stream = io.StringIO()
        progress = Progress("Details", 100, 20, stream)
        progress.started -= 10
        progress.advance(10)
        output = stream.getvalue()
        self.assertIn("30.00%", output)
        self.assertIn("30/100", output)
        self.assertIn("ETA", output)
        self.assertEqual("01:01:01", format_duration(3661))

        finished = io.StringIO()
        Progress("List", 1, 1, finished)
        self.assertIn("ETA 00:00:00", finished.getvalue())

    def test_timeout_recovery(self):
        class RecoveringCurl(Curl):
            def _run(self, requests, config, rate=None, cookie_file=None, tick=None):
                self.calls += 1
                if tick:
                    tick()
                if self.calls == 3:
                    requests[0].output.write_text("ok", encoding="utf-8")
                downloaded = {request.key for request in requests if request.output.is_file()}
                return downloaded, not downloaded

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            curl = RecoveringCurl("curl", 8, directory / "cookies.txt", cooldown=0, stream=io.StringIO())
            curl.calls = 0
            request = Request(1, "https://example.invalid", directory / "1.html")
            self.assertEqual({1}, curl.fetch([request], directory))
            self.assertEqual(3, curl.calls)
            self.assertIn("Timeout started:", curl.stream.getvalue())
            self.assertIn("Connection restored", curl.stream.getvalue())

    def test_curl_is_sequential_and_rate_limited(self):
        class FinishedProcess:
            def communicate(self, timeout=None):
                return None, ""

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            curl = Curl("curl", 8, directory / "cookies.txt")
            requests = [Request(i, f"https://example.invalid/{i}", directory / f"{i}.html") for i in range(2)]
            with patch("obrnadzor.subprocess.Popen", return_value=FinishedProcess()) as popen:
                curl._run(requests, directory / "curl.cfg")
            command = popen.call_args.args[0]
            self.assertNotIn("--parallel", command)
            self.assertEqual("8/s", command[command.index("--rate") + 1])

    def test_three_workers_share_global_rate(self):
        class RecordingCurl(Curl):
            def _run(self, requests, config, rate=None, cookie_file=None, tick=None):
                self.calls.append((len(requests), config, rate, cookie_file))
                for request in requests:
                    request.output.write_text("ok", encoding="utf-8")
                return {request.key for request in requests}, False

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            curl = RecordingCurl("curl", 8, directory / "cookies.txt")
            curl.calls = []
            requests = [Request(i, f"https://example.invalid/{i}", directory / f"{i}.html")
                        for i in range(9)]
            self.assertEqual(set(range(9)), curl.fetch(requests, directory))
            self.assertEqual([2, 3, 3], sorted(call[2] for call in curl.calls))
            self.assertEqual(3, len({call[1] for call in curl.calls}))
            self.assertEqual(3, len({call[3] for call in curl.calls}))

    def test_control_pass_preserves_page_positions(self):
        source = '''<select id="region"><option value="">Не выбрано</option>
        <option value="77">г. Москва</option><option value="01">Республика Адыгея</option></select>'''
        self.assertEqual([("77", "г. Москва"), ("01", "Республика Адыгея")], parse_regions(source))
        duplicate = {"id": 42, "list_name": "A", "registration_number": "R", "order_text": "O",
                     "validity": "V", "list_status": "Действующая"}
        missing = {**duplicate, "id": 43, "list_name": "B", "registration_number": "R2"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            main_path = directory / "main.sqlite3"
            with closing(sqlite3.connect(main_path)) as main:
                main.execute("""CREATE TABLE licenses(id INTEGER,inn TEXT,full_name TEXT,list_name TEXT,
                             status TEXT,list_status TEXT,registration_number TEXT,region TEXT,detail_done INTEGER)""")
                main.executemany("INSERT INTO licenses VALUES(?,?,?,?,?,?,?,?,?)",
                                 [(42, "7700000000", "A", "", "Действующая", "", "R", "Москва", 1),
                                  (44, "7800000000", "C", "", "Действующая", "", "R2", "Москва", 1)])
                main.commit()
            with closing(sqlite3.connect(directory / "control.sqlite3")) as db:
                db.executescript(CONTROL_SCHEMA)
                db.execute("INSERT INTO regions VALUES(?,?,?,?)", ("77", "г. Москва", 2, "now"))
                save_page(db, "77", 1, [duplicate, duplicate])
                save_page(db, "77", 2, [missing])
                self.assertEqual((3, 2), db.execute(
                    "SELECT count(*),count(DISTINCT license_id) FROM page_rows").fetchone())
                summary = compare(db, main_path, directory)
            self.assertEqual((1, 1, 1),
                             (summary["missing_from_main"], summary["main_only"], summary["duplicate_ids"]))
            self.assertEqual((3, 3, 1), (summary["union_ids"], summary["union_with_inn"],
                                        summary["inn_from_registration_number"]))
            with (directory / "union.csv").open(encoding="utf-8-sig", newline="") as stream:
                union = list(csv.DictReader(stream))
            self.assertEqual(["оба", "региональный", "основной"],
                             [row["Источник записи"] for row in union])
            self.assertEqual(("7800000000", "регистрационный номер"),
                             (union[1]["ИНН"], union[1]["Источник ИНН"]))

    def test_authority_pass_parses_and_merges_new_id(self):
        source = '<select id="lo"><option value="1">Federal</option></select>'
        self.assertEqual([("1", "Federal")], parse_options(source, "lo"))
        row = {"id": 3, "list_name": "Three", "registration_number": "R3", "order_text": "O",
               "validity": "V", "list_status": "Действующая"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with closing(sqlite3.connect(directory / "control.sqlite3")) as control, closing(
                    sqlite3.connect(directory / "main.sqlite3")) as main:
                control.executescript(CONTROL_SCHEMA)
                control.execute("INSERT INTO regions VALUES(?,?,?,?)", ("1", "Federal", 1, "now"))
                save_page(control, "1", 1, [row])
                main.executescript(MAIN_SCHEMA)
                self.assertEqual(1, merge_rows(control, main))
                self.assertEqual((3, "Three"), main.execute(
                    "SELECT id,list_name FROM licenses").fetchone())

    def test_control_pass_refreshes_changed_region_and_accepts_last_page(self):
        def page(item, last_link):
            return f'''<tbody id="licenses"><tr><td><a href="/view/{item}">Name</a></td>
            <td>R</td><td>O</td><td>V</td><td>Действующая</td></tr></tbody>
            <a onclick="return pageObject.search({last_link})">last</a>'''

        class RegionalCurl:
            def fetch(self, requests, _tempdir, tick=None):
                for request in requests:
                    number = int(request.data.rsplit("p=", 1)[1])
                    request.output.write_text(page(100 + number, 2 if number == 3 else 3), encoding="utf-8")
                return {request.key for request in requests}

        old = {"id": 1, "list_name": "Old", "registration_number": "R", "order_text": "O",
               "validity": "V", "list_status": "Действующая"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with closing(sqlite3.connect(directory / "control.sqlite3")) as db:
                db.executescript(CONTROL_SCHEMA)
                db.execute("INSERT INTO regions VALUES(?,?,?,?)", ("77", "Москва", 2, "old"))
                save_page(db, "77", 1, [old])
                save_page(db, "77", 2, [old])
                discover_totals(db, RegionalCurl(), [("77", "Москва")], 16, directory)
                self.assertEqual((3, 1), db.execute(
                    "SELECT total_pages,(SELECT count(*) FROM pages) FROM regions").fetchone())
                download_pages(db, RegionalCurl(), 16, directory)
                self.assertEqual([(1,), (2,), (3,)], db.execute("SELECT page FROM pages ORDER BY page").fetchall())

    def test_fill_inn_merges_into_main_database(self):
        row = {"id": 1, "list_name": "One", "registration_number": "R1", "order_text": "O",
               "validity": "V", "list_status": "Действующая"}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            main_path, control_path, details_path = (directory / name for name in
                                                      ("main.sqlite3", "control.sqlite3", "inn.sqlite3"))
            with closing(sqlite3.connect(main_path)) as main:
                main.executescript(MAIN_SCHEMA)
                main.executemany("""INSERT INTO licenses(id,inn,list_name,registration_number,
                                 list_status,detail_done) VALUES(?,?,?,?,?,?)""",
                                 [(1, "7700000000", "One", "R1", "Действующая", 1),
                                  (2, "", "Two", "R2", "Действующая", 1)])
                main.commit()
            with closing(sqlite3.connect(control_path)) as control:
                control.executescript(CONTROL_SCHEMA)
                control.execute("INSERT INTO regions VALUES(?,?,?,?)", ("77", "Москва", 1, "now"))
                save_page(control, "77", 1, [row, {**row, "id": 3, "list_name": "Three",
                                                    "registration_number": "R3"}])
                control.commit()
            with closing(sqlite3.connect(details_path)) as details:
                details.executescript(MAIN_SCHEMA)
                details.execute("""INSERT INTO licenses(id,inn,full_name,status,detail_done)
                                VALUES(3,'7800000000','Three','Действующая',1)""")
                details.commit()
            with closing(sqlite3.connect(main_path)) as main:
                self.assertEqual(1, merge_details(main, details_path))
                self.assertEqual((3, 0), enqueue_missing(main, control_path))
                self.assertEqual("7800000000", main.execute(
                    "SELECT inn FROM licenses WHERE id=3").fetchone()[0])
                export_csv(main, directory)
            with closing(sqlite3.connect(control_path)) as control:
                summary = compare(control, main_path, directory)
            with (directory / "union.csv").open(encoding="utf-8-sig", newline="") as stream:
                union = {int(row["ID"]): row for row in csv.DictReader(stream)}
            self.assertEqual(("7800000000", "карточка"),
                             (union[3]["ИНН"], union[3]["Источник ИНН"]))
            with (directory / "active_inn_name.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(2, len(list(csv.DictReader(stream))))
            self.assertEqual(2, summary["union_with_inn"])


if __name__ == "__main__":
    unittest.main()
