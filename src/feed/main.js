/* Main — точка сборки (bootstrap): создаёт Store, подписывает View, связывает
   DOM-события с командами Store, оркеструет сохранение отметок. Поток:
   событие -> store.update -> View.render. Глобалы VACANCIES/SAL_MAX/SAVED_MARKS
   приходят из feed-data.js (грузится <script> до бандла). */

import {
  applyVacancy, exportMarks, importMarks, loadInitialMarks,
  pullJson, pushServer, saveLocal,
} from './marks.js';
import {
  APPLY_LABELS, abCompare, abText, appliedInRange, cardPaint, convert, countActiveFilters, crmStats,
  effectiveApplied, effectiveChat, effectiveStatus, esc, fmtK, isPersonalChat, journalById,
  pickSummary, pulseLine, staleNow, syntheticCard, waitingByAccount,
} from './model.js';
import { createStore } from './store.js';
import {
  applyCardStatus, bustCard, closeModal, refreshCardStatus, render, runThemeTransition,
  setAccounts, setStale, setSync, setThemePaper, showModal,
} from './view.js';

const V_MAP = Object.fromEntries(VACANCIES.map(v => [v.id, v]));
/* Канон городов из выдачи: отличает выбор из списка (точное совпадение) от набранной вручную
   подстроки — иначе выбранная «Москва» тянула бы ещё и «Московский». */
const CITY_SET = new Set(VACANCIES.map(v => v.city).filter(Boolean));

const CUR_DEFAULT = 'RUB';   /* дефолтный набор валют показа — одна валюта; см. блок «Валюты» ниже */

const store = createStore({
  vacancies: VACANCIES,
  marks: loadInitialMarks(),
  serverMode: false,
  langs: new Set(),
  roles: new Set(),
  showNonIt: false,
  exps: new Set(),
  emps: new Set(),           /* формы оформления: ТК/самозанятый/ИП/ГПХ + «не указано» */
  minSal: 0,
  maxSal: SAL_MAX,
  salMax: SAL_MAX,
  city: '',
  cityExact: false,          /* true — значение выбрано из datalist, а не набрано частично */
  schedule: 'all',
  status: 'all',
  source: 'all',
  displayCurs: [CUR_DEFAULT],
  dateFrom: '',
  dateTo: '',
  sort: 'none',
  matchSort: false,
  resumeOnly: false,
  chatFilter: '',            /* '' | 'wait' | 'manual' — состояние переписки */
  accountFilter: new Set(),  /* коды аккаунтов hh.ru — мультиселект профилей (RFC-004); пусто = все */
  search: [],
});

store.subscribe(render);   /* View перерисовывает список на изменение состояния */

/* ── Сохранение отметок (debounced на сервер, мгновенно в localStorage) ── */
let _saveTimer = null;
function persist() {
  const { marks, serverMode } = store.get();
  saveLocal(marks);
  if (!serverMode) { setSync('local'); return; }
  setSync('busy');
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(async () => {
    try { await pushServer(store.get().marks); setSync('ok'); }
    catch { setSync('err'); }
  }, 400);
}

function setStatus(id, st, card) {
  const marks = { ...store.get().marks };
  if (st) marks[id] = st; else delete marks[id];
  store.update({ marks }, false);     /* без полного ре-рендера — карточку красим точечно */
  /* Кнопку и тон считает то же правило, что и рендер (model.js::cardPaint): CRM-статус важнее
     ручной отметки, поэтому «✓» по вакансии с отказом работодателя НЕ подсвечивает кнопку —
     иначе она возвращалась к CRM-статусу на ближайшем тике оверлея, и клик «не срабатывал».
     Тон рамки при этом переживает ручной тоггл: жёлтое бот-интервью не зеленеет от «✓ Отклик». */
  if (card) {
    const { status, tone } = cardPaint(V_MAP[id], st);
    applyCardStatus(card, status, tone);
  }
  persist();
}

/* ── Модалка: открытие по клику на карточку, закрытие ── */
document.getElementById('cards').addEventListener('click', e => {
  const stBtn = e.target.closest('.status-btn');
  if (stBtn) {                                  /* тоггл статуса — модалку не открываем */
    const card = stBtn.closest('.card');
    const id   = card.dataset.id;
    const act  = stBtn.dataset.act;
    const next = store.get().marks[id] === act ? '' : act;
    setStatus(id, next, card);
    return;
  }
  const card = e.target.closest('.card');
  if (!card) return;
  const v = V_MAP[card.dataset.id];
  if (v) showModal(v);
});
/* Карточка — div с обработчиком клика, поэтому с клавиатуры она была недоступна вовсе.
   Разметка получила tabindex/role, здесь — реакция на Enter и пробел, как у кнопки. */
document.getElementById('cards').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const card = e.target.closest?.('.card');
  if (!card || e.target.closest('.status-btn, .card-title')) return;
  e.preventDefault();                              /* пробел не должен прокручивать список */
  const v = V_MAP[card.dataset.id];
  if (v) showModal(v);
});
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target.id === 'modal-overlay') closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* ── Кнопка «Откликнуться в фоне» в модалке (serve-режим) -> POST /api/apply ──
   Подписи исхода — из моста Python (`model.js::APPLY_LABELS` ← `APPLY_LABELS_PY`), словаря
   здесь нет: локальная копия уже разъехалась с сервером (не было метки `taken`, которую отдаёт
   server.py::_apply_post, и `captcha`) — аудит 2026-09-22, §3.2. */
