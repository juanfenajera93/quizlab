"""Question type registry: everything type-specific, one class per type.

The *spec* of a type (CSV columns, `correct` encoding, limits, defaults,
labels, the AI-prompt text) lives in question_spec.py, which also drives
the CSV template, the AI prompt and the importer. Everything the type
*does* lives here:

  live game  which player messages answer it, the per-question shuffle,
             the options a phone sees, scoring, the phone's and the host's
             reveal payloads, the per-option distribution
  review     the "your answer / right answer" text (live end-of-game
             review and homework)
  homework   the scoring key when there is no live session
  history    analytics answers_json and the session-detail view
  editor     validate_question() for the save endpoint

Browser-side rendering for the same types is static/js/qtypes.js; the
metadata both sides need (KIND_META) is injected into the pages from here.
game_manager.py, main.py and the JS call these hooks instead of branching on
question_type.
"""

import json
import math
import random
import re
import unicodedata
from collections import Counter
from typing import Dict, List, Optional, Tuple

import question_spec
from scoring import Outcome

ARROW = " → "
_ANY_OPTION = set(range(100))     # what ms scoring treats as an option index


def _int_or_zero(correct_json) -> int:
    try:
        return int(correct_json) if correct_json != "" else 0
    except (ValueError, TypeError):
        return 0


def _option_text(options, i) -> Optional[str]:
    if isinstance(i, int) and 0 <= i < len(options):
        return str(options[i])
    return None


def _join(options, indices, sep) -> str:
    return sep.join(t for t in (_option_text(options, i) for i in indices) if t is not None)


def normalize_text(text: str) -> str:
    """How short answers are compared: case, accents and extra spaces do
    not count ("Tamaño" = "tamano" = " TAMAÑO ")."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(no_accents.casefold().split())


def _word_freq(texts) -> Dict[str, int]:
    freq: Dict[str, int] = {}
    for txt in texts:
        key = txt.lower().strip()
        if key:
            freq[key] = freq.get(key, 0) + 1
    return freq


class QuestionKind:
    code = ""
    # Has a right answer: moves streaks, counts toward correct_count, gets
    # a ✓/✗ in reviews, shows the live points counter.
    scored = True
    partial_credit = False
    # Always worth 0 (word cloud): phones say "no points" even if a value
    # sneaks into `points`.
    never_scores = False
    # Player websocket message types that answer this type. Types added
    # after the first six answer with the generic "submit" message and
    # implement normalize_submit().
    messages: Tuple[str, ...] = ()
    # How the session-detail page shows the stored answers_json:
    # "bars" (per-option %), "words" (top answers), "texts" or None
    stats_view_kind: Optional[str] = None
    # Several "submit" messages per player (brainstorm ideas): a rejoin
    # only counts as answered once the type locked the question.
    multi_submit = False

    @property
    def spec(self) -> Optional[question_spec.QuestionType]:
        return question_spec.QUESTION_TYPES.get(self.code)

    def meta(self) -> dict:
        """What the browser needs to know (KIND_META)."""
        spec = self.spec
        return {
            "code": self.code,
            "label": spec.label if spec else self.code,
            "scored": self.scored,
            "partial_credit": self.partial_credit,
            "never_scores": self.never_scores,
            "min_options": spec.min_options if spec else 0,
            "max_options": spec.max_options if spec else 0,
            "default_options": list(spec.default_options) if spec else [],
            "fixed_points": spec.fixed_points if spec else None,
            "correct_kind": spec.correct_kind if spec else None,
            "default_points": spec.default_points if spec else 100,
            "default_time": spec.default_time if spec else 20,
            "requires_image": spec.requires_image if spec else False,
            "max_len": getattr(self, "MAX_LEN", None),
        }

    # ── Live game ───────────────────────────────────────────────────────
    def prepare(self, q: dict) -> Optional[list]:
        """Per-question state chosen when the question starts (stored in
        session.order_correct): the ordering shuffle. None = nothing."""
        return None

    def player_options(self, q: dict, state) -> list:
        """The options as the phones (and the host tiles) see them."""
        return list(q.get("options", []))

    def player_question_extra(self, q: dict) -> dict:
        """Settings the phones need with the question (never the answer)."""
        return {}

    def scoring_key(self, q: dict, state) -> str:
        """The correct_json scoring compares against in a live game."""
        return q.get("correct_json", "")

    def homework_key(self, q: dict) -> str:
        """The same without a live session (homework shows the options in
        their original order)."""
        return q.get("correct_json", "")

    def normalize_submit(self, q: dict, value, previous):
        """A "submit" message's value -> (answer to store, locked), or None
        to ignore it. `previous` is what the player already submitted."""
        return None

    def homework_answer(self, q: dict, value):
        """A homework answer as submitted -> the value to grade."""
        return value

    def score(self, key: str, answer, out: Outcome) -> dict:
        return out.wrong()

    def correct_index(self, q: dict) -> int:
        return -1

    def player_reveal_extra(self, q: dict, player, qi: int) -> dict:
        return {}

    def host_reveal_extra(self, q: dict, session, qi: int) -> dict:
        return {}

    def host_live_state(self, session, qi: int) -> dict:
        """Extra live-question state a reconnecting host needs."""
        return {}

    def host_live_update(self, session, qi: int) -> dict:
        """Extra fields for the host's answer_counts after a "submit"."""
        return {}

    def host_view(self, q: dict, value) -> Optional[dict]:
        """Validate a projector-side arrangement the host saves for the
        revealed question (brainstorm groups) -> what to store, or None."""
        return None

    def distribution(self, q: dict, answers: list, state) -> dict:
        """Type-specific part of the host reveal's distribution, from every
        player's final (scored) answer."""
        return {}

    # ── Review ──────────────────────────────────────────────────────────
    def your_answer_text(self, q: dict, answer, seen_options: list) -> str:
        return "—"

    def correct_answer_text(self, q: dict) -> str:
        return ""

    # ── History ─────────────────────────────────────────────────────────
    def analytics_answers(self, q: dict, qi: int, players, session) -> str:
        return "[]"

    def stats_view(self, answers_raw, total: int) -> list:
        return []

    # ── Editor / save ───────────────────────────────────────────────────
    def validate_question(self, q: dict) -> List[str]:
        """Problems that make the question unplayable (save returns 400)."""
        return []


