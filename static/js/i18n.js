/* QuizLab i18n — Spanish default, English alternative.
   - window.qlT(key, fallback) for dynamic strings in JS
   - elements with [data-i18n="key"] are translated on DOMContentLoaded
   - language persisted in localStorage('quizlab-lang'); the ES/EN button
     in base.html toggles it and reloads. */
(function () {
  'use strict';

  var STRINGS = {
    es: {
      // Player
      join_title: 'Ingresa el código de sala y tu apodo',
      room_code_label: 'Código de Sala',
      nickname_label: 'Apodo',
      nickname_placeholder: 'Tu nombre',
      join_btn: 'Unirse →',
      pick_your_name: 'Selecciona tu nombre',
      youre_in: '¡Estás dentro!',
      waiting_start: 'Esperando que el profesor inicie...',
      game_starting: '¡El juego comienza!',
      players: 'Jugadores',
      read_question: 'Lee la pregunta...',
      confirm: 'Confirmar',
      confirm_order: 'Confirmar Orden',
      write_answer: 'Escribe tu respuesta...',
      waiting_others: 'Esperando a los demás...',
      correct: '¡Correcto!',
      incorrect: '¡Incorrecto!',
      partial: '¡Parcial!',
      times_up: '¡Tiempo!',
      thanks: '¡Gracias!',
      sent: '¡Enviado!',
      no_answer: 'Sin respuesta',
      no_points: 'Sin puntos',
      streak: 'Racha',
      total: 'Total',
      rank_of: 'Estás <strong>#{rank}</strong> de {total} jugadores',
      waiting_next: 'Esperando la siguiente pregunta...',
      game_over: '¡Juego terminado!',
      your_position: 'Tu posición',
      top_players: 'Top jugadores',
      play_again: 'Jugar de nuevo',
      your_review: 'Tu repaso',
      your_answer: 'Tu respuesta',
      right_answer: 'Respuesta correcta',
      reconnecting: 'Reconectando...',
      conn_lost: 'Conexión perdida. Recarga la página.',
      session_gone: 'La sesión ya no está disponible.',
      kicked_msg: 'El profesor te ha sacado de la sala.',
      room_locked: 'La sala está cerrada. Pide al profesor que la abra.',
      nickname_not_allowed: 'Ese apodo no está permitido. Usa tu nombre real.',
      nickname_taken: 'Ese apodo ya está en uso',
      room_not_found: 'Sala no encontrada',
      game_in_progress: 'El juego ya comenzó',
      enter_room_code: 'Ingresa un código de sala de 6 caracteres',
      enter_nickname: 'Ingresa un apodo',
      quiz_stopped: 'El quiz ha sido detenido por el profesor.',
      quiz_ended_title: 'EL QUIZ HA TERMINADO',
      quiz_ended_sub: 'El profesor ha detenido la sesión.',
      your_final_score: 'Tu puntuación final',
      your_team: 'Tu equipo',
      question: 'Pregunta',
      questions: 'preguntas',
      // Host
      room_code: 'Código de Sala',
      start_game: 'Iniciar Juego',
      waiting_players: 'Esperando jugadores...',
      players_ready: '{n} jugador(es) listos',
      stop_quiz: '■ Detener Quiz',
      reveal_now: 'Revelar',
      next: 'Siguiente →',
      live_answers: 'Respuestas en vivo',
      words_sent: 'Palabras enviadas',
      students_writing: 'Los estudiantes están escribiendo...',
      wordcloud_title: 'NUBE DE PALABRAS',
      top_5: 'Top Jugadores',
      teams: 'Equipos',
      team_mode: 'Equipos',
      team_off: 'Individual',
      class_label: 'Clase',
      class_open: 'Abierta (cualquier nombre)',
      lock_room: 'Cerrar sala',
      unlock_room: 'Abrir sala',
      kick_confirm: '¿Sacar a {name} de la sala?',
      stop_confirm: '¿Detener el quiz? Esto terminará la sesión para todos los jugadores.',
      stop_error: 'Error al detener el quiz. Intenta de nuevo.',
      partial_results: 'RESULTADOS PARCIALES',
      stopped_at: 'Quiz detenido en pregunta {a} de {b}',
      go_dashboard: 'IR AL DASHBOARD →',
      projector: 'Proyector',
      sound: 'Sonido',
      read_the_question: 'Lee la pregunta',
      game_over_host: 'Game Over!',
      // Assignment
      hw_who: '¿Quién eres?',
      hw_start: 'Comenzar →',
      hw_next: 'Siguiente →',
      hw_submit: 'Entregar ✓',
      hw_done: 'Tarea entregada',
      hw_review: 'Revisión',
      hw_deadline: 'Fecha límite',
      hw_closed: 'Esta tarea ya cerró.',
      hw_not_found: 'Esta tarea no existe.',
      hw_already: 'Ya entregaste esta tarea.',
      hw_all_done: 'Todos los estudiantes de la clase ya entregaron.',
      hw_pick_name: 'Escribe o selecciona tu nombre.',
      hw_load_error: 'No se pudo cargar la tarea.',
      hw_submit_error: 'No se pudo entregar. Intenta de nuevo.',
      hw_confirm_submit: '¿Entregar la tarea?',
      hw_unanswered: 'Tienes preguntas sin responder:',
      hw_correct: 'correctas',
      hw_your_answer: 'Tu respuesta',
      hw_right_answer: 'Respuesta correcta',
      hw_write_answer: 'Escribe tu respuesta…',
      // Admin chrome (light touch)
      nav_classes: 'Clases',
      nav_new_quiz: '+ Nuevo Quiz',
      nav_logout: 'Salir',
      nav_edit: 'Editar',
      nav_assignments: 'Tarea'
    },
    en: {
      join_title: 'Enter the room code and your nickname',
      room_code_label: 'Room Code',
      nickname_label: 'Nickname',
      nickname_placeholder: 'Your name',
      join_btn: 'Join →',
      pick_your_name: 'Pick your name',
      youre_in: "You're in!",
      waiting_start: 'Waiting for the teacher to start...',
      game_starting: 'The game is starting!',
      players: 'Players',
      read_question: 'Read the question...',
      confirm: 'Confirm',
      confirm_order: 'Confirm Order',
      write_answer: 'Type your answer...',
      waiting_others: 'Waiting for the others...',
      correct: 'Correct!',
      incorrect: 'Incorrect!',
      partial: 'Partial!',
      times_up: "Time's up!",
      thanks: 'Thanks!',
      sent: 'Sent!',
      no_answer: 'No answer',
      no_points: 'No points',
      streak: 'Streak',
      total: 'Total',
      rank_of: "You're <strong>#{rank}</strong> of {total} players",
      waiting_next: 'Waiting for the next question...',
      game_over: 'Game over!',
      your_position: 'Your position',
      top_players: 'Top players',
      play_again: 'Play again',
      your_review: 'Your review',
      your_answer: 'Your answer',
      right_answer: 'Correct answer',
      reconnecting: 'Reconnecting...',
      conn_lost: 'Connection lost. Reload the page.',
      session_gone: 'The session is no longer available.',
      kicked_msg: 'The teacher removed you from the room.',
      room_locked: 'The room is locked. Ask the teacher to open it.',
      nickname_not_allowed: 'That nickname is not allowed. Use your real name.',
      nickname_taken: 'That nickname is already taken',
      room_not_found: 'Room not found',
      game_in_progress: 'Game already in progress',
      enter_room_code: 'Enter a 6-character room code',
      enter_nickname: 'Enter a nickname',
      quiz_stopped: 'The quiz was stopped by the teacher.',
      quiz_ended_title: 'THE QUIZ HAS ENDED',
      quiz_ended_sub: 'The teacher stopped the session.',
      your_final_score: 'Your final score',
      your_team: 'Your team',
      question: 'Question',
      questions: 'questions',
      room_code: 'Room Code',
      start_game: 'Start Game',
      waiting_players: 'Waiting for players...',
      players_ready: '{n} player(s) ready',
      stop_quiz: '■ Stop Quiz',
      reveal_now: 'Reveal',
      next: 'Next →',
      live_answers: 'Live Answers',
      words_sent: 'Words sent',
      students_writing: 'Students are typing...',
      wordcloud_title: 'WORD CLOUD',
      top_5: 'Top Players',
      teams: 'Teams',
      team_mode: 'Teams',
      team_off: 'Individual',
      class_label: 'Class',
      class_open: 'Open (any name)',
      lock_room: 'Lock room',
      unlock_room: 'Unlock room',
      kick_confirm: 'Remove {name} from the room?',
      stop_confirm: 'Stop the quiz? This ends the session for all players.',
      stop_error: 'Could not stop the quiz. Try again.',
      partial_results: 'PARTIAL RESULTS',
      stopped_at: 'Quiz stopped at question {a} of {b}',
      go_dashboard: 'GO TO DASHBOARD →',
      projector: 'Projector',
      sound: 'Sound',
      read_the_question: 'Read the question',
      game_over_host: 'Game Over!',
      hw_who: 'Who are you?',
      hw_start: 'Start →',
      hw_next: 'Next →',
      hw_submit: 'Submit ✓',
      hw_done: 'Assignment submitted',
      hw_review: 'Review',
      hw_deadline: 'Deadline',
      hw_closed: 'This assignment is closed.',
      hw_not_found: 'This assignment does not exist.',
      hw_already: 'You already submitted this assignment.',
      hw_all_done: 'Every student in the class has already submitted.',
      hw_pick_name: 'Type or pick your name.',
      hw_load_error: 'Could not load the assignment.',
      hw_submit_error: 'Could not submit. Try again.',
      hw_confirm_submit: 'Submit the assignment?',
      hw_unanswered: 'You have unanswered questions:',
      hw_correct: 'correct',
      hw_your_answer: 'Your answer',
      hw_right_answer: 'Correct answer',
      hw_write_answer: 'Type your answer…',
      nav_classes: 'Classes',
      nav_new_quiz: '+ New Quiz',
      nav_logout: 'Logout',
      nav_edit: 'Edit',
      nav_assignments: 'Assign'
    }
  };

  var lang = localStorage.getItem('quizlab-lang') || 'es';
  if (lang !== 'es' && lang !== 'en') lang = 'es';

  window.qlLang = lang;

  window.qlT = function (key, fallback) {
    var table = STRINGS[lang] || STRINGS.es;
    if (key in table) return table[key];
    if (key in STRINGS.es) return STRINGS.es[key];
    return fallback !== undefined ? fallback : key;
  };

  window.qlToggleLang = function () {
    localStorage.setItem('quizlab-lang', lang === 'es' ? 'en' : 'es');
    location.reload();
  };

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var key = el.getAttribute('data-i18n');
      var val = window.qlT(key, null);
      if (val !== null) el.textContent = val;
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      var key = el.getAttribute('data-i18n-placeholder');
      var val = window.qlT(key, null);
      if (val !== null) el.placeholder = val;
    });
    var btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = lang === 'es' ? 'EN' : 'ES';
  });
})();
