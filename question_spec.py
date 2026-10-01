"""Single source of truth for QuizLab's question CSV format.

Everything about the bulk-import format lives here:

  * COLUMNS          — every CSV column, its meaning and default
  * QUESTION_TYPES   — every question type: option limits, how `correct` is
                       encoded, scoring, time guidance and a worked example
  * numeric limits   — MAX_OPTIONS, POINTS_MIN/MAX, TIME_MIN/MAX
  * TEMPLATE_DELIMITER / ACCEPTED_DELIMITERS

Four outputs are generated from those definitions, so they cannot drift
apart:

  * build_template_csv() — the downloadable CSV template (/admin/csv-template)
  * build_ai_prompt()    — the downloadable AI prompt (/admin/ai-prompt)
  * parse_csv()          — the importer + validation (/admin/import-csv)
  * build_quiz_csv()     — an existing quiz in the template format
                           (/admin/quiz/{id}/export.csv); parse_csv() reads
                           it back to the same questions

Adding a question type:
  1. a QuestionType entry here (with an example row and example_answer):
     the template, the AI prompt and the importer pick it up;
  2. a class in qtypes.py (scoring, reveal, review, homework, history);
  3. an entry in static/js/qtypes.js (phone, projector, homework, editor).
tests/test_registry.py fails if the three disagree or if the example is
not scored as correct.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Optional

MAX_OPTIONS = 6
LETTERS = "ABCDEF"[:MAX_OPTIONS]
POINTS_MIN, POINTS_MAX = 0, 1000
TIME_MIN, TIME_MAX = 5, 120

# The template uses ";" because Excel with Spanish (and most European)
# regional settings uses ";" as its list separator: a ";" file opens straight
# into columns on double-click. The importer accepts either.
TEMPLATE_DELIMITER = ";"
ACCEPTED_DELIMITERS = (",", ";")

# Separators accepted between letters in a multi-letter `correct` value.
_MULTI_SPLIT = re.compile(r"[\s,;|/]+")


# ─── Columns ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Column:
    name: str
    description: str
    required: bool = False
    aliases: tuple = ()          # legacy header names accepted on import


OPTION_COLUMNS = [f"option_{i}" for i in range(1, MAX_OPTIONS + 1)]
_LEGACY_OPTION_ALIASES = {f"option_{i}": (f"option_{c}",)
                          for i, c in zip(range(1, 5), "abcd")}

COLUMNS = [
    Column("question", "The question text shown to students.",
           required=True),
    Column("type", "Question type code (see the question types below). "
                   "Blank means mc."),
    *[Column(name,
             f"Answer option {LETTERS[i]}. "
             + ("Fill options from option_1 onward with no gaps; "
                "leave unused option columns blank." if i == 0 else
                "Blank if unused."),
             aliases=_LEGACY_OPTION_ALIASES.get(name, ()))
      for i, name in enumerate(OPTION_COLUMNS)],
    Column("correct", "The correct answer, encoded per question type "
                      "(letters A-F refer to option_1..option_6)."),
    Column("time_limit", f"Seconds students get to answer: whole number "
                         f"{TIME_MIN}-{TIME_MAX}. Blank uses the type's "
                         f"default."),
    Column("points", f"Maximum points for a fully correct answer: whole "
                     f"number {POINTS_MIN}-{POINTS_MAX}. 0 = not scored. "
                     f"Blank uses the type's default."),
    Column("image_url", "Optional. A direct, public http(s) link to an image "
                        "file (the URL itself must return the image, not a "
                        "web page that contains it). Blank for no image."),
]
COLUMN_NAMES = [c.name for c in COLUMNS]


# ─── Question types ──────────────────────────────────────────────────────────

# How the `correct` column is read for a type.
CORRECT_SINGLE = "single"    # exactly one letter
CORRECT_MULTI = "multi"      # one or more letters
CORRECT_ORDER = "order"      # blank: the options are already in correct order
CORRECT_NONE = "none"        # blank: nothing is correct (poll, wordcloud)
CORRECT_ANSWERS = "answers"  # accepted texts separated by | (short)
CORRECT_ZONES = "zones"      # circles on the image, in percent (pin)
CORRECT_SETTINGS = "settings"  # no right answer; key=value settings (brainstorm, scale)

SHORT_MAX_LEN = 20           # characters a student can type for `short`
PIN_DEFAULT_FALLOFF = 1.0    # zone radii outside a zone that still score
OPEN_MAX_LEN = 250           # characters for an open-ended answer
IDEA_MAX_LEN = 80            # characters per brainstorm idea


@dataclass(frozen=True)
class Setting:
    """A key=value setting a type reads from the `correct` cell."""
    name: str
    choices: tuple               # allowed whole numbers
    default: int
    description: str


@dataclass(frozen=True)
class QuestionType:
    code: str
    label: str
    summary: str
    min_options: int
    max_options: int
    correct_kind: str
    correct_rule: str            # human explanation of the `correct` encoding
    self_check: str              # line for the AI prompt's self-check list
    scoring: str                 # human explanation of how points are earned
    default_time: int
    time_guidance: str
    default_points: int
    example: dict                # column name -> cell value (template row)
    default_options: tuple = ()  # used when all option columns are blank
    fixed_points: Optional[int] = None   # type ignores the points column
    example_answer: object = None        # a player answer the example scores
                                         # as correct (verified by tests)
    requires_image: bool = False         # image_url is mandatory
    settings: tuple = ()                 # Setting entries (CORRECT_SETTINGS)


QUESTION_TYPES = {t.code: t for t in [
    QuestionType(
        code="mc", label="Multiple choice",
        summary="One correct answer among 2-6 options.",
        min_options=2, max_options=MAX_OPTIONS,
        correct_kind=CORRECT_SINGLE,
        correct_rule="Exactly one letter: the correct option (e.g. B).",
        self_check="`mc`: `correct` is exactly one letter pointing to a filled option, and that option is the only correct one.",
        scoring="Full points for the correct option (faster answers earn "
                "more in speed mode), 0 otherwise.",
        default_time=20, time_guidance="15-30 s",
        default_points=100,
        example={
            "question": "¿Qué medida de tendencia central es más robusta "
                        "ante valores atípicos?",
            "type": "mc",
            "option_1": "Media", "option_2": "Mediana",
            "option_3": "Moda", "option_4": "Rango",
            "correct": "B", "time_limit": "20", "points": "200",
        },
        example_answer=1,
    ),
    QuestionType(
        code="tf", label="True / False",
        summary="A statement that is either true or false.",
        min_options=2, max_options=2,
        correct_kind=CORRECT_SINGLE,
        correct_rule="A if option_1 (true) is correct, B if option_2 (false) "
                     "is correct.",
        self_check="`tf`: exactly two options (true first, false second) and `correct` is A or B.",
        scoring="Same as mc.",
        default_time=15, time_guidance="10-20 s",
        default_points=100,
        default_options=("Verdadero", "Falso"),
        example={
            "question": "Una correlación de 0,9 entre dos variables demuestra "
                        "que una causa la otra.",
            "type": "tf",
            "option_1": "Verdadero", "option_2": "Falso",
            "correct": "B", "time_limit": "15", "points": "100",
        },
        example_answer=1,
    ),
    QuestionType(
        code="ms", label="Multiple select",
        summary="Two or more options can be correct; students tick all that "
                "apply.",
        min_options=2, max_options=MAX_OPTIONS,
        correct_kind=CORRECT_MULTI,
        correct_rule="Every correct letter, separated by commas (e.g. A,C,D). "
                     "In a ;-separated file write it as is; in a ,-separated "
                     "file wrap the cell in double quotes (\"A,C,D\").",
        self_check="`ms`: `correct` lists every correct letter, comma-separated, each pointing to a filled option; the other options are clearly wrong.",
        scoring="Full points only if exactly the correct set is chosen; "
                "partial credit for a subset; any wrong option gives 0.",
        default_time=30, time_guidance="30-45 s",
        default_points=100,
        example={
            "question": "¿Cuáles de estos gráficos sirven para mostrar la "
                        "distribución de una variable numérica?",
            "type": "ms",
            "option_1": "Histograma", "option_2": "Diagrama de caja",
            "option_3": "Gráfico de pastel", "option_4": "Gráfico de densidad",
            "correct": "A,B,D", "time_limit": "30", "points": "300",
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/1/1a/"
                         "Boxplot_vs_PDF.svg",
        },
        example_answer=[0, 1, 3],
    ),
    QuestionType(
        code="poll", label="Poll",
        summary="Opinion question with no right answer; results are shown "
                "as a bar chart.",
        min_options=2, max_options=MAX_OPTIONS,
        correct_kind=CORRECT_NONE,
        correct_rule="Leave blank (there is no correct answer).",
        self_check="`poll`: `correct` is blank and no option is \"the right one\".",
        scoring="Every student who answers gets the points value (use 0 for "
                "an unscored poll).",
        default_time=20, time_guidance="15-30 s",
        default_points=0,
        example={
            "question": "¿Qué herramienta usas con más frecuencia para "
                        "analizar datos?",
            "type": "poll",
            "option_1": "Excel", "option_2": "Python", "option_3": "R",
            "option_4": "Power BI o Tableau",
            "time_limit": "20", "points": "0",
        },
        example_answer=2,
    ),
    QuestionType(
        code="order", label="Ordering",
        summary="Students put 2-6 items in the right sequence. The game "
                "shuffles the items for each student.",
        min_options=2, max_options=MAX_OPTIONS,
        correct_kind=CORRECT_ORDER,
        correct_rule="Leave blank. Write the options in the CORRECT order "
                     "(option_1 = first, option_2 = second, ...); QuizLab "
                     "shuffles them when the question is played.",
        self_check="`order`: options are written in the correct order and `correct` is blank.",
        scoring="Full points for the exact sequence; partial credit for each "
                "item in the right position.",
        default_time=45, time_guidance="30-60 s",
        default_points=100,
        example={
            "question": "Ordena las etapas del análisis de datos, de la "
                        "primera a la última.",
            "type": "order",
            "option_1": "Definir la pregunta",
            "option_2": "Recolectar los datos",
            "option_3": "Limpiar y preparar los datos",
            "option_4": "Analizar y visualizar",
            "option_5": "Comunicar los resultados",
            "time_limit": "45", "points": "500",
        },
        example_answer=[0, 1, 2, 3, 4],
    ),
    QuestionType(
        code="wordcloud", label="Word cloud",
        summary="Students type a short free-text answer (max 50 characters); "
                "answers are shown as a word cloud.",
        min_options=0, max_options=0,
        correct_kind=CORRECT_NONE,
        correct_rule="Leave blank. Leave all option columns blank too.",
        self_check="`wordcloud`: no options, `correct` blank, points 0.",
        scoring="Never scored: points are always 0.",
        default_time=30, time_guidance="30-60 s",
        default_points=0, fixed_points=0,
        example={
            "question": "En una palabra: ¿qué es lo más difícil de limpiar "
                        "datos?",
            "type": "wordcloud",
            "time_limit": "30", "points": "0",
        },
        example_answer="duplicados",
    ),
    QuestionType(
        code="short", label="Short answer",
        summary=f"Students type a short answer (max {SHORT_MAX_LEN} "
                "characters); any accepted answer is correct. Case, accents "
                "and extra spaces are ignored.",
        min_options=0, max_options=0,
        correct_kind=CORRECT_ANSWERS,
        correct_rule="Every accepted answer, separated by | (e.g. "
                     "Mediana | la mediana). Case, accents and extra spaces "
                     "do not matter (Tamaño = tamano = \" TAMAÑO \"), so list "
                     "real alternatives only, each at most "
                     f"{SHORT_MAX_LEN} characters. Leave the option columns "
                     "blank.",
        self_check="`short`: option columns blank; `correct` lists every acceptable answer separated by `|`, each 20 characters or fewer.",
        scoring="Full points (faster answers earn more in speed mode) if the "
                "answer matches any accepted answer, 0 otherwise.",
        default_time=30, time_guidance="20-40 s",
        default_points=100,
        example={
            "question": "¿Qué medida de tendencia central divide los datos "
                        "ordenados en dos mitades iguales?",
            "type": "short",
            "correct": "Mediana | la mediana", "time_limit": "30",
            "points": "200",
        },
        example_answer="  LA  MEDIANA ",
    ),
    QuestionType(
        code="pin", label="Pin on image",
        summary="Students tap the image to place one pin. Correct inside a "
                "marked circular zone; outside, fewer points the farther it "
                "lands. Needs an image.",
        min_options=0, max_options=0,
        correct_kind=CORRECT_ZONES,
        correct_rule="One or more circular zones separated by |, each written "
                     "`x y r` in percent: x across the image from the left, y "
                     "down from the top, r (the radius) as a percent of the "
                     "image width (e.g. 50 17.5 5). Optional: falloff=F, how "
                     "far outside a zone a pin still earns points, in zone "
                     f"radii (default {PIN_DEFAULT_FALLOFF:g}), and aspect=A, "
                     "the image height divided by its width (the quiz editor "
                     "measures it when you save). Requires image_url.",
        self_check="`pin`: only when you can see a real image the teacher gave you; `image_url` is set and every `x y r` zone covers the answer in that image.",
        scoring="Full points (speed formula) inside any zone. Outside, the "
                "points fall linearly with the distance to the nearest zone "
                "edge, reaching 0 at the falloff distance (default: one zone "
                "radius).",
        default_time=30, time_guidance="20-40 s",
        default_points=100,
        requires_image=True,
        example={
            "question": "En el diagrama de caja, marca dónde está la mediana.",
            "type": "pin",
            "correct": "50 17.5 5 | falloff=1 | aspect=1.0895",
            "time_limit": "30", "points": "200",
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/1/1a/"
                         "Boxplot_vs_PDF.svg",
        },
        example_answer={"x": 0.51, "y": 0.18},
    ),
    QuestionType(
        code="open", label="Open-ended",
        summary=f"Students write a free answer (max {OPEN_MAX_LEN} "
                "characters); the projector shows them as an anonymous card "
                "wall. No right answer, no points.",
        min_options=0, max_options=0,
        correct_kind=CORRECT_NONE,
        correct_rule="Leave blank. Leave all option columns blank too.",
        self_check="`open`: no options, `correct` blank, points 0.",
        scoring="Never scored: points are always 0.",
        default_time=60, time_guidance="45-90 s",
        default_points=0, fixed_points=0,
        example={
            "question": "¿Qué parte del análisis de datos te gustaría "
                        "practicar más y por qué?",
            "type": "open", "time_limit": "60", "points": "0",
        },
        example_answer="Limpiar datos, porque siempre me tardo mucho.",
    ),
    QuestionType(
        code="brainstorm", label="Brainstorm",
        summary="Each student sends several short ideas; the projector groups "
                "similar ideas (automatically, then adjustable by hand). No "
                "right answer, no points.",
        min_options=0, max_options=0,
        correct_kind=CORRECT_SETTINGS,
        correct_rule="Optional: ideas=N, how many ideas each student can send "
                     "(1-10, blank = 3). Leave the option columns blank.",
        self_check="`brainstorm`: no options, `correct` blank or ideas=N (1-10), points 0.",
        scoring="Never scored: points are always 0.",
        default_time=90, time_guidance="60-120 s",
        default_points=0, fixed_points=0,
        settings=(Setting("ideas", tuple(range(1, 11)), 3,
                          "ideas each student can send"),),
        example={
            "question": "Lluvia de ideas: ¿qué fuentes de datos podríamos usar "
                        "para estudiar el tráfico de la ciudad?",
            "type": "brainstorm", "correct": "ideas=3",
            "time_limit": "90", "points": "0",
        },
        example_answer=["sensores en semáforos"],
    ),
    QuestionType(
        code="scale", label="Scale",
        summary="Students rate from 1 to 5 (or 1 to 10); the projector shows "
                "the distribution and the average. No right answer, no points.",
        min_options=3, max_options=3,
        correct_kind=CORRECT_SETTINGS,
        correct_rule="Optional: max=5 or max=10 (blank = 5). The three options "
                     "are the labels: option_1 at 1, option_2 in the middle, "
                     "option_3 at the top of the scale.",
        self_check="`scale`: exactly three labels (low, middle, high), `correct` blank, max=5 or max=10, points 0.",
        scoring="Never scored: points are always 0.",
        default_time=20, time_guidance="15-30 s",
        default_points=0, fixed_points=0,
        default_options=("Nada seguro", "Más o menos", "Muy seguro"),
        settings=(Setting("max", (5, 10), 5, "top of the scale"),),
        example={
            "question": "¿Qué tan seguro te sientes interpretando un diagrama "
                        "de caja?",
            "type": "scale",
            "option_1": "Nada seguro", "option_2": "Más o menos",
            "option_3": "Muy seguro",
            "correct": "max=5", "time_limit": "20", "points": "0",
        },
        example_answer=4,
    ),
]}


# ─── `correct` cells for answers and zones ──────────────────────────────────
# Shared by the importer, the save endpoint (qtypes validation) and the CSV
# export, so an exported quiz re-imports identically.

def parse_answers_cell(raw: str) -> list:
    return [a.strip() for a in (raw or "").split("|") if a.strip()]


def answers_error(answers) -> Optional[str]:
    if not isinstance(answers, list) or not answers:
        return "needs at least one accepted answer."
    for a in answers:
        if not isinstance(a, str) or not a.strip():
            return "accepted answers cannot be empty."
        if "|" in a:
            return f"'{a.strip()}' contains |, which separates accepted answers."
        if len(a.strip()) > SHORT_MAX_LEN:
            return (f"'{a.strip()}' is longer than {SHORT_MAX_LEN} characters, "
                    f"the most a student can type.")
    return None


def _num(token: str) -> float:
    return float(token.strip().replace(",", "."))   # Excel may write 17,5


def parse_zones_cell(raw: str) -> dict:
    """`x y r | x y r | falloff=F | aspect=A` (percent) -> the stored
    config. Raises ValueError with a readable message."""
    zones, falloff, aspect = [], PIN_DEFAULT_FALLOFF, None
    for token in (t.strip() for t in (raw or "").split("|")):
        if not token:
            continue
        key, eq, val = token.partition("=")
        if eq:
            key = key.strip().lower()
            if key == "falloff":
                falloff = _num(val)
            elif key == "aspect":
                aspect = _num(val)
            else:
                raise ValueError(f"unknown setting '{key}' (use falloff= or aspect=).")
            continue
        parts = token.split()
        if len(parts) != 3:
            raise ValueError(f"zone '{token}' must be three numbers: x y r.")
        x, y, r = (_num(v) for v in parts)
        zones.append({"x": round(x / 100, 4), "y": round(y / 100, 4),
                      "r": round(r / 100, 4)})
    cfg = {"zones": zones, "falloff": falloff, "aspect": aspect}
    err = zones_error(cfg, need_aspect=False)
    if err:
        raise ValueError(err)
    return cfg


def zones_error(cfg, need_aspect=True) -> Optional[str]:
    if not isinstance(cfg, dict) or not cfg.get("zones"):
        return "needs at least one zone (x y r)."
    for z in cfg["zones"]:
        try:
            x, y, r = float(z["x"]), float(z["y"]), float(z["r"])
        except (KeyError, TypeError, ValueError):
            return "every zone needs x, y and r."
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return "zone centers must be inside the image (0-100%)."
        if not 0 < r <= 1:
            return "zone radius must be more than 0% and at most 100% of the width."
    try:
        falloff = float(cfg.get("falloff", PIN_DEFAULT_FALLOFF))
    except (TypeError, ValueError):
        return "falloff must be a number."
    if not 0 <= falloff <= 10:
        return "falloff must be between 0 and 10 zone radii."
    aspect = cfg.get("aspect")
    if aspect is None:
        return "the image proportions are unknown; open the question in the editor with a working image." if need_aspect else None
    try:
        if not 0.05 <= float(aspect) <= 20:
            return "aspect (image height / width) must be between 0.05 and 20."
    except (TypeError, ValueError):
        return "aspect must be a number."
    return None


def parse_settings_cell(qtype: "QuestionType", raw: str) -> dict:
    """`key=N | key=N` -> {key: N} with every setting of the type (defaults
    filled in). Raises ValueError with a readable message."""
    known = {st.name: st for st in qtype.settings}
    out = {st.name: st.default for st in qtype.settings}
    for token in (t.strip() for t in (raw or "").split("|")):
        if not token:
            continue
        key, eq, val = token.partition("=")
        key = key.strip().lower()
        if not eq or key not in known:
            names = ", ".join(f"{n}=" for n in known) or "nothing"
            raise ValueError(f"'{token}' is not a setting of {qtype.code} (use {names}).")
        try:
            num = int(val.strip())
        except ValueError:
            raise ValueError(f"{key} must be a whole number.")
        if num not in known[key].choices:
            raise ValueError(f"{key} must be one of "
                             f"{', '.join(map(str, known[key].choices))}.")
        out[key] = num
    return out


def settings_error(qtype: "QuestionType", settings) -> Optional[str]:
    if not isinstance(settings, dict):
        return "settings are missing."
    for st in qtype.settings:
        if settings.get(st.name, st.default) not in st.choices:
            return (f"{st.name} must be one of "
                    f"{', '.join(map(str, st.choices))}.")
    return None


def encode_settings_cell(settings: dict) -> str:
    return " | ".join(f"{k}={v}" for k, v in settings.items())


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def encode_zones_cell(cfg: dict) -> str:
    parts = [f"{_fmt(z['x'] * 100)} {_fmt(z['y'] * 100)} {_fmt(z['r'] * 100)}"
             for z in cfg.get("zones", [])]
    parts.append(f"falloff={_fmt(float(cfg.get('falloff', PIN_DEFAULT_FALLOFF)))}")
    if cfg.get("aspect"):
        parts.append(f"aspect={float(cfg['aspect']):.4f}".rstrip("0").rstrip("."))
    return " | ".join(parts)
DEFAULT_TYPE = "mc"


# ─── Template ────────────────────────────────────────────────────────────────

def _write_rows(rows, delimiter=TEMPLATE_DELIMITER) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(COLUMN_NAMES)
    for row in rows:
        writer.writerow([row.get(name, "") for name in COLUMN_NAMES])
    return buf.getvalue()


def build_template_csv() -> bytes:
    """Header + one example row per question type, UTF-8 with BOM so Excel
    detects the encoding and shows accents correctly."""
    return _write_rows(t.example for t in QUESTION_TYPES.values()) \
        .encode("utf-8-sig")


# ─── Export ──────────────────────────────────────────────────────────────────
# The inverse of _parse_row: a saved question -> the template row that
# imports back to it.

_FORMULA_START = ("=", "+", "-", "@")


def _safe_cell(text) -> str:
    """Excel runs a cell that starts with = + - @ as a formula. A leading
    space keeps it text; the importer strips it, so the round trip holds."""
    text = "" if text is None else str(text)
    return " " + text if text.startswith(_FORMULA_START) else text


def _correct_cell(qtype: "QuestionType", correct_json: str, n_opts: int) -> str:
    kind = qtype.correct_kind
    if kind == CORRECT_SINGLE:
        try:
            idx = int(correct_json) if correct_json != "" else 0
        except (TypeError, ValueError):
            idx = 0
        return LETTERS[idx] if 0 <= idx < len(LETTERS) else ""
    if kind == CORRECT_MULTI:
        try:
            idx = json.loads(correct_json or "[]")
        except (TypeError, ValueError):
            idx = []
        return ",".join(LETTERS[i] for i in sorted(set(idx))
                        if isinstance(i, int) and 0 <= i < len(LETTERS))
    if kind == CORRECT_ANSWERS:
        try:
            answers = json.loads(correct_json or "[]")
        except (TypeError, ValueError):
            answers = []
        return " | ".join(str(a) for a in answers)
    if kind == CORRECT_ZONES:
        try:
            return encode_zones_cell(json.loads(correct_json or "{}"))
        except (TypeError, ValueError, KeyError):
            return ""
    if kind == CORRECT_SETTINGS:
        try:
            stored = json.loads(correct_json or "{}")
        except (TypeError, ValueError):
            stored = {}
        settings = {st.name: stored.get(st.name, st.default) for st in qtype.settings}
        return encode_settings_cell(settings)
    return ""      # order (the options are in the right order) / none


def question_row(q: dict) -> dict:
    """One saved question ({question_type, text, options, correct_json,
    time_limit, points, image_url}) as a template row."""
    code = q.get("question_type") or DEFAULT_TYPE
    qtype = QUESTION_TYPES.get(code)
    options = [o for o in (q.get("options") or []) if str(o).strip()]
    row = {
        "question": _safe_cell(q.get("text", "")),
        "type": code,
        "time_limit": str(q.get("time_limit", "")),
        "points": str(q.get("points", "")),
        "image_url": q.get("image_url") or "",
    }
    if qtype is None:                     # unknown type: export what we have
        row["correct"] = _safe_cell(q.get("correct_json", ""))
    else:
        if qtype.max_options:
            options = options[:qtype.max_options]
        row["correct"] = _safe_cell(_correct_cell(qtype, q.get("correct_json", ""),
                                                  len(options)))
    for name, opt in zip(OPTION_COLUMNS, options):
        row[name] = _safe_cell(opt)
    return row


def build_quiz_csv(questions) -> bytes:
    """A quiz in the template format (same header, ; separator, UTF-8 with
    BOM), ready to edit in Excel and import again."""
    return _write_rows(question_row(q) for q in questions).encode("utf-8-sig")


# ─── AI prompt ───────────────────────────────────────────────────────────────

def _type_table() -> str:
    lines = ["| type | Name | What it is | Options | `correct` |",
             "|---|---|---|---|---|"]
    for t in QUESTION_TYPES.values():
        if t.max_options == 0:
            opts = "none (leave blank)"
        elif t.min_options == t.max_options:
            opts = f"exactly {t.max_options}"
            if t.default_options:
                opts += f" ({' / '.join(t.default_options)})"
        else:
            opts = f"{t.min_options}-{t.max_options}"
        lines.append(f"| `{t.code}` | {t.label} | {t.summary} | {opts} | "
                     f"{t.correct_rule} |")
    return "\n".join(lines)


def _options_rule(t: QuestionType) -> str:
    if t.max_options == 0:
        return "none; leave every option column blank."
    if t.min_options != t.max_options:
        return (f"{t.min_options}-{t.max_options}, filled from option_1 with "
                f"no gaps.")
    rule = f"exactly {t.max_options}"
    if t.default_options:
        rule += (f": `{t.default_options[0]}` in option_1 and "
                 f"`{t.default_options[1]}` in option_2 (in the material's "
                 f"language)")
    return rule + "."


def _points_rule(t: QuestionType) -> str:
    if t.fixed_points is not None:
        return f"always {t.fixed_points}."
    return f"{POINTS_MIN}-{POINTS_MAX} (default {t.default_points})."


def build_ai_prompt() -> str:
    type_codes = ", ".join(f"`{c}`" for c in QUESTION_TYPES)
    column_lines = "\n".join(
        f"{i}. `{c.name}`{' (required)' if c.required else ''}: "
        f"{c.description}"
        for i, c in enumerate(COLUMNS, start=1))
    type_sections = "\n\n".join(
        f"### `{t.code}`: {t.label}\n\n"
        f"- {t.summary}\n"
        f"- Options: {_options_rule(t)}\n"
        f"- `correct`: {t.correct_rule}\n"
        f"- Scoring: {t.scoring}\n"
        f"- Suggested time_limit: {t.time_guidance} "
        f"(default {t.default_time})\n"
        f"- Points: {_points_rule(t)}"
        for t in QUESTION_TYPES.values())
    examples = _write_rows(t.example for t in QUESTION_TYPES.values())
    type_checks = "\n".join(f"- [ ] {t.self_check}"
                            for t in QUESTION_TYPES.values())
    letters_range = f"{LETTERS[0]}-{LETTERS[-1]}"

    return f"""# QuizLab question generator: instructions for the AI assistant

