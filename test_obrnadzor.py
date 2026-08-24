import io
import unittest

from obrnadzor import Progress, format_duration, parse_activities, parse_detail, parse_list


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


if __name__ == "__main__":
    unittest.main()