document.getElementById('modal-box').addEventListener('click', async e => {
  const btn = e.target.closest('#m-apply');
  if (!btn) return;
  const id = btn.dataset.id;
  const cover = (document.getElementById('cover-text')?.value || '').trim();
  const statusEl = document.getElementById('m-apply-status');
  btn.disabled = true;
  if (statusEl) statusEl.textContent = '⏳ Откликаюсь… (браузер + HH, до минуты)';
  try {
    /* employer шлём рядом с name: журнал фиксирует работодателя в момент клика — без него
       карточка-призрак не ищется по компании (инцидент 01.08.2026, docs/errors.md). */
    const res = await applyVacancy(id, btn.dataset.url, cover,
                                   V_MAP[id]?.name || '', V_MAP[id]?.employer || '');
    if (statusEl) {
      statusEl.textContent = (APPLY_LABELS[res.status] || res.status) + (res.letter ? ' + письмо' : '');
    }
    if (res.status === 'applied' || res.status === 'already') {
      setStatus(id, 'applied');                 /* отметить в ленте */
    } else if (res.status === 'form') {
      const v = V_MAP[id];
      if (v) { v.needs_form = true; refreshCardStatus(v); refreshFormsChip(); }  /* живой бейдж «форма» */
      /* кнопку НЕ включаем: форма уже в очереди, повторный автоклик не нужен */
    } else if (res.status === 'queued') {
      if (statusEl) statusEl.textContent = `➕ в очереди крона (${res.position || '?'} в ожидании) — дожмёт после батча`;
      /* кнопку НЕ включаем: вакансия уже поставлена в очередь */
    } else {
      btn.disabled = false;
    }
  } catch {
    if (statusEl) statusEl.textContent = '⚠ сервер недоступен';
    btn.disabled = false;
  }
});

/* ── Фильтры ──
   Чипы-галочки: четыре группы отличались только селектором и ключом состояния (аудит
   2026-09-22, §3.3) — таблица разводит их одним циклом.
   ЛОВУШКА: `.lang-cb` — общий класс разметки чипа-галочки, поэтому языковой селектор обязан
   исключить ВСЕ специализированные чипы (опыт, оформление), иначе код формы попал бы в набор
   языков и обнулил выдачу. */
const CHECK_GROUPS = [
  ['.lang-cb:not(.exp-cb):not(.emp-cb)', 'langs'],
  ['.exp-cb', 'exps'],
  ['.emp-cb', 'emps'],
  ['.role-cb', 'roles'],
];
for (const [selector, key] of CHECK_GROUPS) {
  document.querySelectorAll(selector).forEach(cb => {
    cb.addEventListener('change', () => {
      const set = store.get()[key];
      if (cb.checked) set.add(cb.value); else set.delete(cb.value);
      store.update({ [key]: set });
    });
  });
}

/* Подписи свёрнутых выпадашек «Язык»/«Роль» (24.09.2026: пилюли ушли в выпадающие списки).
   Порядок — разметки; источник выбора — стор, поэтому подпись верна и после сброса фильтров. */
const PICK_SUMMARIES = [['lang-summary', '.lang-cb:not(.exp-cb):not(.emp-cb)', 'langs'],
                        ['role-summary', '.role-cb', 'roles']];
for (const [id, selector, key] of PICK_SUMMARIES) {
  const el = document.getElementById(id);
  if (!el) continue;
  const order = [...document.querySelectorAll(selector)].map(cb => cb.value);
  let last = '';
  store.subscribe(s => {
    const text = pickSummary(s[key], order);
    if (text !== last) { last = text; el.textContent = text; el.classList.toggle('active', text !== 'все'); }
  });
}

const nonitBtn = document.getElementById('nonit-toggle');
if (nonitBtn) nonitBtn.addEventListener('click', () => {
  const showNonIt = !store.get().showNonIt;
  nonitBtn.classList.toggle('active', showNonIt);
  store.update({ showNonIt });
});

const salMinEl  = document.getElementById('sal-min');
const salMaxEl  = document.getElementById('sal-max');
const salMinVal = document.getElementById('sal-min-val');
const salMaxVal = document.getElementById('sal-max-val');

salMinEl.addEventListener('input', () => {
  const minSal = +salMinEl.value;
  let { maxSal } = store.get();
  if (minSal > maxSal) { maxSal = minSal; salMaxEl.value = minSal; salMaxVal.textContent = fmtK(maxSal); }
  salMinVal.textContent = fmtK(minSal);
  store.update({ minSal, maxSal });
});
salMaxEl.addEventListener('input', () => {
  const maxSal = +salMaxEl.value;
  let { minSal } = store.get();
  if (maxSal < minSal) { minSal = maxSal; salMinEl.value = maxSal; salMinVal.textContent = fmtK(minSal); }
  salMaxVal.textContent = fmtK(maxSal);
  store.update({ minSal, maxSal });
});
salMinVal.textContent = fmtK(0);
salMaxVal.textContent = fmtK(SAL_MAX);

/* ── Валюты показа зарплат — МУЛЬТИВЫБОР: вилка печатается в КАЖДОЙ выбранной валюте
   (`model.js::fmtSalMulti`), свёрнуто набор видно в подписи `<summary>`.
   ПЕРВАЯ выбранная (порядок разметки) — валюта ШКАЛЫ ползунка и сортировки: вилки
   сравниваются в одной валюте, и это же число уходит в `comparableSalary`.
   ИНВАРИАНТ: SAL_MAX приходит из Python уже В РУБЛЯХ (feed.py::_salary_slider_max), поэтому
   здесь ровно одна конверсия RUB -> валюта шкалы. Раньше это число было максимумом по СЫРЫМ
   вилкам разных валют, и та же строка конвертировала узбекские сумы как рубли. ── */
const curBoxes = [...document.querySelectorAll('.cur-cb')];

function pickedCurs() {
  return curBoxes.filter(cb => cb.checked).map(cb => cb.value);
}

/* Подпись свёрнутой группы — выбранные валюты в порядке разметки. */
function curSummary(curs) {
  const el = document.getElementById('cur-summary');
  if (!el) return;
  el.textContent = curBoxes.filter(cb => curs.includes(cb.value))
    .map(cb => cb.parentElement.textContent.trim()).join(' ') || CUR_DEFAULT;
}

