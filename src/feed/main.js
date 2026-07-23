/* Main — точка сборки (bootstrap): создаёт Store, подписывает View, связывает
   DOM-события с командами Store, оркеструет сохранение отметок. Поток:
   событие -> store.update -> View.render. Глобалы VACANCIES/SAL_MAX/SAVED_MARKS
   приходят из feed-data.js (грузится <script> до бандла). */

import {
  applyVacancy, exportMarks, importMarks, loadInitialMarks,
  pullJson, pullServer, pushServer, saveLocal,
} from './marks.js';
import { appliedInRange, convert, fmtK } from './model.js';
import { createStore } from './store.js';
import {
  applyCardStatus, bustCard, closeModal, refreshCardStatus, render, setSync, showModal,
} from './view.js';

const V_MAP = Object.fromEntries(VACANCIES.map(v => [v.id, v]));

const store = createStore({
  vacancies: VACANCIES,
  marks: loadInitialMarks(),
  serverMode: false,
  langs: new Set(),
  roles: new Set(),
  showNonIt: false,
  exps: new Set(),
  minSal: 0,
  maxSal: SAL_MAX,
  salMax: SAL_MAX,
  city: '',
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
  if (card) applyCardStatus(card, st);
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
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target.id === 'modal-overlay') closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* ── Кнопка «Откликнуться в фоне» в модалке (serve-режим) → POST /api/apply ── */
const APPLY_LABELS = {
  applied: '✅ Отклик отправлен', already: 'уже откликались',
  form: '📝 нужна форма — в очереди', skip: '✖ пропущено (внешний/архив/опросник)',
  queued: '➕ в очереди крона',
  busy: '⏳ занято — идёт крон-отклик, попробуйте через пару минут',
  'no-session': '⚠ нет сессии — hh.py autoclick --login',
  error: '⚠ ошибка',
};
document.getElementById('modal-box').addEventListener('click', async e => {
  const btn = e.target.closest('#m-apply');
  if (!btn) return;
  const id = btn.dataset.id;
  const cover = (document.getElementById('cover-text')?.value || '').trim();
  const statusEl = document.getElementById('m-apply-status');
  btn.disabled = true;
  if (statusEl) statusEl.textContent = '⏳ Откликаюсь… (браузер + HH, до минуты)';
  try {
    const res = await applyVacancy(id, btn.dataset.url, cover, V_MAP[id]?.name || '');
    if (statusEl) {
      statusEl.textContent = (APPLY_LABELS[res.status] || res.status) + (res.letter ? ' + письмо' : '');
    }
    if (res.status === 'applied' || res.status === 'already') {
      setStatus(id, 'applied');                 /* отметить в ленте */
    } else if (res.status === 'form') {
      const v = V_MAP[id];
      if (v) { v.needs_form = true; refreshCardStatus(v); }  /* живой бейдж «форма», без пересборки */
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
document.querySelectorAll('.lang-cb:not(.exp-cb)').forEach(cb => {
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
   При смене — пересчитать масштаб слайдера (SAL_MAX в RUR -> выбранная) и сбросить диапазон. ── */
function rescaleSalary(cur) {
  const max = Math.round(convert(SAL_MAX, 'RUR', cur));
  salMinEl.max = max; salMaxEl.max = max;
  salMinEl.value = 0; salMaxEl.value = max;
  salMinVal.textContent = fmtK(0);
  salMaxVal.textContent = fmtK(max);
  store.update({ minSal: 0, maxSal: max, salMax: max, displayCur: cur });
}
document.querySelectorAll('[data-cur]').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('.sched-btns').querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
    btn.classList.add('active');
    rescaleSalary(btn.dataset.cur);
  });
});

document.getElementById('city-sel').addEventListener('change', e => {
  store.update({ city: e.target.value });
});

document.querySelectorAll('[data-sched]').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('.sched-btns').querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
    btn.classList.add('active');
    store.update({ schedule: btn.dataset.sched });
  });
});
document.querySelectorAll('[data-sort]').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('.sched-btns').querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
    btn.classList.add('active');
    store.update({ sort: btn.dataset.sort });
  });
});
document.querySelectorAll('[data-status]').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('.sched-btns').querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
    btn.classList.add('active');
    store.update({ status: btn.dataset.status });
  });
});
document.querySelectorAll('[data-source]').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('.sched-btns').querySelectorAll('.sched-btn').forEach(b => { b.classList.remove('active'); });
    btn.classList.add('active');
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
  store.get().langs.clear();
  store.get().exps.clear();
  store.get().roles.clear();
  document.querySelectorAll('.lang-cb, .role-cb').forEach(cb => { cb.checked = false; });
  if (nonitBtn) nonitBtn.classList.remove('active');
  salMinEl.max = SAL_MAX; salMaxEl.max = SAL_MAX;          /* валюта -> дефолт RUB */
  salMinEl.value = 0; salMaxEl.value = SAL_MAX;
  salMinVal.textContent = fmtK(0); salMaxVal.textContent = fmtK(SAL_MAX);
  document.querySelectorAll('[data-cur]').forEach(b => { b.classList.remove('active'); });
  const curDef = document.querySelector('[data-cur="RUB"]');
  if (curDef) curDef.classList.add('active');
  document.getElementById('city-sel').value = '';
  document.querySelectorAll('[data-sched]').forEach(b => { b.classList.remove('active'); });
  document.querySelector('[data-sched="all"]').classList.add('active');
  document.querySelectorAll('[data-sort]').forEach(b => { b.classList.remove('active'); });
  document.querySelector('[data-sort="none"]').classList.add('active');
  document.querySelectorAll('[data-status]').forEach(b => { b.classList.remove('active'); });
  const stAll = document.querySelector('[data-status="all"]');
  if (stAll) stAll.classList.add('active');
  document.querySelectorAll('[data-source]').forEach(b => { b.classList.remove('active'); });
  const srcAll = document.querySelector('[data-source="all"]');
  if (srcAll) srcAll.classList.add('active');
  resumeBtn.classList.remove('active');
  if (matchBtn) matchBtn.classList.remove('active');
  const si = document.getElementById('search-input');
  if (si) si.value = '';
  store.update({
    minSal: 0, maxSal: SAL_MAX, salMax: SAL_MAX, city: '', schedule: 'all', status: 'all',
    source: 'all', displayCur: 'RUB', dateFrom: '', dateTo: '', sort: 'none',
    matchSort: false, resumeOnly: false, search: [], showNonIt: false,
  });
}
window.resetFilters = resetFilters;   /* вызывается из onclick в feed.html.j2 */

