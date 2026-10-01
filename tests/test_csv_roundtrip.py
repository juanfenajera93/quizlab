"""CSV template / AI prompt / importer tests.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v

The end-to-end test starts the real app under uvicorn against a throwaway
SQLite file, so quizlab.db is never touched.
"""

import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# game_manager imports database, which builds an engine from DATABASE_URL at
# import time; point it somewhere harmless before importing.
_TMP = tempfile.mkdtemp(prefix="quizlab_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'unit.db').as_posix()}"

import question_spec as spec  # noqa: E402
from game_manager import _score_answer  # noqa: E402


def _rows(text, delimiter=spec.TEMPLATE_DELIMITER):
    header = delimiter.join(spec.COLUMN_NAMES)
    return (header + "\n" + text).encode("utf-8")


class TemplateRoundTrip(unittest.TestCase):

    def setUp(self):
        self.result = spec.parse_csv(spec.build_template_csv())

    def test_template_is_utf8_bom_and_semicolon(self):
        raw = spec.build_template_csv()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.result.delimiter, ";")
        self.assertIn("¿Qué medida", raw.decode("utf-8-sig"))

    def test_imports_unchanged_with_no_issues(self):
        self.assertEqual(self.result.issues, [])
        self.assertEqual([q["question_type"] for q in self.result.questions],
                         list(spec.QUESTION_TYPES))

    def test_every_example_matches_its_definition(self):
        for q, t in zip(self.result.questions, spec.QUESTION_TYPES.values()):
            with self.subTest(type=t.code):
                ex = t.example
                self.assertEqual(q["text"], ex["question"])
                self.assertEqual(q["options"],
                                 [ex[c] for c in spec.OPTION_COLUMNS if ex.get(c)])
                self.assertEqual(q["time_limit"], int(ex["time_limit"]))
                self.assertEqual(q["points"], int(ex["points"]))
                self.assertEqual(q["image_url"], ex.get("image_url"))
                self.assertTrue(t.min_options <= len(q["options"]) <= t.max_options)

    def test_correct_encoding_per_type(self):
        got = {q["question_type"]: q["correct_json"] for q in self.result.questions}
        self.assertEqual(got, {
            "mc": "1", "tf": "1", "ms": "[0, 1, 3]", "poll": "",
            "order": "[0, 1, 2, 3, 4]", "wordcloud": "",
        })

    def test_game_engine_scores_each_example_answer_as_correct(self):
        # Catches a new type added to the spec without game support.
        for q, t in zip(self.result.questions, spec.QUESTION_TYPES.values()):
            with self.subTest(type=t.code):
                pts, ok = _score_answer(q["question_type"], q["correct_json"],
                                        t.example_answer, 1.0, q["time_limit"],
                                        q["points"], "accuracy")
                self.assertTrue(ok)
                self.assertEqual(pts, q["points"])

    def test_comma_separated_and_cp1252_files_import_the_same(self):
        comma = spec._write_rows(
            (t.example for t in spec.QUESTION_TYPES.values()), delimiter=",")
        for raw in (comma.encode("utf-8"), comma.encode("cp1252"),
                    spec.build_template_csv().decode("utf-8-sig").encode("cp1252")):
            r = spec.parse_csv(raw)
            self.assertEqual(r.issues, [])
            self.assertEqual(r.questions, self.result.questions)
        self.assertEqual(spec.parse_csv(comma.encode("utf-8")).delimiter, ",")

    def test_ai_prompt_covers_every_column_and_type(self):
        prompt = spec.build_ai_prompt()
        for name in spec.COLUMN_NAMES:
            self.assertIn(f"`{name}`", prompt)
        for t in spec.QUESTION_TYPES.values():
            self.assertIn(f"### `{t.code}`", prompt)
            self.assertIn(t.self_check, prompt)
            self.assertIn(t.example["question"], prompt)
        self.assertIn("0 to 1000", prompt)
        self.assertIn("Self-check", prompt)