class _CountsOptions(QuestionKind):
    """Types answered by choosing options: per-option counts."""
    stats_view_kind = "bars"

    def distribution(self, q, answers, state):
        n_opts = len(q.get("options", []))
        counts = [0] * n_opts
        for a in answers:
            picked = a if isinstance(a, list) else [a]
            for i in set(x for x in picked if isinstance(x, int)):
                if 0 <= i < n_opts:
                    counts[i] += 1
        return {"counts": counts}

    @staticmethod
    def _picked(answer) -> list:
        """Option indices a locked answer chose (for analytics)."""
        return []

    def analytics_answers(self, q, qi, players, session):
        counts = [0] * max(len(q.get("options", [])), 1)
        for p in players:
            for idx in self._picked(p.answers.get(qi)):
                if isinstance(idx, int) and 0 <= idx < len(counts):
                    counts[idx] += 1
        return json.dumps(counts)

    def stats_view(self, answers_raw, total):
        if not isinstance(answers_raw, list):
            return []
        return [{"label": chr(65 + i), "count": cnt,
                 "pct": round(cnt / total * 100, 1) if total > 0 else 0.0}
                for i, cnt in enumerate(answers_raw)]


class SingleChoice(_CountsOptions):
    """mc and tf: one option is right."""
    messages = ("answer",)

    def __init__(self, code):
        self.code = code

    @staticmethod
    def _picked(answer):
        return [answer] if isinstance(answer, int) else []

    def score(self, key, answer, out):
        return out.full() if answer == _int_or_zero(key) else out.wrong()

    def correct_index(self, q):
        return _int_or_zero(q.get("correct_json", ""))

    def your_answer_text(self, q, answer, seen_options):
        return _option_text(seen_options, answer) or "—"

    def correct_answer_text(self, q):
        return _option_text(q.get("options", []), self.correct_index(q)) or ""


