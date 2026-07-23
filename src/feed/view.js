/* View — рендеринг DOM из состояния. Чистые билдеры разметки + императивные
   обновления (innerHTML, classList). Бизнес-логику не содержит — берёт из Model. */

import { COVER_TEMPLATES, coverLetter } from './cover.js';
import { loadDescriptions } from './marks.js';
import {
  ageColor, cardColor, esc, filterVacancies, fmtSal, hashId,
  matchColor, SCHED_LABELS, STATUS_BTNS, statusInfo, tagClr,
} from './model.js';
import { resumeMatch } from './resume.js';

/* ── Билдеры разметки (чистые) ── */
function tagsHTML(techs, limit) {
  return techs.slice(0, limit ?? techs.length).map(t => {
    const [bg, fg] = tagClr(t);
    return `<span class="tag" style="background:${bg};color:${fg}">${esc(t)}</span>`;
  }).join('');
}

function matchBadge(v) {
  const m = resumeMatch(v);
  const title = `Совпадение с резюме ${m.pct}% — стек ${m.stack}/60 · опыт ${m.exp}/25 · удалёнка ${m.remote}/15`;
  return `<span class="match-badge" style="background:${matchColor(m.pct)}" title="${esc(title)}">${m.pct}%</span>`;
}

/* Бейджи CRM: реальный статус отклика с HH (приоритет над ручной пометкой) + «форма».
   Пусто, если статуса нет (не синхронизировано / не откликались). */
