(function () {
  'use strict';

  var CIRCUMFERENCE = 2 * Math.PI * 45;

  var t = window.qlT || function (k, fb) { return fb || k; };

  var ws = null;
  var playerId = null;
  var roomCode = null;
  var currentQuestionId = null;
  var answered = false;
  var controller = null;      // the question type's answer UI (qtypes.js)
  var timerInterval = null;
  var readTimerTimeout = null;
  var playerScore = 0;
  var reconnectAttempts = 0;
  var reconnectTimeout = null;
  var joinedNickname = '';    // what we actually joined with (input or roster pick)
  var rosterMode = false;
  var myTeamName = null;
  var wasInRoom = false;      // joined/rejoined at least once on this page load
  var myStreak = 0;           // consecutive fully-correct answers (from server)
  var currentQuestion = null; // last question payload (live points counter)
  var pointsClock = null;     // QLScore.Clock while the counter is live
  var pointsInterval = null;
  var questionEndsAt = null;  // performance.now() when the answer phase ends (server reference)
  var pointsFrozen = false;

  // ── Views ──────────────────────────────────────────────────────
  function _hideAllViews() {
    document.querySelectorAll('.pview').forEach(function (v) { v.classList.remove('active'); });
    document.getElementById('reveal-view').classList.remove('active');
    document.getElementById('final-view').classList.remove('active');
    var ended = document.getElementById('game-ended-view');
    if (ended) ended.classList.remove('active');
  }

  function showView(id) {
    _hideAllViews();
    var el = document.getElementById(id);
    if (el) el.classList.add('active');
  }

  function showReveal() {
    _hideAllViews();
    document.getElementById('reveal-view').classList.add('active');
  }

  function showFinal() {
    _hideAllViews();
    document.getElementById('final-view').classList.add('active');
  }

  function showGameEnded() {
    _hideAllViews();
    var el = document.getElementById('game-ended-view');
    if (el) el.classList.add('active');
  }

  // ── Stored identity ────────────────────────────────────────────
  // localStorage (not sessionStorage): a phone that locks for a while may
  // have its tab discarded and recreated, or the student may re-scan the QR
  // and land in a brand-new tab. Either way the same device must come back
  // as the same player. It is cleared when the game ends, the player is
  // kicked, the server says the room is gone, or a different room's QR is
  // opened.
  var ID_KEYS = { room: 'quizlab_room', nick: 'quizlab_nickname', pid: 'quizlab_player_id' };

  function storageGet(store, key) {
    try { return store.getItem(key); } catch (e) { return null; }
  }

  function getIdentity() {
    var stores = [window.localStorage, window.sessionStorage];
    for (var i = 0; i < stores.length; i++) {
      var room = storageGet(stores[i], ID_KEYS.room);
      var nick = storageGet(stores[i], ID_KEYS.nick);
      var pid  = storageGet(stores[i], ID_KEYS.pid);
      if (room && nick && pid) return { room: room, nick: nick, pid: pid };
    }
    return null;
  }

  function storeIdentity(room, nick, pid) {
    try {
      localStorage.setItem(ID_KEYS.room, room);
      localStorage.setItem(ID_KEYS.nick, nick);
      localStorage.setItem(ID_KEYS.pid, pid);
    } catch (e) {}
  }

  function clearIdentity() {
    [window.localStorage, window.sessionStorage].forEach(function (store) {
      try {
        store.removeItem(ID_KEYS.room);
        store.removeItem(ID_KEYS.nick);
        store.removeItem(ID_KEYS.pid);
      } catch (e) {}
    });
  }

  // ── WebSocket ──────────────────────────────────────────────────
  var lastMessageAt = Date.now();  // last frame received (server pings every 25s)
  var probeSentAt = 0;             // when we last asked the server "still there?"
  var PROBE_AFTER_IDLE_MS = 40000; // > server heartbeat interval
  var PROBE_TIMEOUT_MS = 8000;

  function wsUrl() {
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return proto + '://' + location.host + '/ws/player';
  }

  function attachHandlers(sock, onOpen, onClose) {
    sock.onopen = function () {
      lastMessageAt = Date.now();
      probeSentAt = 0;
      if (onOpen) onOpen();
    };
    sock.onmessage = function (e) {
      lastMessageAt = Date.now();
      probeSentAt = 0;
      try { handleMessage(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    sock.onclose = function () {
      // A stale socket closing after we already opened a new one must not
      // trigger a second reconnect cycle.
      if (sock !== ws) return;
      if (onClose) onClose();
    };
    sock.onerror = function () {};
  }

  function connect(onOpen) {
    ws = new WebSocket(wsUrl());
    attachHandlers(ws, onOpen, function () {
      if (getIdentity()) reconnect(); else showError(t('conn_lost'));
    });
  }

  // Backoff: quick first retries, then every 8s for as long as we have a
  // stored identity. The loop only stops when the server says the room or
  // player is gone (see onError), so a long outage or server restart still
  // ends with the student back in the room.
  var RECONNECT_DELAYS = [1000, 2000, 4000, 8000];

  function reconnect(immediate) {
    var id = getIdentity();
    if (!id) return;
    if (ws && ws.readyState === WebSocket.CONNECTING) return; // already trying
    if (reconnectTimeout) {
      if (!immediate) return;                                  // already scheduled
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    var delay = immediate ? 0
      : RECONNECT_DELAYS[Math.min(reconnectAttempts, RECONNECT_DELAYS.length - 1)];
    reconnectAttempts++;
    showReconnectBanner();
    reconnectTimeout = setTimeout(function () {
      reconnectTimeout = null;
      var current = getIdentity();
      if (!current) { hideReconnectBanner(); return; }
      var old = ws;
      if (old && (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING)) {
        try { old.close(); } catch (e) {}
      }
      ws = new WebSocket(wsUrl());
      attachHandlers(ws, function () {
        ws.send(JSON.stringify({
          type: 'rejoin',
          room_code: current.room,
          player_id: current.pid,
          nickname: current.nick
        }));
      }, function () {
        if (getIdentity()) reconnect();
      });
    }, delay);
  }

  // Half-open sockets: after a screen lock or a WiFi↔cellular switch the
  // browser may keep reporting OPEN on a connection the server already lost,
  // and onclose never fires. If nothing has arrived for longer than the
  // server's heartbeat we ask explicitly, and if that goes unanswered we
  // close the socket ourselves, which kicks off the normal rejoin.
  function checkLiveness() {
    if (document.visibilityState !== 'visible') return;
    if (!getIdentity()) return;
    if (!ws || ws.readyState === WebSocket.CLOSED) { reconnect(); return; }
    if (ws.readyState !== WebSocket.OPEN) return;
    var now = Date.now();
    if (probeSentAt) {
      if (now - probeSentAt > PROBE_TIMEOUT_MS) {
        probeSentAt = 0;
        // Don't wait for the browser's close handshake to time out on a dead
        // connection: start the rejoin now, the stale socket is ignored.
        try { ws.close(); } catch (e) {}
        reconnect(true);
      }
    } else if (now - lastMessageAt > PROBE_AFTER_IDLE_MS) {
      probeSentAt = now;
      send({ type: 'ping' });
    }
  }

  // Called when the page comes back to the foreground or the network returns.
  function resumeConnection() {
    if (!getIdentity()) return;
    reconnectAttempts = 0;
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Looks alive — verify it, the timers were frozen while backgrounded
      probeSentAt = Date.now();
      send({ type: 'ping' });
    } else if (!ws || ws.readyState !== WebSocket.CONNECTING) {
      reconnect(true);
    }
  }

  function showReconnectBanner() {
    var banner = document.getElementById('reconnect-banner');
    if (banner) banner.classList.add('visible');
  }

  function hideReconnectBanner() {
    var banner = document.getElementById('reconnect-banner');
    if (banner) banner.classList.remove('visible');
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  window.sendReaction = function (emoji) {
    send({ type: 'reaction', emoji: emoji });
  };

  // ── Message handling ───────────────────────────────────────────
  function handleMessage(msg) {
    switch (msg.type) {
      case 'joined':        onJoined(msg);       break;
      case 'player_update': onPlayerUpdate(msg); break;
      case 'game_start':    onGameStart();        break;
      case 'question':      onQuestion(msg);      break;
      case 'reveal':        onReveal(msg);        break;
      case 'game_end':      onGameEnd(msg);       break;
      case 'state_sync':    onStateSync(msg);     break;
      case 'game_ended':    onGameEnded(msg);       break;
      case 'rejoined':      onRejoined(msg);        break;
      case 'answer_ack':    onAnswerAck(msg);       break;
      case 'answer_rejected': onAnswerRejected(msg); break;
      case 'room_info':     onRoomInfo(msg);        break;
      case 'team_update':   onTeamUpdate(msg);      break;
      case 'kicked':        onKicked();             break;
      case 'ping':          send({ type: 'pong' }); break;
      case 'pong':          break; // liveness probe answered (see attachHandlers)
      case 'error':         onError(msg);           break;
    }
  }

  function onError(msg) {
    hideReconnectBanner();
    if (msg.message === 'room_not_found' || msg.message === 'player_not_found') {
      // Rejected rejoin: the session is gone — clear it so reconnect stops looping
      clearIdentity();
      showView('join-view');
      // Only explain if they were actually in a room on this page; a stale
      // identity from last week's quiz should just land on a clean join form
      if (wasInRoom) showError(t('session_gone'));
    } else if (msg.message === 'room_locked') {
      showError(t('room_locked'));
    } else if (msg.message === 'nickname_not_allowed') {
      showError(t('nickname_not_allowed'));
    } else if (msg.message === 'Nickname already taken') {
      showError(t('nickname_taken'));
    } else if (msg.message === 'Room not found') {
      showError(t('room_not_found'));
    } else if (msg.message === 'Game already in progress') {
      showError(t('game_in_progress'));
    } else if (msg.message === 'pick_from_roster') {
      showRosterPicker(msg.roster_names || []);
    } else {
      showError(msg.message);
    }
  }

  function onKicked() {
    clearIdentity();
    clearTimer();
    stopPointsCounter();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = 'none';
    showView('join-view');
    showError(t('kicked_msg'));
  }

  function onJoined(msg) {
    playerId = msg.player_id;
    roomCode = msg.room_code;
    storeIdentity(msg.room_code, joinedNickname, msg.player_id);
    wasInRoom = true;
    reconnectAttempts = 0;
    hideReconnectBanner();
    document.getElementById('waiting-room-code').textContent = roomCode;
    document.getElementById('my-nickname').textContent = joinedNickname;
    updateTeamBadge(msg.team, msg.team_name);
    showView('waiting-view');
    renderWaitingPlayers(msg.player_list || []);
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = 'flex';
  }

  function onPlayerUpdate(msg) {
    renderWaitingPlayers(msg.player_list || []);
  }

  function onTeamUpdate(msg) {
    updateTeamBadge(msg.team, msg.team_name);
  }

  var TEAM_COLORS = ['var(--lime)', 'var(--fire)', 'var(--violet)', 'var(--answer-b)'];

  function updateTeamBadge(team, teamName) {
    myTeamName = teamName || null;
    var badge = document.getElementById('team-badge');
    if (!badge) return;
    if (team === null || team === undefined || !teamName) {
      badge.style.display = 'none';
      return;
    }
    badge.style.display = '';
    badge.textContent = teamName;
    badge.style.borderColor = TEAM_COLORS[team % TEAM_COLORS.length];
    badge.style.color = TEAM_COLORS[team % TEAM_COLORS.length];
  }

  function onGameStart() {
    document.getElementById('waiting-pulse-text').textContent = t('game_starting');
  }

  // ── Two-phase question flow ────────────────────────────────────
  function onQuestion(msg) {
    currentQuestionId = msg.id;
    answered = false;
    controller = null;

    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    clearTimer();

    showView('question-view');

    document.getElementById('q-num-label').textContent = 'P' + msg.number + '/' + msg.total;

    document.getElementById('player-q-text').textContent = msg.text;
    var img = document.getElementById('player-q-image');
    img.onerror = function () { img.style.display = 'none'; };
    img.onclick = function () { openImageLightbox(img.src); };
    if (msg.image_url) {
      img.src = msg.image_url;
      img.style.display = 'block';
    } else {
      img.style.display = 'none';
    }

    document.getElementById('answered-overlay').classList.remove('show');

    // Remove any lingering confirm button
    var oldConfirm = document.getElementById('ms-confirm-btn');
    if (oldConfirm) oldConfirm.remove();

    startPointsCounter(msg);

    var readTime = msg.read_time || 0;
    if (readTime > 0) {
      startReadPhase(msg, readTime);
    } else {
      buildAnswerButtons(msg);
      enterAnswerPhase(msg.time_limit);
    }
  }

  function startReadPhase(msg, readTime) {
    var answersEl = document.getElementById('player-answers');
    answersEl.innerHTML = '';
    answersEl.classList.add('read-phase');

    var timerRing = document.querySelector('.player-timer-ring');
    if (timerRing) timerRing.style.visibility = 'hidden';

    var readBar = document.getElementById('player-read-bar');
    var readFill = document.getElementById('player-read-fill');
    if (readBar) readBar.classList.add('visible');
    if (readFill) {
      readFill.style.transition = 'none';
      readFill.style.width = '100%';
      readFill.getBoundingClientRect();
      readFill.style.transition = 'width ' + readTime + 's linear';
      readFill.style.width = '0%';
    }

    readTimerTimeout = setTimeout(function () {
      readTimerTimeout = null;
      if (readBar) readBar.classList.remove('visible');
      buildAnswerButtons(msg);
      enterAnswerPhase(msg.time_limit);
    }, readTime * 1000);
  }

  // ── Answer UI: built by the question type (qtypes.js) ─────────
  // The type gets a small context of shared player actions and returns a
  // controller: timeout() runs when the ring hits 0 unanswered, lock()
  // when a rejoin or a rejected answer says the question is closed.
  function buildAnswerButtons(msg) {
    var container = document.getElementById('player-answers');
    container.innerHTML = '';
    container.classList.remove('read-phase');
    controller = QLTypes.get(msg.question_type).player.build(answerContext(msg, container));
  }

  function answerContext(msg, container) {
    var qid = msg.id;
    return {
      q: msg,
      container: container,
      // Every answer message carries the question it answers
      send: function (obj) { send(Object.assign({ question_id: qid }, obj)); },
      isAnswered: function () { return answered; },
      setAnswered: function () { answered = true; },
      freezePoints: freezePoints,
      stopPointsCounter: stopPointsCounter,
      showOverlay: function () {
        document.getElementById('answered-overlay').classList.add('show');
      },
      // The confirm button sits right after the answers (id ms-confirm-btn)
      addConfirm: function (label, onClick, visible) {
        var btn = document.createElement('button');
        btn.id = 'ms-confirm-btn';
        btn.className = 'ms-confirm-btn' + (visible ? ' visible' : '');
        btn.textContent = label;
        btn.addEventListener('click', onClick);
        container.parentNode.insertBefore(btn, container.nextSibling);
        return btn;
      },
      confirmButton: function () { return document.getElementById('ms-confirm-btn'); },
      hideConfirm: function () {
        var btn = document.getElementById('ms-confirm-btn');
        if (btn) btn.style.display = 'none';
      }
    };
  }

  function enterAnswerPhase(timeLimit) {
    var timerRing = document.querySelector('.player-timer-ring');
    if (timerRing) timerRing.style.visibility = 'visible';
    startTimer(timeLimit);
  }

  // ── Reveal screen ─────────────────────────────────────────────
  function onReveal(msg) {
    clearTimer();
    stopPointsCounter();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    if (typeof msg.streak === 'number') myStreak = msg.streak;

    var qType = msg.question_type || 'mc';
    var isCorrect = msg.is_correct || false;
    var ptsEarned = msg.points_earned || 0;
    var totalScore = msg.total_score || 0;
    playerScore = totalScore;
    var rank = msg.rank || '—';
    var total = msg.total_players || '—';
    var yourAnswer = msg.your_answer;
    var didAnswer = yourAnswer !== -1 && yourAnswer !== null && yourAnswer !== undefined;

    showReveal();

    var iconEl  = document.getElementById('reveal-icon');
    var labelEl = document.getElementById('reveal-result-label');
    var popupEl = document.getElementById('score-popup');
    var totalEl = document.getElementById('reveal-total-score');
    var rankEl  = document.getElementById('rank-display');

    var flashClass = '';
    var qtype = QLTypes.get(qType);
    // Types without a right/wrong answer (poll, word cloud...) say so
    var own = qtype.player.reveal(msg);
    if (own) {
      if (iconEl) { iconEl.textContent = own.icon; iconEl.style.color = own.color; }
      labelEl.textContent = own.label;
      labelEl.className = 'reveal-label ' + own.cls;
      popupEl.textContent = own.popup;
      popupEl.className = own.popupCls;
      flashClass = own.flash || '';
    } else if (!didAnswer) {
      if (iconEl) { iconEl.textContent = '⏱'; iconEl.style.color = 'var(--answer-a)'; }
      labelEl.textContent = t('times_up');
      labelEl.className = 'reveal-label wrong';
      popupEl.textContent = '0 pts';
      popupEl.className = 'score-popup wrong';
      flashClass = 'flash-wrong';
    } else if (isCorrect) {
      if (iconEl) { iconEl.textContent = '✓'; iconEl.style.color = 'var(--answer-d)'; }
      labelEl.textContent = t('correct');
      labelEl.className = 'reveal-label correct';
      popupEl.textContent = '+' + ptsEarned + ' pts';
      popupEl.className = 'score-popup';
      flashClass = 'flash-correct';
    } else if (ptsEarned > 0) {
      // Partial (ms)
      if (iconEl) { iconEl.textContent = '~'; iconEl.style.color = 'var(--answer-c)'; }
      labelEl.textContent = t('partial');
      labelEl.className = 'reveal-label partial';
      popupEl.textContent = '+' + ptsEarned + ' pts';
      popupEl.className = 'score-popup';
    } else {
      if (iconEl) { iconEl.textContent = '✗'; iconEl.style.color = 'var(--answer-a)'; }
      labelEl.textContent = t('incorrect');
      labelEl.className = 'reveal-label wrong';
      popupEl.textContent = '0 pts';
      popupEl.className = 'score-popup wrong';
      flashClass = 'flash-wrong';
    }

    if (msg.no_points && !qtype.meta.never_scores) {
      popupEl.textContent = t('no_points');
      popupEl.className = 'score-popup wrong';
    }

    // Full-screen result flash + haptic feedback (game-controller feel)
    var revealView = document.getElementById('reveal-view');
    if (revealView && flashClass) {
      revealView.classList.remove('flash-correct', 'flash-wrong');
      revealView.offsetHeight;
      revealView.classList.add(flashClass);
      setTimeout(function () { revealView.classList.remove(flashClass); }, 900);
    }
    if (navigator.vibrate && flashClass) {
      navigator.vibrate(flashClass === 'flash-correct' ? [60] : [40, 60, 40]);
    }

    // Streak counter
    var streakEl = document.getElementById('streak-display');
    if (streakEl) {
      var streak = msg.streak || 0;
      if (streak >= 2 && isCorrect) {
        streakEl.style.display = '';
        streakEl.textContent = '🔥 ' + t('streak') + ' ×' + streak;
        streakEl.style.animation = 'none';
        streakEl.offsetHeight;
        streakEl.style.animation = '';
      } else {
        streakEl.style.display = 'none';
      }
    }

    renderBreakdown(msg);

    if (totalEl) totalEl.textContent = t('total') + ': ' + totalScore + ' pts';
    if (rankEl) {
      rankEl.innerHTML = t('rank_of')
        .replace('{rank}', rank).replace('{total}', total);
    }

    // Team standing during reveal
    var teamRankEl = document.getElementById('team-rank-display');
    if (teamRankEl) {
      var teams = msg.teams || [];
      var mine = teams.find(function (te) { return te.team === msg.your_team; });
      if (mine) {
        teamRankEl.style.display = '';
        teamRankEl.textContent = mine.name + ' — #' + mine.rank + ' · ' + mine.score + ' pts';
      } else {
        teamRankEl.style.display = 'none';
      }
    }

    // Restart animation
    popupEl.style.animation = 'none';
    popupEl.offsetHeight;
    popupEl.style.animation = '';
  }

  function onGameEnd(msg) {
    clearTimer();
    stopPointsCounter();
    clearIdentity();  // the room is over: next visit goes to the join form
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = 'none';
    showFinal();

    var lb = msg.leaderboard || [];
    var myEntry = lb.find(function (e) { return e.player_id === playerId; });

    if (myEntry) {
      document.getElementById('final-rank-num').textContent = '#' + myEntry.rank;
      document.getElementById('final-total-score').textContent = myEntry.score + ' pts';
    }

    // Team result
    var teamEl = document.getElementById('final-team');
    if (teamEl) {
      var teams = msg.teams || [];
      var mine = teams.find(function (te) { return te.team === msg.your_team; });
      if (mine) {
        teamEl.style.display = '';
        teamEl.textContent = mine.name + ' — #' + mine.rank + ' · ' + mine.score + ' pts';
      } else {
        teamEl.style.display = 'none';
      }
    }

    var list = document.getElementById('final-mini-lb-list');
    list.innerHTML = '';
    var lbSection = document.querySelector('.final-mini-lb');
    if (lbSection) lbSection.style.display = lb.length ? '' : 'none';
    lb.slice(0, 8).forEach(function (entry, i) {
      var row = document.createElement('div');
      row.className = 'mini-lb-row';
      row.style.animationDelay = (i * 80) + 'ms';
      row.innerHTML =
        '<span class="mini-lb-rank">' + entry.rank + '</span>' +
        '<span class="mini-lb-name">' + escapeHtml(entry.nickname) + '</span>' +
        '<span class="mini-lb-score">' + entry.score + '</span>';
      list.appendChild(row);
    });

    renderReview(msg.review || []);

    if (myEntry && myEntry.rank <= 3 && window.confetti) {
      confetti({ particleCount: 150, spread: 70, colors: ['#B9FF66', '#FF6B35', '#7B61FF'] });
    }
  }

  // Post-game review: each question with the player's answer vs the right one,
  // so the game ends with actual learning, not just a rank
  function renderReview(review) {
    var wrap = document.getElementById('final-review');
    var list = document.getElementById('final-review-list');
    if (!wrap || !list) return;
    list.innerHTML = '';
    if (!review.length) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';
    review.forEach(function (item) {
      var div = document.createElement('div');
      div.className = 'review-item';
      var mark = !item.scored
        ? '<span class="review-mark neutral">◦</span>'
        : item.correct
          ? '<span class="review-mark ok">✓</span>'
          : '<span class="review-mark bad">✗</span>';
      var html =
        '<div class="review-q">' + mark + ' ' + (item.index + 1) + '. ' +
          escapeHtml(item.text) + '</div>' +
        '<div class="review-a">' + t('your_answer') + ': <strong>' +
          escapeHtml(item.your_answer) + '</strong></div>';
      if (item.correct_answer && !item.correct && item.scored) {
        html += '<div class="review-a">' + t('right_answer') + ': <strong>' +
          escapeHtml(item.correct_answer) + '</strong></div>';
      }
      div.innerHTML = html;
      list.appendChild(div);
    });
  }

  // ── Reconnect state restore ───────────────────────────────────
  function onStateSync(msg) {
    hideReconnectBanner();
    reconnectAttempts = 0;
    playerId = msg.player_id;
    playerScore = msg.score || 0;
    var identity = getIdentity();
    roomCode = identity ? identity.room : roomCode;

    if (msg.state === 'lobby') {
      showView('waiting-view');
    } else if (msg.state === 'question' && msg.question) {
      var qData = {
        id: msg.question.id,
        text: msg.question.text,
        image_url: msg.question.image_url,
        options: msg.question.options,
        time_limit: msg.question.time_limit,
        read_time: msg.question.read_time,
        number: msg.question.number,
        total: msg.question.total,
        question_type: msg.question.question_type,
      };
      if (msg.phase === 'answering') {
        qData.read_time = 0;
        qData.time_limit = msg.answer_time_remaining || 0;
      } else if (msg.phase === 'reading') {
        qData.read_time = msg.read_time_remaining || 0;
      }
      onQuestion(qData);
      if (msg.already_answered) {
        answered = true;
        document.getElementById('answered-overlay').classList.add('show');
      }
    } else if (msg.state === 'reveal') {
      showReveal();
    } else if (msg.state === 'ended') {
      showGameEnded();
    }
  }

  function onRejoined(msg) {
    hideReconnectBanner();
    reconnectAttempts = 0;
    wasInRoom = true;
    playerScore = msg.score || 0;
    var identity = getIdentity();
    if (msg.player_id) {
      playerId = msg.player_id;
      if (identity) storeIdentity(identity.room, identity.nick, msg.player_id);
    }
    if (identity) {
      // A "join" that was silently upgraded to a rejoin (seat reclaimed by
      // name) never went through onJoined, so fill the waiting screen here.
      roomCode = identity.room;
      joinedNickname = identity.nick;
      var wrc = document.getElementById('waiting-room-code');
      var wnick = document.getElementById('my-nickname');
      if (wrc) wrc.textContent = roomCode;
      if (wnick) wnick.textContent = joinedNickname;
    }
    if (msg.team !== undefined) updateTeamBadge(msg.team, msg.team_name);
    if (typeof msg.streak === 'number') myStreak = msg.streak;
    var state = msg.state;
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = (state === 'ended') ? 'none' : 'flex';
    if (state === 'lobby') {
      showView('waiting-view');
      if (msg.player_list) renderWaitingPlayers(msg.player_list);
    } else if (state === 'question' && msg.question) {
      var qData = msg.question;
      if (msg.phase === 'reading') {
        qData.read_time = msg.read_time_remaining || 0;
      } else {
        qData.read_time = 0;
        qData.time_limit = msg.answer_time_remaining || 0;
      }
      onQuestion(qData);
      if (msg.already_answered) {
        answered = true;
        if (controller) controller.lock();
        document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
        var confirmBtn = document.getElementById('ms-confirm-btn');
        if (confirmBtn) confirmBtn.style.display = 'none';
        document.getElementById('answered-overlay').classList.add('show');
        freezePoints();
        if (msg.answer_ack) onAnswerAck(msg.answer_ack);
      }
    } else if (state === 'question') {
      // Fallback if the server sent no question payload
      answered = true;
      showView('question-view');
      document.getElementById('answered-overlay').classList.add('show');
    } else if (state === 'reveal' && msg.reveal) {
      onReveal(msg.reveal);
    } else if (state === 'reveal') {
      showReveal();
    } else if (state === 'ended') {
      showGameEnded();
    }
  }

  function onGameEnded(msg) {
    clearTimer();
    stopPointsCounter();
    clearIdentity();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = 'none';
    var scoreEl = document.querySelector('#game-ended-view .game-ended-score');
    if (scoreEl) scoreEl.textContent = t('your_final_score') + ': ' + playerScore + ' pts';
    showGameEnded();
  }

  // ── Live points counter ───────────────────────────────────────
  // Shows what a fully correct answer is worth right now, using the same
  // formula and time reference as the server (scoring.js). Freezes when the
  // student answers; the server's answer_ack then replaces the local
  // estimate with the value it actually recorded.
  function startPointsCounter(q) {
    stopPointsCounter();
    // Countdown ring end, same server reference as the counter (an old
    // server without answer_starts_in falls back to the local timer).
    questionEndsAt = typeof q.answer_starts_in === 'number' && q.full_time_limit
      ? performance.now() + (q.answer_starts_in + q.full_time_limit) * 1000 : null;
    currentQuestion = q;
    pointsClock = null;
    pointsFrozen = false;
    var box = document.getElementById('player-points-live');
    var ap = document.getElementById('answered-points');
    if (ap) { ap.style.display = 'none'; ap.innerHTML = ''; }
    if (!box) return;
    box.classList.remove('frozen');
    if (!window.QLScore || !QLScore.showsCounter(q)) {
      box.classList.add('hidden');
      return;
    }
    pointsClock = new QLScore.Clock(q);
    box.classList.remove('hidden');
    renderLivePoints(pointsClock.points());
    if (pointsClock.mode !== 'accuracy') {
      pointsInterval = setInterval(function () {
        renderLivePoints(pointsClock.points());
      }, 100);
    }
  }

  function stopPointsCounter() {
    if (pointsInterval) { clearInterval(pointsInterval); pointsInterval = null; }
  }

  function renderLivePoints(pts) {
    var el = document.getElementById('player-points-value');
    if (el) el.textContent = pts;
  }

  // Lock in the local estimate right away; onAnswerAck corrects it.
  function freezePoints() {
    stopPointsCounter();
    if (!pointsClock) return;
    pointsFrozen = true;
    var box = document.getElementById('player-points-live');
    if (box) box.classList.add('frozen');
    var bonus = currentQuestion && currentQuestion.streak_bonus
      ? QLScore.streakBonus(pointsClock.base, myStreak + 1) : 0;
    showIfCorrect(pointsClock.points(), bonus);
  }

  // The server owns the clock: an answer that arrived after the deadline
  // (or during the read phase) was not recorded. Say so instead of showing
  // "if correct: N pts".
  function onAnswerRejected(msg) {
    if (msg.question_id !== currentQuestionId) return;
    answered = true;
    if (controller) controller.lock();
    stopPointsCounter();
    pointsFrozen = false;
    document.querySelectorAll('.player-ans-btn, .order-arrow-btn').forEach(function (b) { b.disabled = true; });
    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) confirmBtn.style.display = 'none';
    var ap = document.getElementById('answered-points');
    if (ap) {
      ap.textContent = msg.reason === 'read_phase' ? t('answer_too_early') : t('answer_too_late');
      ap.style.display = '';
    }
    document.getElementById('answered-overlay').classList.add('show');
  }

  function onAnswerAck(msg) {
    if (!pointsFrozen || msg.question_id !== currentQuestionId) return;
    showIfCorrect(msg.max_points, msg.streak_bonus || 0);
  }

  function showIfCorrect(pts, bonus) {
    renderLivePoints(pts);
    var ap = document.getElementById('answered-points');
    if (!ap || !currentQuestion) return;
    var partial = QLScore.hasPartialCredit(currentQuestion);
    var html = (partial ? t('if_all_correct') : t('if_correct')) +
      ': <strong>' + pts + ' pts</strong>';
    if (bonus > 0) html += ' + ' + bonus + ' ' + t('streak_bonus_short') + ' 🔥';
    if (partial) {
      html += '<span class="ap-note">' +
        t('partial_rule').replace('{base}', pointsClock.base) + '</span>';
    }
    ap.innerHTML = html;
    ap.style.display = '';
  }

  // How this question's points were reached (server numbers only).
  function renderBreakdown(msg) {
    var el = document.getElementById('score-breakdown');
    if (!el) return;
    var bd = msg.breakdown;
    var qtype = QLTypes.get(msg.question_type);
    if (!bd || msg.no_points || qtype.meta.never_scores) {
      el.style.display = 'none';
      el.innerHTML = '';
      return;
    }
    function row(label, val, cls) {
      return '<div class="bd-row' + (cls ? ' ' + cls : '') + '"><span>' +
        escapeHtml(label) + '</span><span class="bd-val">' + val + '</span></div>';
    }
    var html = row(t('bd_base'), bd.base);
    var secs = typeof bd.time_taken === 'number' ? bd.time_taken.toFixed(1) : '—';
    if (bd.kind === 'speed') {
      var label = bd.speed_factor <= QLScore.SPEED_FLOOR
        ? t('bd_speed_floor').replace('{s}', secs)
        : t('bd_speed').replace('{pct}', Math.round(bd.speed_factor * 100))
                       .replace('{s}', secs);
      html += row(label, bd.question_points);
    } else if (bd.kind === 'partial' && bd.hits > 0) {
      html += row(t('bd_partial').replace('{hits}', bd.hits)
                                 .replace('{parts}', bd.parts), bd.question_points);
    } else if (bd.kind === 'full') {
      html += row(t(qtype.player.fullLabelKey), bd.question_points);
    } else if (bd.kind === 'none') {
      html += row(t('bd_none'), 0);
    } else {
      html += row(t('bd_wrong'), 0);
    }
    if (bd.streak_bonus > 0) {
      var pct = Math.min(bd.streak - 1, 5) * 10;
      html += row(t('bd_streak').replace('{n}', bd.streak).replace('{pct}', pct),
                  '+' + bd.streak_bonus);
    }
    html += row(t('bd_total'), bd.total, 'bd-total');
    el.innerHTML = html;
    el.style.display = '';
  }

  // ── Timer ─────────────────────────────────────────────────────
  function startTimer(limit) {
    var timeLeft = limit;
    var endsAt = questionEndsAt || (performance.now() + limit * 1000);
    updateTimerDisplay(timeLeft, limit);

    timerInterval = setInterval(function () {
      // Wall-clock based, not "-0.1 per tick": background tabs throttle
      // intervals, which made the ring lag the server (and the points counter).
      timeLeft = (endsAt - performance.now()) / 1000;
      if (timeLeft <= 0) {
        timeLeft = 0;
        clearInterval(timerInterval);
        timerInterval = null;
        updateTimerDisplay(0, limit);
        // Unanswered when the ring runs out: the type decides (auto-confirm
        // a pending pick, send what was typed, lock the buttons...)
        if (!answered && controller) controller.timeout();
      } else {
        updateTimerDisplay(timeLeft, limit);
      }
    }, 100);
  }

  function clearTimer() {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
  }

  function updateTimerDisplay(remaining, total) {
    if (total <= 0) return;
    var pct = remaining / total;
    var offset = CIRCUMFERENCE * (1 - pct);

    var ring = document.getElementById('player-timer-progress');
    if (ring) ring.style.strokeDashoffset = offset;

    var num = document.getElementById('player-timer-num');
    if (num) {
      num.textContent = Math.ceil(remaining);
      num.classList.toggle('urgent', remaining <= 5 && remaining > 0);
    }
  }

  // ── Waiting room ───────────────────────────────────────────────
  function renderWaitingPlayers(players) {
    var list  = document.getElementById('waiting-player-list');
    var count = document.getElementById('waiting-player-count');
    if (!list) return;
    list.innerHTML = '';
    players.forEach(function (p) {
      var chip = document.createElement('div');
      chip.className = 'waiting-player-chip' + (p.connected === false ? ' offline' : '');
      chip.textContent = p.nickname;
      list.appendChild(chip);
    });
    if (count) count.textContent = players.length;
  }

  // ── Join flow ──────────────────────────────────────────────────
  // joinGame first probes the room (room_info): locked rooms get a clear
  // message, and rooms with a class attached show the roster picker instead
  // of the free nickname input.
  window.joinGame = function () {
    var rc = document.getElementById('room-input').value.trim().toUpperCase();
    if (!rc || rc.length !== 6) { showError(t('enter_room_code')); return; }

    hideError();
    if (ws && ws.readyState === WebSocket.OPEN) {
      send({ type: 'room_info', room_code: rc });
    } else {
      connect(function () { send({ type: 'room_info', room_code: rc }); });
    }
  };

  function doJoin(nickname) {
    var rc = document.getElementById('room-input').value.trim().toUpperCase();
    joinedNickname = nickname;
    hideError();
    if (ws && ws.readyState === WebSocket.OPEN) {
      send({ type: 'join', room_code: rc, nickname: nickname });
    } else {
      connect(function () { send({ type: 'join', room_code: rc, nickname: nickname }); });
    }
  }

  function onRoomInfo(msg) {
    if (!msg.exists) { showError(t('room_not_found')); return; }
    if (msg.locked)  { showError(t('room_locked')); return; }
    if (msg.has_roster) {
      showRosterPicker(msg.roster_names || []);
      return;
    }
    rosterMode = false;
    var nick = document.getElementById('nickname-input').value.trim();
    if (!nick) {
      showError(t('enter_nickname'));
      document.getElementById('nickname-input').focus();
      return;
    }
    doJoin(nick);
  }

  function showRosterPicker(names) {
    rosterMode = true;
    var nickField = document.getElementById('nickname-field');
    var rosterField = document.getElementById('roster-field');
    var list = document.getElementById('roster-list');
    var joinBtn = document.getElementById('join-btn');
    if (nickField) nickField.style.display = 'none';
    if (joinBtn) joinBtn.style.display = 'none';
    if (!rosterField || !list) return;
    rosterField.style.display = '';
    list.innerHTML = '';
    names.forEach(function (name) {
      var b = document.createElement('button');
      b.className = 'roster-name-btn';
      b.textContent = name;
      b.addEventListener('click', function () { doJoin(name); });
      list.appendChild(b);
    });
  }

  // ── Errors ─────────────────────────────────────────────────────
  function showError(msg) {
    var el = document.getElementById('join-error');
    if (el) { el.textContent = msg; el.classList.add('show'); }
  }

  function hideError() {
    var el = document.getElementById('join-error');
    if (el) el.classList.remove('show');
  }

  // ── Util ───────────────────────────────────────────────────────
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── Init ───────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    var ring = document.getElementById('player-timer-progress');
    if (ring) {
      ring.style.strokeDasharray = CIRCUMFERENCE;
      ring.style.strokeDashoffset = '0';
    }

    // Auto-fill room from URL
    var params = new URLSearchParams(location.search);
    var room = params.get('room');
    if (room) {
      var roomInput = document.getElementById('room-input');
      if (roomInput) roomInput.value = room.toUpperCase();
      setTimeout(function () {
        var nick = document.getElementById('nickname-input');
        if (nick) nick.focus();
      }, 100);
    }

    // Enter key on join form
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        var joinView = document.getElementById('join-view');
        if (joinView && joinView.classList.contains('active')) joinGame();
      }
    });

    // A QR for a *different* room than the one we're remembered in means a
    // new session: drop the old identity so we show the join form.
    var remembered = getIdentity();
    if (room && remembered && remembered.room !== room.toUpperCase()) {
      clearIdentity();
      remembered = null;
    }

    // Wake-ups: screen unlock / tab foregrounded, network back, page
    // restored from the back-forward cache. Each one re-validates the
    // socket (probe) or starts an immediate rejoin.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') resumeConnection();
    });
    window.addEventListener('online', function () { resumeConnection(); });
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) resumeConnection();
    });
    window.addEventListener('focus', function () { resumeConnection(); });
    setInterval(checkLiveness, 5000);

    // After a page reload (or a recreated tab), rejoin automatically instead
    // of showing the join form (the join path rejects rooms in progress)
    if (remembered) {
      reconnectAttempts = 0;
      reconnect(true);
    }
  });

})();


// ── Question image: tap to view full-screen (pinch to zoom stays enabled) ──
function openImageLightbox(src) {
  var box = document.getElementById('img-lightbox');
  var big = document.getElementById('img-lightbox-img');
  if (!box || !big || !src) return;
  big.src = src;
  box.classList.add('show');
}

function closeImageLightbox() {
  var box = document.getElementById('img-lightbox');
  if (box) box.classList.remove('show');
}
