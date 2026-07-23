/* Дашборд — переключение вкладок-графиков и источника (Все / hh / hirify).
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

function initDashboard(src, key) { _src = src; _key = key; _render(); }

window.showTab = showTab;          /* onclick из шаблона */
window.showSource = showSource;
window.initDashboard = initDashboard;
