import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from obrnadzor import Curl, Progress, Request, format_duration, parse_activities, parse_detail, parse_list


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


if __name__ == "__main__":
    unittest.main()