/* ── Поиск по названию/компании — поле инжектируется первым в панель фильтров ── */
(function initSearch() {
  const bar = document.querySelector('.filter-bar');
  if (!bar) return;
  const group = document.createElement('div');
  group.className = 'filter-group';
  group.innerHTML =
    '<span class="filter-label">Поиск: вакансия / компания</span>' +
    '<div class="search-wrap"><input type="search" id="search-input" class="search-input"' +
    ' placeholder="🔍 напр. альфа-банк или python backend" autocomplete="off"></div>';
  bar.insertBefore(group, bar.firstChild);
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

/* Журнал откликов -> {id: {ts, via, status, name, url}} с самым РАННИМ ts на вакансию
   (первый отклик). Обогащаем существующие карточки и синтезируем «призраков» для откликов
   на вакансии, которых нет в текущем сборе (они видны только в режиме «Мои отклики»). */
function applyJournal(applied) {
  const byId = {};
  for (const e of applied || []) {
    if (!e?.id) continue;
    const prev = byId[e.id];
    if (!prev || (e.ts && e.ts < prev.ts)) byId[e.id] = { ts: e.ts, via: e.via, status: e.status, name: e.name, url: e.url };
  }
  let count = 0;
  for (const [id, a] of Object.entries(byId)) {
    const v = V_MAP[id];
    if (v) { v.applied = a; bustCard(id); count++; continue; }
    const syn = {
      id, name: a.name || `Вакансия ${id}`, url: a.url || `https://hh.ru/vacancy/${id}`,
      employer: '', city: '', techs: [], sal_from: null, sal_to: null, sal_mid: null,
      currency: '', exp: '', schedule: '', remote_any: false, role: '', age: null,
      gap: null, fresh: 'unknown', resp: null, status: null, needs_form: false,
      form_dead: false,
      source: '', _synthetic: true, applied: a,
    };
    VACANCIES.push(syn); V_MAP[id] = syn; count++;
  }
  return count;
}

/* Контрол «Чаты ждут ответа»: показать только те вакансии, где работодатель написал
   последним. Отдельная кнопка — «лично» (деньги/переезд, бот отвечать не должен). */
function injectChatFilter(count, personal, contacts) {
  const bar = document.querySelector('.filter-bar');
  if (!bar || document.getElementById('chat-group')) return;
  const g = document.createElement('div');
  g.className = 'filter-group';
  g.id = 'chat-group';
  g.innerHTML =
    '<span class="filter-label">💬 Чаты</span>' +
    '<div class="sched-btns">' +
    `<button class="sched-btn" id="chat-personal" title="Живой человек (без фриз-заглушек «свяжемся»), чат открыт для ответа">👤 Личные (${personal})</button>` +
    `<button class="sched-btn" id="chat-wait">Все ждут ответа (${count})</button>` +
    '<button class="sched-btn" id="chat-manual">💰 Решай сам</button>' +
    `<button class="sched-btn" id="chat-contact" title="Рекрутёр оставил телефон/телеграм в переписке">📞 С контактами (${contacts})</button>` +
    '</div>';
  bar.insertBefore(g, bar.firstChild);
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
  const bar = document.querySelector('.filter-bar');
  if (!bar || document.getElementById('applied-group')) return;
  const g = document.createElement('div');
  g.className = 'filter-group';
  g.id = 'applied-group';
  g.innerHTML =
    '<span class="filter-label">📮 Мои отклики за период</span>' +
    '<div class="sched-btns" id="applied-ctrl">' +
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
  bar.insertBefore(g, bar.firstChild);

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

/* ── Живой оверлей форм/статусов/журнала с сервера: подхватывает изменения, случившиеся
   ПОСЛЕ сборки ленты (форма после отклика, свежий --sync-status, журнал откликов), без
   пересборки feed-data.js. file:// -> fetch падает -> null -> no-op. ── */
async function initOverlay() {
  const [forms, statuses, applied, chats] = await Promise.all([
    pullJson('api/forms'), pullJson('api/statuses'), pullJson('api/applied'),
    pullJson('api/chats'),
  ]);
  let changed = false;
  for (const id of Object.keys(forms || {})) {
    const v = V_MAP[id];
    if (v && !v.needs_form) { v.needs_form = true; bustCard(id); changed = true; }
  }
  for (const [id, st] of Object.entries(statuses || {})) {
    const v = V_MAP[id];
    if (v && v.status !== st) { v.status = st; bustCard(id); changed = true; }
  }
  const nApplied = applyJournal(applied);
  if (nApplied) { injectAppliedControl(nApplied); changed = true; }
  /* Переписка — ПОСЛЕ applyJournal: тот синтезирует карточки-призраки для вакансий, выпавших
     из выдачи (после пересбора их в VACANCIES уже нет). Раньше обрабатывали до него и теряли
     половину чатов — 46 из 93. */
  let waiting = 0, personal = 0, contacts = 0;
  for (const [id, info] of Object.entries(chats || {})) {
    const v = V_MAP[id];
    if (!v) continue;
    v.chat = info;
    if (info.contact) contacts++;
    if (info.needs_reply) {
      waiting++;
      if (info.sender === 'human' && info.can_write !== false && info.kind !== 'ack') personal++;
    }
    bustCard(id); changed = true;
  }
  if (waiting || contacts) { injectChatFilter(waiting, personal, contacts); changed = true; }
  if (changed) store.update({});   /* ре-рендер с обновлёнными CRM-бейджами */
}

/* первый рендер + синхронизация */
store.update({});   /* notify -> render */
initSync();
initOverlay();
