(function () {
  'use strict';

  var CIRCUMFERENCE = 2 * Math.PI * 45; // ≈ 282.74

  var t = window.qlT || function (k, fb) { return fb || k; };

  var ws = null;
  var roomCode = null;
  var quizId = window.QUIZ_ID;
  var currentQuestion = null;
  var timerInterval = null;
  var readTimerTimeout = null;
  var timeLeft = 0;
  var revealSent = false;
  var inReadPhase = false;
  var answerCounts = [];
  var answersRevealed = false;
  var latestCounts = [];
  var RECONNECT_DELAYS = [2000, 4000, 8000, 8000, 8000];
  var reconnectAttempts = 0;
  var reconnectTimeout = null;
  var rejoinPending = false;
  var sessionEnded = false;
  var roomLocked = false;
  var teamCount = 0;
  var prevLbRects = {};        // player_id -> {top, rank} from the previous leaderboard render (FLIP)
  var soundEnabled = localStorage.getItem('quizlab-sound') === 'on';
  var audioCtx = null;
  var lastTickSecond = -1;
  var renderedWordCount = 0;   // incremental word-feed rendering
  var revealTimeouts = [];     // staged reveal choreography timers

  var TEAM_COLORS = ['var(--lime)', 'var(--fire)', 'var(--violet)', 'var(--answer-b)'];

  // ── Sound (WebAudio ticks — no assets needed) ──────────────────
  function ensureAudio() {
    if (!audioCtx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (AC) audioCtx = new AC();
    }
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
  }

  function playTone(freq, duration, volume) {
    if (!soundEnabled || !audioCtx) return;
    var osc = audioCtx.createOscillator();
    var gain = audioCtx.createGain();
    osc.type = 'square';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(volume || 0.06, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + duration);
  }

  function playTick()   { playTone(880, 0.07); }
  function playReveal() { playTone(523, 0.12, 0.08); setTimeout(function () { playTone(784, 0.18, 0.08); }, 120); }

  // ── Views ──────────────────────────────────────────────────────
  function showView(id) {
    document.querySelectorAll('.view').forEach(function (v) {
      v.classList.remove('active');
    });
    var el = document.getElementById(id);
    if (el) el.classList.add('active');
  }

  // ── WebSocket ──────────────────────────────────────────────────
  function connect() {
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var url = proto + '://' + location.host + '/ws/host/' + quizId;
    ws = new WebSocket(url);
    ws.onopen = function () {
      console.log('Host WS connected');
      // Rejoin a live session for this quiz if we have one (page reload or
      // reconnect after a drop); otherwise create a fresh room.
      var storedRoom = sessionStorage.getItem('quizlab_host_room');
      var storedQuiz = sessionStorage.getItem('quizlab_host_quiz');
      if (storedRoom && String(storedQuiz) === String(quizId)) {
        rejoinPending = true;
        send({ type: 'host_rejoin', room_code: storedRoom });
      } else {
        send({ type: 'create_session' });
      }
    };
    ws.onmessage = function (e) {
      try { handleMessage(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    ws.onclose = function () {
      console.log('Host WS closed');
      scheduleReconnect();
    };
    ws.onerror = function (e) { console.error('Host WS error', e); };
  }

  function scheduleReconnect() {
    if (sessionEnded || reconnectAttempts >= 5) return;
    var delay = RECONNECT_DELAYS[reconnectAttempts] || 8000;
    reconnectAttempts++;
    if (reconnectTimeout) clearTimeout(reconnectTimeout);
    reconnectTimeout = setTimeout(function () {
      reconnectTimeout = null;
      connect();
    }, delay);
  }

  function clearHostSession() {
    sessionStorage.removeItem('quizlab_host_room');
    sessionStorage.removeItem('quizlab_host_quiz');
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  // ── Message handling ───────────────────────────────────────────
  function handleMessage(msg) {
    switch (msg.type) {
      case 'session_created': onSessionCreated(msg); break;
      case 'host_rejoined':   onHostRejoined(msg);   break;
      case 'player_update':   onPlayerUpdate(msg);   break;
      case 'question':        onQuestion(msg);        break;
      case 'answer_counts':   onAnswerCounts(msg);    break;
      case 'reveal':          onReveal(msg);          break;
      case 'game_end':        onGameEnd(msg);         break;
      case 'ping':              send({ type: 'pong' }); break;
      case 'reaction':          onReaction(msg);        break;
      case 'wordcloud_update':  onWordcloudUpdate(msg); break;
      case 'room_locked':       onRoomLocked(msg);      break;
      case 'class_set':         break; // ack only
      case 'error':           onError(msg);           break;
    }
  }

  function onRoomLocked(msg) {
    roomLocked = !!msg.locked;
    updateLockButton();
  }

  function updateLockButton() {
    var btn = document.getElementById('lock-btn');
    if (!btn) return;
    btn.textContent = roomLocked ? '🔒' : '🔓';
    btn.title = roomLocked ? t('unlock_room') : t('lock_room');
    btn.style.borderColor = roomLocked ? 'var(--fire)' : '';
    btn.style.color = roomLocked ? 'var(--fire)' : '';
  }

  function onError(msg) {
    if (rejoinPending) {
      // The stored room is gone (server restart, session expired): fall back
      // to creating a brand-new session on the same socket.
      rejoinPending = false;
      clearHostSession();
      send({ type: 'create_session' });
      return;
    }
    alert('Error: ' + msg.message);
  }

  function onHostRejoined(msg) {
    rejoinPending = false;
    reconnectAttempts = 0;
    roomCode = msg.room_code;
    sessionStorage.setItem('quizlab_host_room', roomCode);
    sessionStorage.setItem('quizlab_host_quiz', String(quizId));

    // Restore session settings state
    roomLocked = !!msg.locked;
    updateLockButton();
    teamCount = msg.team_count || 0;
    var teamSel = document.getElementById('team-select');
    if (teamSel) teamSel.value = String(teamCount);
    if (msg.class_id) {
      var classSel = document.getElementById('class-select');
      if (classSel) classSel.dataset.pendingValue = String(msg.class_id);
      if (classSel && classSel.querySelector('option[value="' + msg.class_id + '"]')) {
        classSel.value = String(msg.class_id);
      }
    }
    if (msg.teams) renderTeamStandings(msg.teams);

    if (msg.state === 'lobby' || !msg.question) {
      onSessionCreated({ room_code: msg.room_code, quiz_name: msg.quiz_name });
      onPlayerUpdate({
        players: msg.player_list || [],
        count: (msg.player_list || []).length
      });
      return;
    }

    var qd = msg.question;
    if (msg.state === 'question') {
      if (msg.phase === 'reading') {
        qd.read_time = msg.read_time_remaining || 0;
      } else {
        qd.read_time = 0;
        qd.time_limit = msg.answer_time_remaining || 0;
      }
      onQuestion(qd);
      latestCounts = msg.answer_counts || [];
      if (qd.question_type === 'wordcloud' && msg.words) {
        onWordcloudUpdate({ words: msg.words });
      }
    } else if (msg.state === 'reveal') {
      qd.read_time = 0;
      onQuestion(qd);                        // rebuild layout + answer tiles
      latestCounts = msg.answer_counts || [];
      if (msg.reveal) onReveal(msg.reveal, true);  // instant repaint, no choreography
      revealSent = true;
      answersRevealed = true;
      updateChart(latestCounts);
      var tc = document.getElementById('timer-container');
      if (tc) tc.style.visibility = 'hidden';
    }
  }

  function onReaction(msg) {
    var el = document.createElement('div');
    el.className = 'reaction-float';
    el.style.left = (8 + Math.random() * 84) + '%';
    el.innerHTML =
      '<span class="reaction-emoji">' + escapeHtml(msg.emoji) + '</span>' +
      '<span class="reaction-nick">' + escapeHtml(msg.nickname) + '</span>';
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 2500);
  }

  function onWordcloudUpdate(msg) {
    if (answersRevealed) return;
    var feed = document.getElementById('word-feed');
    if (!feed) return;
    var words = msg.words || [];
    if (words.length < renderedWordCount) {
      // New question or rejoin: rebuild from scratch
      feed.innerHTML = '';
      renderedWordCount = 0;
    }
    // Only the new words animate in — existing chips stay put
    words.slice(renderedWordCount).forEach(function (word) {
      var chip = document.createElement('span');
      chip.className = 'word-chip word-chip-pop';
      chip.textContent = escapeHtml(word);
      feed.insertBefore(chip, feed.firstChild);
    });
    renderedWordCount = words.length;
    while (feed.children.length > 60) feed.removeChild(feed.lastChild);
  }

  function onSessionCreated(msg) {
    roomCode = msg.room_code;
    rejoinPending = false;
    reconnectAttempts = 0;
    sessionStorage.setItem('quizlab_host_room', roomCode);
    sessionStorage.setItem('quizlab_host_quiz', String(quizId));
    showView('lobby');
    document.getElementById('room-code-display').textContent = roomCode;
    document.getElementById('qr-img').src = '/qr/' + roomCode;
    document.getElementById('join-url').textContent = location.origin + '/play?room=' + roomCode;
    document.getElementById('quiz-name-display').textContent = msg.quiz_name;
  }

  function onPlayerUpdate(msg) {
    var players = msg.players || msg.player_list || [];
    var count = msg.count || msg.player_count || players.length;
    if (msg.team_count !== undefined) teamCount = msg.team_count || 0;

    var badge = document.getElementById('player-count-badge');
    if (badge) badge.textContent = count;

    var chips = document.getElementById('player-chips');
    if (chips) {
      chips.innerHTML = '';
      players.forEach(function (p) {
        var chip = document.createElement('div');
        chip.className = 'player-chip kickable';
        chip.title = '✕';
        if (p.team !== null && p.team !== undefined) {
          var dot = document.createElement('span');
          dot.className = 'team-dot';
          dot.style.background = TEAM_COLORS[p.team % TEAM_COLORS.length];
          chip.appendChild(dot);
        }
        chip.appendChild(document.createTextNode(p.nickname));
        // Click a chip to kick that player (with confirmation)
        chip.addEventListener('click', function () {
          if (!confirm(t('kick_confirm').replace('{name}', p.nickname))) return;
          send({ type: 'kick_player', player_id: p.player_id });
        });
        chips.appendChild(chip);
      });
    }

    var startBtn = document.getElementById('start-btn');
    if (startBtn) {
      startBtn.disabled = count < 1;
      var hint = document.getElementById('start-hint');
      if (hint) hint.textContent = count < 1
        ? t('waiting_players')
        : t('players_ready').replace('{n}', count);
    }
  }

  // ── Area 6: Two-phase question flow — tiles hidden during read phase ──
  function onQuestion(msg) {
    currentQuestion = msg;
    revealSent = false;
    answerCounts = new Array((msg.options || []).length).fill(0);
    answersRevealed = false;
    latestCounts = [];
    inReadPhase = true;
    renderedWordCount = 0;
    lastTickSecond = -1;
    revealTimeouts.forEach(clearTimeout);
    revealTimeouts = [];
    var timerC = document.getElementById('timer-container');
    if (timerC) timerC.classList.remove('urgent-ring');

    showView('game');

    // Header
    document.getElementById('q-num').textContent = msg.number;
    document.getElementById('q-total').textContent = msg.total;

    var bgNum = document.getElementById('q-bg-num');
    if (bgNum) bgNum.textContent = String(msg.number).padStart(2, '0');

    // Question text + image
    document.getElementById('q-text').textContent = msg.text;
    var qImg = document.getElementById('q-image');
    if (msg.image_url) {
      qImg.src = msg.image_url;
      qImg.style.display = 'block';
    } else {
      qImg.style.display = 'none';
    }

    // Area 6: During read phase — tiles are EMPTY (not rendered yet)
    var tiles = document.getElementById('answer-tiles');
    tiles.innerHTML = '';
    tiles.className = 'answer-tiles'; // clear count class

    // Reset chart (build dynamic bar rows)
    buildChart(msg.options || []);

    // Reset reveal panel + buttons
    document.getElementById('reveal-panel').classList.add('hidden');
    document.getElementById('next-btn').classList.add('hidden');
    document.getElementById('reveal-btn').classList.add('hidden');

    // Hide main timer ring during read phase
    var timerContainer = document.getElementById('timer-container');
    if (timerContainer) timerContainer.style.visibility = 'hidden';

    // Reset timer display
    updateTimerDisplay(msg.time_limit, msg.time_limit);

    var readTime = msg.read_time || 0;
    if (readTime > 0) {
      startReadPhase(readTime, msg.time_limit);
    } else {
      enterAnswerPhase(msg.time_limit);
    }
  }

  function startReadPhase(readTime, timeLimit) {
    var bar = document.getElementById('read-phase-bar');
    var fill = document.getElementById('read-bar-fill');
    var countEl = document.getElementById('read-bar-count');

    if (bar) bar.classList.add('visible');
    if (countEl) countEl.textContent = readTime;

    if (fill) {
      fill.style.transition = 'none';
      fill.style.width = '100%';
      fill.getBoundingClientRect();
      fill.style.transition = 'width ' + readTime + 's linear';
      fill.style.width = '0%';
    }

    var remaining = readTime;
    var countInterval = setInterval(function () {
      remaining--;
      if (countEl) countEl.textContent = Math.max(0, remaining);
      if (remaining <= 0) clearInterval(countInterval);
    }, 1000);

    readTimerTimeout = setTimeout(function () {
      clearInterval(countInterval);
      if (bar) bar.classList.remove('visible');
      enterAnswerPhase(timeLimit);
    }, readTime * 1000);
  }

  function enterAnswerPhase(timeLimit) {
    inReadPhase = false;

    // Area 6: NOW build and show the answer tiles with slide-up animation
    buildAnswerTiles(currentQuestion);

    // Show main timer ring
    var timerContainer = document.getElementById('timer-container');
    if (timerContainer) timerContainer.style.visibility = 'visible';

    // Show reveal button
    document.getElementById('reveal-btn').classList.remove('hidden');

    // Start countdown timer
    startTimer(timeLimit);
  }

  // Area 6 + 7: Build answer tiles dynamically
  function buildAnswerTiles(msg) {
    if (!msg) return;
    var qType = msg.question_type || 'mc';
    var tiles = document.getElementById('answer-tiles');
    tiles.innerHTML = '';

    if (qType === 'wordcloud') {
      tiles.className = 'answer-tiles';
      var note = document.createElement('div');
      note.className = 'wc-waiting';
      note.textContent = t('students_writing');
      tiles.appendChild(note);
      return;
    }

    var options = msg.options || [];
    var numOptions = options.length;
    var letters = ['A', 'B', 'C', 'D', 'E', 'F'];

    // Set count class for CSS grid adjustments
    tiles.className = 'answer-tiles count-' + numOptions;

    options.forEach(function (opt, i) {
      if (!opt && opt !== 0) return;
      var tile = document.createElement('div');
      tile.className = 'answer-tile slide-up';
      tile.dataset.idx = i;
      tile.style.animationDelay = (i * 60) + 'ms';
      tile.innerHTML =
        '<span class="tile-letter">' + (letters[i] || String(i + 1)) + '</span>' +
        '<span class="tile-text">' + escapeHtml(String(opt)) + '</span>';
      tiles.appendChild(tile);
    });
  }

  function onAnswerCounts(msg) {
    latestCounts = msg.counts;
    if (answersRevealed) {
      updateChart(latestCounts);
    }
  }

  // Choreographed reveal: freeze → beat of suspense → correct answer pulses
  // while wrong answers fade → leaderboard slides in with rank-change FLIP.
  // `instant` (rejoin repaint) skips the theater and paints final state.
  function onReveal(msg, instant) {
    clearTimer();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    var timerC = document.getElementById('timer-container');
    if (timerC) timerC.classList.remove('urgent-ring');

    var qType = msg.question_type || 'mc';
    var correctIdx = msg.correct_index;

    // If tiles haven't been built yet (e.g., reveal during read phase), build them now
    var tiles = document.getElementById('answer-tiles');
    if (tiles && tiles.children.length === 0 && currentQuestion) {
      buildAnswerTiles(currentQuestion);
    }

    document.getElementById('reveal-btn').classList.add('hidden');
    var tilesEl = document.getElementById('answer-tiles');
    if (tilesEl) tilesEl.classList.remove('read-phase');

    function paintTiles() {
      if (qType === 'wordcloud') {
        renderWordcloudReveal(msg, instant);
        return;
      }
      document.querySelectorAll('.answer-tile').forEach(function (tile) {
        var idx = parseInt(tile.dataset.idx);
        if (qType === 'poll' || qType === 'order') {
          tile.style.opacity = '1';
        } else if (idx === correctIdx) {
          tile.classList.add('correct');
          if (!instant) tile.classList.add('correct-pulse');
        } else {
          tile.classList.add('wrong');
        }
      });
      if (!instant) playReveal();
    }

    function showPanel() {
      if (msg.teams) renderTeamStandings(msg.teams);
      renderRevealLeaderboard(msg.leaderboard || [], instant);
      document.getElementById('next-btn').classList.remove('hidden');
      document.getElementById('reveal-panel').classList.remove('hidden');
    }

    if (instant) {
      paintTiles();
      showPanel();
      return;
    }

    // The suspense beat: half a second of stillness before the answer lands
    revealTimeouts.forEach(clearTimeout);
    revealTimeouts = [
      setTimeout(paintTiles, 500),
      setTimeout(showPanel, 1200),
    ];
  }

  function renderWordcloudReveal(msg, instant) {
    var tiles = document.getElementById('answer-tiles');
    tiles.innerHTML = '';
    tiles.className = 'wordcloud-display';

    var titleEl = document.createElement('div');
    titleEl.className = 'wc-reveal-title';
    titleEl.textContent = t('wordcloud_title');
    tiles.appendChild(titleEl);

    var cloudEl = document.createElement('div');
    cloudEl.className = 'wc-cloud';
    tiles.appendChild(cloudEl);

    var wordsMap = msg.words || {};
    var entries = Object.keys(wordsMap)
      .map(function (w) { return [w, wordsMap[w]]; })
      .sort(function (a, b) { return b[1] - a[1]; })
      .slice(0, 20);
    var maxFreq = entries.length > 0 ? entries[0][1] : 1;
    var colors = ['var(--answer-a)', 'var(--answer-b)', 'var(--answer-c)',
                  'var(--answer-d)', 'var(--answer-e)', 'var(--answer-f)'];
    entries.forEach(function (pair, i) {
      var size = Math.round(18 + (pair[1] / maxFreq) * (72 - 18));
      var span = document.createElement('span');
      span.className = 'wc-word' + (instant ? '' : ' wc-word-pop');
      span.style.fontSize = size + 'px';
      span.style.color = colors[i % colors.length];
      if (!instant) span.style.animationDelay = (i * 80) + 'ms';
      span.textContent = pair[0];
      cloudEl.appendChild(span);
    });

    var wordFeed = document.getElementById('word-feed');
    if (wordFeed) wordFeed.style.display = 'none';
  }

  function renderTeamStandings(teams) {
    var wrap = document.getElementById('team-standings');
    var list = document.getElementById('team-standings-list');
    if (!wrap || !list) return;
    if (!teams || !teams.length) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';
    list.innerHTML = '';
    teams.forEach(function (te) {
      var row = document.createElement('div');
      row.className = 'team-standing-row';
      row.innerHTML =
        '<span class="team-dot" style="background:' + TEAM_COLORS[te.team % TEAM_COLORS.length] + '"></span>' +
        '<span class="team-standing-name">' + escapeHtml(te.name) + '</span>' +
        '<span class="team-standing-score">' + te.score + '</span>';
      list.appendChild(row);
    });
  }

  // Podium ceremony: 3rd, then 2nd, then 1st — each lands with confetti,
  // the full list fades in after the champion is crowned.
  function onGameEnd(msg) {
    clearTimer();
    if (readTimerTimeout) { clearTimeout(readTimerTimeout); readTimerTimeout = null; }
    revealTimeouts.forEach(clearTimeout);
    revealTimeouts = [];
    sessionEnded = true;
    clearHostSession();
    showView('final');

    var lb = msg.leaderboard || [];

    // Team result banner (team mode)
    var teamFinal = document.getElementById('team-final');
    if (teamFinal) {
      var teams = msg.teams || [];
      if (teams.length) {
        teamFinal.style.display = '';
        teamFinal.innerHTML = '';
        teams.forEach(function (te) {
          var div = document.createElement('div');
          div.className = 'team-final-row' + (te.rank === 1 ? ' winner' : '');
          div.innerHTML =
            '<span class="team-dot" style="background:' + TEAM_COLORS[te.team % TEAM_COLORS.length] + '"></span>' +
            '<span class="team-final-name">' + (te.rank === 1 ? '🏆 ' : '') + escapeHtml(te.name) + '</span>' +
            '<span class="team-final-score">' + te.score + '</span>';
          teamFinal.appendChild(div);
        });
      } else {
        teamFinal.style.display = 'none';
      }
    }

    renderFinalLeaderboard(lb);

    // Stage the podium: items start hidden, pop in 3-2-1
    var podiumItems = document.querySelectorAll('.podium-item');
    var lbList = document.getElementById('final-lb-list');
    if (lbList) lbList.classList.add('stage-hidden');
    podiumItems.forEach(function (item) { item.classList.add('stage-hidden'); });

    function popPodium(rank, delay, confettiOpts) {
      revealTimeouts.push(setTimeout(function () {
        podiumItems.forEach(function (item) {
          if (parseInt(item.dataset.rank) === rank) {
            item.classList.remove('stage-hidden');
            item.classList.add('podium-pop');
          }
        });
        playTone(rank === 1 ? 784 : rank === 2 ? 659 : 523, 0.2, 0.08);
        if (window.confetti && confettiOpts) confetti(confettiOpts);
      }, delay));
    }

    var colors = ['#B9FF66', '#FF6B35', '#7B61FF'];
    popPodium(3, 600,  { particleCount: 50, spread: 50, origin: { x: 0.7, y: 0.6 }, colors: colors });
    popPodium(2, 1500, { particleCount: 80, spread: 60, origin: { x: 0.3, y: 0.6 }, colors: colors });
    popPodium(1, 2700, { particleCount: 220, spread: 90, colors: colors });
    revealTimeouts.push(setTimeout(function () {
      if (window.confetti) {
        confetti({ particleCount: 120, spread: 100, origin: { x: 0.2, y: 0.6 }, colors: colors });
        confetti({ particleCount: 120, spread: 100, origin: { x: 0.8, y: 0.6 }, colors: colors });
      }
    }, 3200));
    revealTimeouts.push(setTimeout(function () {
      if (lbList) lbList.classList.remove('stage-hidden');
    }, 3600));
  }

  // ── Timer ──────────────────────────────────────────────────────
  function startTimer(limit) {
    timeLeft = limit;
    updateTimerDisplay(timeLeft, limit);

    timerInterval = setInterval(function () {
      timeLeft -= 0.1;
      if (timeLeft <= 0) {
        timeLeft = 0;
        updateTimerDisplay(0, limit);
        clearInterval(timerInterval);
        timerInterval = null;
        triggerReveal();
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

    var ring = document.getElementById('timer-ring-progress');
    if (ring) ring.style.strokeDashoffset = offset;

    var urgent = remaining <= 5 && remaining > 0;
    var num = document.getElementById('timer-number');
    if (num) {
      num.textContent = Math.ceil(remaining);
      num.classList.toggle('urgent', urgent);
    }

    // Tension ramp: ring turns fire + pulses, one tick per second
    var container = document.getElementById('timer-container');
    if (container) container.classList.toggle('urgent-ring', urgent);
    if (urgent && !inReadPhase) {
      var sec = Math.ceil(remaining);
      if (sec !== lastTickSecond) {
        lastTickSecond = sec;
        playTick();
      }
    }
  }

  function triggerReveal() {
    if (revealSent || inReadPhase) return;
    revealSent = true;
    answersRevealed = true;
    updateChart(latestCounts);
    send({ type: 'reveal' });
  }

  // ── Chart — built dynamically for up to 6 options ─────────────
  function buildChart(options) {
    var barRows = document.querySelector('.bar-rows');
    var wordFeed = document.getElementById('word-feed');
    var chartTitle = document.querySelector('.chart-title');
    if (!barRows) return;

    var qType = currentQuestion ? (currentQuestion.question_type || 'mc') : 'mc';

    if (qType === 'wordcloud') {
      barRows.style.display = 'none';
      if (wordFeed) { wordFeed.style.display = 'flex'; wordFeed.innerHTML = ''; renderedWordCount = 0; }
      if (chartTitle) chartTitle.textContent = t('words_sent');
      return;
    }

    if (wordFeed) wordFeed.style.display = 'none';
    barRows.style.display = '';
    if (chartTitle) chartTitle.textContent = t('live_answers');

    barRows.innerHTML = '';
    var letters = ['A', 'B', 'C', 'D', 'E', 'F'];
    options.forEach(function (opt, i) {
      var row = document.createElement('div');
      row.className = 'bar-row';
      row.dataset.opt = i;
      row.innerHTML =
        '<span class="bar-letter" style="color:var(--answer-' + String.fromCharCode(97 + i) + ')">' + (letters[i] || String(i + 1)) + '</span>' +
        '<div class="bar-track"><div class="bar-fill"></div></div>' +
        '<span class="bar-count">0</span>';
      barRows.appendChild(row);
    });
  }

  function updateChart(counts) {
    var total = counts.reduce(function (a, b) { return a + b; }, 0);
    counts.forEach(function (count, i) {
      var row = document.querySelector('.bar-row[data-opt="' + i + '"]');
      if (!row) return;
      var pct = total > 0 ? (count / total * 100) : 0;
      row.querySelector('.bar-fill').style.width = pct.toFixed(1) + '%';
      row.querySelector('.bar-count').textContent = count;
    });
  }

  // FLIP-animated leaderboard: rows that were already on the board slide from
  // their previous slot to the new one, with ▲/▼ markers for rank changes —
  // the climb is the moment students watch for.
  function renderRevealLeaderboard(lb, instant) {
    var container = document.getElementById('reveal-lb-list');
    if (!container) return;

    // FIRST: capture old positions before rebuilding
    var oldRects = {};
    container.querySelectorAll('.lb-row[data-pid]').forEach(function (row) {
      oldRects[row.dataset.pid] = row.getBoundingClientRect().top;
    });

    container.innerHTML = '';
    var newRows = [];
    lb.slice(0, 5).forEach(function (entry, i) {
      var row = document.createElement('div');
      row.className = 'lb-row';
      row.dataset.pid = entry.player_id;
      var isNewToBoard = !(entry.player_id in prevLbRects) && Object.keys(prevLbRects).length > 0;
      if (instant || !isNewToBoard) row.style.animation = 'none';
      else row.style.animationDelay = (i * 80) + 'ms';

      var rankClass = 'lb-rank' + (i === 0 ? ' top1' : i === 1 ? ' top2' : i === 2 ? ' top3' : '');
      var prevRank = prevLbRects[entry.player_id] ? prevLbRects[entry.player_id].rank : null;
      var delta = '';
      if (!instant && prevRank !== null && prevRank !== entry.rank) {
        delta = prevRank > entry.rank
          ? '<span class="lb-delta up">▲</span>'
          : '<span class="lb-delta down">▼</span>';
      }
      row.innerHTML =
        '<span class="' + rankClass + '">' + entry.rank + '</span>' +
        '<span class="lb-name">' + escapeHtml(entry.nickname) + '</span>' +
        delta +
        '<span class="lb-score">' + entry.score + '</span>';
      container.appendChild(row);
      newRows.push({ row: row, entry: entry });
    });

    // LAST + INVERT + PLAY: slide moved rows from their old position
    if (!instant) {
      newRows.forEach(function (item) {
        var pid = item.row.dataset.pid;
        if (!(pid in oldRects)) return;
        var newTop = item.row.getBoundingClientRect().top;
        var dy = oldRects[pid] - newTop;
        if (Math.abs(dy) < 2) return;
        item.row.style.animation = 'none';
        item.row.style.transform = 'translateY(' + dy + 'px)';
        item.row.style.transition = 'none';
        item.row.getBoundingClientRect();
        item.row.style.transition = 'transform 0.5s cubic-bezier(0.22, 1, 0.36, 1)';
        item.row.style.transform = '';
      });
    }

    // Remember ranks for the next reveal's ▲/▼ markers
    prevLbRects = {};
    lb.forEach(function (entry) {
      prevLbRects[entry.player_id] = { rank: entry.rank };
    });
  }

  function renderFinalLeaderboard(lb) {
    var container = document.getElementById('final-lb-list');
    if (!container) return;
    container.innerHTML = '';

    var podium = document.getElementById('podium-row');
    if (podium) {
      podium.innerHTML = '';
      var podiumOrder = [1, 0, 2];
      podiumOrder.forEach(function (idx) {
        if (!lb[idx]) return;
        var entry = lb[idx];
        var div = document.createElement('div');
        div.className = 'podium-item';
        div.dataset.rank = entry.rank;
        var medals = ['🥇', '🥈', '🥉'];
        div.innerHTML =
          '<div class="podium-rank">' + (medals[entry.rank - 1] || entry.rank) + '</div>' +
          '<div class="podium-name">' + escapeHtml(entry.nickname) + '</div>' +
          '<div class="podium-score">' + entry.score + '</div>';
        podium.appendChild(div);
      });
    }

    lb.forEach(function (entry, i) {
      var row = document.createElement('div');
      row.className = 'final-lb-row';
      row.style.animationDelay = (i * 60) + 'ms';
      var numClass = 'final-lb-num' + (i === 0 ? ' top1' : i === 1 ? ' top2' : i === 2 ? ' top3' : '');
      row.innerHTML =
        '<span class="' + numClass + '">' + String(entry.rank).padStart(2, '0') + '</span>' +
        '<span class="final-lb-name">' + escapeHtml(entry.nickname) + '</span>' +
        '<span class="final-lb-score">' + entry.score + '</span>';
      container.appendChild(row);
    });
  }

  // ── Controls ───────────────────────────────────────────────────
  window.hostStartGame = function () { ensureAudio(); send({ type: 'start_game' }); };
  window.hostReveal    = function () { triggerReveal(); };
  window.hostNext      = function () {
    answersRevealed = true;
    updateChart(latestCounts);
    send({ type: 'next_question' });
  };
  window.hostStopQuiz  = function () {
    if (!confirm(t('stop_confirm'))) return;
    fetch('/admin/quiz/end/' + roomCode, { method: 'POST' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (data) { showStoppedLeaderboard(data); })
      .catch(function () { alert(t('stop_error')); });
  };

  window.hostSetClass = function () {
    var sel = document.getElementById('class-select');
    if (!sel) return;
    var v = sel.value;
    send({ type: 'set_class', class_id: v ? parseInt(v) : null });
  };

  window.hostSetTeams = function () {
    var sel = document.getElementById('team-select');
    if (!sel) return;
    teamCount = parseInt(sel.value) || 0;
    send({ type: 'set_teams', count: teamCount });
  };

  window.hostToggleLock = function () {
    send({ type: 'lock_room', locked: !roomLocked });
  };

  window.hostToggleSound = function () {
    soundEnabled = !soundEnabled;
    localStorage.setItem('quizlab-sound', soundEnabled ? 'on' : 'off');
    if (soundEnabled) { ensureAudio(); playTick(); }
    updateSoundButton();
  };

  function updateSoundButton() {
    var btn = document.getElementById('sound-btn');
    if (btn) {
      btn.textContent = soundEnabled ? '🔊' : '🔇';
      btn.title = t('sound');
    }
  }

  window.hostToggleProjector = function () {
    var on = document.body.classList.toggle('projector-mode');
    localStorage.setItem('quizlab-projector', on ? 'on' : 'off');
    updateProjectorButton();
  };

  function updateProjectorButton() {
    var btn = document.getElementById('projector-btn');
    if (btn) {
      var on = document.body.classList.contains('projector-mode');
      btn.style.borderColor = on ? 'var(--lime)' : '';
      btn.style.color = on ? 'var(--lime)' : '';
      btn.title = t('projector');
    }
  }

  function loadClasses() {
    fetch('/admin/api/classes')
      .then(function (r) { return r.json(); })
      .then(function (classes) {
        var sel = document.getElementById('class-select');
        if (!sel || !Array.isArray(classes)) return;
        classes.forEach(function (c) {
          var opt = document.createElement('option');
          opt.value = c.id;
          opt.textContent = c.name + ' (' + c.student_count + ')';
          sel.appendChild(opt);
        });
        // A rejoin may have arrived before the class list loaded
        if (sel.dataset.pendingValue &&
            sel.querySelector('option[value="' + sel.dataset.pendingValue + '"]')) {
          sel.value = sel.dataset.pendingValue;
        }
      })
      .catch(function () {});
  }

  function showStoppedLeaderboard(data) {
    sessionEnded = true;
    clearHostSession();
    var lb = data.leaderboard || [];
    var qAnswered = data.questions_answered || 0;
    var qTotal = data.total_questions || 0;

    var rowsHtml = '';
    lb.forEach(function (entry) {
      var rankColor = entry.rank === 1 ? '#FFD700'
                    : entry.rank === 2 ? '#C0C0C0'
                    : entry.rank === 3 ? '#CD7F32'
                    : 'var(--text-muted)';
      rowsHtml +=
        '<div class="final-lb-row" style="animation:none">' +
          '<span class="final-lb-num" style="color:' + rankColor + '">' +
            String(entry.rank).padStart(2, '0') +
          '</span>' +
          '<span class="final-lb-name">' + escapeHtml(entry.nickname) + '</span>' +
          '<span class="final-lb-score">' + entry.score + '</span>' +
        '</div>';
    });

    var overlay = document.createElement('div');
    overlay.style.cssText =
      'position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999;' +
      'background:var(--bg);display:flex;flex-direction:column;' +
      'padding:40px 48px;box-sizing:border-box;overflow:hidden;';

    overlay.innerHTML =
      '<h1 style="font-family:\'Barlow Condensed\',sans-serif;font-weight:900;' +
        'font-size:52px;text-transform:uppercase;letter-spacing:-0.02em;' +
        'line-height:1;margin:0 0 8px">' + t('partial_results') + '</h1>' +
      '<p style="font-size:15px;color:var(--text-muted);margin:0 0 32px">' +
        t('stopped_at').replace('{a}', qAnswered).replace('{b}', qTotal) +
      '</p>' +
      '<div style="flex:1;overflow-y:auto;min-height:0;max-width:600px">' +
        rowsHtml +
      '</div>' +
      '<div style="margin-top:32px;flex-shrink:0">' +
        '<button class="btn btn-primary btn-lg" ' +
          'onclick="location.href=\'/admin/dashboard\'" ' +
          'style="min-width:200px">' + t('go_dashboard') + '</button>' +
      '</div>';

    document.body.appendChild(overlay);
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
    connect();
    loadClasses();
    updateSoundButton();
    if (localStorage.getItem('quizlab-projector') === 'on') {
      document.body.classList.add('projector-mode');
    }
    updateProjectorButton();
    updateLockButton();
    // Browsers require a user gesture before audio can start
    document.addEventListener('click', function onFirstClick() {
      if (soundEnabled) ensureAudio();
      document.removeEventListener('click', onFirstClick);
    });
    var ring = document.getElementById('timer-ring-progress');
    if (ring) {
      ring.style.strokeDasharray = CIRCUMFERENCE;
      ring.style.strokeDashoffset = '0';
    }
  });

})();