function rescaleSalary(curs) {
  const cur = curs[0];
  const max = Math.round(convert(SAL_MAX, 'RUR', cur));
  salMinEl.max = max; salMaxEl.max = max;
  salMinEl.value = 0; salMaxEl.value = max;
  salMinVal.textContent = fmtK(0);
  salMaxVal.textContent = fmtK(max);
  store.update({ minSal: 0, maxSal: max, salMax: max, displayCurs: curs });
}

/* Сброс набора к дефолту (рубль) — из `resetFilters`, чтобы дефолт не был закодирован дважды. */
function resetCurs() {
  curBoxes.forEach(cb => { cb.checked = cb.value === CUR_DEFAULT; });
  curSummary([CUR_DEFAULT]);
}
/* Активация пилюли: `active` — ровно на одной кнопке группы (снять с соседей, зажечь нажатой).
   Блок был скопирован пять раз подряд (`[data-cur]`/`[data-sort]`/`[data-status]`/`[data-source]`,
   аудит 2026-09-22, §3.3): теперь одна функция на все группы. Группы-селекты и мультивыбор
   валют в подсветке не нуждаются — состояние видно по самой выпадашке/набору галочек. */
function activate(btn) {
  btn.closest('.sched-btns')?.querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
  btn.classList.add('active');
}

/* Дефолты пилюль — ОДИН список на три роли: атрибут группы в разметке, значение, которое
   зажигается, и ключ состояния ленты. Раньше дефолты были закодированы дважды (разметка +
   resetFilters против store), а два селектора из пяти брались без null-guard: удаление кнопки
   из разметки роняло сброс TypeError-ом (аудит 2026-09-22, §5). */
const UI_DEFAULTS = [['data-sort', 'none', 'sort'], ['data-status', 'all', 'status']];
/* Группы-СЕЛЕКТЫ (Формат, Портал): тот же контракт, что у пилюль — id элемента, ключ состояния,
   дефолт. Отдельный список, потому что пилюли зажигаются классом `active`, а селект — значением
   элемента; роль и та, и другая одна: ОДИН список дефолтов на разметку и сброс. «Портал» был
   рядом из 13 пилюль до 24.09.2026 (решение владельца, по образцу «Формата» 23.09). */
const SELECT_GROUPS = [['sched-sel', 'schedule', 'all'], ['source-sel', 'source', 'all']];

/* Сброс группы к дефолту: зажечь дефолтную пилюлю, а если её нет в разметке — просто снять
   активность со всей группы (единый null-guard вместо падения). */
function resetPills(attr, value) {
  const def = document.querySelector(`[${attr}="${value}"]`);
  if (def) { activate(def); return; }
  document.querySelectorAll(`[${attr}]`).forEach(b => { b.classList.remove('active'); });
}

/* Смена набора: пустым он быть НЕ МОЖЕТ (печатать нечего) — последнюю галочку возвращаем на
   месте, ровно как прежние пилюли не давали снять единственную активную валюту. */
curBoxes.forEach(cb => {
  cb.addEventListener('change', () => {
    if (!curBoxes.some(b => b.checked)) cb.checked = true;
    const curs = pickedCurs();
    curSummary(curs);
    rescaleSalary(curs);
  });
});
curSummary(pickedCurs());   /* подпись при загрузке: набор уже проставлен `checked` в разметке */

/* Группы-селекты: смена значения сразу пишет код фильтра в состояние (ре-фильтр идёт по всей
   выдаче). Селект отдельного класса-подсветки не требует — видно выбранную опцию. */
for (const [id, key] of SELECT_GROUPS) {
  const sel = document.getElementById(id);
  if (sel) sel.addEventListener('change', () => store.update({ [key]: sel.value }));
}

/* Пилюли-переключатели: группы `[data-sort]`/`[data-status]` отличались только атрибутом и
   ключом состояния (аудит 2026-09-22, §3.3). Атрибут и есть ключ `dataset`, второй столбец —
   ключ состояния.
   ЛОВУШКА: `#mine-toggle` (инжектится оверлеем ПОЗЖЕ, несёт `data-status="mine"`) — свой
   тоггл-обработчик; общий цикл идёт по разметке на старте и доинжектенных кнопок не видит. */
const PILL_GROUPS = [['sort', 'sort'], ['status', 'status']];
for (const [attr, key] of PILL_GROUPS) {
  document.querySelectorAll(`[data-${attr}]`).forEach(btn => {
    btn.addEventListener('click', () => {
      activate(btn);
      store.update({ [key]: btn.dataset[attr] });
    });
  });
}

/* Город: выбор из datalist даёт ТОЧНОЕ имя (cityExact), свободный ввод — поиск по подстроке
   («сан» -> Санкт-Петербург). Дебаунс как у поиска: ре-фильтр идёт по всей выдаче. */
(() => {
  const inp = document.getElementById('city-inp');
  if (!inp) return;
  let t = null;
  inp.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => {
      const city = inp.value.trim();
      store.update({ city, cityExact: CITY_SET.has(city) });
    }, 150);
  });
})();

const resumeBtn = document.getElementById('resume-toggle');
resumeBtn.addEventListener('click', () => {
  const resumeOnly = !store.get().resumeOnly;
  resumeBtn.classList.toggle('active', resumeOnly);
  store.update({ resumeOnly });
});

const matchBtn = document.getElementById('match-toggle');
if (matchBtn) matchBtn.addEventListener('click', () => {
  const matchSort = !store.get().matchSort;
  matchBtn.classList.toggle('active', matchSort);
  store.update({ matchSort });   /* совпадение — основной ключ; зарплата ↑/↓ остаётся вторичным */
});