class ImportValidation(unittest.TestCase):

    def issue(self, text, delimiter=";"):
        r = spec.parse_csv(_rows(text, delimiter))
        return r, [(i.level, i.row, i.column) for i in r.issues]

    def test_errors_report_row_and_column(self):
        r, issues = self.issue(
            "Ok;mc;a;b;;;;;A;20;100;\n"              # row 2 fine
            "Bad letter;mc;a;b;;;;;C;20;100;\n"      # row 3: C points to empty
            ";mc;a;b;;;;;A;20;100;\n"                # row 4: no text
            "Gap;mc;a;;c;;;;A;20;100;\n"             # row 5: option_2 empty
            "Type;quiz;a;b;;;;;A;20;100;\n"          # row 6: unknown type
            "Time;mc;a;b;;;;;A;veinte;100;\n"        # row 7: not a number
            "Order;order;a;b;c;;;;B,A,C;20;100;\n"   # row 8: order needs blank
            "Many;mc;a;b;;;;;A,B;20;100;\n"          # row 9: mc takes one letter
            "Img;mc;a;b;;;;;A;20;100;foto.png\n"     # row 10: not http(s)
            "Few;ms;a;;;;;;A;20;100;\n"              # row 11: ms needs 2+ options
        )
        self.assertEqual(len(r.questions), 1)
        self.assertEqual([i for i in issues if i[0] == "error"], [
            ("error", 3, "correct"), ("error", 4, "question"),
            ("error", 5, "option_2"), ("error", 6, "type"),
            ("error", 7, "time_limit"), ("error", 8, "correct"),
            ("error", 9, "correct"), ("error", 10, "image_url"),
            ("error", 11, "option_1"),
        ])
        bad_letter = r.errors[0]
        self.assertEqual(bad_letter.value, "C")
        self.assertIn("option_3", bad_letter.message)

    def test_warnings_keep_the_row(self):
        r, issues = self.issue(
            "Pts;mc;a;b;;;;;b;20;5000;\n"
            "WC;wordcloud;x;;;;;;A;30;200;\n"
            "Poll;poll;a;b;;;;;A;20;;\n"
            "Img;mc;a;b;;;;;A;20;;https://drive.google.com/file/d/123/view\n")
        self.assertEqual(len(r.questions), 4)
        self.assertEqual(r.errors, [])
        self.assertEqual(r.questions[0]["points"], 1000)
        self.assertEqual(r.questions[0]["correct_json"], "1")   # lowercase ok
        self.assertEqual(r.questions[1]["options"], [])
        self.assertEqual(r.questions[1]["points"], 0)
        self.assertEqual(r.questions[2]["correct_json"], "")
        self.assertEqual(r.questions[2]["points"], 0)            # poll default
        self.assertIn(("warning", 2, "points"), issues)
        self.assertIn(("warning", 3, "option_1"), issues)
        self.assertIn(("warning", 3, "correct"), issues)
        self.assertIn(("warning", 3, "points"), issues)
        self.assertIn(("warning", 5, "image_url"), issues)

    def test_defaults_tf_options_and_blank_rows(self):
        r, issues = self.issue("Statement;tf;;;;;;;A;;;\n;;;;;;;;;;;\n")
        self.assertEqual(issues, [])
        q = r.questions[0]
        self.assertEqual(q["options"], ["Verdadero", "Falso"])
        self.assertEqual(q["time_limit"], spec.QUESTION_TYPES["tf"].default_time)

    def test_ms_accepts_common_separators(self):
        for cell in ('"A,C"', "A;C", "A|C", "a c"):
            delim = "," if cell == '"A,C"' else ";"
            line = delim.join(["Q", "ms", "a", "b", "c", "", "", "",
                               cell, "20", "100", ""])
            if cell == "A;C":
                line = line.replace("A;C", '"A;C"')
            r = spec.parse_csv(_rows(line + "\n", delim))
            self.assertEqual(r.errors, [], cell)
            self.assertEqual(r.questions[0]["correct_json"], "[0, 2]", cell)

    def test_missing_question_column_is_a_file_error(self):
        r = spec.parse_csv(b"pregunta;tipo\nx;mc\n")
        self.assertEqual(r.questions, [])
        self.assertEqual(r.errors[0].row, 1)
        self.assertEqual(r.errors[0].column, "question")

    def test_legacy_option_a_format_still_imports(self):
        r = spec.parse_csv(
            b"question,option_a,option_b,option_c,option_d,correct,time_limit,points,image_url\n"
            b"What is 2+2?,3,4,5,6,B,20,100,\n")
        self.assertEqual(r.errors, [])
        self.assertEqual(r.questions[0]["options"], ["3", "4", "5", "6"])
        self.assertEqual(r.questions[0]["correct_json"], "1")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _multipart(field, filename, content):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode() \
        + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class EndToEnd(unittest.TestCase):
    """Download the template over HTTP, import it unchanged, save the quiz,
    and check what landed in the database."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = Path(_TMP, "e2e.db")
        cls.port = _free_port()
        env = dict(os.environ,
                   DATABASE_URL=f"sqlite:///{cls.db_path.as_posix()}",
                   ADMIN_PASSWORD="test-pass", SUPABASE_URL="",
                   SUPABASE_SERVICE_KEY="", PYTHONDONTWRITEBYTECODE="1")
        # stderr to a file, not a pipe nobody reads: once the app's log
        # output filled the pipe buffer the server blocked and tests hung.
        cls.log_path = Path(_TMP, "e2e_server.log")
        cls.log = open(cls.log_path, "wb")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--port", str(cls.port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=cls.log)
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/admin/login", timeout=1)
                break
            except OSError:
                if cls.server.poll() is not None:
                    cls.log.close()
                    raise RuntimeError(cls.log_path.read_text(errors="replace"))
                time.sleep(0.3)
        else:
            raise RuntimeError("server did not start")
        cls.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        cls.opener.open(cls.base + "/admin/login",
                        urllib.parse.urlencode({"password": "test-pass"}).encode())

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        cls.log.close()

    def test_template_download_import_save(self):
        resp = self.opener.open(self.base + "/admin/csv-template")
        self.assertIn("text/csv", resp.headers["Content-Type"])
        template = resp.read()
        self.assertEqual(template, spec.build_template_csv())

        body, ctype = _multipart("file", "quizlab_template.csv", template)
        req = urllib.request.Request(self.base + "/admin/import-csv", data=body,
                                     headers={"Content-Type": ctype})
        data = json.loads(self.opener.open(req).read())
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["warnings"], [])
        self.assertEqual(data["delimiter"], ";")
        self.assertEqual(len(data["questions"]), len(spec.QUESTION_TYPES))

        # Same payload the editor sends on "Save Quiz".
        save = urllib.request.Request(
            self.base + "/admin/quiz/save",
            data=json.dumps({"name": "Round trip", "questions": data["questions"]}).encode(),
            headers={"Content-Type": "application/json"})
        quiz_id = json.loads(self.opener.open(save).read())["quiz_id"]

        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT text, question_type, options_json, correct_json, time_limit, "
                "points, image_url FROM question WHERE quiz_id = ? ORDER BY position",
                (quiz_id,)).fetchall()
        self.assertEqual(len(rows), len(spec.QUESTION_TYPES))
        expected = spec.parse_csv(spec.build_template_csv()).questions
        for row, exp in zip(rows, expected):
            with self.subTest(type=exp["question_type"]):
                self.assertEqual(row, (exp["text"], exp["question_type"],
                                       json.dumps(exp["options"]),
                                       exp["correct_json"], exp["time_limit"],
                                       exp["points"], exp["image_url"]))

    def _save(self, payload):
        req = urllib.request.Request(
            self.base + "/admin/quiz/save", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(self.opener.open(req).read())

    def test_save_enforces_points_range(self):
        q = {"text": "Q", "question_type": "mc", "options": ["a", "b"],
             "correct_json": "0", "time_limit": 20, "points": spec.POINTS_MAX}
        quiz_id = self._save({"name": "Points", "questions": [q]})["quiz_id"]

        for bad in (spec.POINTS_MAX + 1, spec.POINTS_MIN - 1, 2.5, "abc"):
            with self.subTest(points=bad):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self._save({"name": "Points", "quiz_id": quiz_id,
                                "questions": [dict(q, points=bad)]})
                self.assertEqual(cm.exception.code, 400)
                self.assertIn(str(spec.POINTS_MAX),
                              json.loads(cm.exception.read())["error"])

        # A rejected update must leave the saved quiz untouched
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT points FROM question WHERE quiz_id = ?",
                              (quiz_id,)).fetchall()
        self.assertEqual(rows, [(spec.POINTS_MAX,)])

        # Wordcloud is always worth 0, whatever the client sends
        wc = {"text": "W", "question_type": "wordcloud", "options": [],
              "correct_json": "", "time_limit": 30, "points": 100}
        quiz_id = self._save({"name": "WC", "questions": [wc]})["quiz_id"]
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT points FROM question WHERE quiz_id = ?",
                              (quiz_id,)).fetchall()
        self.assertEqual(rows, [(0,)])

    def test_editor_uses_spec_point_limits(self):
        html = self.opener.open(self.base + "/admin/quiz/new").read().decode()
        self.assertIn(f'min="{spec.POINTS_MIN}" max="{spec.POINTS_MAX}"', html)
        self.assertIn(f"POINTS_MIN = {spec.POINTS_MIN}, POINTS_MAX = {spec.POINTS_MAX}",
                      html)

    def test_ai_prompt_download(self):
        resp = self.opener.open(self.base + "/admin/ai-prompt")
        self.assertIn("attachment; filename=quizlab_ai_prompt.md",
                      resp.headers["Content-Disposition"])
        self.assertEqual(resp.read().decode("utf-8"), spec.build_ai_prompt())

    def test_downloads_require_admin(self):
        for path in ("/admin/csv-template", "/admin/ai-prompt"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
