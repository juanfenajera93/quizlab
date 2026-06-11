/* Self-paced assignment (homework) flow: name → questions at own pace → submit
   → score + review. No websocket; plain fetch against /api/assignment/. */
(function () {
  'use strict';

  var code = window.ASSIGNMENT_CODE;
  var t = window.qlT || function (k, fb) { return fb || k; };
  var info = null;
  var questions = [];
  var answers = [];        // per question: int | [int] | string | null
  var orderState = [];     // per question: array of original indices in display order
  var current = 0;

  function $(id) { return document.getElementById(id); }
  function show(id) {
    ['hw-start', 'hw-question', 'hw-result', 'hw-error'].forEach(function (v) {
      $(v).style.display = (v === id) ? '' : 'none';
    });
  }

  function fail(msg) {
    $('hw-error-msg').textContent = msg;
    show('hw-error');
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Load assignment info ───────────────────────────────────────
  fetch('/api/assignment/' + code)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.ok) { fail(t('hw_not_found', 'Esta tarea no existe.')); return; }
      info = data;
      $('hw-quiz-name').textContent = data.quiz_name + ' · ' +
        data.question_count + ' ' + t('questions', 'preguntas');
      if (data.deadline) {
        var dl = new Date(data.deadline);
        var el = $('hw-deadline');
        el.style.display = '';
        el.textContent = t('hw_deadline', 'Fecha límite') + ': ' + dl.toLocaleString();
      }
      if (data.closed) { fail(t('hw_closed', 'Esta tarea ya cerró.')); return; }
      if (data.has_roster) {
        $('hw-name-free').style.display = 'none';
        var box = $('hw-name-roster');
        box.style.display = '';
        if (!data.roster_names || data.roster_names.length === 0) {
          fail(t('hw_all_done', 'Todos los estudiantes de la clase ya entregaron.'));
          return;
        }
        data.roster_names.forEach(function (name) {
          var b = document.createElement('button');
          b.className = 'hw-opt';
          b.innerHTML = '<span class="letter">👤</span><span>' + escapeHtml(name) + '</span>';
          b.addEventListener('click', function () {
            box.querySelectorAll('.hw-opt').forEach(function (o) { o.classList.remove('sel'); });
            b.classList.add('sel');
            box.dataset.selected = name;
          });
          box.appendChild(b);
        });
      }
      show('hw-start');
    })
    .catch(function () { fail(t('hw_load_error', 'No se pudo cargar la tarea.')); });

  function getNickname() {
    if (info.has_roster) return $('hw-name-roster').dataset.selected || '';
    return $('hw-nickname').value.trim();
  }

  // ── Start: fetch questions ─────────────────────────────────────
  window.hwStart = function () {
    if (!getNickname()) {
      alert(t('hw_pick_name', 'Escribe o selecciona tu nombre.'));
      return;
    }
    fetch('/api/assignment/' + code + '/questions')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) { fail(t('hw_closed', 'Esta tarea ya cerró.')); return; }
        questions = data.questions;
        answers = questions.map(function () { return null; });
        orderState = questions.map(function (q) {
          if (q.question_type !== 'order') return null;
          // Shuffle display order; submission maps back to original indices
          var idx = q.options.map(function (_, i) { return i; });
          for (var i = idx.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var tmp = idx[i]; idx[i] = idx[j]; idx[j] = tmp;
          }
          return idx;
        });
        current = 0;
        renderQuestion();
        show('hw-question');
      })
      .catch(function () { fail(t('hw_load_error', 'No se pudo cargar la tarea.')); });
  };

  // ── Render one question ────────────────────────────────────────
  function renderQuestion() {
    var q = questions[current];
    $('hw-q-count').textContent =
      t('question', 'Pregunta') + ' ' + (current + 1) + ' / ' + questions.length;
    $('hw-q-text').textContent = q.text;
    var img = $('hw-q-image');
    if (q.image_url) { img.src = q.image_url; img.style.display = ''; }
    else { img.style.display = 'none'; }

    var body = $('hw-q-body');
    body.innerHTML = '';
    var letters = ['A', 'B', 'C', 'D', 'E', 'F'];
    var qType = q.question_type;

    if (qType === 'mc' || qType === 'tf' || qType === 'poll') {
      var opts = (qType === 'tf' && q.options.length < 2)
        ? ['Verdadero', 'Falso'] : q.options;
      opts.forEach(function (opt, i) {
        var b = document.createElement('button');
        b.className = 'hw-opt' + (answers[current] === i ? ' sel' : '');
        b.innerHTML = '<span class="letter">' + (letters[i] || i + 1) + '</span><span>' +
          escapeHtml(String(opt)) + '</span>';
        b.addEventListener('click', function () {
          answers[current] = i;
          body.querySelectorAll('.hw-opt').forEach(function (o) { o.classList.remove('sel'); });
          b.classList.add('sel');
        });
        body.appendChild(b);
      });

    } else if (qType === 'ms') {
      if (!Array.isArray(answers[current])) answers[current] = [];
      q.options.forEach(function (opt, i) {
        var b = document.createElement('button');
        b.className = 'hw-opt' + (answers[current].indexOf(i) !== -1 ? ' sel' : '');
        b.innerHTML = '<span class="letter">' + (letters[i] || i + 1) + '</span><span>' +
          escapeHtml(String(opt)) + '</span>';
        b.addEventListener('click', function () {
          var pos = answers[current].indexOf(i);
          if (pos === -1) answers[current].push(i);
          else answers[current].splice(pos, 1);
          b.classList.toggle('sel');
        });
        body.appendChild(b);
      });

    } else if (qType === 'order') {
      renderOrder(body, q);

    } else if (qType === 'wordcloud') {
      var input = document.createElement('input');
      input.type = 'text';
      input.className = 'hw-input';
      input.maxLength = 50;
      input.placeholder = t('hw_write_answer', 'Escribe tu respuesta…');
      input.value = typeof answers[current] === 'string' ? answers[current] : '';
      input.addEventListener('input', function () {
        answers[current] = input.value.trim() || null;
      });
      body.appendChild(input);
    }

    $('hw-prev').style.visibility = current === 0 ? 'hidden' : 'visible';
    $('hw-next').textContent = current === questions.length - 1
      ? t('hw_submit', 'Entregar ✓') : t('hw_next', 'Siguiente →');
  }

  function renderOrder(body, q) {
    body.innerHTML = '';
    var order = orderState[current];
    answers[current] = order.slice();   // current arrangement is the answer
    order.forEach(function (origIdx, pos) {
      var item = document.createElement('div');
      item.className = 'hw-order-item';
      var arrows = document.createElement('span');
      arrows.className = 'arrows';
      var up = document.createElement('button');
      up.textContent = '↑';
      up.disabled = pos === 0;
      up.addEventListener('click', function () { swapOrder(pos, pos - 1, body, q); });
      var down = document.createElement('button');
      down.textContent = '↓';
      down.disabled = pos === order.length - 1;
      down.addEventListener('click', function () { swapOrder(pos, pos + 1, body, q); });
      arrows.appendChild(up);
      arrows.appendChild(down);
      var txt = document.createElement('span');
      txt.textContent = String(q.options[origIdx]);
      item.appendChild(arrows);
      item.appendChild(txt);
      body.appendChild(item);
    });
  }

  function swapOrder(a, b, body, q) {
    var order = orderState[current];
    var tmp = order[a]; order[a] = order[b]; order[b] = tmp;
    renderOrder(body, q);
  }

  // ── Navigation + submit ────────────────────────────────────────
  window.hwPrev = function () {
    if (current > 0) { current--; renderQuestion(); }
  };

  window.hwNext = function () {
    if (current < questions.length - 1) { current++; renderQuestion(); return; }
    var unanswered = answers.filter(function (a, i) {
      return questions[i].question_type !== 'order' &&
             (a === null || (Array.isArray(a) && a.length === 0));
    }).length;
    var msg = t('hw_confirm_submit', '¿Entregar la tarea?');
    if (unanswered > 0) {
      msg += ' ' + t('hw_unanswered', 'Tienes preguntas sin responder:') + ' ' + unanswered;
    }
    if (!confirm(msg)) return;
    $('hw-next').disabled = true;
    fetch('/api/assignment/' + code + '/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname: getNickname(), answers: answers })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.data.ok) {
          var err = res.data.error;
          if (err === 'already_submitted') fail(t('hw_already', 'Ya entregaste esta tarea.'));
          else if (err === 'closed') fail(t('hw_closed', 'Esta tarea ya cerró.'));
          else fail(t('hw_submit_error', 'No se pudo entregar. Intenta de nuevo.'));
          return;
        }
        renderResult(res.data);
      })
      .catch(function () {
        $('hw-next').disabled = false;
        alert(t('hw_submit_error', 'No se pudo entregar. Intenta de nuevo.'));
      });
  };

  function renderResult(data) {
    $('hw-score').textContent = data.score + ' pts';
    $('hw-correct').textContent =
      data.correct_count + ' / ' + data.total + ' ' + t('hw_correct', 'correctas');
    var box = $('hw-review');
    box.innerHTML = '';
    (data.review || []).forEach(function (item) {
      var div = document.createElement('div');
      div.className = 'hw-review-item';
      var mark = !item.scored ? '◦'
        : item.correct ? '<span class="ok-mark">✓</span>'
        : '<span class="bad-mark">✗</span>';
      var html = '<div class="hw-review-q">' + mark + ' ' +
        (item.index + 1) + '. ' + escapeHtml(item.text) + '</div>' +
        '<div class="hw-review-a">' + t('hw_your_answer', 'Tu respuesta') + ': <strong>' +
        escapeHtml(item.your_answer) + '</strong></div>';
      if (item.correct_answer && !item.correct) {
        html += '<div class="hw-review-a">' + t('hw_right_answer', 'Respuesta correcta') +
          ': <strong>' + escapeHtml(item.correct_answer) + '</strong></div>';
      }
      div.innerHTML = html;
      box.appendChild(div);
    });
    show('hw-result');
    if (window.confetti && data.correct_count === data.total && data.total > 0) {
      confetti({ particleCount: 150, spread: 70, colors: ['#B9FF66', '#FF6B35', '#7B61FF'] });
    }
  }
})();
