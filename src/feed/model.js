/* Model — чистая доменная логика ленты: форматирование, фильтрация/сортировка.
   БЕЗ DOM и IO (тестируемо). Профиль резюме — в resume.js, письма — в cover.js. */
import { matchesResume, resumeMatch } from './resume.js';

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ── Валюты: курсы (per-USD) и алиасы приходят из feed-data.js (FX_RATES/FX_ALIAS).
   В тестах их нет — читаем через guard, базовые алиасы вшиты. ── */
const CUR_SYMBOL = { RUB: '₽', USD: '$', EUR: '€' };
const BASE_ALIAS = { RUR: 'RUB', USDT: 'USD', BYR: 'BYN' };
const fxRates = () => (typeof FX_RATES !== 'undefined' && FX_RATES) || {};
const fxAlias = () => ({ ...BASE_ALIAS, ...((typeof FX_ALIAS !== 'undefined' && FX_ALIAS) || {}) });

/** Код валюты -> канонический (RUR->RUB, USDT->USD, BYR->BYN). */
export function resolveCur(code) {
  const c = String(code || '').toUpperCase();
  return fxAlias()[c] || c;
}

/** Конвертация суммы между валютами через базу USD (курсы per-USD). Нет курса -> как есть. */
export function convert(amount, from, to) {
  if (amount == null) return null;
  const f = resolveCur(from), t = resolveCur(to);
  if (f === t) return amount;
  const r = fxRates(), rf = r[f], rt = r[t];
  if (!rf || !rt) return amount;               /* неизвестная валюта -> без конверсии */
  return amount / rf * rt;                      /* amount(f) -> USD -> t */
}

/** Зарплата (уже месячная на этапе сборки) в валюте `cur`, суффикс «/мес». Валюта записи
    без курса -> показываем в её оригинальной валюте. */
export function fmtSal(v, cur = 'RUB') {
  if (v.sal_from == null && v.sal_to == null) return '';
  const from = resolveCur(v.currency);
  const canConv = from === cur || (fxRates()[from] && fxRates()[cur]);
  const outCur = canConv ? cur : from;
  const sym = CUR_SYMBOL[outCur] || outCur;
  const conv = n => (n == null ? null : Math.round(canConv ? convert(n, v.currency, cur) : n));
  const fmt = n => (n != null ? n.toLocaleString('ru') : null);
  const f = fmt(conv(v.sal_from)), t = fmt(conv(v.sal_to));
  const unit = `${sym}/мес`;
  if (f && t) return `${f} — ${t} ${unit}`;
  if (f)      return `от ${f} ${unit}`;
  if (t)      return `до ${t} ${unit}`;
  return '';
}

export function fmtK(n) { return n >= 1000 ? `${n / 1000 | 0}к` : String(n); }

/* Давность последнего сообщения работодателя для чат-бейджа: «сегодня»/«вчера»/«N дн».
   now инъектируется в тестах; битая/пустая метка -> '' (бейдж без хвоста, не NaN). */
export function chatAgeLabel(ts, now = Date.now()) {
  if (!ts) return '';
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return '';
  const days = Math.floor((now - t) / 86400000);
  if (days < 0) return '';
  if (days === 0) return 'сегодня';
  if (days === 1) return 'вчера';
  return `${days} дн`;
}

