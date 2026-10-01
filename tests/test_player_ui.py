"""Static guards for the player's end-of-game screen layout.

The "Juego terminado" overlay must scroll as one page, and the Top jugadores
box must keep its natural height. When the overlay was overflow:hidden and
that box was its only flexible child (flex:1; min-height:0), appending the
review list squeezed the box to 0px (heading with no rows) and the review
was clipped with no way to scroll to it. Checked visually in headless Edge
at 375x667 and 1366x768; these tests stop the rules from regressing.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "player.css").read_text(encoding="utf-8")


def rule(selector: str) -> dict:
    """Declarations of the first top-level rule whose selector is exactly
    `selector`, as {property: value}."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if m.group(1).strip() == selector:
            decls = {}
            for part in m.group(2).split(";"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    decls[k.strip()] = v.strip()
            return decls
    raise AssertionError(f"no rule for {selector!r} in player.css")


class FinalScreenLayout(unittest.TestCase):
    def test_overlay_scrolls(self):
        r = rule(".player-final")
        self.assertEqual(r.get("overflow-y"), "auto")
        self.assertNotEqual(r.get("overflow"), "hidden")

    def test_children_never_shrink(self):
        self.assertEqual(rule(".player-final > *").get("flex-shrink"), "0")

    def test_top_players_not_squeezable(self):
        r = rule(".final-mini-lb")
        self.assertNotIn("flex", r)
        self.assertNotEqual(r.get("min-height"), "0")


if __name__ == "__main__":
    unittest.main()
