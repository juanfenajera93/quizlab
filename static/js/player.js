(function () {
  'use strict';

  var CIRCUMFERENCE = 2 * Math.PI * 45;

  var t = window.qlT || function (k, fb) { return fb || k; };

  var ws = null;
  var playerId = null;
  var roomCode = null;
  var currentQuestionId = null;
  var currentQuestionType = 'mc';
  var answered = false;
  var msConfirmed = false;
  var currentOrdering = [];   // for order type
  var msSelections = [];      // for ms type
  var mcSelectedIdx = null;   // for mc/tf/poll pre-confirm selection
  var timerInterval = null;
  var readTimerTimeout = null;
  var playerScore = 0;
  var reconnectAttempts = 0;
  var reconnectTimeout = null;
  var joinedNickname = '';    // what we actually joined with (input or roster pick)
  var rosterMode = false;
  var myTeamName = null;
  var wasInRoom = false;      // joined/rejoined at least once on this page load

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
    currentQuestionType = msg.question_type || 'mc';
    answered = false;
    msConfirmed = false;
    msSelections = [];
    currentOrdering = [];
    mcSelectedIdx = null;

    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    clearTimer();

    showView('question-view');

    document.getElementById('q-num-label').textContent = 'P' + msg.number + '/' + msg.total;

    document.getElementById('player-q-text').textContent = msg.text;
    var img = document.getElementById('player-q-image');
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

  // ── Area 3E + 5 + 7: Build answer buttons per type ────────────
  function buildAnswerButtons(msg) {
    var container = document.getElementById('player-answers');
    container.innerHTML = '';
    container.classList.remove('read-phase');

    var qType = msg.question_type || 'mc';
    var options = msg.options || [];
    var letters = ['A', 'B', 'C', 'D', 'E', 'F'];

    if (qType === 'tf') {
      // Two large full-width buttons — tap selects, confirm submits
      container.className = 'player-answers count-2';
      ['Verdadero', 'Falso'].forEach(function (label, i) {
        var btn = document.createElement('button');
        btn.className = 'player-ans-btn slide-in';
        btn.dataset.idx = i;
        btn.style.animationDelay = (i * 80) + 'ms';
        btn.innerHTML =
          '<span class="btn-letter">' + (i === 0 ? 'V' : 'F') + '</span>' +
          '<span>' + escapeHtml(label) + '</span>';
        btn.addEventListener('click', (function (idx) {
          return function () { selectMcOption(idx); };
        })(i));
        container.appendChild(btn);
      });
      var confirmBtn = document.createElement('button');
      confirmBtn.id = 'ms-confirm-btn';
      confirmBtn.className = 'ms-confirm-btn';
      confirmBtn.textContent = t('confirm');
      confirmBtn.addEventListener('click', confirmMcAnswer);
      container.parentNode.insertBefore(confirmBtn, container.nextSibling);

    } else if (qType === 'ms') {
      // Multi-select: tap toggles, confirm button
      container.className = 'player-answers count-' + options.length;
      options.forEach(function (opt, i) {
        var btn = document.createElement('button');
        btn.className = 'player-ans-btn slide-in';
        btn.dataset.idx = i;
        btn.style.animationDelay = (i * 80) + 'ms';
        btn.innerHTML =
          '<span class="btn-letter">' + (letters[i] || String(i + 1)) + '</span>' +
          '<span>' + escapeHtml(String(opt)) + '</span>';
        btn.addEventListener('click', (function (idx) {
          return function () { toggleMsSelection(idx); };
        })(i));
        container.appendChild(btn);
      });

      // Confirm button (hidden until ≥1 selection)
      var confirmBtn = document.createElement('button');
      confirmBtn.id = 'ms-confirm-btn';
      confirmBtn.className = 'ms-confirm-btn';
      confirmBtn.textContent = t('confirm');
      confirmBtn.addEventListener('click', confirmMsAnswer);
      // Insert after the answers container
      container.parentNode.insertBefore(confirmBtn, container.nextSibling);

    } else if (qType === 'order') {
      // Order type: vertical list with up/down arrows, explicit confirm
      currentOrdering = options.map(function (_, i) { return i; });
      container.className = 'player-answers'; // not grid for order
      container.style.display = 'block';
      renderOrderList(container, options);
      var confirmBtn = document.createElement('button');
      confirmBtn.id = 'ms-confirm-btn';
      confirmBtn.className = 'ms-confirm-btn visible';
      confirmBtn.textContent = t('confirm_order');
      confirmBtn.addEventListener('click', confirmOrderAnswer);
      container.parentNode.insertBefore(confirmBtn, container.nextSibling);

    } else if (qType === 'wordcloud') {
      container.className = 'player-answers';
      var wrapEl = document.createElement('div');
      wrapEl.className = 'wc-input-area';
      var inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.id = 'wc-input';
      inputEl.className = 'wc-input';
      inputEl.maxLength = 50;
      inputEl.placeholder = t('write_answer');
      inputEl.autocomplete = 'off';
      var charCount = document.createElement('div');
      charCount.className = 'wc-char-count';
      charCount.textContent = '0 / 50';
      inputEl.addEventListener('input', function () {
        charCount.textContent = inputEl.value.length + ' / 50';
      });
      inputEl.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); submitWordcloud(); }
      });
      wrapEl.appendChild(inputEl);
      wrapEl.appendChild(charCount);
      container.appendChild(wrapEl);
      var wcBtn = document.createElement('button');
      wcBtn.id = 'ms-confirm-btn';
      wcBtn.className = 'ms-confirm-btn';
      wcBtn.textContent = t('confirm').toUpperCase();
      wcBtn.addEventListener('click', submitWordcloud);
      container.parentNode.insertBefore(wcBtn, container.nextSibling);
      // Focus the input after read phase
      setTimeout(function () { if (inputEl) inputEl.focus(); }, 50);

    } else {
      // mc or poll: tap selects, confirm submits
      container.className = 'player-answers count-' + options.length;
      options.forEach(function (opt, i) {
        var btn = document.createElement('button');
        btn.className = 'player-ans-btn slide-in';
        btn.dataset.idx = i;
        btn.style.animationDelay = (i * 80) + 'ms';
        btn.innerHTML =
          '<span class="btn-letter">' + (letters[i] || String(i + 1)) + '</span>' +
          '<span>' + escapeHtml(String(opt)) + '</span>';
        btn.addEventListener('click', (function (idx) {
          return function () { selectMcOption(idx); };
        })(i));
        container.appendChild(btn);
      });
      var confirmBtn = document.createElement('button');
      confirmBtn.id = 'ms-confirm-btn';
      confirmBtn.className = 'ms-confirm-btn';
      confirmBtn.textContent = t('confirm');
      confirmBtn.addEventListener('click', confirmMcAnswer);
      container.parentNode.insertBefore(confirmBtn, container.nextSibling);
    }
  }

  // ── Order type rendering ──────────────────────────────────────
  function renderOrderList(container, options) {
    container.innerHTML = '';
    var list = document.createElement('div');
    list.className = 'order-list';

    currentOrdering.forEach(function (optIdx, position) {
      var item = document.createElement('div');
      item.className = 'order-item';
      item.dataset.position = position;

      var arrows = document.createElement('div');
      arrows.className = 'order-arrows';

      var upBtn = document.createElement('button');
      upBtn.className = 'order-arrow-btn';
      upBtn.textContent = '↑';
      upBtn.disabled = (position === 0);
      upBtn.addEventListener('click', (function (pos) {
        return function () { swapOrderItems(pos, pos - 1, container, options); };
      })(position));

      var downBtn = document.createElement('button');
      downBtn.className = 'order-arrow-btn';
      downBtn.textContent = '↓';
      downBtn.disabled = (position === currentOrdering.length - 1);
      downBtn.addEventListener('click', (function (pos) {
        return function () { swapOrderItems(pos, pos + 1, container, options); };
      })(position));

      arrows.appendChild(upBtn);
      arrows.appendChild(downBtn);

      var text = document.createElement('span');
      text.className = 'order-item-text';
      text.textContent = String(options[optIdx]);

      item.appendChild(arrows);
      item.appendChild(text);
      list.appendChild(item);
    });

    container.appendChild(list);

    // Send current ordering immediately
    sendOrderUpdate();
  }

  function swapOrderItems(posA, posB, container, options) {
    if (answered) return;
    var tmp = currentOrdering[posA];
    currentOrdering[posA] = currentOrdering[posB];
    currentOrdering[posB] = tmp;
    renderOrderList(container, options);
    sendOrderUpdate();
  }

  function sendOrderUpdate() {
    send({
      type: 'order_update',
      question_id: currentQuestionId,
      ordering: currentOrdering.slice()
    });
  }

  // ── ms selection toggle ────────────────────────────────────────
  function toggleMsSelection(idx) {
    if (msConfirmed) return;
    var pos = msSelections.indexOf(idx);
    if (pos === -1) {
      msSelections.push(idx);
    } else {
      msSelections.splice(pos, 1);
    }

    // Update button visual
    var btns = document.querySelectorAll('.player-ans-btn');
    btns.forEach(function (btn) {
      var btnIdx = parseInt(btn.dataset.idx);
      if (msSelections.indexOf(btnIdx) !== -1) {
        btn.classList.add('selected-ms');
      } else {
        btn.classList.remove('selected-ms');
      }
    });

    // Show/hide confirm button
    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) {
      if (msSelections.length > 0) {
        confirmBtn.classList.add('visible');
      } else {
        confirmBtn.classList.remove('visible');
      }
    }

    // Send live selection to server
    send({
      type: 'selection',
      question_id: currentQuestionId,
      selections: msSelections.slice()
    });
  }

  function confirmMsAnswer() {
    if (msConfirmed || msSelections.length === 0) return;
    msConfirmed = true;
    answered = true;

    send({
      type: 'confirm',
      question_id: currentQuestionId
    });

    // Area 4: Do NOT stop timer — keep it running
    // Dim buttons, show overlay
    var btns = document.querySelectorAll('.player-ans-btn');
    btns.forEach(function (btn) {
      btn.disabled = true;
      if (msSelections.indexOf(parseInt(btn.dataset.idx)) === -1) {
        btn.classList.add('dimmed');
      }
    });

    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) confirmBtn.style.display = 'none';

    document.getElementById('answered-overlay').classList.add('show');
  }

  function selectMcOption(idx) {
    if (answered) return;
    mcSelectedIdx = idx;
    document.querySelectorAll('.player-ans-btn').forEach(function (btn) {
      if (parseInt(btn.dataset.idx) === idx) {
        btn.classList.add('selected-mc');
      } else {
        btn.classList.remove('selected-mc');
      }
    });
    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) confirmBtn.classList.add('visible');
  }

  function confirmMcAnswer() {
    if (answered || mcSelectedIdx === null) return;
    document.querySelectorAll('.player-ans-btn').forEach(function (btn) {
      btn.classList.remove('selected-mc');
    });
    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) confirmBtn.style.display = 'none';
    submitAnswer(mcSelectedIdx);
  }

  function confirmOrderAnswer() {
    if (answered) return;
    answered = true;
    sendOrderUpdate();
    document.querySelectorAll('.order-arrow-btn').forEach(function (b) { b.disabled = true; });
    var confirmBtn = document.getElementById('ms-confirm-btn');
    if (confirmBtn) confirmBtn.style.display = 'none';
    document.getElementById('answered-overlay').classList.add('show');
  }

  function submitWordcloud() {
    if (answered) return;
    var inputEl = document.getElementById('wc-input');
    if (!inputEl) return;
    var text = inputEl.value.trim().slice(0, 50);
    if (!text) return;
    answered = true;
    inputEl.disabled = true;
    var btn = document.getElementById('ms-confirm-btn');
    if (btn) btn.style.display = 'none';
    send({ type: 'wordcloud_answer', question_id: currentQuestionId, text: text });
    document.getElementById('answered-overlay').classList.add('show');
  }

  function enterAnswerPhase(timeLimit) {
    var timerRing = document.querySelector('.player-timer-ring');
    if (timerRing) timerRing.style.visibility = 'visible';
    startTimer(timeLimit);
  }

  // ── Reveal screen ─────────────────────────────────────────────
  function onReveal(msg) {
    clearTimer();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }

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
    if (qType === 'wordcloud') {
      var yourText = msg.your_text || '';
      if (iconEl) { iconEl.textContent = '☁'; iconEl.style.color = 'var(--violet)'; }
      labelEl.textContent = yourText ? t('sent') : t('no_answer');
      labelEl.className = 'reveal-label ' + (yourText ? 'poll' : 'wrong');
      popupEl.textContent = t('no_points');
      popupEl.className = 'score-popup wrong';
    } else if (qType === 'poll') {
      // Poll: everyone who answered gets points
      if (iconEl) { iconEl.textContent = '✓'; iconEl.style.color = 'var(--lime)'; }
      labelEl.textContent = t('thanks');
      labelEl.className = 'reveal-label poll';
      popupEl.textContent = didAnswer ? '+' + ptsEarned + ' pts' : '0 pts';
      popupEl.className = didAnswer ? 'score-popup' : 'score-popup wrong';
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

    if (msg.no_points && qType !== 'wordcloud') {
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
        msConfirmed = true;
        document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
        var confirmBtn = document.getElementById('ms-confirm-btn');
        if (confirmBtn) confirmBtn.style.display = 'none';
        document.getElementById('answered-overlay').classList.add('show');
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
    clearIdentity();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    var emojiBar = document.getElementById('emoji-bar');
    if (emojiBar) emojiBar.style.display = 'none';
    var scoreEl = document.querySelector('#game-ended-view .game-ended-score');
    if (scoreEl) scoreEl.textContent = t('your_final_score') + ': ' + playerScore + ' pts';
    showGameEnded();
  }

  // ── Answer submission ──────────────────────────────────────────
  function submitAnswer(idx) {
    if (answered) return;
    answered = true;

    send({
      type: 'answer',
      question_id: currentQuestionId,
      answer_index: idx,
      client_timestamp: Date.now()
    });

    // Area 4: Do NOT stop timer after answering — keep it running
    var btns = document.querySelectorAll('.player-ans-btn');
    btns.forEach(function (btn) {
      btn.disabled = true;
      if (parseInt(btn.dataset.idx) !== idx) btn.classList.add('dimmed');
    });

    document.getElementById('answered-overlay').classList.add('show');
    // Timer keeps running — clearTimer() only called in onReveal()
  }

  // ── Timer ─────────────────────────────────────────────────────
  function startTimer(limit) {
    var timeLeft = limit;
    updateTimerDisplay(timeLeft, limit);

    timerInterval = setInterval(function () {
      timeLeft -= 0.1;
      if (timeLeft <= 0) {
        timeLeft = 0;
        clearInterval(timerInterval);
        timerInterval = null;
        updateTimerDisplay(0, limit);
        if (!answered) {
          if (currentQuestionType === 'wordcloud') {
            var wci = document.getElementById('wc-input');
            var wct = wci ? wci.value.trim().slice(0, 50) : '';
            if (wct) {
              answered = true;
              if (wci) wci.disabled = true;
              send({ type: 'wordcloud_answer', question_id: currentQuestionId, text: wct });
            }
            document.getElementById('answered-overlay').classList.add('show');
            var wcConfirm = document.getElementById('ms-confirm-btn');
            if (wcConfirm) wcConfirm.style.display = 'none';
          } else if (currentQuestionType === 'order') {
            answered = true;
            sendOrderUpdate();
            document.getElementById('answered-overlay').classList.add('show');
            document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
            var confirmBtn = document.getElementById('ms-confirm-btn');
            if (confirmBtn) confirmBtn.style.display = 'none';
          } else if (mcSelectedIdx !== null) {
            // mc/tf/poll: player selected but hadn't pressed confirm — auto-confirm now
            document.querySelectorAll('.player-ans-btn').forEach(function (btn) {
              btn.classList.remove('selected-mc');
            });
            var cb = document.getElementById('ms-confirm-btn');
            if (cb) cb.style.display = 'none';
            submitAnswer(mcSelectedIdx);
          } else {
            // ms without confirm, or mc/tf/poll with no selection
            document.getElementById('answered-overlay').classList.add('show');
            document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
            var confirmBtn = document.getElementById('ms-confirm-btn');
            if (confirmBtn) confirmBtn.style.display = 'none';
          }
        }
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
