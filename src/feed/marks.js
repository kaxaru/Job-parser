/* Marks-сервис — граница сайд-эффектов: localStorage + REST /api/marks +
   экспорт/импорт + ленивая загрузка описаний. Состоянием не владеет (оно в Store),
   только выполняет IO и возвращает данные/промисы.

   Источник правды на диске — data/marks.json. serve-режим автосейвит туда (POST),
   file:// — localStorage + вшитые при сборке SAVED_MARKS. */

const STATUS_KEY = 'hh_feed_status_v1';

/** Стартовые отметки: диск (вшитый SAVED_MARKS) под локальными правками localStorage. */
export function loadInitialMarks() {
  const disk = (typeof SAVED_MARKS !== 'undefined' && SAVED_MARKS) ? SAVED_MARKS : {};
  let local = {};
  try { local = JSON.parse(localStorage.getItem(STATUS_KEY)) || {}; } catch { local = {}; }
  return { ...disk, ...local };
}

export function saveLocal(marks) {
  try { localStorage.setItem(STATUS_KEY, JSON.stringify(marks)); } catch { /* нет localStorage */ }
}

/** POST отметок на сервер (serve-режим). Бросает при ошибке — в т.ч. на HTTP-статус:
    fetch реджектится только на сетевом сбое, 500 без проверки дал бы ложное «✓». */
export async function pushServer(marks) {
  const r = await fetch('api/marks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(marks),
  });
  if (!r.ok) throw new Error(`marks save: HTTP ${r.status}`);
}

/** GET отметок с сервера. Возвращает объект отметок или null (file:// / нет сервера). */
export async function pullServer() {
  try {
    const r = await fetch('api/marks', { cache: 'no-store' });
    if (!r.ok) return null;
    const disk = await r.json();
    return (disk && typeof disk === 'object') ? disk : {};
  } catch {
    return null;   /* file:// — сервера нет */
  }
}

/** Отклик в фоне через локальный сервер (serve-режим): сервер сам жмёт «Откликнуться»
    в Playwright и шлёт письмо в чат. Возвращает {status, letter}. Бросает на HTTP-ошибке.
    Медленно (браузер + DDoS-Guard) — вызывающий показывает индикатор.

    `employer` уезжает в журнал откликов вместе с названием и фиксируется В МОМЕНТ КЛИКА:
    к моменту дренажа очереди вакансия уходит из выдачи, и карточка-призрак (syntheticCard)
    не находится поиском по компании (инцидент 01.08.2026). Значение — как в кеше, БЕЗ
    префикса «ИП»: его дорисовывает интерфейс HH. Необязателен — сервер терпит его отсутствие. */
export async function applyVacancy(id, url, cover, name, employer = '') {
  const r = await fetch('api/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, url, cover, name, employer }),
  });
  if (!r.ok) throw new Error(`apply: HTTP ${r.status}`);
  return r.json();
}

/** GET произвольного JSON-эндпоинта сервера -> объект или null (file:// / нет сервера).
    Для живого оверлея форм/статусов поверх статики (без пересборки ленты). */
export async function pullJson(path) {
  try {
    const r = await fetch(path, { cache: 'no-store' });
    if (!r.ok) return null;
    const obj = await r.json();
    return (obj && typeof obj === 'object') ? obj : {};
  } catch {
    return null;   /* file:// — сервера нет */
  }
}

export function exportMarks(marks) {
  const blob = new Blob([JSON.stringify(marks, null, 0)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'marks.json';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/** Прочитать marks.json из файла -> объект отметок (или null при ошибке). */
export function importMarks(file) {
  return new Promise(resolve => {
    const rd = new FileReader();
    rd.onload = () => {
      let obj;
      try { obj = JSON.parse(rd.result); } catch { obj = null; }
      resolve(obj && typeof obj === 'object' ? obj : null);
    };
    rd.readAsText(file);
  });
}

/* Описания вынесены в feed-desc.js (грузятся ЛЕНИВО при первом открытии модалки).
   Через <script> (не fetch) — чтобы работало из file:// (там fetch блокируется CORS). */
let _descPromise = null;
export function loadDescriptions() {
  if (window.__DESC) return Promise.resolve(window.__DESC);
  if (_descPromise) return _descPromise;
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'feed-desc.js';
    s.onload = () => resolve(window.__DESC || {});
    s.onerror = () => reject(new Error('feed-desc.js не загружен'));
    document.head.appendChild(s);
  });
  /* Кешируем ТОЛЬКО успех: отвергнутый промис, оставленный в _descPromise, блокировал
     описания до перезагрузки страницы — один обрыв сети при первом открытии модалки, и
     дальше каждая карточка показывала «Не удалось загрузить описание» (08.08.2026).
     Сбрасываем ссылку, если она всё ещё указывает на этот промис: параллельная попытка,
     успевшая записать свой, не должна быть затёрта. */
  p.catch(() => { if (_descPromise === p) _descPromise = null; });
  _descPromise = p;
  return p;
}