function resetFilters() {
  /* Один проход по выдаче пересчитывает ВСЕ профильные поля (статус, чат, отклик), а не только
     статусы: сброс профилей оставлял чаты и отклики от прежнего аккаунта до следующего тика
     оверлея. До store.update — ре-рендер должен увидеть уже общие значения. */
  recomputeForAccounts(new Set());
  /* Состояние сброса: ключи пилюль дописывает цикл ниже (из UI_DEFAULTS) — так значение
     дефолта не разъезжается между разметкой, UI и стором. */
  const state = {
    minSal: 0, maxSal: SAL_MAX, salMax: SAL_MAX, city: '', cityExact: false,
    dateFrom: '', dateTo: '', matchSort: false, resumeOnly: false, search: [],
    showNonIt: false, chatFilter: '', accountFilter: new Set(),
    displayCurs: [CUR_DEFAULT],          /* валюты показа — галочки ставит resetCurs ниже */
  };
  store.get().langs.clear();
  store.get().exps.clear();
  store.get().emps.clear();
  store.get().roles.clear();
  document.querySelectorAll('.lang-cb, .role-cb').forEach(cb => { cb.checked = false; });
  nonitBtn?.classList.remove('active');
  salMinEl.max = SAL_MAX; salMaxEl.max = SAL_MAX;          /* валюта -> дефолт RUB */
  salMinEl.value = 0; salMaxEl.value = SAL_MAX;
  salMinVal.textContent = fmtK(0); salMaxVal.textContent = fmtK(SAL_MAX);
  for (const [attr, value, key] of UI_DEFAULTS) { resetPills(attr, value); state[key] = value; }
  for (const [id, key, def] of SELECT_GROUPS) {           /* селекты: дефолт — значением */
    const sel = document.getElementById(id);
    if (sel) sel.value = def;
    state[key] = def;
  }
  resetCurs();                                            /* валюты: дефолт — набором галочек */
  const ci = document.getElementById('city-inp');
  if (ci) ci.value = '';
  resumeBtn.classList.remove('active');
  if (matchBtn) matchBtn.classList.remove('active');
  /* `#search-input` живёт в разметке липкой полосы (templates/feed.html.j2), поэтому guard
     на его отсутствие больше не нужен. */
  document.getElementById('search-input').value = '';
  store.update(state);
}
window.resetFilters = resetFilters;   /* вызывается из onclick в feed.html.j2 */

/* ── Поиск по названию/компании — живёт в ЛИПКОЙ полосе, а не в панели фильтров:
      панель сворачивается, а поиск нужен всегда. Сам `#search-input` — в разметке полосы
      (templates/feed.html.j2, перед .toolbar-spacer): поле, созданное из JS, жило только до
      первой полосы с разметкой и требовало фолбэка на сворачиваемую панель. Здесь остаётся
      только привязка дебаунса: ре-фильтр идёт по всей выдаче. ── */
(function initSearch() {
  const inp = document.getElementById('search-input');
  let t = null;
  inp.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => {
      const search = inp.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      store.update({ search });
    }, 150);
  });
})();

/* ── Тема: тёмная (по умолчанию) / светлая ──
   Переключатель в шапке: иконка показывает ТЕКУЩУЮ тему (луна = сейчас тёмная), подпись
   кнопки говорит, что произойдёт по клику. Палитра целиком в CSS-токенах, поэтому здесь
   меняется один атрибут на <html>. Единственное, что нельзя отдать CSS, — тона, которые
   считаются под контраст с подложкой (цвет технологии, бейдж возраста): их пересчитывает
   view.js, поэтому подложку берём из самих токенов, а не дублируем константой в JS. */
(function initTheme() {
  const btn = document.getElementById('theme-toggle');
  const KEY = 'feed.theme';
  const apply = theme => {
    const light = theme === 'light';
    document.documentElement.dataset.theme = light ? 'light' : 'dark';
    if (btn) {
      btn.setAttribute('aria-label', light ? 'Включить тёмную тему' : 'Включить светлую тему');
      btn.setAttribute('aria-pressed', String(light));
    }
    const paper = getComputedStyle(document.documentElement).getPropertyValue('--paper').trim();
    setThemePaper(paper);
  };
  apply(localStorage.getItem(KEY) === 'light' ? 'light' : 'dark');
  btn?.addEventListener('click', e => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    const swap = () => {
      localStorage.setItem(KEY, next);
      apply(next);
      store.update({});                  /* перерисовать карточки с пересчитанными тонами */
    };
    runThemeTransition(swap, e.currentTarget);
  });
})();

/* ── Панель фильтров: сворачивание + счётчик активных ──
   Панель занимала 40% экрана на 1600px и 77% на 900px, оставаясь липкой при прокрутке.
   Теперь она обычный блок, который можно свернуть; состояние помнится между сессиями,
   а счётчик на кнопке не даёт забыть, что выдача урезана свёрнутыми фильтрами. */
(function initFiltersPanel() {
  const btn = document.getElementById('filters-toggle');
  const panel = document.getElementById('filter-bar');
  const badge = document.getElementById('filters-count');
  if (!btn || !panel) return;
  const KEY = 'feed.filtersOpen';
  const apply = open => {
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
  };
  apply(localStorage.getItem(KEY) !== '0');
  btn.addEventListener('click', () => {
    const open = panel.hidden;                    /* было скрыто -> открываем */
    apply(open);
    localStorage.setItem(KEY, open ? '1' : '0');
  });
  if (badge) {
    store.subscribe(s => {
      const n = countActiveFilters(s);
      badge.textContent = String(n);
      badge.hidden = n === 0;
    });
  }
})();

/* ── Экспорт/импорт отметок ── */
const expBtn = document.getElementById('marks-export');
if (expBtn) expBtn.addEventListener('click', () => exportMarks(store.get().marks));
const impInput = document.getElementById('marks-import');
if (impInput) impInput.addEventListener('change', async e => {
  const file = e.target.files?.[0];
  e.target.value = '';
  if (!file) return;
  const marks = await importMarks(file);
  if (!marks) { alert('Не удалось прочитать marks.json'); return; }
  store.update({ marks });
  persist();
});

