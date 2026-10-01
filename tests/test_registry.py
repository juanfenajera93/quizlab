"""The question type registry is complete and the only place type
behavior lives: qtypes.py (server), static/js/qtypes.js (browser) and
question_spec.py (CSV template / AI prompt / importer) define the same
types, and nothing else branches on a type name.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import json
import re
import shutil
import subprocess
import unittest

from _support import ROOT

import qtypes
import question_spec

TYPE_BRANCH = re.compile(
    r"""(==|===|!=|!==|\bin\s*\()\s*\(?\s*['"](mc|tf|ms|poll|order|wordcloud|"""
    + "|".join(map(re.escape, question_spec.QUESTION_TYPES)) + r""")['"]""")

# Files that must ask the registry instead of comparing type names
NO_BRANCHING = ["game_manager.py", "main.py", "static/js/player.js",
                "static/js/host.js", "static/js/assignment.js",
                "templates/admin_quiz_editor.html"]


class Registry(unittest.TestCase):

    def test_python_registry_matches_spec(self):
        self.assertEqual(list(qtypes.KINDS), list(question_spec.QUESTION_TYPES))
        for code, kind in qtypes.KINDS.items():
            self.assertIs(kind.spec, question_spec.QUESTION_TYPES[code])

    def test_every_example_scores_as_correct(self):
        """The CSV example of each type, imported, is answered right by its
        example_answer according to the registry."""
        from scoring import Outcome
        for code, spec in question_spec.QUESTION_TYPES.items():
            q = question_spec._parse_row(dict(spec.example), lambda *a: None)
            kind = qtypes.get_kind(code)
            key = kind.homework_key(q)
            d = kind.score(key, spec.example_answer, Outcome(1.0, 20, 100, "accuracy"))
            self.assertTrue(d["correct"], code)

    def test_no_type_branching_outside_the_registries(self):
        for rel in NO_BRANCHING:
            text = (ROOT / rel).read_text(encoding="utf-8")
            hits = [m.group(0) for m in TYPE_BRANCH.finditer(text)]
            self.assertEqual(hits, [], rel)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_registry_defines_every_type(self):
        script = (
            "global.window = global; global.document = {};"
            "window.QL_KIND_META = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            f"require({json.dumps(str(ROOT / 'static/js/qtypes.js'))});"
            "const out = {};"
            "for (const [code, t] of Object.entries(QLTypes.all)) out[code] = {"
            "  build: typeof t.player.build, reveal: typeof t.host.reveal,"
            "  hw: typeof t.homework.render, short: t.editor.short,"
            "  scored: t.meta.scored};"
            "console.log(JSON.stringify(out));"
        )
        out = json.loads(subprocess.run(
            ["node", "-e", script], input=json.dumps(qtypes.kind_meta()),
            capture_output=True, text=True, check=True).stdout)
        self.assertEqual(list(out), list(qtypes.KINDS))
        for code, hooks in out.items():
            self.assertEqual(hooks["build"], "function", code)
            self.assertEqual(hooks["reveal"], "function", code)
            self.assertEqual(hooks["hw"], "function", code)
            self.assertTrue(hooks["short"], code)
            self.assertEqual(hooks["scored"], qtypes.KINDS[code].scored, code)


if __name__ == "__main__":
    unittest.main()