class Poll(SingleChoice):
    scored = False

    def score(self, key, answer, out):
        if answer is not None and answer != -1:
            return out.flat()
        return out.wrong()

    def correct_index(self, q):
        return -1

    def correct_answer_text(self, q):
        return ""


class MultiSelect(_CountsOptions):
    code = "ms"
    partial_credit = True
    messages = ("selection", "confirm")

    @staticmethod
    def _correct(correct_json):
        return json.loads(correct_json)

    @staticmethod
    def _picked(answer):
        return answer if isinstance(answer, list) else []

    def score(self, key, answer, out):
        try:
            correct_indices = set(self._correct(key))
        except Exception:
            return out.wrong()
        if not isinstance(answer, list):
            return out.wrong()
        selected = set(answer)
        if selected & (_ANY_OPTION - correct_indices):   # any wrong pick → zero
            return out.wrong()
        overlap = len(selected & correct_indices)
        if overlap == len(correct_indices):
            return out.full()
        if overlap > 0:
            return out.partial(overlap, len(correct_indices))
        return out.wrong()

    def host_reveal_extra(self, q, session, qi):
        try:
            return {"correct_indices": sorted(
                i for i in self._correct(q.get("correct_json", "")) if isinstance(i, int))}
        except (ValueError, TypeError):
            return {"correct_indices": []}

    def your_answer_text(self, q, answer, seen_options):
        if isinstance(answer, list):
            return _join(seen_options, answer, ", ") or "—"
        return "—"

    def correct_answer_text(self, q):
        try:
            return _join(q.get("options", []), self._correct(q.get("correct_json", "")), ", ")
        except Exception:
            return ""


class Ordering(QuestionKind):
    code = "order"
    partial_credit = True
    messages = ("order_update",)

    def prepare(self, q):
        """perm[original index] = position in the shuffled list sent to the
        phones. A correct submission (indices into that list, in order) is
        exactly perm."""
        n = len(q.get("options", []))
        if n <= 1:
            return list(range(n))
        shuffled_indices = list(range(n))
        random.shuffle(shuffled_indices)
        perm = [0] * n
        for orig_pos, shuf_pos in enumerate(shuffled_indices):
            perm[shuf_pos] = orig_pos
        return perm

    def player_options(self, q, perm):
        options = list(q.get("options", []))
        if perm and len(perm) == len(options):
            shown = [""] * len(options)
            for orig_idx, shuf_pos in enumerate(perm):
                shown[shuf_pos] = options[orig_idx]
            return shown
        return options

    def scoring_key(self, q, perm):
        return json.dumps(perm or [])

    def homework_key(self, q):
        # Homework submits original indices in the chosen order
        return json.dumps(list(range(len(q.get("options", [])))))

    def score(self, key, answer, out):
        try:
            correct_order = json.loads(key) if key else []
        except Exception:
            return out.wrong()
        if not isinstance(answer, list) or not correct_order:
            return out.wrong()
        if len(answer) != len(correct_order):
            return out.wrong()
        matching = sum(1 for a, b in zip(answer, correct_order) if a == b)
        if matching == len(correct_order):
            return out.full()
        return out.partial(matching, len(correct_order))

    def host_reveal_extra(self, q, session, qi):
        # The host was sent the shuffled list; the correct sequence is the
        # options as the teacher wrote them.
        return {"correct_options": list(q.get("options", []))}

    def distribution(self, q, answers, perm):
        n_opts = len(q.get("options", []))
        perm = perm or list(range(n_opts))
        in_place = [0] * n_opts
        full = 0
        for a in answers:
            if not isinstance(a, list) or len(a) != n_opts:
                continue
            hits = [a[j] == perm[j] for j in range(n_opts)]
            for j, hit in enumerate(hits):
                if hit:
                    in_place[j] += 1
            if all(hits):
                full += 1
        return {"full_correct": full, "in_place": in_place}

    def your_answer_text(self, q, answer, seen_options):
        if isinstance(answer, list):
            return _join(seen_options, answer, ARROW) or "—"
        return "—"

    def correct_answer_text(self, q):
        return ARROW.join(str(o) for o in q.get("options", []))

    def analytics_answers(self, q, qi, players, session):
        return json.dumps([sum(1 for p in players if qi in p.answers)])