function statusBadge(v) {
  let out = '';
  if (v._synthetic) {                         /* отклик на вакансию, выпавшую из выдачи — данных мало */
    out += '<span class="status-badge" style="background:#6b7280"'
         + ' title="Вакансия выпала из выдачи; сохранён отклик и чат">👻 вне выдачи</span>';
  }
  const st = statusInfo(v);
  if (st) {
    out += `<span class="status-badge" style="background:${st.color}"`
         + ` title="Статус на HH: ${esc(st.label)}">${esc(st.label)}</span>`;
  }
  if (v.needs_form) {
    out += '<span class="status-badge" style="background:#B279A2"'
         + ' title="Требует заполнения формы-опросника на HH">📝 форма</span>';
  }
  if (v.form_dead) {                          /* свип форм не нашёл полей — вакансия снята/архив */
    out += '<span class="status-badge" style="background:#5c6068"'
         + ' title="Форма отклика недоступна — вакансия, скорее всего, снята с публикации">⌛ не актуальна</span>';
  }
  /* Чат ждёт ответа. Цвет = КТО написал: живой человек важнее шаблона и бота.
     `sender` считается по двум признакам — isBot из API + повтор текста по корпусу
     (шаблонную рассылку часто шлют от имени рекрутера, флаг API её не ловит). */
  if (v.chat?.needs_reply) {
    const c = v.chat;
    const frozen = c.kind === 'ack';                 /* «резюме получено, свяжемся» — тупик, висит неделями */
    const who = frozen ? '' : c.sender === 'human' ? '👤' : c.sender === 'bot' ? '🤖' : '📋';
    const bg = frozen ? '#5b8fb0'                    /* фриз — ледяной, отличать от живых вопросов */
             : c.sender === 'human' ? '#D64550'      /* личное — требует внимания */
             : c.sender === 'bot' ? '#7B8794'        /* бот — серый, фоновый */
             : '#4C9BD1';                            /* шаблонная рассылка */
    const tip = (c.preview || '').replace(/"/g, '&quot;');
    const locked = c.can_write === false ? ' 🔒' : '';
    out += `<span class="status-badge" style="background:${bg}"`
         + ` title="${esc(tip)}">${who}${who ? ' ' : ''}${esc(c.label || 'ответ')}`
         + `${c.manual_only ? ' · решай сам' : ''}${locked}</span>`;
  }
  if (v.chat?.contact) {                      /* рекрутёр оставил связь прямо в переписке */
    out += `<span class="status-badge" style="background:#C7702E"`
         + ` title="Контакт из переписки: ${esc(v.chat.contact)}">📞 ${esc(v.chat.contact.split(' · ')[0])}</span>`;
  }
  if (v.applied?.ts) {
    const [y, m, d] = v.applied.ts.slice(0, 10).split('-');
    const via = v.applied.via === 'cron' ? 'крон' : v.applied.via === 'feed' ? 'лента' : (v.applied.via || '');
    out += `<span class="status-badge" style="background:#4C78A8"`
         + ` title="Ты откликнулся ${esc(y)}-${esc(m)}-${esc(d)} (${esc(via)})">📮 ${esc(d)}.${esc(m)}</span>`;
  }
  return out;
}

/* Бейдж свежести: возраст с создания + метка гост-вакансии (>60 дн, переоткрыта).
   Пусто, если тайминга нет (старый кеш до пере-сбора). */
function freshBadge(v) {
  if (v.age == null) return '';
  const color = ageColor(v.age);              /* температура: свежая горячая → старая холодная */
  const icon  = v.fresh === 'ghost' ? '👻 ' : (v.fresh === 'fresh' ? '🔥 ' : '');
  const gap   = (v.gap != null && v.gap > 7) ? ` · переопубл. +${v.gap}д` : '';
  const resp  = (v.resp != null) ? ` · ${v.resp} откл.` : '';
  const title = `Создана ${v.age} дн назад${gap}${resp}`;
  return `<span class="fresh-badge" style="color:${color};font-weight:600"`
       + ` title="${esc(title)}">${icon}${v.age}д${gap}</span>`;
}

/* Кнопки статуса — без активного состояния (одинаковы для всех карточек, поэтому
   константа). Активную подсветку ставит applyCardStatus уже после вставки в DOM. */
const STATUS_BTNS_HTML = STATUS_BTNS.map(s =>
  `<button class="status-btn st-${s.act}-btn" data-act="${s.act}" title="${s.title}">${s.label}</button>`,
).join('');

/* Разметка карточки БЕЗ статуса (статус — класс, навешивается после вставки),
   чтобы кэш HTML оставался валидным при тоггле отклика/отказа. */
function cardHTML(v) {
  const sal     = fmtSal(v, _displayCur);
  const salLine = sal ? `${sal} · ${esc(v.exp)}` : esc(v.exp);
  const sub     = [v.employer, v.city].filter(Boolean).map(esc).join(' · ');
  return `<div class="card${v.form_dead ? ' st-dead' : ''}" data-id="${esc(v.id)}">
  <div class="tags">${tagsHTML(v.techs, 8)}</div>
  <a class="card-title" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer"
     onclick="event.stopPropagation()">${esc(v.name)}</a>
  <div class="card-sub">${sub} ${freshBadge(v)}</div>
  <div class="card-status">${statusBadge(v)}</div>
  <div class="card-sal"><span>${salLine}</span>${matchBadge(v)}</div>
  <div class="card-foot">${STATUS_BTNS_HTML}</div>
</div>`;
}

/* ── Кэш разметки карточек: данные иммутабельны → строим HTML один раз на вакансию.
   На 34k это убирает повторный пересчёт fmtSal/resumeMatch/tagClr при каждом тоггле. ── */
const _cardCache = new Map();
let _displayCur = 'RUB';        /* валюта показа зарплат — прокидывается из state в render */
function cardHTMLCached(v) {
  let h = _cardCache.get(v.id);
  if (h === undefined) {
    h = cardHTML(v);
    _cardCache.set(v.id, h);
  }
  return h;
}

/* ── Инкрементальный рендер: НЕ вставляем десятки тысяч узлов разом (это и есть
   причина долгого отклика). Рисуем чанк, следующий догружаем, когда сторож у низа
   списка входит во вьюпорт (IntersectionObserver). Чинит и первый лоад, и тоггл-назад. ── */
const CHUNK = 80;
let _filtered = [];
let _rendered = 0;
let _marks = {};
let _sentinel = null;
let _io = null;

function applyStatuses(root, slice) {
  for (const v of slice) {
    const mark = _marks[v.id];
    if (!mark) continue;                     /* marks разрежены — трогаем только отмеченные */
    const st = cardColor(v) || mark;         /* CRM-статус (отказ/приглашение) важнее пометки — цвет И кнопка */
    const card = root.querySelector(`.card[data-id="${v.id}"]`);
    if (card) applyCardStatus(card, st);
  }
}

function renderChunk() {
  const el = document.getElementById('cards');
  const slice = _filtered.slice(_rendered, _rendered + CHUNK);
  if (!slice.length) return;
  el.insertAdjacentHTML('beforeend', slice.map(cardHTMLCached).join(''));
  applyStatuses(el, slice);
  _rendered += slice.length;
  if (_rendered >= _filtered.length && _io) _io.unobserve(_sentinel);
}

function ensureSentinel(el) {
  if (_sentinel?.isConnected) return _sentinel;
  _sentinel = document.createElement('div');
  _sentinel.className = 'cards-sentinel';
  _sentinel.setAttribute('aria-hidden', 'true');
  _sentinel.style.height = '1px';
  el.after(_sentinel);                       /* сосед ПОСЛЕ грида — не ломает grid-раскладку */
  return _sentinel;
}

/* ── Главный рендер списка: state -> счётчик + первый чанк (остальное лениво) ── */
export function render(state) {
  if ((state.displayCur || 'RUB') !== _displayCur) {   /* смена валюты -> зарплаты в кэше устарели */
    _displayCur = state.displayCur || 'RUB';
    _cardCache.clear();
  }
  _filtered = filterVacancies(state.vacancies, state);
  _marks = state.marks;
  _serverMode = state.serverMode;
  _rendered = 0;
  document.getElementById('counter').textContent =
    `Показано ${_filtered.length.toLocaleString('ru')} из ${state.vacancies.length.toLocaleString('ru')}`;
  const el = document.getElementById('cards');
  el.innerHTML = '';
  const sentinel = ensureSentinel(el);
  if (!_filtered.length) {
    if (_io) _io.disconnect();
    sentinel.style.display = 'none';
    el.innerHTML = '<div class="no-results">Вакансий не найдено — попробуйте изменить фильтры</div>';
    return;
  }
  sentinel.style.display = '';
  if (_io) {
    _io.disconnect();                        /* сбросить наблюдение прошлого рендера */
  } else {
    _io = new IntersectionObserver(
      entries => { if (entries.some(e => e.isIntersecting)) renderChunk(); },
      { rootMargin: '800px' },               /* догружать заранее, до достижения низа */
    );
  }
  _io.observe(sentinel);
  renderChunk();                             /* первый чанк — синхронно, мгновенный first paint */
}

/* Точечно подсветить статус карточки без полного ре-рендера: цвет тела + активная кнопка ✓/✕.
   Вызывающий передаёт эффективный статус (CRM важнее ручной пометки — см. applyStatuses). */
export function applyCardStatus(card, st) {
  card.classList.remove('st-applied', 'st-rejected');
  if (st) card.classList.add(`st-${st}`);
  card.querySelectorAll('.status-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.act === st);
  });
}

