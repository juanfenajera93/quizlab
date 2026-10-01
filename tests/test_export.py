"""Export to CSV: a saved quiz in the template format that imports back to
the same questions (question_spec.build_quiz_csv is parse_csv's inverse).
The HTTP endpoint is covered in test_csv_roundtrip.EndToEnd.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import csv
import io
import json
import unittest

import _support  # noqa: F401
import question_spec as spec


def _semantic(q: dict) -> dict:
    """A question with correct_json parsed, so "[0,1]" == "[0, 1]"."""
    out = {k: q.get(k) for k in ("text", "question_type", "options",
                                 "time_limit", "points")}
    out["image_url"] = q.get("image_url") or None
    try:
        out["correct"] = json.loads(q.get("correct_json") or "null")
    except ValueError:
        out["correct"] = q.get("correct_json")
    if q.get("question_type") == "ms":          # a set of correct options
        out["correct"] = sorted(out["correct"])
    return out


def roundtrip(questions):
    raw = spec.build_quiz_csv(questions)
    result = spec.parse_csv(raw)
    return raw, result


# Questions as the quiz editor saves them (compact JSON, its own key order)
EDITOR_QUIZ = [
    {"text": "=SUMA(A1) no es una fórmula; \"comillas\", y punto y coma;",
     "question_type": "mc", "options": ["-1", "+2", "@tres", "Opción, con coma"],
     "correct_json": "3", "time_limit": 25, "points": 150, "image_url": None},
    {"text": "Línea 1\nLínea 2", "question_type": "tf",
     "options": ["Verdadero", "Falso"], "correct_json": "0", "time_limit": 10,
     "points": 0, "image_url": "https://example.org/a.png"},
    {"text": "Elige", "question_type": "ms", "options": list("abcdef"),
     "correct_json": "[5,0,2]", "time_limit": 120, "points": 1000, "image_url": None},
    {"text": "Encuesta", "question_type": "poll", "options": ["sí", "no"],
     "correct_json": "", "time_limit": 5, "points": 30, "image_url": None},
    {"text": "Ordena", "question_type": "order", "options": ["1º", "2º", "3º"],
     "correct_json": "[0,1,2]", "time_limit": 45, "points": 300, "image_url": None},
    {"text": "Una palabra", "question_type": "wordcloud", "options": [],
     "correct_json": "", "time_limit": 30, "points": 0, "image_url": None},
    {"text": "Corta", "question_type": "short", "options": [],
     "correct_json": '["Tamaño","tamaño de muestra"]', "time_limit": 20,
     "points": 200, "image_url": None},
    {"text": "Pin", "question_type": "pin", "options": [],
     "correct_json": '{"zones":[{"x":0.2996,"y":0.7993,"r":0.06},'
                     '{"x":0.675,"y":0.425,"r":0.1}],"falloff":1.5,"aspect":0.625}',
     "time_limit": 30, "points": 100, "image_url": "https://example.org/map.png"},
    {"text": "Abierta", "question_type": "open", "options": [], "correct_json": "",
     "time_limit": 60, "points": 0, "image_url": None},
    {"text": "Ideas", "question_type": "brainstorm", "options": [],
     "correct_json": '{"ideas":5}', "time_limit": 90, "points": 0, "image_url": None},
    {"text": "Escala", "question_type": "scale", "options": ["Bajo", "Medio", "Alto"],
     "correct_json": '{"max":10}', "time_limit": 20, "points": 0, "image_url": None},
]


class ExportRoundTrip(unittest.TestCase):
    maxDiff = None

    def test_every_type_reimports_identically(self):
        self.assertEqual({q["question_type"] for q in EDITOR_QUIZ},
                         set(spec.QUESTION_TYPES))
        raw, result = roundtrip(EDITOR_QUIZ)
        self.assertEqual(result.issues, [])
        self.assertEqual([_semantic(q) for q in result.questions],
                         [_semantic(q) for q in EDITOR_QUIZ])

    def test_export_of_an_export_is_byte_identical(self):
        raw, result = roundtrip(EDITOR_QUIZ)
        self.assertEqual(spec.build_quiz_csv(result.questions), raw)

    def test_template_format(self):
        raw, _ = roundtrip(EDITOR_QUIZ)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))           # UTF-8 BOM for Excel
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0], spec.COLUMN_NAMES)
        self.assertEqual(rows[0], spec.build_template_csv().decode("utf-8-sig")
                         .splitlines()[0].split(";"))
        mc = dict(zip(rows[0], rows[1]))
        self.assertEqual(mc["correct"], "D")
        # Excel would run these as formulas; a leading space keeps them text
        self.assertEqual(mc["question"][:2], " =")
        self.assertEqual([mc["option_1"], mc["option_2"], mc["option_3"]],
                         [" -1", " +2", " @tres"])
        cells = {r[1]: dict(zip(rows[0], r)) for r in rows[1:]}
        self.assertEqual(cells["ms"]["correct"], "A,C,F")
        self.assertEqual(cells["short"]["correct"], "Tamaño | tamaño de muestra")
        self.assertEqual(cells["pin"]["correct"],
                         "29.96 79.93 6 | 67.5 42.5 10 | falloff=1.5 | aspect=0.625")
        self.assertEqual(cells["brainstorm"]["correct"], "ideas=5")
        self.assertEqual(cells["scale"]["correct"], "max=10")
        self.assertEqual(cells["order"]["correct"], "")

    def test_template_examples_survive_export(self):
        original = spec.parse_csv(spec.build_template_csv()).questions
        _, again = roundtrip(original)
        self.assertEqual(again.questions, original)

    def test_answers_with_the_separator_are_rejected(self):
        self.assertIn("|", spec.answers_error(["a|b"]))


if __name__ == "__main__":
    unittest.main()
