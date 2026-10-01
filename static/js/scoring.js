/* QuizLab live points counter — client mirror of game_manager.speed_points()
   and streak_bonus(). Display only: the server scores every answer and its
   numbers (answer_ack, reveal) always win. Keep the formulas in step with
   game_manager.py. */
(function () {
  'use strict';

  var SPEED_FLOOR = 0.5;
  var SCORED_TYPES = ['mc', 'tf', 'ms', 'order'];   // = STREAK_TYPES
  var PARTIAL_TYPES = ['ms', 'order'];

  function speedPoints(base, timeLimit, timeTaken, mode) {
    if (mode === 'accuracy') return base;
    var minPts = Math.floor(base * SPEED_FLOOR);
    var remaining = Math.max(0, timeLimit - timeTaken);
    var pts = timeLimit > 0 ? Math.floor(base * (remaining / timeLimit)) : 0;
    return Math.max(minPts, pts);
  }

  function streakBonus(base, streakAfter) {
    if (streakAfter < 2) return 0;
    return Math.floor(base * 0.1 * Math.min(streakAfter - 1, 5));
  }

  // Clock anchored to the server's answer-phase start. q.answer_starts_in is
  // seconds from when the server built the message (negative once answering
  // has begun), so no clock comparison between devices is needed. The read
  // phase never counts: elapsed stays 0 until the answer phase starts.
  function Clock(q) {
    var startsIn = typeof q.answer_starts_in === 'number'
      ? q.answer_starts_in : (q.read_time || 0);
    this.answerStart = performance.now() + startsIn * 1000;
    this.timeLimit = q.full_time_limit || q.time_limit || 0;
    this.base = q.points || 0;
    this.mode = q.scoring_mode || 'speed';
  }
  Clock.prototype.elapsed = function () {
    return Math.max(0, (performance.now() - this.answerStart) / 1000);
  };
  Clock.prototype.points = function () {
    return speedPoints(this.base, this.timeLimit, this.elapsed(), this.mode);
  };

  window.QLScore = {
    SPEED_FLOOR: SPEED_FLOOR,
    speedPoints: speedPoints,
    streakBonus: streakBonus,
    Clock: Clock,
    // Counter shown only for scored types worth something (not poll,
    // wordcloud or 0-point questions).
    showsCounter: function (q) {
      return SCORED_TYPES.indexOf(q.question_type || 'mc') !== -1 && (q.points || 0) > 0;
    },
    hasPartialCredit: function (q) {
      return PARTIAL_TYPES.indexOf(q.question_type || 'mc') !== -1;
    }
  };
})();
