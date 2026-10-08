// Apply the saved theme before the stylesheet loads, to avoid a flash of the wrong theme.
(() => {
  const key = 'work-tracker-theme';
  const system = matchMedia('(prefers-color-scheme: dark)');
  let choice = 'system';
  try {
    const saved = localStorage.getItem(key);
    if (saved === 'light' || saved === 'dark') choice = saved;
  } catch { /* Storage can be unavailable; the control still works for this page. */ }

  function apply() {
    document.documentElement.dataset.theme = choice === 'system' ? (system.matches ? 'dark' : 'light') : choice;
    const control = document.getElementById('theme');
    if (control) {
      const label = document.documentElement.dataset.theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
      control.setAttribute('aria-label', label);
      control.title = label;
    }
  }

  apply();
  system.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    const control = document.getElementById('theme');
    apply();
    control.addEventListener('click', () => {
      choice = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      apply();
      try {
        localStorage.setItem(key, choice);
      } catch { /* Keep the choice in memory when storage is unavailable. */ }
    });
  });
})();
