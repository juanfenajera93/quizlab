"""Scoring primitives shared by every question type (qtypes.py).

static/js/scoring.js mirrors speed_points() and streak_bonus() for the live
points counter; keep the two in step. The server's numbers are always the
ones that count.
"""

import math

SPEED_FLOOR = 0.5            # speed mode never drops below 50% of base points


def speed_points(base_points, time_limit, time_taken, scoring_mode="speed"):
    """Points for a fully correct answer given `time_taken` seconds into the
    answer phase (the read phase never counts)."""
    if scoring_mode == "accuracy":
        # Accuracy mode: full points for a correct answer, no time pressure
        return base_points
    min_pts = math.floor(base_points * SPEED_FLOOR)
    time_remaining = max(0.0, time_limit - time_taken)
    pts = math.floor(base_points * (time_remaining / time_limit)) if time_limit > 0 else 0
    return max(min_pts, pts)


def streak_bonus(base_points, streak_after):
    """Bonus for a correct answer that brings the streak to `streak_after`:
    +10% of base points per consecutive correct, capped at +50%."""
    if streak_after < 2:
        return 0
    return math.floor(base_points * 0.1 * min(streak_after - 1, 5))


class Outcome:
    """Builders for the score details dict a type's score() returns.

    kind: "speed"   fully correct in speed mode (speed_factor applied)
          "full"    fully correct with no time factor (accuracy mode, or a
                    type that pays a flat amount for answering, like poll)
          "partial" partial credit, no time factor (hits of parts)
          "wrong"   0 points
    """

    def __init__(self, time_taken, time_limit, base_points, scoring_mode):
        self.time_taken = time_taken
        self.time_limit = time_limit
        self.base = base_points
        self.mode = scoring_mode

    def full(self):
        pts = speed_points(self.base, self.time_limit, self.time_taken, self.mode)
        if self.mode == "accuracy":
            return {"points": pts, "correct": True, "kind": "full"}
        remaining = max(0.0, self.time_limit - self.time_taken)
        factor = remaining / self.time_limit if self.time_limit > 0 else 0.0
        return {"points": pts, "correct": True, "kind": "speed",
                "speed_factor": round(max(SPEED_FLOOR, factor), 3),
                "time_taken": round(self.time_taken, 2)}

    def flat(self):
        """The base points, no time factor (poll / word cloud participation)."""
        return {"points": self.base, "correct": True, "kind": "full"}

    def near(self, proximity, distance):
        """Close but not inside (pin on image): the full answer's points
        (speed formula) times `proximity` (1 at the zone edge, 0 at the
        falloff distance). `distance` is how far outside the nearest zone
        the answer landed, in zone radii."""
        full = self.full()
        # round first: 0.6 - 0.65 style float noise must not cost a point
        proximity = round(proximity, 6)
        out = {"points": math.floor(full["points"] * proximity + 1e-9),
               "correct": False, "kind": "near",
               "proximity": round(proximity, 3), "distance": round(distance, 2),
               "full_points": full["points"]}
        for key in ("speed_factor", "time_taken"):
            if key in full:
                out[key] = full[key]
        return out

    def partial(self, hits, parts):
        return {"points": math.floor(self.base * (hits / parts)),
                "correct": False, "kind": "partial",
                "hits": hits, "parts": parts}

    @staticmethod
    def wrong():
        return {"points": 0, "correct": False, "kind": "wrong"}
