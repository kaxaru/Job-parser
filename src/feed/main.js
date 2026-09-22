/* Main — точка сборки (bootstrap): создаёт Store, подписывает View, связывает
   DOM-события с командами Store, оркеструет сохранение отметок. Поток:
   событие -> store.update -> View.render. Глобалы VACANCIES/SAL_MAX/SAVED_MARKS
   приходят из feed-data.js (грузится <script> до бандла). */

import {
  applyVacancy, exportMarks, importMarks, loadInitialMarks,
  pullJson, pullServer, pushServer, saveLocal,
} from './marks.js';
import {
  APPLY_LABELS, appliedInRange, cardTone, convert, countActiveFilters, crmStats,
  effectiveApplied, effectiveChat, effectiveStatus, esc, fmtK, isFrozenChat, journalById,
  staleNow, syntheticCard,
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
  displayCur: 'RUB',
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
  /* тон рамки переживает ручной тоггл: жёлтое бот-интервью не должно позеленеть от «✓ Отклик» */
  if (card) applyCardStatus(card, st, cardTone(V_MAP[id]) || st);
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

/* ── Кнопка «Откликнуться в фоне» в модалке (serve-режим) → POST /api/apply ──
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

/* ── Фильтры ── */
/* `.lang-cb` — общий класс разметки чипа-галочки, поэтому языковой обработчик обязан
   исключить ВСЕ специализированные чипы (опыт, оформление), иначе код формы попал бы
   в набор языков и обнулил выдачу. */
document.querySelectorAll('.lang-cb:not(.exp-cb):not(.emp-cb)').forEach(cb => {
  cb.addEventListener('change', () => {
    const langs = store.get().langs;
    if (cb.checked) langs.add(cb.value); else langs.delete(cb.value);
    store.update({ langs });
  });
});
document.querySelectorAll('.exp-cb').forEach(cb => {
  cb.addEventListener('change', () => {
    const exps = store.get().exps;
    if (cb.checked) exps.add(cb.value); else exps.delete(cb.value);
    store.update({ exps });
  });
});
document.querySelectorAll('.emp-cb').forEach(cb => {
  cb.addEventListener('change', () => {
    const emps = store.get().emps;
    if (cb.checked) emps.add(cb.value); else emps.delete(cb.value);
    store.update({ emps });
  });
});
document.querySelectorAll('.role-cb').forEach(cb => {
  cb.addEventListener('change', () => {
    const roles = store.get().roles;
    if (cb.checked) roles.add(cb.value); else roles.delete(cb.value);
    store.update({ roles });
  });
});

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

/* ── Переключатель валюты: зарплаты приводятся к выбранной валюте (курсы FX_RATES).
   При смене — пересчитать масштаб слайдера и сбросить диапазон.
   ИНВАРИАНТ: SAL_MAX приходит из Python уже В РУБЛЯХ (feed.py::_salary_slider_max), поэтому
   здесь ровно одна конверсия RUB -> выбранная. Раньше это число было максимумом по СЫРЫМ
   вилкам разных валют, и та же строка конвертировала узбекские сумы как рубли. ── */
function rescaleSalary(cur) {
  const max = Math.round(convert(SAL_MAX, 'RUR', cur));
  salMinEl.max = max; salMaxEl.max = max;
  salMinEl.value = 0; salMaxEl.value = max;
  salMinVal.textContent = fmtK(0);
  salMaxVal.textContent = fmtK(max);
  store.update({ minSal: 0, maxSal: max, salMax: max, displayCur: cur });
}
/* Активация пилюли: `active` — ровно на одной кнопке группы (снять с соседей, зажечь нажатой).
   Блок был скопирован пять раз подряд (`[data-cur]`/`[data-sched]`/`[data-sort]`/`[data-status]`/
   `[data-source]`, аудит 2026-09-22, §3.3): теперь одна функция на все группы. */
function activate(btn) {
  btn.closest('.sched-btns')?.querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
  btn.classList.add('active');
}

/* Дефолты пилюль — ОДИН список на три роли: атрибут группы в разметке, значение, которое
   зажигается, и ключ состояния ленты. Раньше дефолты были закодированы дважды (разметка +
   resetFilters против store), а два селектора из пяти брались без null-guard: удаление кнопки
   из разметки роняло сброс TypeError-ом (аудит 2026-09-22, §5). */
const UI_DEFAULTS = [['data-cur', 'RUB', 'displayCur'], ['data-sched', 'all', 'schedule'],
                     ['data-sort', 'none', 'sort'], ['data-status', 'all', 'status'],
                     ['data-source', 'all', 'source']];

/* Сброс группы к дефолту: зажечь дефолтную пилюлю, а если её нет в разметке — просто снять
   активность со всей группы (единый null-guard вместо падения). */
function resetPills(attr, value) {
  const def = document.querySelector(`[${attr}="${value}"]`);
  if (def) { activate(def); return; }
  document.querySelectorAll(`[${attr}]`).forEach(b => { b.classList.remove('active'); });
}

document.querySelectorAll('[data-cur]').forEach(btn => {
  btn.addEventListener('click', () => {
    activate(btn);
    rescaleSalary(btn.dataset.cur);
  });
});

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

document.querySelectorAll('[data-sched]').forEach(btn => {
  btn.addEventListener('click', () => {
    activate(btn);
    store.update({ schedule: btn.dataset.sched });
  });
});
document.querySelectorAll('[data-sort]').forEach(btn => {
  btn.addEventListener('click', () => {
    activate(btn);
    store.update({ sort: btn.dataset.sort });
  });
});
document.querySelectorAll('[data-status]').forEach(btn => {
  btn.addEventListener('click', () => {
    activate(btn);
    store.update({ status: btn.dataset.status });
  });
});
document.querySelectorAll('[data-source]').forEach(btn => {
  btn.addEventListener('click', () => {
    activate(btn);
    store.update({ source: btn.dataset.source });
  });
});

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
  recomputeStatuses(new Set());   /* до store.update: ре-рендер должен увидеть общие статусы */
  /* Состояние сброса: ключи пилюль дописывает цикл ниже (из UI_DEFAULTS) — так значение
     дефолта не разъезжается между разметкой, UI и стором. */
  const state = {
    minSal: 0, maxSal: SAL_MAX, salMax: SAL_MAX, city: '', cityExact: false,
    dateFrom: '', dateTo: '', matchSort: false, resumeOnly: false, search: [],
    showNonIt: false, chatFilter: '', accountFilter: new Set(),
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
  const ci = document.getElementById('city-inp');
  if (ci) ci.value = '';
  resumeBtn.classList.remove('active');
  if (matchBtn) matchBtn.classList.remove('active');
  const si = document.getElementById('search-input');
  if (si) si.value = '';
  store.update(state);
}
window.resetFilters = resetFilters;   /* вызывается из onclick в feed.html.j2 */

/* ── Поиск по названию/компании — живёт в ЛИПКОЙ полосе, а не в панели фильтров:
      панель сворачивается, а поиск нужен всегда. Фолбэк на панель — если полосы нет. ── */
(function initSearch() {
  const toolbar = document.getElementById('filter-toolbar');
  const bar = document.querySelector('.filter-bar');
  const host = toolbar || bar;
  if (!host) return;
  const inp = document.createElement('input');
  inp.type = 'search';
  inp.id = 'search-input';
  inp.className = 'search-input';
  inp.placeholder = '🔍 напр. альфа-банк или python backend';
  inp.autocomplete = 'off';
  inp.setAttribute('aria-label', 'Поиск: вакансия или компания');
  if (toolbar) toolbar.insertBefore(inp, toolbar.querySelector('.toolbar-spacer'));
  else bar.insertBefore(inp, bar.firstChild);
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

/* ── Подхватить marks.json с сервера (serve-режим) ── */
async function initSync() {
  const disk = await pullServer();
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
/* Выпадающий список профилей с чекбоксами — можно выбрать один или несколько (RFC-004).
   `<details>` даёт открытие/закрытие без своего JS. Идемпотентна: ре-полл оверлея перерисовывает
   на месте, сохраняя выбор (из store) и открытость (запоминаем перед перерисовкой). */
/* Пересчитать показываемый статус карточек под выбранные профили (RFC-004). Статусы хранятся
   по аккаунтам (v.statusByAcct); под фильтром acc2 берём статус acc2, а не общий. */
function recomputeStatuses(filter = store.get().accountFilter) {
  let changed = false;
  for (const v of VACANCIES) {
    if (!v.statusByAcct) continue;
    const st = effectiveStatus(v.statusByAcct, filter);
    if (v.status !== st) { v.status = st; bustCard(v.id); changed = true; }
  }
  return changed;
}

/* То же для ПЕРЕПИСКИ (RFC-004): под фильтром acc2 показываем чат и его дату у acc2, а не у
   основного (иначе на бейдже отказа светилась бы дата main). Чат — объект (на ре-полле новый
   инстанс), поэтому просто переустанавливаем и бустим карточку, как делал прежний оверлей. */
function recomputeChats(filter = store.get().accountFilter) {
  let any = false;
  for (const v of VACANCIES) {
    if (!v.chatByAcct) continue;
    v.chat = effectiveChat(v.chatByAcct, filter);
    bustCard(v.id); any = true;
  }
  return any;
}

/* То же для ОТКЛИКА (RFC-004): дата и via на бейдже 📮, счётчик «Показать (N)» и фильтр по
   периоду — по выбранному профилю, а не по самому раннему среди всех. */
function recomputeApplied(filter = store.get().accountFilter) {
  let any = false;
  for (const v of VACANCIES) {
    if (!v.appliedByAcct) continue;
    v.applied = effectiveApplied(v.appliedByAcct, filter, v.appliedAccounts);
    bustCard(v.id); any = true;
  }
  return any;
}

/* Счётчики чипов «Чаты» из ЭФФЕКТИВНОГО чата (под текущим фильтром профиля): ждут ответа,
   личные (живой человек, открыт), с контактами. Под фильтром acc2 считаются чаты acc2. */
function chatCounts() {
  let count = 0, personal = 0, contacts = 0;
  for (const v of VACANCIES) {
    const info = v.chat;
    if (!info) continue;
    if (info.contact) contacts++;
    if (info.needs_reply) {
      count++;
      if (info.sender === 'human' && info.can_write !== false && !isFrozenChat(info)) personal++;
    }
  }
  return { count, personal, contacts };
}

/* Сменить фильтр профилей: СНАЧАЛА пересчитать статусы/чаты/отклики под новый набор, обновить
   счётчики чипов «Чаты», потом обновить стор — чтобы ре-рендер списка и подписчики (счётчик
   «Показать (N)») увидели уже верные значения (подписчик render зарегистрирован раньше). */
function setAccountFilter(next) {
  recomputeStatuses(next);
  recomputeChats(next);
  recomputeApplied(next);
  const { count, personal, contacts } = chatCounts();
  if (document.getElementById('chat-group') || count || contacts) injectChatFilter(count, personal, contacts);
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

let _accData = null;         /* последние {accounts, applied, statuses} — для перерисовки при выборе */
let _accSubscribed = false;
function renderAccountsPanel(accounts, applied, statuses) {
  if (!accounts || accounts.length < 2) return;
  const g = filterGroup('acc-group');
  if (!g) return;
  _accData = { accounts, applied, statuses };
  if (!_accSubscribed) {     /* выбор профиля -> перерисовать сводку/чекбоксы (render уже отфильтрует) */
    _accSubscribed = true;
    let prev = store.get().accountFilter;
    store.subscribe(s => {
      if (s.accountFilter !== prev) { prev = s.accountFilter; if (_accData) renderAccountsPanel(
        _accData.accounts, _accData.applied, _accData.statuses); }
    });
  }
  const stats = crmStats(applied, statuses);
  const sel = store.get().accountFilter || new Set();
  const anyWarn = accounts.some(a => a.session && a.session !== 'ok');
  const summary = sel.size === 0 ? 'все' : accounts.filter(a => sel.has(a.code))
    .map(a => a.label).join(', ');
  const row = a => {
    const s = stats[a.code] || { applied: 0, invited: 0, rejected: 0, other: 0 };
    const warn = a.session && a.session !== 'ok' ? ` ⚠ ${SESSION_WORD[a.session] || a.session}` : '';
    return `<label class="acc-opt" title="Приглашений ${s.invited}, отказов ${s.rejected}, без исхода ${s.other}${warn}">`
         + `<input type="checkbox" data-acc="${esc(a.code)}"${sel.has(a.code) ? ' checked' : ''}>`
         + `<span>${esc(a.label)} · ${s.applied} <span class="acc-mini">📩${s.invited} ✖${s.rejected}</span>`
         + `${warn ? `<span class="acc-warn">${esc(warn)}</span>` : ''}</span></label>`;
  };
  const wasOpen = g.querySelector('details')?.open || false;
  g.innerHTML = '<span class="filter-label">👥 Профили</span>'
    + `<details class="acc-dd"${wasOpen ? ' open' : ''}>`
    + `<summary class="acc-summary${sel.size ? ' active' : ''}">${anyWarn ? '⚠ ' : ''}`
    + `${esc(summary)} ▾</summary>`
    + `<div class="acc-menu">${accounts.map(row).join('')}`
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

/* Инъекция контрола «Мои отклики за период» (кнопка + два date-инпута + пресеты). */
function injectAppliedControl(count) {
  if (document.getElementById('applied-group')) return;
  const g = filterGroup('applied-group');
  if (!g) return;
  g.innerHTML =
    '<span class="filter-label">📮 Мои отклики за период</span>' +
    '<div class="sched-btns">' +
    `<button class="sched-btn" id="mine-toggle" data-status="mine">📮 Показать (${count})</button>` +
    '<input type="date" id="date-from" class="date-input" title="С даты">' +
    '<span class="date-dash">—</span>' +
    '<input type="date" id="date-to" class="date-input" title="По дату">' +
    '<button class="sched-btn" data-preset="today">Сегодня</button>' +
    '<button class="sched-btn" data-preset="7">7 дней</button>' +
    '<button class="sched-btn" data-preset="30">30 дней</button>' +
    '<button class="sched-btn" data-preset="all">Все</button>' +
    '<button class="sched-btn" data-preset="reset" title="Сбросить диапазон">⨯</button>' +
    '</div>';

  document.getElementById('mine-toggle').addEventListener('click', () => {
    store.update({ status: store.get().status === 'mine' ? 'all' : 'mine' });
  });
  const df = document.getElementById('date-from');
  const dt = document.getElementById('date-to');
  df.addEventListener('change', () => store.update({ dateFrom: df.value, status: 'mine' }));
  dt.addEventListener('change', () => store.update({ dateTo: dt.value, status: 'mine' }));
  g.querySelectorAll('[data-preset]').forEach(b => {
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

  const appliedVacs = VACANCIES.filter(v => v.applied);   /* только отклики (для счётчика) */

  /* Синхронизация UI контрола с состоянием: счётчик на кнопке = число откликов в ВЫБРАННОМ
     диапазоне (пусто -> все), активность кнопки, значения дат, гашение чипов статуса. */
  store.subscribe(state => {
    const btn = document.getElementById('mine-toggle');
    if (btn) {
      const n = appliedVacs.filter(v => appliedInRange(v, state.dateFrom, state.dateTo)).length;
      btn.textContent = `📮 Показать (${n})`;
      btn.classList.toggle('active', state.status === 'mine');
    }
    if (df.value !== state.dateFrom) df.value = state.dateFrom;
    if (dt.value !== state.dateTo) dt.value = state.dateTo;
    if (state.status === 'mine') {
      document.querySelectorAll('.sched-btns [data-status]:not(#mine-toggle)')
        .forEach(b => { b.classList.remove('active'); });
    }
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
    renderAccountsPanel(accounts, applied, statuses);
  }
  /* Журнал — ПЕРВЫМ: applyJournal синтезирует карточки-призраки для вакансий, выпавших из
     выдачи; формы/статусы/чаты, обработанные ДО него, призраков не находили и терялись
     (чаты: инцидент «46 из 93»; формы: 52 в очереди vs 51 подсвеченных — fix.md №12). */
  const nApplied = applyJournal(applied);
  if (nApplied) { injectAppliedControl(nApplied); changed = true; }
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
     профиля (recomputeStatuses) — иначе под фильтром acc2 светилась бы метка основного. */
  for (const [id, byAcct] of Object.entries(statuses || {})) {
    const v = V_MAP[id];
    if (v) { v.statusByAcct = byAcct; }
  }
  if (recomputeStatuses()) changed = true;
  /* Чаты приходят ПО АККАУНТАМ ({vid: {account: info}}, RFC-004): у вакансии с откликом от обоих
     чат и ДАТА разные. Кладём карту, показываемый чат выбираем по фильтру профиля
     (recomputeChats) — иначе под фильтром acc2 светились бы чат и дата основного. */
  for (const [id, byAcct] of Object.entries(chats || {})) {
    const v = V_MAP[id];
    if (v) v.chatByAcct = byAcct;
  }
  if (recomputeChats()) changed = true;
  const { count: waiting, personal, contacts } = chatCounts();   /* из эффективного чата под фильтром */
  if (waiting || contacts) { injectChatFilter(waiting, personal, contacts); changed = true; }
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