/* ── Подхватить marks.json с сервера (serve-режим): GET того же эндпоинта, что и pushServer.
   Прежний отдельный GET-хелпер отметок был второй реализацией marks.js::pullJson (тела
   совпадали символ в символ, различался только путь) — аудит 2026-09-22, §3.4. ── */
async function initSync() {
  const disk = await pullJson('api/marks');
  if (disk === null) { setSync('local'); return; }
  /* слияние: локальные-только отметки не теряем, диск — приоритет */
  const merged = { ...store.get().marks, ...disk };
  const needPush = JSON.stringify(merged) !== JSON.stringify(disk);
  store.update({ marks: merged, serverMode: true });
  saveLocal(merged);
  if (needPush) persist(); else setSync('ok');
}

/* Локальная дата 'YYYY-MM-DD' (по часовому поясу пользователя, не UTC — иначе у полуночи
   съезжает на день). Для пресетов календаря. */
function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/* Журнал откликов -> обогащение существующих карточек + синтез «призраков» для откликов
   на вакансии, которых нет в текущем сборе (видны в «Мои отклики» и под чат-фильтрами).
   Свёртка журнала и форма карточки — в model.js (чистые, под тестом). */
function applyJournal(applied) {
  let count = 0;
  const filter = store.get().accountFilter;
  for (const [id, a] of Object.entries(journalById(applied))) {
    let v = V_MAP[id];
    if (!v) { v = syntheticCard(id, a); VACANCIES.push(v); V_MAP[id] = v; }
    /* Отклик ПО АККАУНТАМ (RFC-004): показываемая запись (дата, via) — профиля из фильтра;
       полный набор accounts остаётся для метки-конфликта. Пересчёт при смене фильтра — ниже. */
    v.appliedByAcct = a.byAcct;
    v.appliedAccounts = a.accounts;
    v.applied = effectiveApplied(a.byAcct, filter, a.accounts);
    bustCard(id); count++;
  }
  return count;
}

/* Панель аккаунтов hh.ru (RFC-004): баннер «нужен вход», сводка по меткам и чипы-фильтры
   по профилю. Рисуется только при ≥2 аккаунтах — с одним лента прежняя. Идемпотентна:
   ре-полл оверлея перерисовывает её на месте. */
const SESSION_WORD = { ok: '', expired: 'нужен вход', foreign: 'чужая сессия', unknown: 'вход не проверен' };
/* Пересчитать показываемые по выбранным профилям поля карточек (RFC-004): статус, чат и отклик
   у вакансии берутся у профиля из фильтра, а не у основного аккаунта. Было три копии одного
   прохода по всей выдаче (статусы / чаты / отклики, аудит 2026-09-22, §3.3), причём отклики
   пересчитывались построчно тем же effectiveApplied, что вызывающий (applyJournal) уже применил.

   Флаги для вызывающих: `changed` — изменился хоть один показываемый СТАТУС (нужен ре-рендер
   бейджей), `any` — нашлась хоть одна вакансия с чатом или откликом по аккаунтам (на первом
   полле это тоже повод перерисовать список). */
function recomputeForAccounts(filter = store.get().accountFilter) {
  let changed = false, any = false;
  for (const v of VACANCIES) {
    if (v.statusByAcct) {
      const st = effectiveStatus(v.statusByAcct, filter);
      if (v.status !== st) { v.status = st; bustCard(v.id); changed = true; }
    }
    /* Чат — объект (на ре-полле новый инстанс), поэтому переустанавливаем и бустим карточку
       безусловно: под фильтром acc2 показываем чат и его дату у acc2, а не у основного
       (иначе на бейдже отказа светилась бы дата main). */
    if (v.chatByAcct) {
      v.chat = effectiveChat(v.chatByAcct, filter);
      bustCard(v.id); any = true;
    }
    /* Отклик: дата и via на бейдже 📮, счётчик «Показать (N)» и фильтр по периоду — по
       выбранному профилю, а не по самому раннему среди всех. */
    if (v.appliedByAcct) {
      v.applied = effectiveApplied(v.appliedByAcct, filter, v.appliedAccounts);
      bustCard(v.id); any = true;
    }
  }
  return { changed, any };
}

/* Счётчики чипов «Чаты» из ЭФФЕКТИВНОГО чата (под текущим фильтром профиля): ждут ответа,
   личные (живой человек, открыт), с контактами. Под фильтром acc2 считаются чаты acc2.
   «Личный» считает model.js::isPersonalChat — то же правило, что отбирает ветка
   `chatFilter === 'personal'` в filterVacancies, иначе счётчик на чипе расходился бы с выдачей. */
function chatCounts() {
  let count = 0, personal = 0, contacts = 0;
  for (const v of VACANCIES) {
    const info = v.chat;
    if (!info) continue;
    if (info.contact) contacts++;
    if (info.needs_reply) {
      count++;
      if (isPersonalChat(info)) personal++;
    }
  }
  return { count, personal, contacts };
}

/* Единый конвейер «пересчёт -> chatCounts -> injectChatFilter»: он был написан дважды
   (setAccountFilter и initOverlay) и условия вставки уже разъехались — `|| count` против
   `waiting || contacts`. Условие одно: группа нужна, если есть что показать (ждут ответа или
   есть контакты). Существующую группу injectChatFilter освежает на месте (идемпотентен),
   поэтому счётчики не застревают на числах прежнего профиля, когда чатов стало ноль.
   Возвращает «есть что показать» — по нему initOverlay решает, перерисовывать ли список. */
function refreshChatFilter() {
  const { count, personal, contacts } = chatCounts();
  const show = !!(count || contacts);
  if (show || document.getElementById('chat-group')) injectChatFilter(count, personal, contacts);
  return show;
}