class WordCloud(QuestionKind):
    code = "wordcloud"
    scored = False
    never_scores = True
    messages = ("wordcloud_answer",)
    stats_view_kind = "words"
    MAX_LEN = 50

    def score(self, key, answer, out):
        if answer and isinstance(answer, str) and answer.strip():
            return out.flat()
        return out.wrong()

    def player_reveal_extra(self, q, player, qi):
        return {"your_text": player.answers.get(qi, "")}

    def host_reveal_extra(self, q, session, qi):
        return {"words": _word_freq(session.wordcloud_answers.get(qi, {}).values())}

    def host_live_state(self, session, qi):
        # live word feed (reactions are transient — no state)
        return {"words": list(session.wordcloud_answers.get(qi, {}).values())}

    def your_answer_text(self, q, answer, seen_options):
        if isinstance(answer, str) and answer.strip():
            return answer.strip()[:self.MAX_LEN]
        return "—"

    def analytics_answers(self, q, qi, players, session):
        return json.dumps(_word_freq(session.wordcloud_answers.get(qi, {}).values()))

    def stats_view(self, answers_raw, total):
        return _top_words_view(answers_raw, total)


def _top_words_view(answers_raw, total):
    if not isinstance(answers_raw, dict):
        return []
    top = sorted(answers_raw.items(), key=lambda x: x[1], reverse=True)[:5]
    return [{"word": w, "count": c} for w, c in top]