You are helping a university teacher turn class material into quiz questions
for **QuizLab**, a live classroom quiz app (similar to Kahoot). Your output is
a CSV file that the teacher imports directly, so the format below must be
followed exactly.

## Your task

1. Read the class material the teacher provides (pasted text, slides, PDF,
   notes...).
2. Write questions that check understanding of the **key ideas** in that
   material: concepts, interpretation, applying a method, spotting common
   mistakes. Avoid trivia and questions answerable without the material.
3. Unless the teacher says otherwise: write **10 questions**, mostly `mc`, with
   some `tf`, `ms`, `order` and `short`, and at most one opinion question
   (`poll`, `wordcloud`, `open`, `brainstorm` or `scale`) as a warm-up or
   closing reflection. Use `pin` only when the teacher gives you an image you
   can see. Follow any numbers, types, difficulty or topics the teacher asks
   for instead.
4. Write questions and options in **the same language as the material**
   (usually Spanish). Keep accents and ñ as normal characters.
5. Output the CSV inside a single code block, then a short list of any
   assumptions you made. Do not put anything else inside the code block.

## File format

- One header row, then one row per question.
- The header must be exactly:

```
{TEMPLATE_DELIMITER.join(COLUMN_NAMES)}
```

- Separator: **semicolon (`{TEMPLATE_DELIMITER}`)**. (QuizLab also accepts
  commas, but use semicolons so the file opens correctly in Spanish-language
  Excel.)