/* Сменить фильтр профилей: СНАЧАЛА пересчитать статусы/чаты/отклики под новый набор, обновить
   счётчики чипов «Чаты», потом обновить стор — чтобы ре-рендер списка и подписчики (счётчик
   «Показать (N)») увидели уже верные значения (подписчик render зарегистрирован раньше). */
function setAccountFilter(next) {
  recomputeForAccounts(next);
  refreshChatFilter();
  store.update({ accountFilter: next });
}

/* Каркас группы фильтров: вернуть существующую (ре-полл оверлея перерисовывает на месте)
   или создать и вставить в начало панели. Блок «создать `.filter-group` и вставить» был
   скопирован в трёх инжектах (панель профилей, чипы чатов, «Мои отклики») — аудит 2026-09-22,
   §3.3. null — панели фильтров в разметке нет, инжектить некуда. */
function filterGroup(id) {
  const bar = document.querySelector('.filter-bar');
  if (!bar) return null;
  let g = document.getElementById(id);
  if (!g) {
    g = document.createElement('div');
    g.className = 'filter-group';
    g.id = id;
    bar.insertBefore(g, bar.firstChild);
  }
  return g;
}

/* Последний снимок данных оверлея {accounts, applied, statuses}: по нему панель профилей
   перерисовывается при смене выбора (подписка ниже). */
let _accData = null;
let _accSubscribed = false;
/* Выпадающий список профилей с чекбоксами — можно выбрать один или несколько (RFC-004).
   `<details>` даёт открытие/закрытие без своего JS. Идемпотентна: ре-полл оверлея перерисовывает
   на месте, сохраняя выбор (из store) и открытость (запоминаем перед перерисовкой).
   Аргументов нет: данные — тот же `_accData`, который заполняет вызывающий; три параметра
   дублировали его поля (аудит 2026-09-22, §3.3). */
function renderAccountsPanel() {
  const { accounts, applied, statuses } = _accData || {};
  if (!accounts || accounts.length < 2) return;
  /* Главный переключатель страницы — В ШАПКЕ (`#acc-slot`, решение владельца 24.09.2026): от
     профиля зависят статусы, чаты, даты и все счётчики, и свёрнутая панель фильтров его прятать
     не должна. Фолбэк — группа в панели: старый feed.html без слота (бандл новее каркаса). */
  const slot = document.getElementById('acc-slot');
  const g = slot || filterGroup('acc-group');
  if (!g) return;
  if (!_accSubscribed) {     /* выбор профиля -> перерисовать сводку/чекбоксы (render уже отфильтрует) */
    _accSubscribed = true;
    let prev = store.get().accountFilter;
    store.subscribe(s => {
      if (s.accountFilter !== prev) { prev = s.accountFilter; if (_accData) renderAccountsPanel(); }
    });
  }
  const stats = crmStats(applied, statuses);
  /* «Ждут ответа» — по чату КАЖДОГО профиля, мимо фильтра: чипы «Чаты» считают только выбранный
     профиль, а у второго резюме автоответов нет — его письма нельзя прятать за переключателем. */
  const waiting = waitingByAccount(VACANCIES);
  const waitAll = Object.values(waiting).reduce((n, k) => n + k, 0);
  const sel = store.get().accountFilter || new Set();
  const anyWarn = accounts.some(a => a.session && a.session !== 'ok');
  const summary = sel.size === 0 ? 'все' : accounts.filter(a => sel.has(a.code))
    .map(a => a.label).join(', ');
  const row = a => {
    const s = stats[a.code] || { applied: 0, invited: 0, rejected: 0, other: 0 };
    const warn = a.session && a.session !== 'ok' ? ` ⚠ ${SESSION_WORD[a.session] || a.session}` : '';
    const wait = waiting[a.code] || 0;
    const pulse = pulseLine(a);          /* сегодня / окно 24ч / последний отклик / синк */
    return `<label class="acc-opt" title="За всё время: приглашений ${s.invited}, отказов ${s.rejected}, без исхода ${s.other}${warn}">`
         + `<input type="checkbox" data-acc="${esc(a.code)}"${sel.has(a.code) ? ' checked' : ''}>`
         + `<span>${esc(a.label)} · ${s.applied} <span class="acc-mini">📩${s.invited} ✖${s.rejected}</span>`
         + `${wait ? `<span class="acc-wait" title="Живой человек написал последним — ждёт ответа (как чип «👤 Личные»)">💬 ${wait}</span>` : ''}`
         + `${warn ? `<span class="acc-warn">${esc(warn)}</span>` : ''}`
         + `${pulse ? `<span class="acc-pulse">${esc(pulse)}</span>` : ''}</span></label>`;
  };
  /* A/B резюме за ОБЩИЙ период (crmStats выше — за всё время, у профилей оно несопоставимо). */
  const ab = abCompare(applied, statuses);
  let abHtml = '';
  if (ab) {
    const t = abText(ab, accounts);
    abHtml = `<div class="acc-ab"><div class="acc-ab-head">${esc(t.head)}</div>`
      + t.lines.map(l => `<div>${esc(l)}</div>`).join('')
      + `${t.note ? `<div class="acc-ab-note">${esc(t.note)}</div>` : ''}</div>`;
  }
  const wasOpen = g.querySelector('details')?.open || false;
  g.innerHTML = `<span class="filter-label">👥 ${slot ? 'Профиль' : 'Профили'}</span>`
    + `<details class="acc-dd"${wasOpen ? ' open' : ''}>`
    + `<summary class="acc-summary${sel.size ? ' active' : ''}">${anyWarn ? '⚠ ' : ''}`
    + `${esc(summary)}${waitAll ? ` · 💬${waitAll}` : ''} ▾</summary>`
    + `<div class="acc-menu">${accounts.map(row).join('')}${abHtml}`
    + '<button class="acc-clear" id="acc-clear">Сбросить</button></div></details>';
  for (const box of g.querySelectorAll('input[data-acc]')) {
    box.addEventListener('change', () => {
      const next = new Set(store.get().accountFilter || []);
      box.checked ? next.add(box.getAttribute('data-acc')) : next.delete(box.getAttribute('data-acc'));
      setAccountFilter(next);
    });
  }
  g.querySelector('#acc-clear')?.addEventListener('click', () => setAccountFilter(new Set()));
}

