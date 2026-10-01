/* Self-paced assignment (homework) flow: name → questions at own pace → submit
   → score + review. No websocket; plain fetch against /api/assignment/. */
(function () {
  'use strict';

  var code = window.ASSIGNMENT_CODE;
  var t = window.qlT || function (k, fb) { return fb || k; };
  var info = null;
  var questions = [];
  var answers = [];        // per question: int | [int] | string | null
  var typeState = [];      // per question: the type's own state (e.g. the order shuffle)
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
        // The server's Ecuador wall-clock text, not the browser's own zone:
        // the deadline is the same instant for everyone.
        var el = $('hw-deadline');
        el.style.display = '';
        el.textContent = t('hw_deadline', 'Fecha límite') + ': ' +
          (data.deadline_display
            ? data.deadline_display + ' (' + t('hw_ecuador_time', 'hora de Ecuador') + ')'
            : new Date(data.deadline).toLocaleString());
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
        typeState = questions.map(function (q) {
          return QLTypes.get(q.question_type).homework.init(q);
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
    img.onerror = function () { img.style.display = 'none'; };
    if (q.image_url) { img.src = q.image_url; img.style.display = ''; }
    else { img.style.display = 'none'; }

    var body = $('hw-q-body');
    body.innerHTML = '';
    // The question type renders its own input (qtypes.js)
    var idx = current;
    QLTypes.get(q.question_type).homework.render(body, q, {
      get: function () { return answers[idx]; },
      set: function (v) { answers[idx] = v; },
      state: typeState[idx]
    });

    $('hw-prev').style.visibility = current === 0 ? 'hidden' : 'visible';
    $('hw-next').textContent = current === questions.length - 1
      ? t('hw_submit', 'Entregar ✓') : t('hw_next', 'Siguiente →');
  }

  // ── Navigation + submit ────────────────────────────────────────
  window.hwPrev = function () {
    if (current > 0) { current--; renderQuestion(); }
  };

  window.hwNext = function () {
    if (current < questions.length - 1) { current++; renderQuestion(); return; }
    var unanswered = answers.filter(function (a, i) {
      return QLTypes.get(questions[i].question_type).homework.unanswered(a);
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