- If a cell contains a semicolon, a line break or a double quote, wrap the
  whole cell in double quotes and double any quote inside it (`""`). Otherwise
  do not quote cells.
- Encoding: UTF-8.
- Do not start a cell with `=`, `+`, `-` or `@` (Excel treats it as a formula).
  Rephrase, or put a space or word before it.
- Keep options short: they appear on phones, ideally under 60 characters.

## Columns

Letters {letters_range} always mean option_1-option_{MAX_OPTIONS}: A = option_1,
B = option_2, and so on.

{column_lines}

## Question types

Valid `type` values: {type_codes}.

{_type_table()}

{type_sections}

## Points and time

- `points` is a whole number from **{POINTS_MIN} to {POINTS_MAX}**. Suggested
  scale: 100 for easy recall, 200-300 for understanding, 500-1000 for hard
  application questions. Use 0 for warm-ups that should not affect the
  ranking.
- `time_limit` is a whole number of seconds from **{TIME_MIN} to {TIME_MAX}**.
  Use the suggested range for each type, and add time when the question is
  long, includes a calculation or relies on an image students must read.

## Images (`image_url`)

Only include an image if it clearly helps the question. The value must be a
**direct, public URL to the image file itself** (opening it shows only the
image), starting with `https://`, typically ending in `.png`, `.jpg`,
`.gif`, `.webp` or `.svg`. Do **not** use links to web pages, Google Images
results, Google Drive / OneDrive share pages, or images behind a login. Never
invent a URL. If you are not certain the URL exists and is direct, leave
`image_url` blank and mention in your notes what image the teacher could add.

