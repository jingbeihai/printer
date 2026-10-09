(() => {
  const button = document.querySelector('.motion-toggle');
  const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
  let paused = preference.matches;
  const update = () => {
    document.body.classList.toggle('motion-paused', paused);
    button.setAttribute('aria-pressed', String(paused));
    button.textContent = paused ? '播放动画' : '暂停动画';
    button.hidden = preference.matches;
  };
  button.addEventListener('click', () => { paused = !paused; update(); });
  preference.addEventListener('change', () => { paused = preference.matches; update(); });
  const visibility = () => document.body.classList.toggle('page-hidden', document.hidden);
  document.addEventListener('visibilitychange', visibility);
  visibility();
  update();
})();