/* Контрол «Чаты ждут ответа»: показать только те вакансии, где работодатель написал
   последним. Отдельная кнопка — «лично» (деньги/переезд, бот отвечать не должен). */
function injectChatFilter(count, personal, contacts) {
  if (document.getElementById('chat-group')) {   /* ре-полл оверлея: освежить счётчики */
    document.getElementById('chat-personal').textContent = `👤 Личные (${personal})`;
    document.getElementById('chat-wait').textContent = `Все ждут ответа (${count})`;
    document.getElementById('chat-contact').textContent = `📞 С контактами (${contacts})`;
    return;
  }
  const g = filterGroup('chat-group');
  if (!g) return;
  g.innerHTML =
    '<span class="filter-label">💬 Чаты</span>' +
    '<div class="sched-btns">' +
    `<button class="sched-btn" id="chat-personal" title="Живой человек (без фризов: заглушек «свяжемся» и бот-интервью), чат открыт для ответа">👤 Личные (${personal})</button>` +
    `<button class="sched-btn" id="chat-wait">Все ждут ответа (${count})</button>` +
    '<button class="sched-btn" id="chat-manual">💰 Решай сам</button>' +
    `<button class="sched-btn" id="chat-contact" title="Рекрутёр оставил телефон/телеграм в переписке">📞 С контактами (${contacts})</button>` +
    '</div>';
  const set = f => () => store.update({ chatFilter: store.get().chatFilter === f ? '' : f });
  document.getElementById('chat-personal').addEventListener('click', set('personal'));
  document.getElementById('chat-wait').addEventListener('click', set('wait'));
  document.getElementById('chat-manual').addEventListener('click', set('manual'));
  document.getElementById('chat-contact').addEventListener('click', set('contact'));
  store.subscribe(s => {
    for (const [id, f] of [['chat-personal', 'personal'], ['chat-wait', 'wait'],
                           ['chat-manual', 'manual'], ['chat-contact', 'contact']]) {
      document.getElementById(id)?.classList.toggle('active', s.chatFilter === f);
    }
  });
}

/* «📮 Мои» — режим статуса отклика (решение владельца 24.09.2026). Раньше это была отдельная
   группа «Мои отклики за период» из 8 контролов первой строкой панели (на телефоне она и
   распирала страницу вбок), хотя по смыслу это ещё одно значение `status` — фильтр разбирает
   его тем же ключом. Теперь пилюля живёт в группе «Статус отклика», а строка периода (даты +
   пресеты) появляется только в режиме «Мои». Фолбэк — своя группа, если статусной в каркасе
   нет (`has_status` = false). Числа на кнопке при инжекте нет: единственный источник числа —
   подписка ниже (аудит 2026-09-22, §3.3). */
function injectAppliedControl() {
  if (document.getElementById('mine-toggle')) return;
  let pills = document.querySelector('.sched-btns [data-status="all"]')?.parentElement || null;
  let g = pills?.closest('.filter-group') || null;
  if (!g) {
    g = filterGroup('applied-group');
    if (!g) return;
    g.innerHTML = '<span class="filter-label">📮 Мои отклики</span><div class="sched-btns"></div>';
    pills = g.querySelector('.sched-btns');
  }
  const mineBtn = document.createElement('button');
  mineBtn.className = 'sched-btn';
  mineBtn.id = 'mine-toggle';
  mineBtn.dataset.status = 'mine';
  mineBtn.title = 'Мои отклики; период — строкой ниже';
  mineBtn.textContent = '📮 Мои';
  pills.appendChild(mineBtn);
  const period = document.createElement('div');
  period.className = 'sched-btns mine-period';
  period.id = 'mine-period';
  period.hidden = true;
  period.innerHTML =
    '<input type="date" id="date-from" class="date-input" title="С даты">' +
    '<span class="date-dash">—</span>' +
    '<input type="date" id="date-to" class="date-input" title="По дату">' +
    '<button class="sched-btn" data-preset="today">Сегодня</button>' +
    '<button class="sched-btn" data-preset="7">7 дней</button>' +
    '<button class="sched-btn" data-preset="30">30 дней</button>' +
    '<button class="sched-btn" data-preset="all">Все</button>' +
    '<button class="sched-btn" data-preset="reset" title="Сбросить диапазон">⨯</button>';
  g.appendChild(period);

  mineBtn.addEventListener('click', () => {
    store.update({ status: store.get().status === 'mine' ? 'all' : 'mine' });
  });
  const df = document.getElementById('date-from');
  const dt = document.getElementById('date-to');
  df.addEventListener('change', () => store.update({ dateFrom: df.value, status: 'mine' }));
  dt.addEventListener('change', () => store.update({ dateTo: dt.value, status: 'mine' }));
  period.querySelectorAll('[data-preset]').forEach(b => {
    b.addEventListener('click', () => {
      const p = b.dataset.preset;
      // «Все» и «⨯» — снять границы дат и показать все отклики (режим «Мои отклики»)
      if (p === 'all' || p === 'reset') { store.update({ dateFrom: '', dateTo: '', status: 'mine' }); return; }
      const now = new Date();
      const to = ymd(now);
      const from = p === '7' ? ymd(new Date(now.getTime() - 6 * 864e5))
        : p === '30' ? ymd(new Date(now.getTime() - 29 * 864e5)) : to;
      store.update({ dateFrom: from, dateTo: to, status: 'mine' });
    });
  });

  /* Синхронизация UI контрола с состоянием: счётчик на кнопке = число откликов в ВЫБРАННОМ
     диапазоне (пусто -> все), активность кнопки, значения дат, гашение чипов статуса.
     Снимок откликов берётся ЗДЕСЬ, на каждом тике: журнал дописывает вакансии-«призраки»
     в VACANCIES уже после первого инжекта, и снимок, снятый один раз, их не считал. */
  store.subscribe(state => {
    const n = VACANCIES.filter(v => v.applied && appliedInRange(v, state.dateFrom, state.dateTo)).length;
    mineBtn.textContent = `📮 Мои (${n})`;
    period.hidden = state.status !== 'mine';
    if (df.value !== state.dateFrom) df.value = state.dateFrom;
    if (dt.value !== state.dateTo) dt.value = state.dateTo;
    /* Подсветка статусных пилюль — ПО СОСТОЯНИЮ: выход из «Мои» повторным кликом ставит
       status='all' мимо общего обработчика пилюль, и без этого «Все» оставалась погашенной. */
    document.querySelectorAll('.sched-btns [data-status]')
      .forEach(b => { b.classList.toggle('active', b.dataset.status === state.status); });
  });
}

