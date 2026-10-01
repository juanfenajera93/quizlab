/* QuizLab question types, browser side: one object per type with
   everything the pages do differently for it.

     player    build(ctx) the phone's answer UI -> controller {timeout, lock}
               reveal(msg) the phone's result header (null = generic)
     host      chart ('bars' | 'words' | 'none') beside the question
               buildTiles(tiles, msg, H) during the question (false = generic)
               correct(msg) right option indices (null = no right answer)
               reveal(msg, instant, H) the projector reveal (default:
               highlight correct, per-option counts)
     homework  init(q) per-question state; render(body, q, api);
               unanswered(answer)
     editor    short label, which option/correct UI the quiz editor shows

   Metadata shared with the server (label, scored, partial credit, option
   limits, default options, fixed points...) comes from qtypes.py as
   window.QL_KIND_META; game_manager/main use the same registry. */
(function () {
  'use strict';

  var META = window.QL_KIND_META || {};
  var LETTERS = ['A', 'B', 'C', 'D', 'E', 'F'];
  var t = window.qlT || function (k, fb) { return fb || k; };

  function esc(str) {
    if (!str && str !== 0) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function letter(i) { return LETTERS[i] || String(i + 1); }

  // ── Shared phone pieces ───────────────────────────────────────────
  function optionButton(i, letterText, label) {
    var btn = document.createElement('button');
    btn.className = 'player-ans-btn slide-in';
    btn.dataset.idx = i;
    btn.style.animationDelay = (i * 80) + 'ms';
    btn.innerHTML =
      '<span class="btn-letter">' + letterText + '</span>' +
      '<span>' + esc(String(label)) + '</span>';
    return btn;
  }

  function lockButtons() {
    document.querySelectorAll('.player-ans-btn').forEach(function (b) { b.disabled = true; });
  }

  // One option, tap selects, confirm submits (mc, tf, poll).
  function singleChoicePlayer(labels, letterFor) {
    return function (ctx) {
      var selected = null;
      ctx.container.className = 'player-answers count-' + labels.length;
      labels.forEach(function (label, i) {
        var btn = optionButton(i, letterFor(i), label);
        btn.addEventListener('click', function () {
          if (ctx.isAnswered()) return;
          selected = i;
          document.querySelectorAll('.player-ans-btn').forEach(function (b) {
            b.classList.toggle('selected-mc', parseInt(b.dataset.idx) === i);
          });
          var cb = ctx.confirmButton();
          if (cb) cb.classList.add('visible');
        });
        ctx.container.appendChild(btn);
      });

      function submit() {
        if (ctx.isAnswered() || selected === null) return;
        document.querySelectorAll('.player-ans-btn').forEach(function (b) {
          b.classList.remove('selected-mc');
        });
        ctx.hideConfirm();
        ctx.setAnswered();
        ctx.freezePoints();
        ctx.send({ type: 'answer', answer_index: selected, client_timestamp: Date.now() });
        // The timer keeps running until the reveal
        document.querySelectorAll('.player-ans-btn').forEach(function (b) {
          b.disabled = true;
          if (parseInt(b.dataset.idx) !== selected) b.classList.add('dimmed');
        });
        ctx.showOverlay();
      }

      ctx.addConfirm(t('confirm'), submit, false);
      return {
        timeout: function () {
          if (selected !== null) { submit(); return; }   // auto-confirm the pick
          ctx.stopPointsCounter();
          ctx.showOverlay();
          lockButtons();
          ctx.hideConfirm();
        },
        lock: function () {}
      };
    };
  }

  // ── Shared host pieces ────────────────────────────────────────────
  function optionsReveal(msg, instant, H) {
    var correct = this.host.correct(msg);
    document.querySelectorAll('.answer-tile').forEach(function (tile) {
      var idx = parseInt(tile.dataset.idx);
      if (!correct) return;                   // no right answer (poll)
      if (correct.indexOf(idx) !== -1) {
        tile.classList.add('correct');
        if (!instant) tile.classList.add('correct-pulse');
      } else {
        tile.classList.add('wrong');
      }
    });
    H.paintDistribution(msg, instant);
  }

  // ── Shared homework pieces ────────────────────────────────────────
  function homeworkOptions(body, opts, isSelected, onClick) {
    opts.forEach(function (opt, i) {
      var b = document.createElement('button');
      b.className = 'hw-opt' + (isSelected(i) ? ' sel' : '');
      b.innerHTML = '<span class="letter">' + letter(i) + '</span><span>' +
        esc(String(opt)) + '</span>';
      b.addEventListener('click', function () { onClick(i, b); });
      body.appendChild(b);
    });
  }

  function homeworkSingle(body, q, api) {
    var opts = q.options;
    homeworkOptions(body, opts,
      function (i) { return api.get() === i; },
      function (i, b) {
        api.set(i);
        body.querySelectorAll('.hw-opt').forEach(function (o) { o.classList.remove('sel'); });
        b.classList.add('sel');
      });
  }

  // ── Defaults ──────────────────────────────────────────────────────
  var BASE = {
    player: {
      build: function (ctx) { return { timeout: function () {}, lock: function () {} }; },
      reveal: function (msg) { return null; },
      fullLabelKey: 'bd_accuracy'
    },
    host: {
      chart: 'bars',
      buildTiles: function () { return false; },
      correct: function (msg) { return [msg.correct_index]; },
      reveal: optionsReveal
    },
    homework: {
      init: function (q) { return null; },
      render: function (body, q, api) {},
      unanswered: function (a) { return a === null || (Array.isArray(a) && a.length === 0); }
    },
    editor: {
      short: '?',
      options: 'list',        // 'list' | 'fixed' | 'none'
      correct: 'single',      // 'single' | 'multi' | 'sequence' | 'none'
      requireTwoOptions: true
    }
  };

  var TYPES = {};
  function define(code, spec) {
    var type = { code: code, meta: META[code] || {} };
    ['player', 'host', 'homework', 'editor'].forEach(function (part) {
      type[part] = Object.assign({}, BASE[part], spec[part] || {});
    });
    TYPES[code] = type;
  }

  // ── mc ────────────────────────────────────────────────────────────
  define('mc', {
    player: {
      build: function (ctx) {
        return singleChoicePlayer(ctx.q.options || [], letter)(ctx);
      }
    },
    homework: { render: homeworkSingle },
    editor: { short: 'MC' }
  });

  // ── tf ────────────────────────────────────────────────────────────
  define('tf', {
    player: {
      build: function (ctx) {
        // Two large full-width buttons — tap selects, confirm submits
        return singleChoicePlayer(['Verdadero', 'Falso'],
          function (i) { return i === 0 ? 'V' : 'F'; })(ctx);
      }
    },
    homework: {
      render: function (body, q, api) {
        var opts = q.options.length < 2 ? ['Verdadero', 'Falso'] : q.options;
        homeworkSingle(body, Object.assign({}, q, { options: opts }), api);
      }
    },
    editor: { short: 'T/F', options: 'fixed', correctLabels: ['V', 'F'] }
  });

  // ── ms ────────────────────────────────────────────────────────────
  define('ms', {
    player: {
      build: function (ctx) {
        var selections = [];
        var confirmed = false;
        var options = ctx.q.options || [];
        ctx.container.className = 'player-answers count-' + options.length;
        options.forEach(function (opt, i) {
          var btn = optionButton(i, letter(i), opt);
          btn.addEventListener('click', function () { toggle(i); });
          ctx.container.appendChild(btn);
        });

        function toggle(idx) {
          if (confirmed) return;
          var pos = selections.indexOf(idx);
          if (pos === -1) selections.push(idx); else selections.splice(pos, 1);
          document.querySelectorAll('.player-ans-btn').forEach(function (btn) {
            btn.classList.toggle('selected-ms',
              selections.indexOf(parseInt(btn.dataset.idx)) !== -1);
          });
          var cb = ctx.confirmButton();
          if (cb) cb.classList.toggle('visible', selections.length > 0);
          // Live selection to the server (scored even if never confirmed)
          ctx.send({ type: 'selection', selections: selections.slice() });
        }

        function confirm() {
          if (confirmed || selections.length === 0) return;
          confirmed = true;
          ctx.setAnswered();
          ctx.freezePoints();
          ctx.send({ type: 'confirm' });
          // The timer keeps running; dim what was not picked
          document.querySelectorAll('.player-ans-btn').forEach(function (btn) {
            btn.disabled = true;
            if (selections.indexOf(parseInt(btn.dataset.idx)) === -1) btn.classList.add('dimmed');
          });
          ctx.hideConfirm();
          ctx.showOverlay();
        }

        // Hidden until at least one option is selected
        ctx.addConfirm(t('confirm'), confirm, false);
        return {
          timeout: function () {
            if (selections.length > 0) ctx.freezePoints(); else ctx.stopPointsCounter();
            ctx.showOverlay();
            lockButtons();
            ctx.hideConfirm();
          },
          lock: function () { confirmed = true; }
        };
      }
    },
    host: {
      correct: function (msg) {
        if (Array.isArray(msg.correct_indices)) return msg.correct_indices;
        try { return JSON.parse(msg.correct_json || '[]'); } catch (e) { return []; }
      }
    },
    homework: {
      render: function (body, q, api) {
        if (!Array.isArray(api.get())) api.set([]);
        homeworkOptions(body, q.options,
          function (i) { return api.get().indexOf(i) !== -1; },
          function (i, b) {
            var a = api.get();
            var pos = a.indexOf(i);
            if (pos === -1) a.push(i); else a.splice(pos, 1);
            b.classList.toggle('sel');
          });
      }
    },
    editor: { short: 'Multi', correct: 'multi' }
  });

  // ── poll ──────────────────────────────────────────────────────────
  define('poll', {
    player: {
      build: function (ctx) {
        return singleChoicePlayer(ctx.q.options || [], letter)(ctx);
      },
      reveal: function (msg) {
        // Everyone who answered gets the points
        var did = msg.your_answer !== -1 && msg.your_answer !== null &&
                  msg.your_answer !== undefined;
        return {
          icon: '✓', color: 'var(--lime)', label: t('thanks'), cls: 'poll',
          popup: did ? '+' + (msg.points_earned || 0) + ' pts' : '0 pts',
          popupCls: did ? 'score-popup' : 'score-popup wrong', flash: ''
        };
      },
      fullLabelKey: 'bd_poll'
    },
    host: { correct: function () { return null; } },
    homework: { render: homeworkSingle },
    editor: { short: 'Poll', pick: 'Encuesta', correct: 'none', requireTwoOptions: false }
  });

  // ── order ─────────────────────────────────────────────────────────
  define('order', {
    player: {
      build: function (ctx) {
        // Vertical list with up/down arrows, explicit confirm
        var options = ctx.q.options || [];
        var ordering = options.map(function (_, i) { return i; });
        var container = ctx.container;
        container.className = 'player-answers';   // not a grid
        container.style.display = 'block';

        function sendUpdate() {
          ctx.send({ type: 'order_update', ordering: ordering.slice() });
        }

        function swap(a, b) {
          if (ctx.isAnswered()) return;
          var tmp = ordering[a]; ordering[a] = ordering[b]; ordering[b] = tmp;
          render();
          sendUpdate();
        }

        function render() {
          container.innerHTML = '';
          var list = document.createElement('div');
          list.className = 'order-list';
          ordering.forEach(function (optIdx, position) {
            var item = document.createElement('div');
            item.className = 'order-item';
            item.dataset.position = position;
            var arrows = document.createElement('div');
            arrows.className = 'order-arrows';
            var up = document.createElement('button');
            up.className = 'order-arrow-btn';
            up.textContent = '↑';
            up.disabled = (position === 0);
            up.addEventListener('click', function () { swap(position, position - 1); });
            var down = document.createElement('button');
            down.className = 'order-arrow-btn';
            down.textContent = '↓';
            down.disabled = (position === ordering.length - 1);
            down.addEventListener('click', function () { swap(position, position + 1); });
            arrows.appendChild(up);
            arrows.appendChild(down);
            var text = document.createElement('span');
            text.className = 'order-item-text';
            text.textContent = String(options[optIdx]);
            item.appendChild(arrows);
            item.appendChild(text);
            list.appendChild(item);
          });
          container.appendChild(list);
          // Send the current ordering right away
          sendUpdate();
        }

        function confirm() {
          if (ctx.isAnswered()) return;
          ctx.setAnswered();
          ctx.freezePoints();
          sendUpdate();
          document.querySelectorAll('.order-arrow-btn').forEach(function (b) { b.disabled = true; });
          ctx.hideConfirm();
          ctx.showOverlay();
        }

        render();
        ctx.addConfirm(t('confirm_order'), confirm, true);
        return {
          timeout: function () {
            ctx.setAnswered();
            ctx.freezePoints();
            sendUpdate();
            ctx.showOverlay();
            lockButtons();
            ctx.hideConfirm();
          },
          lock: function () {}
        };
      }
    },
    host: {
      chart: 'none',     // per-item "in place" counts live on the reveal list
      // The shuffled tiles are replaced by the correct sequence, numbered
      // 1..n, each with how many students had that item in place.
      reveal: function (msg, instant, H) {
        var tiles = document.getElementById('answer-tiles');
        var dist = msg.distribution || {};
        var items = msg.correct_options || (H.currentQuestion() || {}).options || [];
        var inPlace = dist.in_place || [];
        var answered = dist.answered || 0;
        tiles.innerHTML = '';
        tiles.className = 'order-reveal';

        var head = document.createElement('div');
        head.className = 'order-reveal-head';
        head.innerHTML =
          '<span class="order-reveal-title">' + esc(t('order_correct_title')) + '</span>' +
          '<span class="order-reveal-summary">' +
            esc(t('order_full_correct')
              .replace('{n}', dist.full_correct || 0).replace('{m}', answered)) +
          '</span>';
        tiles.appendChild(head);

        items.forEach(function (text, i) {
          var row = document.createElement('div');
          row.className = 'order-reveal-row' + (instant ? '' : ' slide-up');
          if (!instant) row.style.animationDelay = (i * 90) + 'ms';
          var n = inPlace[i] || 0;
          row.innerHTML =
            '<span class="order-reveal-num">' + (i + 1) + '</span>' +
            '<span class="order-reveal-text">' + esc(String(text)) + '</span>' +
            '<span class="order-reveal-stat" title="' + esc(t('order_in_place').replace('{n}', n)) + '">' +
              '<span class="tile-count">' + n + '</span>' +
              '<span class="tile-pct">' + H.pct(n, answered) + '%</span>' +
            '</span>';
          tiles.appendChild(row);
        });
      }
    },
    homework: {
      // Shuffle the display order; the submission maps back to original indices
      init: function (q) {
        var idx = q.options.map(function (_, i) { return i; });
        for (var i = idx.length - 1; i > 0; i--) {
          var j = Math.floor(Math.random() * (i + 1));
          var tmp = idx[i]; idx[i] = idx[j]; idx[j] = tmp;
        }
        return idx;
      },
      render: function (body, q, api) {
        var order = api.state;
        api.set(order.slice());   // the current arrangement is the answer
        order.forEach(function (origIdx, pos) {
          var item = document.createElement('div');
          item.className = 'hw-order-item';
          var arrows = document.createElement('span');
          arrows.className = 'arrows';
          var up = document.createElement('button');
          up.textContent = '↑';
          up.disabled = pos === 0;
          up.addEventListener('click', function () { swap(pos, pos - 1); });
          var down = document.createElement('button');
          down.textContent = '↓';
          down.disabled = pos === order.length - 1;
          down.addEventListener('click', function () { swap(pos, pos + 1); });
          arrows.appendChild(up);
          arrows.appendChild(down);
          var txt = document.createElement('span');
          txt.textContent = String(q.options[origIdx]);
          item.appendChild(arrows);
          item.appendChild(txt);
          body.appendChild(item);
        });
        function swap(a, b) {
          var tmp = order[a]; order[a] = order[b]; order[b] = tmp;
          body.innerHTML = '';
          TYPES.order.homework.render(body, q, api);
        }
      },
      unanswered: function () { return false; }   // always has an arrangement
    },
    editor: { short: 'Orden', correct: 'sequence', requireTwoOptions: false }
  });

  // ── wordcloud ─────────────────────────────────────────────────────
  define('wordcloud', {
    player: {
      build: function (ctx) {
        var MAX = 50;
        ctx.container.className = 'player-answers';
        var wrapEl = document.createElement('div');
        wrapEl.className = 'wc-input-area';
        var inputEl = document.createElement('input');
        inputEl.type = 'text';
        inputEl.id = 'wc-input';
        inputEl.className = 'wc-input';
        inputEl.maxLength = MAX;
        inputEl.placeholder = t('write_answer');
        inputEl.autocomplete = 'off';
        var charCount = document.createElement('div');
        charCount.className = 'wc-char-count';
        charCount.textContent = '0 / ' + MAX;
        inputEl.addEventListener('input', function () {
          charCount.textContent = inputEl.value.length + ' / ' + MAX;
        });
        inputEl.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') { e.preventDefault(); submit(); }
        });
        wrapEl.appendChild(inputEl);
        wrapEl.appendChild(charCount);
        ctx.container.appendChild(wrapEl);

        function submit() {
          if (ctx.isAnswered()) return;
          var text = inputEl.value.trim().slice(0, MAX);
          if (!text) return;
          ctx.setAnswered();
          inputEl.disabled = true;
          ctx.hideConfirm();
          ctx.send({ type: 'wordcloud_answer', text: text });
          ctx.showOverlay();
        }

        ctx.addConfirm(t('confirm').toUpperCase(), submit, false);
        // Focus the input once the answer phase starts
        setTimeout(function () { if (inputEl) inputEl.focus(); }, 50);
        return {
          timeout: function () {
            var text = inputEl.value.trim().slice(0, MAX);
            if (text) {
              ctx.setAnswered();
              inputEl.disabled = true;
              ctx.send({ type: 'wordcloud_answer', text: text });
            }
            ctx.showOverlay();
            ctx.hideConfirm();
          },
          lock: function () {}
        };
      },
      reveal: function (msg) {
        var yourText = msg.your_text || '';
        return {
          icon: '☁', color: 'var(--violet)',
          label: yourText ? t('sent') : t('no_answer'),
          cls: yourText ? 'poll' : 'wrong',
          popup: t('no_points'), popupCls: 'score-popup wrong', flash: ''
        };
      }
    },
    host: {
      chart: 'words',
      buildTiles: function (tiles) {
        tiles.className = 'answer-tiles';
        var note = document.createElement('div');
        note.className = 'wc-waiting';
        note.textContent = t('students_writing');
        tiles.appendChild(note);
        return true;
      },
      correct: function () { return null; },
      reveal: function (msg, instant, H) {
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
    },
    homework: {
      render: function (body, q, api) {
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'hw-input';
        input.maxLength = 50;
        input.placeholder = t('hw_write_answer', 'Escribe tu respuesta…');
        input.value = typeof api.get() === 'string' ? api.get() : '';
        input.addEventListener('input', function () {
          api.set(input.value.trim() || null);
        });
        body.appendChild(input);
      }
    },
    editor: { short: 'WCloud', options: 'none', correct: 'none',
              requireTwoOptions: false, note: 'wordcloud-note' }
  });

  window.QLTypes = {
    LETTERS: LETTERS,
    all: TYPES,
    get: function (code) { return TYPES[code || 'mc'] || TYPES.mc; },
    meta: function (code) { return (TYPES[code || 'mc'] || TYPES.mc).meta; },
    // Live points counter: scored types worth something
    showsCounter: function (q) {
      var m = META[q.question_type || 'mc'];
      return !!(m && m.scored) && (q.points || 0) > 0;
    },
    hasPartialCredit: function (q) {
      var m = META[q.question_type || 'mc'];
      return !!(m && m.partial_credit);
    }
  };
})();
