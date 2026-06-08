function toggleTheme() {
  var html = document.documentElement;
  var current = html.getAttribute('data-theme');
  var next = current === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('quizlab-theme', next);
  updateThemeIcon(next);
}

function updateThemeIcon(theme) {
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.setAttribute('data-mode', theme);
}

document.addEventListener('DOMContentLoaded', function () {
  var current = document.documentElement.getAttribute('data-theme');
  updateThemeIcon(current);
});