class ShortAnswer(QuestionKind):
    """Typed answer (max SHORT_MAX_LEN chars) checked against a list of
    accepted answers, ignoring case, accents and extra spaces."""
    code = "short"
    messages = ("submit",)
    stats_view_kind = "words"
    TOP = 8
    MAX_LEN = question_spec.SHORT_MAX_LEN

    @staticmethod
    def accepted(correct_json) -> List[str]:
        try:
            answers = json.loads(correct_json or "[]")
        except (ValueError, TypeError):
            return []
        return [a for a in answers if isinstance(a, str)] if isinstance(answers, list) else []

    def _clean(self, value):
        if not isinstance(value, str):
            return None
        text = value.strip()[:question_spec.SHORT_MAX_LEN].strip()
        return text or None

    def normalize_submit(self, q, value, previous):
        text = self._clean(value)
        return (text, True) if text else None

    def homework_answer(self, q, value):
        return self._clean(value)

    def score(self, key, answer, out):
        if not isinstance(answer, str) or not answer.strip():
            return out.wrong()
        accepted = {normalize_text(a) for a in self.accepted(key)}
        return out.full() if normalize_text(answer) in accepted else out.wrong()

    def player_reveal_extra(self, q, player, qi):
        # a typed answer, so the phone knows the student did answer
        answer = player.answers.get(qi)
        return {"your_answer": answer} if isinstance(answer, str) else {}

    def host_reveal_extra(self, q, session, qi):
        return {"accepted_answers": self.accepted(q.get("correct_json"))}

    def distribution(self, q, answers, state):
        """The most common answers, grouped the way they are graded, each
        shown as its most frequent spelling."""
        accepted = {normalize_text(a) for a in self.accepted(q.get("correct_json"))}
        groups: Dict[str, Counter] = {}
        for a in answers:
            if isinstance(a, str) and a.strip():
                groups.setdefault(normalize_text(a), Counter())[a.strip()] += 1
        top = sorted(groups.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
        correct = sum(sum(c.values()) for k, c in groups.items() if k in accepted)
        return {
            "correct_count": correct,
            "top_answers": [{"text": c.most_common(1)[0][0], "count": sum(c.values()),
                             "accepted": k in accepted}
                            for k, c in top[:self.TOP]],
        }

    def your_answer_text(self, q, answer, seen_options):
        return answer.strip() if isinstance(answer, str) and answer.strip() else "—"

    def correct_answer_text(self, q):
        return " / ".join(self.accepted(q.get("correct_json")))

    def analytics_answers(self, q, qi, players, session):
        return json.dumps(dict(Counter(
            normalize_text(p.answers[qi]) for p in players
            if isinstance(p.answers.get(qi), str) and p.answers[qi].strip())))

    def stats_view(self, answers_raw, total):
        return _top_words_view(answers_raw, total)

    def validate_question(self, q):
        err = question_spec.answers_error(self.accepted(q.get("correct_json")))
        return [f"short answer {err}"] if err else []


class PinOnImage(QuestionKind):
    """Tap the image once. Inside any circular zone: full speed-formula
    points. Outside: linearly fewer with the distance to the nearest zone
    edge, 0 at `falloff` zone radii.

    Coordinates are fractions of the image: x of its width, y of its
    height; a zone radius is a fraction of the width, so with
    aspect = height / width every distance is measured in width units and a
    zone is a true circle on any screen."""
    code = "pin"
    messages = ("submit",)
    stats_view_kind = "words"

    @staticmethod
    def config(correct_json) -> dict:
        try:
            cfg = json.loads(correct_json or "{}")
        except (ValueError, TypeError):
            cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    @staticmethod
    def _point(value):
        if not isinstance(value, dict):
            return None
        try:
            x, y = float(value.get("x")), float(value.get("y"))
        except (TypeError, ValueError):
            return None
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        return {"x": round(x, 4), "y": round(y, 4)}

    def normalize_submit(self, q, value, previous):
        pin = self._point(value)
        return (pin, True) if pin else None

    def homework_answer(self, q, value):
        return self._point(value)

    def measure(self, cfg, pin) -> Optional[Tuple[float, float]]:
        """(distance outside the nearest zone, that zone's radius), in
        image-width units; distance <= 0 means inside."""
        aspect = float(cfg.get("aspect") or 1.0)
        best = None
        for z in cfg.get("zones") or []:
            try:
                zx, zy, r = float(z["x"]), float(z["y"]), float(z["r"])
            except (KeyError, TypeError, ValueError):
                continue
            edge = math.hypot(pin["x"] - zx, (pin["y"] - zy) * aspect) - r
            if best is None or edge < best[0]:
                best = (edge, r)
        return best

    def score(self, key, answer, out):
        pin = self._point(answer)
        cfg = self.config(key)
        nearest = self.measure(cfg, pin) if pin else None
        if nearest is None:
            return out.wrong()
        edge, r = nearest
        if edge <= 0:
            return out.full()
        falloff = float(cfg.get("falloff", question_spec.PIN_DEFAULT_FALLOFF)) * r
        proximity = max(0.0, 1 - edge / falloff) if falloff > 0 else 0.0
        return out.near(proximity, edge / r if r > 0 else 0.0)

    def host_reveal_extra(self, q, session, qi):
        cfg = self.config(q.get("correct_json"))
        return {"zones": cfg.get("zones") or [], "aspect": cfg.get("aspect"),
                "falloff": cfg.get("falloff", question_spec.PIN_DEFAULT_FALLOFF)}

    def distribution(self, q, answers, state):
        cfg = self.config(q.get("correct_json"))
        pins = []
        for a in answers:
            pin = self._point(a)
            if pin:
                nearest = self.measure(cfg, pin)
                pins.append({**pin, "inside": bool(nearest and nearest[0] <= 0)})
        return {"pins": pins, "inside": sum(1 for p in pins if p["inside"])}

    def your_answer_text(self, q, answer, seen_options):
        pin = self._point(answer)
        nearest = self.measure(self.config(q.get("correct_json")), pin) if pin else None
        if nearest is None:
            return "—"
        edge, r = nearest
        if edge <= 0:
            return "📍 dentro de la zona"
        return f"📍 fuera, a {edge / r:.1f} radios de la zona".replace(".", ",")

    def correct_answer_text(self, q):
        return "la zona marcada en la imagen"

    def analytics_answers(self, q, qi, players, session):
        cfg = self.config(q.get("correct_json"))
        inside = outside = 0
        for p in players:
            pin = self._point(p.answers.get(qi))
            if pin:
                nearest = self.measure(cfg, pin)
                if nearest and nearest[0] <= 0:
                    inside += 1
                else:
                    outside += 1
        return json.dumps({"dentro": inside, "fuera": outside})

    def stats_view(self, answers_raw, total):
        return _top_words_view(answers_raw, total)

    def validate_question(self, q):
        problems = []
        if not (q.get("image_url") or "").strip():
            problems.append("pin on image needs an image.")
        err = question_spec.zones_error(self.config(q.get("correct_json")))
        if err:
            problems.append(f"pin on image {err}")
        return problems


# ─── Opinion types: no right answer, 0 points, no live counter ─────────────

class _Opinion(QuestionKind):
    scored = False
    never_scores = True
    messages = ("submit",)

    def settings(self, q) -> dict:
        spec = self.spec
        defaults = {st.name: st.default for st in spec.settings} if spec else {}
        try:
            stored = json.loads(q.get("correct_json") or "{}")
        except (ValueError, TypeError):
            stored = {}
        return {**defaults, **(stored if isinstance(stored, dict) else {})}

    def score(self, key, answer, out):
        # Answering counts as participation (like poll), but these types are
        # never worth points, whatever `points` holds.
        if not self.valid(key, answer):
            return out.wrong()
        return {"points": 0, "correct": True, "kind": "full"}

    def valid(self, key, answer) -> bool:
        return answer not in (None, "", [], -1)

    def player_reveal_extra(self, q, player, qi):
        # The phone says "sent" / "no answer"
        return {"your_text": "✓" if player.answers.get(qi) not in (None, "", []) else ""}

    def validate_question(self, q):
        spec = self.spec
        if spec and spec.settings:
            err = question_spec.settings_error(spec, self.settings(q))
            if err:
                return [f"{spec.label}: {err}"]
        return []


class OpenEnded(_Opinion):
    """Free text up to OPEN_MAX_LEN; an anonymous card wall."""
    code = "open"
    stats_view_kind = "texts"
    MAX_LEN = question_spec.OPEN_MAX_LEN

    def _clean(self, value):
        if not isinstance(value, str):
            return None
        text = " ".join(value.split())[:self.MAX_LEN].strip()
        return text or None

    def normalize_submit(self, q, value, previous):
        text = self._clean(value)
        return (text, True) if text else None

    def homework_answer(self, q, value):
        return self._clean(value)

    def host_reveal_extra(self, q, session, qi):
        # Anonymous: texts only, in the order they arrived
        return {"cards": [p.answers[qi] for p in session.players.values()
                          if isinstance(p.answers.get(qi), str)]}

    def your_answer_text(self, q, answer, seen_options):
        return answer if isinstance(answer, str) and answer else "—"

    def analytics_answers(self, q, qi, players, session):
        return json.dumps([p.answers[qi] for p in players
                           if isinstance(p.answers.get(qi), str)], ensure_ascii=False)

    def stats_view(self, answers_raw, total):
        return [str(t) for t in answers_raw] if isinstance(answers_raw, list) else []


# Words that say nothing about an idea's topic (Spanish + English)
STOPWORDS = frozenset("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque
bien cada casi como con contra cual cuales cuando de del desde donde dos e el
ella ellas ello ellos en entre era es esa esas ese eso esos esta estan estas
este esto estos fue fueron ha hace hacer han hay la las le les lo los mas me
mi mis mucho muy ni no nos nuestra nuestro o otra otras otro otros para pero
poco por porque que quien se ser si sin sobre son su sus tambien tan tanto te
tener tiene tienen todo todos tu tus un una unas uno unos usar y ya yo
about after all also an and any are as at be been but by can could do does
for from had has have how if in into is it its just more most my no not of
on or other our out so some such than that the their them then there these
they this to too use used using very was we were what when which who will
with would you your
""".split())

_WORD = re.compile(r"[a-z0-9ñ]+")            # on normalize_text() output
_LETTERS = re.compile(r"[^\W\d_]+")          # words as students wrote them


def idea_tokens(text: str) -> set:
    """Topic tokens of an idea: lower case, no accents, no stopwords, cut
    to a 5-letter prefix so practica / practicar / practicos match."""
    words = _WORD.findall(normalize_text(text))
    return {w[:5] for w in words if w not in STOPWORDS and len(w) > 1}


def group_ideas(texts: List[str], threshold: float = 0.5) -> List[dict]:
    """Automatic brainstorm grouping, no AI and no external service.

    Greedy single-link: each idea joins the existing group holding the idea
    it overlaps most with (shared tokens / tokens of the smaller idea,
    >= threshold), else starts a group. Ideas with no topic tokens join a
    group only when identical once normalised. Groups of one are pooled
    into "Otras ideas". Returns [{"name", "ideas": [indices into texts]}],
    largest groups first."""
    tokens = [idea_tokens(t) for t in texts]
    groups: List[List[int]] = []
    for i, tok in enumerate(tokens):
        best, best_sim = None, 0.0
        for g, members in enumerate(groups):
            for j in members:
                other = tokens[j]
                if tok and other:
                    sim = len(tok & other) / min(len(tok), len(other))
                else:
                    sim = 1.0 if normalize_text(texts[i]) == normalize_text(texts[j]) else 0.0
                if sim > best_sim:
                    best, best_sim = g, sim
        if best is not None and best_sim >= threshold:
            groups[best].append(i)
        else:
            groups.append([i])

    def name(members):
        # The most frequent topic word, in the spelling students used most
        stem_count = Counter(st for j in members for st in tokens[j])
        if not stem_count:
            return texts[members[0]][:40]
        top = max(stem_count.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))[0]
        words = Counter(w for j in members for w in _LETTERS.findall(texts[j].lower())
                        if normalize_text(w)[:5] == top)
        word = words.most_common(1)[0][0] if words else top
        return word[:1].upper() + word[1:]

    multi = [g for g in groups if len(g) > 1]
    singles = [g[0] for g in groups if len(g) == 1]
    out = [{"name": name(g), "ideas": g}
           for g in sorted(multi, key=lambda g: (-len(g), g[0]))]
    if singles:
        out.append({"name": "Otras ideas" if out else "Ideas", "ideas": singles})
    return out


class Brainstorm(_Opinion):
    """Several short ideas per student; the projector groups them."""
    code = "brainstorm"
    stats_view_kind = "words"
    multi_submit = True
    MAX_LEN = question_spec.IDEA_MAX_LEN

    def max_ideas(self, q) -> int:
        return int(self.settings(q).get("ideas", 3))

    def player_question_extra(self, q):
        return {"max_ideas": self.max_ideas(q)}

    def normalize_submit(self, q, value, previous):
        if not isinstance(value, str):
            return None
        idea = " ".join(value.split())[:self.MAX_LEN].strip()
        ideas = list(previous) if isinstance(previous, list) else []
        if not idea or len(ideas) >= self.max_ideas(q):
            return None
        ideas.append(idea)
        return ideas, len(ideas) >= self.max_ideas(q)

    def homework_answer(self, q, value):
        if isinstance(value, str):
            value = value.split("\n")
        if not isinstance(value, list):
            return None
        ideas = [" ".join(str(v).split())[:self.MAX_LEN] for v in value
                 if isinstance(v, str) and v.strip()]
        return ideas[:self.max_ideas(q)] or None

    @staticmethod
    def ideas(session, qi) -> List[str]:
        """Every idea of the room, anonymous, in a stable order (players in
        join order, ideas in the order sent): an idea's id is its index."""
        return [idea for p in session.players.values()
                for idea in (p.answers.get(qi) or []) if isinstance(idea, str)]

    def host_reveal_extra(self, q, session, qi):
        ideas = self.ideas(session, qi)
        return {"ideas": ideas, "auto_groups": group_ideas(ideas),
                "saved_view": session.host_views.get(qi)}

    def host_view(self, q, value):
        """{"mode": "auto"|"manual", "groups": [{"name", "ideas": [ids]}]}"""
        if not isinstance(value, dict) or value.get("mode") not in ("auto", "manual"):
            return None
        groups = []
        for g in value.get("groups") or []:
            if not isinstance(g, dict):
                return None
            ids = [i for i in g.get("ideas") or [] if isinstance(i, int) and 0 <= i < 1000]
            groups.append({"name": str(g.get("name") or "")[:40], "ideas": ids})
        return {"mode": value["mode"], "groups": groups[:50]}

    def your_answer_text(self, q, answer, seen_options):
        if isinstance(answer, list) and answer:
            return " · ".join(str(a) for a in answer)
        return "—"

    def analytics_answers(self, q, qi, players, session):
        """The groups as the teacher left them (manual) or the automatic
        ones: {group name: idea count}."""
        ideas = [idea for p in players for idea in (p.answers.get(qi) or [])
                 if isinstance(idea, str)]
        view = session.host_views.get(qi) if session else None
        groups = (view["groups"] if view and view.get("mode") == "manual"
                  else group_ideas(ideas))
        return json.dumps({g["name"] or "—": len(g["ideas"]) for g in groups if g["ideas"]},
                          ensure_ascii=False)

    def stats_view(self, answers_raw, total):
        return _top_words_view(answers_raw, total)


class Scale(_Opinion):
    """Rate from 1 to max (5 or 10); distribution and average."""
    code = "scale"
    stats_view_kind = "bars"

    def top(self, q) -> int:
        return int(self.settings(q).get("max", 5))

    def valid(self, key, answer) -> bool:
        return self._value({"correct_json": key}, answer) is not None

    def player_question_extra(self, q):
        return {"scale_max": self.top(q)}

    def _value(self, q, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value != int(value) or not 1 <= value <= self.top(q):
            return None
        return int(value)

    def normalize_submit(self, q, value, previous):
        v = self._value(q, value)
        return (v, True) if v is not None else None

    def homework_answer(self, q, value):
        return self._value(q, value)

    def player_options(self, q, state):
        return list(q.get("options", []))          # the three labels

    def distribution(self, q, answers, state):
        top = self.top(q)
        counts = [0] * top
        for a in answers:
            v = self._value(q, a)
            if v is not None:
                counts[v - 1] += 1
        n = sum(counts)
        avg = round(sum((i + 1) * c for i, c in enumerate(counts)) / n, 2) if n else None
        return {"scale_counts": counts, "average": avg, "max": top}

    def your_answer_text(self, q, answer, seen_options):
        v = self._value(q, answer)
        return f"{v} / {self.top(q)}" if v is not None else "—"

    def analytics_answers(self, q, qi, players, session):
        counts = [0] * self.top(q)
        for p in players:
            v = self._value(q, p.answers.get(qi))
            if v is not None:
                counts[v - 1] += 1
        return json.dumps(counts)

    def stats_view(self, answers_raw, total):
        if not isinstance(answers_raw, list):
            return []
        return [{"label": str(i + 1), "count": c,
                 "pct": round(c / total * 100, 1) if total > 0 else 0.0}
                for i, c in enumerate(answers_raw)]


KINDS: Dict[str, QuestionKind] = {k.code: k for k in [
    SingleChoice("mc"),
    SingleChoice("tf"),
    MultiSelect(),
    Poll("poll"),
    Ordering(),
    WordCloud(),
    ShortAnswer(),
    PinOnImage(),
    OpenEnded(),
    Brainstorm(),
    Scale(),
]}

_UNKNOWN = QuestionKind()


def get_kind(code: Optional[str]) -> QuestionKind:
    """The registry entry for a question_type ("mc" when blank). An unknown
    code gets a kind that scores nothing rather than an error."""
    return KINDS.get(code or "mc", _UNKNOWN)


def kind_of(q: dict) -> QuestionKind:
    return get_kind(q.get("question_type", "mc"))


def kind_meta() -> Dict[str, dict]:
    """Injected into the pages as window.QL_KIND_META (see qtypes.js)."""
    return {code: k.meta() for code, k in KINDS.items()}