export function hashId(s) {
  let h = 0; s = String(s);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

/* ── Цвета технологий [bg, fg] и подписи графика ── */
const TC = {
  'Python':       ['#3572A5', '#fff'], 'JavaScript':      ['#f1e05a', '#222'],
  'TypeScript':   ['#2b7489', '#fff'], 'Java':            ['#b07219', '#fff'],
  'Go':           ['#00ADD8', '#fff'], 'C++':             ['#f34b7d', '#fff'],
  'C#':           ['#178600', '#fff'], 'PHP':             ['#4F5D95', '#fff'],
  'Kotlin':       ['#7F52FF', '#fff'], 'Swift':           ['#F05138', '#fff'],
  'Rust':         ['#DEA584', '#222'], 'Ruby':            ['#701516', '#fff'],
  'Scala':        ['#c22d40', '#fff'], '1С':              ['#cc3300', '#fff'],
  'React':        ['#61dafb', '#222'], 'Vue':             ['#41b883', '#fff'],
  'Angular':      ['#dd1b16', '#fff'], 'Next.js':         ['#000', '#fff'],
  'Django':       ['#092E20', '#fff'], 'FastAPI':         ['#009688', '#fff'],
  'Flask':        ['#444', '#fff'],    'Spring':          ['#6db33f', '#fff'],
  'Node.js':      ['#339933', '#fff'], 'NestJS':          ['#e0234e', '#fff'],
  'Docker':       ['#2496ED', '#fff'], 'Kubernetes':      ['#326CE5', '#fff'],
  'PostgreSQL':   ['#336791', '#fff'], 'MySQL':           ['#4479A1', '#fff'],
  'MongoDB':      ['#47A248', '#fff'], 'Redis':           ['#DC382D', '#fff'],
  'Kafka':        ['#231F20', '#fff'], 'AWS':             ['#FF9900', '#222'],
  'ML/AI':        ['#FF6F00', '#fff'], 'Web3/Blockchain': ['#F7931A', '#222'],
};
export function tagClr(t) { return TC[t] || ['#3a3d4a', '#bbb']; }

export const SCHED_LABELS = {
  fullDay: 'Полный день', remote: 'Удалённо', flexible: 'Гибкий график',
  shift: 'Сменный', flyInFlyOut: 'Вахта',
};

/* Словарь пометок — единый источник в Python (marks.py::MARK_VALUES), инжектится через
   feed-data.js (MARK_VALUES_PY). Хардкод — фолбэк для офлайна/тестов. Дублировать значения
   нельзя: так уже разъезжались словари (инцидент "discard" 2026-07-22). */
export const MARK_VALUES = (typeof MARK_VALUES_PY !== 'undefined' && MARK_VALUES_PY)
  || ['applied', 'rejected'];
const MARK_BTN_META = {
  applied:  { label: '✓ Отклик', title: 'Отметить/снять «откликнулся»' },
  rejected: { label: '✕ Отказ',  title: 'Отметить/снять «отказ»' },
};
export const STATUS_BTNS = MARK_VALUES.map(
  v => ({ act: v, ...(MARK_BTN_META[v] || { label: v, title: v }) }));

/* Тёплый (полное совпадение) → холодный (нет совпадения). */
export function matchColor(pct) {
  const hue = 210 - 195 * (pct / 100);       /* 100%→15 тёплый, 0%→210 холодный */
  return `hsl(${hue.toFixed(0)}, 70%, 42%)`;
}

/* Температура возраста вакансии: свежая (0 дн) → горячий красный,
   старая/гост (90+ дн) → холодный синий. null → серый (нет даты). */
export function ageColor(age) {
  if (age == null) return '#9096a0';
  const t   = Math.max(0, Math.min(age, 90)) / 90;   /* 0..1 */
  const hue = 10 + 200 * t;                            /* 10 красный → 210 синий */
  return `hsl(${hue.toFixed(0)}, 72%, 50%)`;
}

/* ── Статус отклика из API HH (v.status = currentApplicantState) ──
   Подписи приходят из Python (chat.STATE_LABELS) через feed-data.js — единый источник.
   Хардкод ниже — фолбэк для офлайна/тестов (feed-data.js не подгружен). */
export const STATE_LABELS = (typeof STATE_LABELS_PY !== 'undefined' && STATE_LABELS_PY) || {
  RESPONSE: 'Отклик', INVITATION: 'Приглашение', CONSIDER: 'Рассматривается',
  PHONE_INTERVIEW: 'Звонок', INTERVIEW: 'Интервью', ASSESSMENT: 'Тестовое',
  HIRED: 'Оффер', DISCARD: 'Отказ', DISCARD_BY_EMPLOYER: 'Отказ',
  DISCARD_BY_APPLICANT: 'Вы отказались', DISCARD_VACANCY_CLOSED: 'Закрыта',
};
/* Тупиковые виды чата («фриз»): заглушка «резюме получено, свяжемся» и интервью с ботом
   в чужом мессенджере — висят неделями и ответа по существу не ждут. Единый источник —
   Python (chat_class.FROZEN_KINDS), инжектится как CHAT_FROZEN_PY; хардкод — фолбэк
   для офлайна/тестов. Раньше код 'ack' был захардкожен в трёх местах ленты. */
export const FROZEN_CHAT_KINDS = new Set(
  (typeof CHAT_FROZEN_PY !== 'undefined' && CHAT_FROZEN_PY) || ['ack', 'bot_interview']);
export const isFrozenChat = c => !!c && FROZEN_CHAT_KINDS.has(c.kind);

/* «Приглашение» — работодатель проявил активность (не просто RESPONSE и не отказ). */
const INVITED = new Set(['INVITATION', 'PHONE_INTERVIEW', 'INTERVIEW', 'ASSESSMENT', 'HIRED', 'CONSIDER']);

export const isDiscard = s => typeof s === 'string' && s.startsWith('DISCARD');
export const isInvited = s => INVITED.has(s);

/* Отклик попадает в календарный диапазон [from, to] (границы — 'YYYY-MM-DD', пустые =
   без границы). Сравниваем ISO-даты как строки — они лексикографически упорядочены. */
export function appliedInRange(v, from, to) {
  if (!v.applied?.ts) return false;
  const d = v.applied.ts.slice(0, 10);
  if (from && d < from) return false;
  if (to && d > to) return false;
  return true;
}

/* Инфо для бейджа: подпись + цвет (отказ красный, приглашение зелёный, отклик серый). */
export function statusInfo(v) {
  const s = v.status;
  if (!s) return null;
  const color = isDiscard(s) ? '#E45756' : isInvited(s) ? '#3FA34D' : '#8a8f98';
  return { code: s, label: STATE_LABELS[s] || s, color };
}

/* Цвет ТЕЛА карточки: CRM-статус HH важнее ручной пометки (тот же приоритет, что у бейджа).
   Отказ красит карточку красной даже поверх ручного «откликнулся» — иначе отклонённая
   вакансия светится зелёной (пометка applied). '' = нет терминального CRM-статуса ->
   цвет ставит вызывающий по ручной пометке. */
export function cardColor(v) {
  if (isDiscard(v.status)) return 'rejected';
  if (isInvited(v.status)) return 'applied';
  return '';
}

/* Тон РАМКИ карточки. Отличается от cardColor тем, что учитывает полумёртвое бот-интервью:
   отклик формально ушёл (рамка была бы зелёной), но интервью проходят у бота в чужом
   мессенджере, а вакансия после этого обычно морозится — красим жёлтым, чтобы такие
   не выглядели живыми откликами. Отказ/приглашение с HH сильнее: это реальный исход.
   '' = тона нет, вызывающий красит по ручной пометке. */
export function cardTone(v) {
  return cardColor(v) || (v?.chat?.kind === 'bot_interview' ? 'botiv' : '');
}

/* Фильтрация + сортировка — чистая: (вакансии, состояние фильтров) -> массив. */
export function filterVacancies(vacancies, f) {
  /* Режим «Мои отклики»: самостоятельный вид — только твои отклики в календарном
     диапазоне (+ поиск), прочие чипы игнорируются; сортировка — по дате отклика ↓. */
  if (f.status === 'mine') {
    const mine = vacancies.filter(v => {
      if (!appliedInRange(v, f.dateFrom, f.dateTo)) return false;
      if (f.search.length) {
        const hay = (`${v.name} ${v.employer || ''}`).toLowerCase();
        if (!f.search.every(t => hay.includes(t))) return false;
      }
      return true;
    });
    mine.sort((a, b) => (b.applied?.ts || '').localeCompare(a.applied?.ts || ''));
    return mine;
  }

  const filtered = vacancies.filter(v => {
    /* карточки-«призраки» (отклик на выпавшую из выдачи вакансию): в общем списке скрыты,
       но показываем в чат-фильтрах — по ним висят живые чаты, на которые надо ответить. */
    if (v._synthetic && !f.chatFilter) return false;
    if (f.search.length) {
      const hay = (`${v.name} ${v.employer || ''}`).toLowerCase();
      if (!f.search.every(t => hay.includes(t))) return false;
    }
    if (f.resumeOnly && !matchesResume(v)) return false;
    /* Совпадение (matchSort/resumeOnly): гост-вакансии (>60 дней, переопубликованные) —
       мёртвые, не должны светиться как высокий матч. Отсеиваем их из режима совпадения. */
    if ((f.matchSort || f.resumeOnly) && v.fresh === 'ghost') return false;
    if (!f.showNonIt && v.role === 'Не-IT') return false;          /* по умолчанию не-IT скрыты */
    if (f.roles.size > 0 && !f.roles.has(v.role)) return false;    /* фильтр по роли (чипы) */
    if (f.langs.size > 0 && !v.techs.some(t => f.langs.has(t))) return false;
    if (f.exps.size > 0 && !f.exps.has(v.exp)) return false;
    if (f.minSal > 0 || f.maxSal < f.salMax) {
      const mid = v.sal_mid === null ? null : convert(v.sal_mid, v.currency, f.displayCur || 'RUB');
      if (mid === null) {
        if (f.minSal > 0) return false;
      } else if (mid < f.minSal || mid > f.maxSal) {
        return false;
      }
    }
    if (f.source && f.source !== 'all' && v.source !== f.source) return false;  /* портал-источник */
    if (f.city && v.city !== f.city) return false;
    if (f.schedule === 'remote' && v.schedule !== 'remote') return false;
    if (f.schedule === 'office' && v.schedule === 'remote') return false;
    /* Статус отклика (API): all | invited | discard | response | form */
    if (f.status === 'invited' && !isInvited(v.status)) return false;
    if (f.status === 'discard' && !isDiscard(v.status)) return false;
    if (f.status === 'response' && v.status !== 'RESPONSE') return false;
    if (f.status === 'form' && !v.needs_form) return false;
    /* переписка: 'wait' — работодатель написал последним; 'manual' — вопрос,
       который бот отвечать не должен (деньги/переезд) */
    if (f.chatFilter === 'wait' && !v.chat?.needs_reply) return false;
    if (f.chatFilter === 'manual' && !v.chat?.manual_only) return false;
    /* 'personal' — живой человек И чат открыт для ответа: реальный список дел,
       без шаблонной рассылки, без чатов, куда HH писать не даст, и БЕЗ фризов
       (заглушка «резюме получено, свяжемся» и бот-интервью в чужом мессенджере —
       диалога там нет) */
    if (f.chatFilter === 'personal'
        && !(v.chat?.needs_reply && v.chat.sender === 'human'
             && v.chat.can_write !== false && !isFrozenChat(v.chat))) {
      return false;
    }
    /* 'contact' — рекрутёр оставил телефон/телеграм в переписке (независимо от needs_reply:
       контакт ценен и после нашего ответа) */
    if (f.chatFilter === 'contact' && !v.chat?.contact) return false;
    return true;
  });

  /* Составная сортировка: % совпадения — ОСНОВНОЙ ключ (matchSort), зарплата —
     ВТОРИЧНЫЙ (или единственный, если совпадение выключено). Можно врубить вместе.
     pct кэшируем по вакансии — resumeMatch раз на запись, а не на каждое сравнение. */
  const salDir = f.sort === 'desc' ? -1 : f.sort === 'asc' ? 1 : 0;
  const salVal = v => (v.sal_mid === null ? null : convert(v.sal_mid, v.currency, f.displayCur || 'RUB'));
  const salCmp = (a, b) => {
    const am = salVal(a), bm = salVal(b);
    if (am === null && bm === null) return 0;
    if (am === null) return 1;               /* nulls always last */
    if (bm === null) return -1;
    return salDir * (am - bm);
  };

  /* Сортировка по дате появления вакансии (age = дней с создания; без даты -> в конец).
     date_new — свежие первыми (малый age), date_old — старые первыми. Самостоятельный ключ. */
  if (f.sort === 'date_new' || f.sort === 'date_old') {
    const dir = f.sort === 'date_new' ? 1 : -1;
    filtered.sort((a, b) => {
      if (a.age == null && b.age == null) return 0;
      if (a.age == null) return 1;
      if (b.age == null) return -1;
      return dir * (a.age - b.age);
    });
    return filtered;
  }

  const wantMatch = f.matchSort || (f.sort === 'none' && f.resumeOnly);

  if (wantMatch) {
    const pct = new Map();
    for (const v of filtered) pct.set(v, resumeMatch(v).pct);
    filtered.sort((a, b) => (pct.get(b) - pct.get(a)) || (salDir ? salCmp(a, b) : 0));
  } else if (salDir) {
    filtered.sort(salCmp);
  }
  return filtered;
}
