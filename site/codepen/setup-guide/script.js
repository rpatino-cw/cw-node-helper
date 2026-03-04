// Nav scroll effect
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 20);
});

// Progress bar
const progressBar = document.getElementById('progressBar');
window.addEventListener('scroll', () => {
  const h = document.documentElement.scrollHeight - window.innerHeight;
  const pct = h > 0 ? (window.scrollY / h) * 100 : 0;
  progressBar.style.height = pct + '%';
});

// Tab switching
document.querySelectorAll('.option-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const group = tab.closest('.option-tabs').dataset.group;
    const target = tab.dataset.tab;
    // Deactivate all tabs + panels in this group
    document.querySelectorAll(`.option-tabs[data-group="${group}"] .option-tab`).forEach(t => t.classList.remove('active'));
    document.querySelectorAll(`.option-panel[data-group="${group}"]`).forEach(p => p.classList.remove('active'));
    // Activate clicked
    tab.classList.add('active');
    document.querySelector(`.option-panel[data-panel="${target}"][data-group="${group}"]`).classList.add('active');
  });
});