/* Инвалидация кэша HTML карточки: после живого изменения данных (форма/статус)
   след. рендер соберёт разметку заново. */
export function bustCard(id) {
  _cardCache.delete(id);
}

/* Живой апдейт CRM-бейджей (форма/статус) одной карточки: бастим кэш + перерисовываем
   ячейку .card-status на месте, если карточка сейчас в DOM (модалку не трогаем). */
export function refreshCardStatus(v) {
  bustCard(v.id);
  const cell = document.querySelector(`#cards .card[data-id="${v.id}"] .card-status`);
  if (cell) cell.innerHTML = statusBadge(v);
}

/* ── Индикатор синхронизации отметок ── */
export function setSync(syncState) {
  const el = document.getElementById('sync-status');
  if (!el) return;
  const map = {
    ok:    ['✓ синхронизировано', 'ok'],
    busy:  ['● сохраняю…', 'busy'],
    err:   ['⚠ ошибка сохранения', 'err'],
    local: ['⬇ локально — «Экспорт» в marks.json', 'local'],
  };
  const [txt, cls] = map[syncState] || ['', ''];
  el.textContent = txt;
  el.className = `sync-status ${cls}`;
}

/* ── Модалка ── */
const overlay  = document.getElementById('modal-overlay');
const modalBox = document.getElementById('modal-box');
let   activeId = null;   /* guard против устаревших загрузок описаний */
let   coverIdx = 0;
let   _serverMode = false;   /* кнопка «Откликнуться в фоне» — только под hh.py serve */

