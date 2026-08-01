/* Дашборд — переключение вкладок-графиков и источника (Все / портал; список строится
   из данных: hh / hirify / talanto / getmatch).
   Статика без данных и импортов: esbuild не нужен, dashboard.py копирует файл как есть.
   Панель = pane-<источник>-<график>; если у источника нет варианта графика — фолбэк на 'all'. */
let _src = 'all';
let _key = null;

function _paneFor(src, key) {
  return document.getElementById(`pane-${src}-${key}`)
      || document.getElementById(`pane-all-${key}`);   /* фолбэк: у источника нет этого графика */
}

function _render() {
  document.querySelectorAll('.tab-pane').forEach(p => { p.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(b => { b.classList.remove('active'); });
  document.querySelectorAll('.src-btn').forEach(b => { b.classList.remove('active'); });
  const pane = _paneFor(_src, _key);
  if (pane) { pane.classList.add('active'); }
  const btn = document.getElementById(`btn-${_key}`);
  if (btn) { btn.classList.add('active'); }
  const sbtn = document.getElementById(`src-${_src}`);
  if (sbtn) { sbtn.classList.add('active'); }
  window.dispatchEvent(new Event('resize'));            /* Plotly перерисует под размер видимой панели */
}

function showTab(key) { _key = key; _render(); }
function showSource(src) { _src = src; _render(); }

/* ── Тема ──
   Страничные цвета живут в CSS-токенах, а вот диаграммы отрисованы на СЕРВЕРЕ с тёмными
   поверхностями, зашитыми в фигуру, — CSS туда не достаёт. Поэтому на переключении
   прогоняем relayout по каждому графику. Заодно разворачиваем последовательные шкалы:
   на тёмном фоне они должны расти к светлому, на светлом — наоборот, иначе максимум
   снова окажется самым незаметным. */
function _applyPlotlyTheme(light) {
  if (!window.Plotly) return;
  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const paper = v('--paper');
  const bg = v('--bg');
  const text = v('--text');
  const grid = v('--grid');
  /* Именно `.plotly-graph-div`, а не `.tab-pane > div`: pio.to_html кладёт график на уровень
     глубже, во внешнюю обёртку, у которой нет `.data` — по прежнему селектору перекраска
     молча не делала ничего, и в светлой теме диаграммы оставались тёмными. */
  document.querySelectorAll('.plotly-graph-div').forEach(div => {
    if (!div.data) return;                       /* фигура ещё не отрисована */
    try {
      window.Plotly.relayout(div, {
        paper_bgcolor: paper, plot_bgcolor: bg,
        'font.color': text, 'title.font.color': text,
        'xaxis.gridcolor': grid, 'yaxis.gridcolor': grid,
        'legend.font.color': text,
      });
      div.data.forEach((trace, i) => {
        const patch = {};
        if (trace.colorscale) patch.reversescale = !light;
        if (trace.colorbar) patch['colorbar.tickfont.color'] = text;
        if (trace.marker?.colorbar) patch['marker.colorbar.tickfont.color'] = text;
        /* усы IQR: светло-жёлтый рассчитан на тёмный фон, на белом он почти невидим */
        if (trace.error_y) patch['error_y.color'] = light ? '#A8730F' : '#EECA3B';
        if (Object.keys(patch).length) window.Plotly.restyle(div, patch, [i]);
      });
    } catch { /* одна сломанная фигура не должна ронять переключение темы */ }
  });
}

function _applyTheme(theme) {
  const light = theme === 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.setAttribute('aria-label', light ? 'Включить тёмную тему' : 'Включить светлую тему');
    btn.setAttribute('aria-pressed', String(light));
  }
  _applyPlotlyTheme(light);
}

function initDashboard(src, key) {
  _src = src;
  _key = key;
  _render();
  _applyTheme(localStorage.getItem('feed.theme') === 'light' ? 'light' : 'dark');
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', e => {
      const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
      const swap = () => {
        localStorage.setItem('feed.theme', next);  /* один ключ с лентой — тема общая */
        _applyTheme(next);
      };
      _themeTransition(swap, e.currentTarget);
    });
  }
}

/* Круговой раскрыв новой темы из кнопки; без View Transitions — переливание цветов
   временным классом. Компактный дубль `view.js::runThemeTransition`: этот файл намеренно
   без импортов и сборки (dashboard.py копирует его как есть). */
function _themeTransition(swap, origin) {
  const root = document.documentElement;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (reduce || !document.startViewTransition) {
    root.classList.add('theme-anim');
    swap();
    setTimeout(() => { root.classList.remove('theme-anim'); }, 360);
    return;
  }
  const r = origin?.getBoundingClientRect?.() || null;
  const x = r ? r.left + r.width / 2 : window.innerWidth;
  const y = r ? r.top + r.height / 2 : 0;
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
  root.classList.add('theme-swap');            /* см. .theme-swap в dashboard.css.j2 */
  const vt = document.startViewTransition(swap);
  vt.finished.finally(() => { root.classList.remove('theme-swap'); });
  vt.ready.then(() => {
    root.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
      { duration: 480, easing: 'ease-in-out', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => { /* переход мог быть прерван вторым кликом — тема уже применена */ });
}

window.showTab = showTab;          /* onclick из шаблона */
window.showSource = showSource;
window.initDashboard = initDashboard;