## Example (one row per type)

```
{examples.rstrip()}
```

## Self-check before you answer

Go through every row and fix anything that fails:

- [ ] The header row is exactly the one above, in the same order, and every
      row has the same number of `{TEMPLATE_DELIMITER}` separators as the
      header ({len(COLUMN_NAMES) - 1}).
- [ ] `type` is one of {type_codes}.
- [ ] Options start at option_1 with no empty columns between them, and the
      count fits the type ({", ".join(
          f"{t.code}: " + (f"{t.min_options}-{t.max_options}"
                           if t.min_options != t.max_options
                           else str(t.max_options))
          for t in QUESTION_TYPES.values())}).
{type_checks}
- [ ] `time_limit` is a whole number {TIME_MIN}-{TIME_MAX}; `points` is a
      whole number {POINTS_MIN}-{POINTS_MAX}.
- [ ] Every `image_url` is blank or a real, direct, public image link.
- [ ] Cells containing `{TEMPLATE_DELIMITER}`, line breaks or `"` are
      wrapped in double quotes.
- [ ] Each answer is supported by the class material, and no question gives
      away the answer to another.
"""


# ─── Import ──────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    level: str                   # "error" (row skipped) | "warning" (imported)
    message: str
    row: Optional[int] = None    # spreadsheet row number (header = row 1)
    column: Optional[str] = None
    value: str = ""

    def as_dict(self):
        return {"level": self.level, "row": self.row, "column": self.column,
                "value": self.value, "message": self.message}


@dataclass
class ImportResult:
    questions: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    delimiter: Optional[str] = None
    encoding: Optional[str] = None

    @property
    def errors(self):
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.level == "warning"]

    def as_dict(self):
        return {
            "questions": self.questions,
            "errors": [i.as_dict() for i in self.errors],
            "warnings": [i.as_dict() for i in self.warnings],
            "delimiter": self.delimiter,
            "encoding": self.encoding,
        }


class _RowError(Exception):
    def __init__(self, column, value, message):
        super().__init__(message)
        self.column, self.value, self.message = column, value, message


def _decode(raw: bytes):
    for enc in ("utf-8-sig", "cp1252"):
        # Excel's plain "CSV" save on Windows writes cp1252, not UTF-8.
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, None


def _detect_delimiter(text: str) -> str:
    header = text.splitlines()[0] if text else ""
    return ";" if header.count(";") > header.count(",") else ","


def _letter_index(letter, column, raw, n_opts):
    idx = LETTERS.find(letter.upper()) if len(letter) == 1 else -1
    if idx < 0:
        raise _RowError(column, raw,
                        f"'{letter}' is not a valid letter; use "
                        f"{LETTERS[0]}-{LETTERS[-1]} (A = option_1).")
    if idx >= n_opts:
        raise _RowError(column, raw,
                        f"'{letter.upper()}' points to option_{idx + 1}, "
                        f"which is empty (this question has {n_opts} "
                        f"options).")
    return idx


def _parse_int(row, column, lo, hi, default, warn):
    raw = (row.get(column) or "").strip()
    if raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise _RowError(column, raw,
                        f"must be a whole number between {lo} and {hi}.")
    if not lo <= val <= hi:
        clamped = max(lo, min(hi, val))
        warn(column, raw, f"out of range {lo}-{hi}; set to {clamped}.")
        return clamped
    return val


_NON_DIRECT_IMAGE_HINTS = (
    "drive.google.com/file", "docs.google.com", "google.com/imgres",
    "google.com/search", "/wiki/file:", "1drv.ms", "onedrive.live.com",
    "sharepoint.com",
)


def _parse_row(row, warn):
    text = (row.get("question") or "").strip()
    if not text:
        raise _RowError("question", "", "question text is empty.")

    raw_type = (row.get("type") or "").strip()
    code = raw_type.lower() or DEFAULT_TYPE
    qtype = QUESTION_TYPES.get(code)
    if qtype is None:
        raise _RowError("type", raw_type,
                        f"unknown type; use one of "
                        f"{', '.join(QUESTION_TYPES)}.")

    # Options: must be filled from option_1 with no gaps.
    cells = [(row.get(c) or "").strip() for c in OPTION_COLUMNS]
    last = max((i for i, v in enumerate(cells) if v), default=-1)
    opts = cells[:last + 1]
    if "" in opts:
        gap = opts.index("")
        raise _RowError(OPTION_COLUMNS[gap], "",
                        f"empty, but option_{last + 1} is filled; fill "
                        f"options from option_1 with no gaps.")

    if qtype.max_options == 0:
        if opts:
            warn("option_1", opts[0],
                 f"{qtype.code} questions have no options; ignored.")
        opts = []
    elif not opts and qtype.default_options:
        opts = list(qtype.default_options)
    elif not qtype.min_options <= len(opts) <= qtype.max_options:
        need = (f"exactly {qtype.max_options}"
                if qtype.min_options == qtype.max_options
                else f"{qtype.min_options}-{qtype.max_options}")
        raise _RowError("option_1" if len(opts) < qtype.min_options
                        else OPTION_COLUMNS[qtype.max_options],
                        "", f"{qtype.code} needs {need} options; "
                            f"found {len(opts)}.")

    # Correct answer, per type.
    raw_correct = (row.get("correct") or "").strip()
    kind = qtype.correct_kind
    if kind == CORRECT_SINGLE:
        if not raw_correct:
            raise _RowError("correct", "", f"{qtype.code} needs one letter. "
                                           f"{qtype.correct_rule}")
        if len(_MULTI_SPLIT.split(raw_correct)) > 1:
            raise _RowError("correct", raw_correct,
                            f"{qtype.code} takes exactly one letter; use ms "
                            f"for several correct options.")
        correct_json = str(_letter_index(raw_correct, "correct", raw_correct,
                                         len(opts)))
    elif kind == CORRECT_MULTI:
        letters = [p for p in _MULTI_SPLIT.split(raw_correct) if p]
        if not letters:
            raise _RowError("correct", "", f"{qtype.code} needs at least one "
                                           f"letter. {qtype.correct_rule}")
        idx = sorted({_letter_index(p, "correct", raw_correct, len(opts))
                      for p in letters})
        correct_json = json.dumps(idx)
    elif kind == CORRECT_ORDER:
        if raw_correct:
            raise _RowError("correct", raw_correct,
                            f"must be blank for {qtype.code}. "
                            f"{qtype.correct_rule}")
        correct_json = json.dumps(list(range(len(opts))))
    elif kind == CORRECT_ANSWERS:
        answers = parse_answers_cell(raw_correct)
        err = answers_error(answers)
        if err:
            raise _RowError("correct", raw_correct,
                            f"{qtype.code} {err} {qtype.correct_rule}")
        correct_json = json.dumps(answers, ensure_ascii=False)
    elif kind == CORRECT_ZONES:
        try:
            cfg = parse_zones_cell(raw_correct)
        except ValueError as e:
            raise _RowError("correct", raw_correct,
                            f"{qtype.code}: {e} {qtype.correct_rule}")
        correct_json = json.dumps(cfg)
    elif kind == CORRECT_SETTINGS:
        try:
            settings = parse_settings_cell(qtype, raw_correct)
        except ValueError as e:
            raise _RowError("correct", raw_correct,
                            f"{qtype.code}: {e} {qtype.correct_rule}")
        correct_json = json.dumps(settings)
    else:
        if raw_correct:
            warn("correct", raw_correct,
                 f"{qtype.code} has no correct answer; ignored.")
        correct_json = ""

    time_limit = _parse_int(row, "time_limit", TIME_MIN, TIME_MAX,
                            qtype.default_time, warn)
    points = _parse_int(row, "points", POINTS_MIN, POINTS_MAX,
                        qtype.default_points, warn)
    if qtype.fixed_points is not None and points != qtype.fixed_points:
        warn("points", str(points),
             f"{qtype.code} is always worth {qtype.fixed_points}; "
             f"set to {qtype.fixed_points}.")
        points = qtype.fixed_points

    image_url = (row.get("image_url") or "").strip() or None
    if image_url:
        if not re.match(r"https?://\S+$", image_url, re.I):
            raise _RowError("image_url", image_url,
                            "must be a direct public link starting with "
                            "https:// (or leave blank).")
        if any(h in image_url.lower() for h in _NON_DIRECT_IMAGE_HINTS):
            warn("image_url", image_url,
                 "looks like a web page or share link, not a direct image "
                 "file; the image may not load.")
    elif qtype.requires_image:
        raise _RowError("image_url", "", f"{qtype.code} questions need an image.")

    return {
        "text": text,
        "question_type": qtype.code,
        "options": opts,
        "correct_json": correct_json,
        "time_limit": time_limit,
        "points": points,
        "image_url": image_url,
    }


def parse_csv(raw: bytes) -> ImportResult:
    """Validate a question CSV. Rows with errors are skipped and reported;
    rows with warnings are imported with the noted adjustment."""
    result = ImportResult()
    text, result.encoding = _decode(raw)
    if text is None:
        result.issues.append(Issue("error", "Could not read the file as text. "
                                            "Save it as 'CSV UTF-8'."))
        return result
    if not text.strip():
        result.issues.append(Issue("error", "The file is empty."))
        return result

    result.delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=result.delimiter)
    raw_header = next(reader)

    # Map header names (case-insensitive, legacy aliases) to canonical columns.
    alias_map = {c.name: c.name for c in COLUMNS}
    for c in COLUMNS:
        for a in c.aliases:
            alias_map[a] = c.name
    header = []
    for i, h in enumerate(raw_header):
        key = h.strip().lower()
        canonical = alias_map.get(key)
        if canonical is None and key:
            result.issues.append(Issue(
                "warning", "unknown column; ignored.", row=1,
                column=h.strip(), value=""))
        header.append(canonical)

    missing = [c.name for c in COLUMNS if c.required and c.name not in header]
    if missing:
        result.issues.append(Issue(
            "error", f"missing required column(s): {', '.join(missing)}. "
                     f"Expected header: "
                     f"{TEMPLATE_DELIMITER.join(COLUMN_NAMES)}",
            row=1, column=", ".join(missing)))
        return result

    for row_num, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue                      # blank line / Excel ";;;;" row
        row = {}
        for name, value in zip(header, cells):
            if name and name not in row:
                row[name] = value
        if len(cells) > len(header) and any(c.strip()
                                             for c in cells[len(header):]):
            result.issues.append(Issue(
                "warning", f"row has {len(cells)} cells but the header has "
                           f"{len(header)}; extra cells ignored. Check for an "
                           f"unquoted '{result.delimiter}' inside a cell.",
                row=row_num))

        row_warnings = []

        def warn(column, value, message, _rw=row_warnings, _n=row_num):
            _rw.append(Issue("warning", message, row=_n, column=column,
                             value=value))

        try:
            question = _parse_row(row, warn)
        except _RowError as e:
            result.issues.append(Issue("error", e.message, row=row_num,
                                       column=e.column, value=e.value))
            continue
        result.issues.extend(row_warnings)
        result.questions.append(question)

    if not result.questions and not result.errors:
        result.issues.append(Issue("error", "No questions found below the "
                                            "header row."))
    return result