export function showModal(v) {
  activeId = v.id;
  const sal      = fmtSal(v, _displayCur);
  const schedLbl = SCHED_LABELS[v.schedule] || v.schedule || '';
  const salLine  = [sal, v.exp, schedLbl].filter(Boolean).join(' · ');
  const sub      = [v.employer, v.city].filter(Boolean).map(esc).join(' · ');
  const portal   = v.source === 'hirify' ? 'hirify.me' : 'hh.ru';
  /* автоклик (Playwright) — только HH; у прочих порталов лишь прямая ссылка */
  const canBg    = _serverMode && (v.source || 'hh') === 'hh';

  modalBox.innerHTML = `
    <div class="modal-head">
      <button class="modal-close" aria-label="Закрыть">&times;</button>
      <div class="modal-tags">${tagsHTML(v.techs)}</div>
      <a class="modal-title" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">${esc(v.name)}</a>
      <div class="modal-meta">${sub}</div>
      <div class="modal-sal"><span>${esc(salLine)}</span>${matchBadge(v)}</div>
    </div>
    <div class="modal-body">
      <div class="cover" id="m-cover"></div>
      <div id="m-desc"><div class="m-loading">Загрузка описания…</div></div>
    </div>
    <div class="modal-foot">
      <span class="apply-label">Откликнуться:</span>
      <a class="modal-hh-btn" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer"
         title="Открыть вакансию на ${esc(portal)} и откликнуться вручную">🌐 на ${esc(portal)}</a>
      ${canBg ? `<button class="modal-apply-btn" id="m-apply" data-id="${esc(v.id)}"
        data-url="${esc(v.url)}" title="Откликнуться в фоне через локальный сервер (Playwright) + письмо из ленты">
        🚀 в фоне (авто)</button><span class="m-apply-status" id="m-apply-status"></span>` : ''}
    </div>`;
  modalBox.querySelector('.modal-close').addEventListener('click', closeModal);

  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
  coverIdx = hashId(v.id) % COVER_TEMPLATES.length;   /* стабильный вариант на вакансию */
  renderCover(v);
  renderDesc(v);
}

export function closeModal() {
  overlay.classList.remove('open');
  document.body.style.overflow = '';
}

function renderCover(v) {
  const el = document.getElementById('m-cover');
  if (!el) return;
  el.innerHTML = `
    <div class="cover-head">
      <span class="cover-title">✉️ Сопроводительное письмо</span>
      <span class="cover-actions">
        <button class="cover-btn" id="cover-reroll">🔄 Другой вариант</button>
        <button class="cover-btn cover-copy" id="cover-copy">📋 Скопировать</button>
      </span>
    </div>
    <textarea class="cover-text" id="cover-text" spellcheck="false"></textarea>
    <div class="cover-hint">Вариант <span id="cover-idx"></span> из ${COVER_TEMPLATES.length} — отредактируйте под себя перед отправкой</div>`;
  fillCover(v);
  document.getElementById('cover-reroll').addEventListener('click', () => {
    coverIdx = (coverIdx + 1) % COVER_TEMPLATES.length;
    fillCover(v);
  });
  document.getElementById('cover-copy').addEventListener('click', copyCover);
}

function fillCover(v) {
  document.getElementById('cover-text').value = coverLetter(v, coverIdx);
  document.getElementById('cover-idx').textContent = coverIdx + 1;
}

function copyCover() {
  const ta = document.getElementById('cover-text');
  ta.focus(); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { /* fallthrough */ }
  if (!ok && navigator.clipboard) {
    try { navigator.clipboard.writeText(ta.value); ok = true; } catch { /* noop */ }
  }
  if (window.getSelection) window.getSelection().removeAllRanges();
  const btn = document.getElementById('cover-copy');
  const orig = btn.textContent;
  btn.textContent = ok ? '✓ Скопировано' : '⚠ Выдели и Ctrl+C';
  setTimeout(() => { btn.textContent = orig; }, 1600);
}

function renderDesc(v) {
  const el = document.getElementById('m-desc');
  if (!el) return;
  const show = (raw) => {
    const html = (raw || '').trim();
    el.innerHTML = html
      ? `<div class="vacancy-desc">${html}</div>`
      : '<div class="m-error">Описание не собрано для этой вакансии</div>';
  };
  if (window.__DESC) { show(window.__DESC[v.id]); return; }
  el.innerHTML = '<div class="m-loading">Загрузка описания…</div>';
  loadDescriptions()
    .then(d => { if (activeId === v.id) show(d[v.id]); })
    .catch(() => { if (activeId === v.id) el.innerHTML = '<div class="m-error">Не удалось загрузить описание</div>'; });
}