/* Живой счётчик чипа «Формы»: число из шаблона запечено при сборке ленты и отстаёт от
   очереди (крон дописывает формы весь день). Считаем по факту оверлея: актуальные/всего. */
function refreshFormsChip() {
  const btn = document.querySelector('.sched-btns [data-status="form"]');
  if (!btn) return;
  const inFeed = VACANCIES.filter(v => v.needs_form);
  const alive = inFeed.filter(v => !v.form_dead).length;
  btn.textContent = inFeed.length === alive
    ? `📝 Формы (${alive})`
    : `📝 Формы (${alive}+${inFeed.length - alive}⌛)`;   /* живые + протухшие (серые) */
}

/* ── Живой оверлей форм/статусов/журнала с сервера: подхватывает изменения, случившиеся
   ПОСЛЕ сборки ленты (форма после отклика, свежий --sync-status, журнал откликов), без
   пересборки feed-data.js. file:// -> fetch падает -> null -> no-op. ── */
async function initOverlay() {
  const [forms, statuses, applied, chats, accounts] = await Promise.all([
    pullJson('api/forms'), pullJson('api/statuses'), pullJson('api/applied'),
    pullJson('api/chats'), pullJson('api/accounts'),
  ]);
  let changed = false;
  /* Аккаунты (RFC-004) — ДО applyJournal: карточка-призрак сразу получит метку профиля.
     При одном аккаунте setAccounts/крон-сводка старый вид не меняют. */
  if (Array.isArray(accounts) && accounts.length) {
    setAccounts(accounts);
    _accData = { accounts, applied, statuses };   /* снимок для перерисовки панели при выборе */
    /* Сама панель рисуется в КОНЦЕ оверлея: ей нужны чаты профилей (v.chatByAcct) для «ждут
       ответа», а они раскладываются ниже. */
  }
  /* Журнал — ПЕРВЫМ: applyJournal синтезирует карточки-призраки для вакансий, выпавших из
     выдачи; формы/статусы/чаты, обработанные ДО него, призраков не находили и терялись
     (чаты: инцидент «46 из 93»; формы: 52 в очереди vs 51 подсвеченных — fix.md №12). */
  const nApplied = applyJournal(applied);
  if (nApplied) { injectAppliedControl(); changed = true; }
  for (const [id, rec] of Object.entries(forms || {})) {
    const v = V_MAP[id];
    if (!v) continue;
    const dead = !!rec?.dead;
    if (!v.needs_form || v.form_dead !== dead) {
      v.needs_form = true; v.form_dead = dead; bustCard(id); changed = true;
    }
  }
  refreshFormsChip();
  /* Статусы приходят ПО АККАУНТАМ ({vid: {account: state}}, RFC-004): у вакансии с откликом от
     обоих статусы разные. Кладём карту на карточку, а показываемый статус выбираем по фильтру
     профиля — иначе под фильтром acc2 светилась бы метка основного. */
  for (const [id, byAcct] of Object.entries(statuses || {})) {
    const v = V_MAP[id];
    if (v) { v.statusByAcct = byAcct; }
  }
  /* Чаты приходят ПО АККАУНТАМ ({vid: {account: info}}, RFC-004): у вакансии с откликом от обоих
     чат и ДАТА разные. Кладём карту, показываемый чат выбираем по фильтру профиля — иначе под
     фильтром acc2 светились бы чат и дата основного. Обе карты раскладываются ДО пересчёта:
     один проход (recomputeForAccounts) читает их вместе и сразу. */
  for (const [id, byAcct] of Object.entries(chats || {})) {
    const v = V_MAP[id];
    if (v) v.chatByAcct = byAcct;
  }
  /* Один проход по выдаче вместо трёх (статусы/чаты/отклики); флаги — те же, что были у
     прежних трёх функций: смена статуса и наличие профильных чатов/откликов. */
  const rec = recomputeForAccounts();
  if (rec.changed || rec.any) changed = true;
  if (refreshChatFilter()) changed = true;
  if (_accData) renderAccountsPanel();   /* после чатов: «ждут ответа» считается по v.chatByAcct */
  if (changed) store.update({});   /* ре-рендер с обновлёнными CRM-бейджами */
}

/* первый рендер + синхронизация */
store.update({});   /* notify -> render */
initSync();
initOverlay();
setStale(staleNow());
/* Вкладка живёт открытой весь день, а one-shot оверлей устаревал до F5 (fix.md №11):
   ре-полл раз в 5 мин — все ветки initOverlay идемпотентны (инжекты обновляют счётчики).
   Баннер возраста среза пересчитывается тем же тиком: вкладку не перезагружают сутками,
   и порог должен переступаться сам, без F5. */
setInterval(() => { initOverlay(); setStale(staleNow()); }, 5 * 60 * 1000);
