(function () {
  'use strict';

  var CIRCUMFERENCE = 2 * Math.PI * 45;

  var ws = null;
  var playerId = null;
  var roomCode = null;
  var currentQuestionId = null;
  var currentQuestionType = 'mc';
  var answered = false;
  var msConfirmed = false;
  var currentOrdering = [];   // for order type
  var msSelections = [];      // for ms type
  var timerInterval = null;
  var readTimerTimeout = null;

  // ── Views ──────────────────────────────────────────────────────
  function showView(id) {
    document.querySelectorAll('.pview').forEach(function (v) {
      v.classList.remove('active');
    });
    document.getElementById('reveal-view').classList.remove('active');
    document.getElementById('final-view').classList.remove('active');
    var el = document.getElementById(id);
    if (el) el.classList.add('active');
  }

  function showReveal() {
    document.querySelectorAll('.pview').forEach(function (v) { v.classList.remove('active'); });
    document.getElementById('reveal-view').classList.add('active');
    document.getElementById('final-view').classList.remove('active');
  }

  function showFinal() {
    document.querySelectorAll('.pview').forEach(function (v) { v.classList.remove('active'); });
    document.getElementById('reveal-view').classList.remove('active');
    document.getElementById('final-view').classList.add('active');
  }

  // ── WebSocket ──────────────────────────────────────────────────
  function connect(onOpen) {
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(proto + '://' + location.host + '/ws/player');
    ws.onopen = function () { if (onOpen) onOpen(); };
    ws.onmessage = function (e) {
      try { handleMessage(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    ws.onclose = function () { showError('Conexión perdida. Recarga la página.'); };
    ws.onerror = function () { showError('Error de conexión.'); };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  // ── Message handling ───────────────────────────────────────────
  function handleMessage(msg) {
    switch (msg.type) {
      case 'joined':        onJoined(msg);       break;
      case 'player_update': onPlayerUpdate(msg); break;
      case 'game_start':    onGameStart();        break;
      case 'question':      onQuestion(msg);      break;
      case 'reveal':        onReveal(msg);        break;
      case 'game_end':      onGameEnd(msg);       break;
      case 'error':         showError(msg.message); break;
    }
  }

  function onJoined(msg) {
    playerId = msg.player_id;
    roomCode = msg.room_code;
    document.getElementById('waiting-room-code').textContent = roomCode;
    document.getElementById('my-nickname').textContent =
      document.getElementById('nickname-input').value.trim();
    showView('waiting-view');
    renderWaitingPlayers(msg.player_list || []);
  }

  function onPlayerUpdate(msg) {
    renderWaitingPlayers(msg.player_list || []);
  }

  function onGameStart() {
    document.getElementById('waiting-pulse-text').textContent = '¡El juego comienza!';
  }

  // ── Two-phase question flow ────────────────────────────────────
  function onQuestion(msg) {
    currentQuestionId = msg.id;
    currentQuestionType = msg.question_type || 'mc';
    answered = false;
    msConfirmed = false;
    msSelections = [];
    currentOrdering = [];

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
      // Two large full-width buttons
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
          return function () { submitAnswer(idx); };
        })(i));
        container.appendChild(btn);
      });

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
      confirmBtn.textContent = 'Confirmar';
      confirmBtn.addEventListener('click', confirmMsAnswer);
      // Insert after the answers container
      container.parentNode.insertBefore(confirmBtn, container.nextSibling);

    } else if (qType === 'order') {
      // Order type: vertical list with up/down arrows
      currentOrdering = options.map(function (_, i) { return i; });
      container.className = 'player-answers'; // not grid for order
      container.style.display = 'block';
      renderOrderList(container, options);

    } else {
      // mc or poll: standard grid
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
          return function () { submitAnswer(idx); };
        })(i));
        container.appendChild(btn);
      });
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

    if (qType === 'poll') {
      // Poll: everyone who answered gets points
      if (iconEl) { iconEl.textContent = '✓'; iconEl.style.color = 'var(--lime)'; }
      labelEl.textContent = '¡Gracias!';
      labelEl.className = 'reveal-label poll';
      popupEl.textContent = didAnswer ? '+' + ptsEarned + ' pts' : '0 pts';
      popupEl.className = didAnswer ? 'score-popup' : 'score-popup wrong';
    } else if (!didAnswer) {
      if (iconEl) { iconEl.textContent = '⏱'; iconEl.style.color = 'var(--answer-a)'; }
      labelEl.textContent = '¡Tiempo!';
      labelEl.className = 'reveal-label wrong';
      popupEl.textContent = '0 pts';
      popupEl.className = 'score-popup wrong';
    } else if (isCorrect) {
      if (iconEl) { iconEl.textContent = '✓'; iconEl.style.color = 'var(--answer-d)'; }
      labelEl.textContent = '¡Correcto!';
      labelEl.className = 'reveal-label correct';
      popupEl.textContent = '+' + ptsEarned + ' pts';
      popupEl.className = 'score-popup';
    } else if (ptsEarned > 0) {
      // Partial (ms)
      if (iconEl) { iconEl.textContent = '~'; iconEl.style.color = 'var(--answer-c)'; }
      labelEl.textContent = '¡Parcial!';
      labelEl.className = 'reveal-label partial';
      popupEl.textContent = '+' + ptsEarned + ' pts';
      popupEl.className = 'score-popup';
    } else {
      if (iconEl) { iconEl.textContent = '✗'; iconEl.style.color = 'var(--answer-a)'; }
      labelEl.textContent = '¡Incorrecto!';
      labelEl.className = 'reveal-label wrong';
      popupEl.textContent = '0 pts';
      popupEl.className = 'score-popup wrong';
    }

    if (totalEl) totalEl.textContent = 'Total: ' + totalScore + ' pts';
    if (rankEl)  rankEl.innerHTML = 'Estás <strong>#' + rank + '</strong> de ' + total + ' jugadores';

    // Restart animation
    popupEl.style.animation = 'none';
    popupEl.offsetHeight;
    popupEl.style.animation = '';
  }

  function onGameEnd(msg) {
    clearTimer();
    showFinal();

    var lb = msg.leaderboard || [];
    var myEntry = lb.find(function (e) { return e.player_id === playerId; });

    if (myEntry) {
      document.getElementById('final-rank-num').textContent = '#' + myEntry.rank;
      document.getElementById('final-total-score').textContent = myEntry.score + ' pts';
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

    if (myEntry && myEntry.rank <= 3 && window.confetti) {
      confetti({ particleCount: 150, spread: 70, colors: ['#B9FF66', '#FF6B35', '#7B61FF'] });
    }
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
          // For order type: auto-submit current ordering
          if (currentQuestionType === 'order') {
            answered = true;
            sendOrderUpdate();
          }
          document.getElementById('answered-overlay').classList.add('show');
          document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
          var confirmBtn = document.getElementById('ms-confirm-btn');
          if (confirmBtn) confirmBtn.style.display = 'none';
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
      chip.className = 'waiting-player-chip';
      chip.textContent = p.nickname;
      list.appendChild(chip);
    });
    if (count) count.textContent = players.length;
  }

  // ── Join flow ──────────────────────────────────────────────────
  window.joinGame = function () {
    var rc   = document.getElementById('room-input').value.trim().toUpperCase();
    var nick = document.getElementById('nickname-input').value.trim();

    if (!rc || rc.length !== 6) { showError('Ingresa un código de sala de 6 caracteres'); return; }
    if (!nick)                  { showError('Ingresa un apodo'); return; }

    hideError();
    if (ws && ws.readyState === WebSocket.OPEN) {
      send({ type: 'join', room_code: rc, nickname: nick });
    } else {
      connect(function () { send({ type: 'join', room_code: rc, nickname: nick }); });
    }
  };

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
  });

})();
