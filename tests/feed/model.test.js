/* Юнит-тесты доменной модели ленты (src/feed/model.js).
   Запуск: node --test tests/feed/  (или `npm test`). Без зависимостей — node:test + node:assert.
   model.js — ЧИСТАЯ логика (без DOM/IO), поэтому тестируется прямым импортом в Node. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  APPLY_LABELS,
  SCHED_LABELS,
  ageColor, appliedInRange, cardPaint, cardColor, cardTone, chatAgeLabel, cityMatches, convert,
  countActiveFilters, esc, filterVacancies, fmtK, fmtSal, fmtSalMulti, hashId, isFrozenChat, isRemoteLike,
  matchColor, matchInk,
  safeUrl,
  isDiscard, isInvited,
  crmStats, effectiveApplied, effectiveChat, effectiveStatus, journalById, portalSite, resolveCur, statusInfo, syntheticCard, tagClr, tagInk,
  abCompare, abText, pickSummary, pulseLine, waitingByAccount,
  comparableSalary,
  employmentLabel,
  hrReplyTime,
  staleAge,
} from '../../src/feed/model.js';

/* Фабрика вакансии с дефолтами — переопределяем только нужные поля в каждом тесте.
   exp — подпись для показа, exp_id — доменный код грейда (по нему идут чипы и скоринг). */
function vac(over = {}) {
  return {
    id: '1', name: 'Python Backend', employer: 'Acme', url: '', city: 'Москва',
    sal_from: null, sal_to: null, sal_mid: null, currency: 'RUR',
    exp: '1–3 года', exp_id: 'between1And3',
    schedule: 'remote', techs: ['Python'], remote_any: true,
    role: 'Backend', status: null, needs_form: false,
    ...over,
  };
}
/* Полное состояние фильтров (как в store) — тесты меняют точечно. */
function flt(over = {}) {
  return {
    search: [], resumeOnly: false, langs: new Set(), roles: new Set(),
    showNonIt: false, exps: new Set(), emps: new Set(),
    minSal: 0, maxSal: 1_000_000, salMax: 1_000_000,
    city: '', schedule: 'all', status: 'all', source: 'all', displayCurs: ['RUB'],
    dateFrom: '', dateTo: '', sort: 'none', matchSort: false,
    ...over,
  };
}
/* Нормализация пробелов: toLocaleString('ru') даёт NBSP/NNBSP в зависимости от
   версии ICU — сравниваем по обычным пробелам, чтобы тест не флапал между Node. */
const ws = s => s.replace(/\s/g, ' ');

describe('esc — экранирование HTML', () => {
  it('экранирует спецсимволы', () => {
    assert.equal(esc('<a href="x">&'), '&lt;a href=&quot;x&quot;&gt;&amp;');
  });
  it('null/undefined → пустая строка', () => {
    assert.equal(esc(null), '');
    assert.equal(esc(undefined), '');
  });
});

/* href берётся из выдачи портала (чужие данные), а esc схему не трогает — аудит 08.08.2026. */
describe('safeUrl — схема ссылки из данных портала', () => {
  it('http и https проходят', () => {
    assert.equal(safeUrl('https://hh.ru/vacancy/1'), 'https://hh.ru/vacancy/1');
    assert.equal(safeUrl('http://arbeitnow.com/x'), 'http://arbeitnow.com/x');
  });
  it('javascript: не проходит', () => {
    assert.equal(safeUrl('javascript:alert(1)'), '#');
    assert.equal(safeUrl('  JavaScript:alert(1)'), '#');
  });
  it('data: и прочие схемы не проходят', () => {
    assert.equal(safeUrl('data:text/html,<script>alert(1)</script>'), '#');
    assert.equal(safeUrl('vbscript:msgbox(1)'), '#');
    assert.equal(safeUrl('//evil.example/x'), '#');
  });
  it('пусто/null → #', () => {
    assert.equal(safeUrl(''), '#');
    assert.equal(safeUrl(null), '#');
    assert.equal(safeUrl(undefined), '#');
  });
  it('кавычка в url экранируется, из атрибута не вырваться', () => {
    assert.equal(safeUrl('https://x.io/a"onmouseover="alert(1)'),
                 'https://x.io/a&quot;onmouseover=&quot;alert(1)');
  });
});

describe('statusInfo — бейдж статуса отклика (API)', () => {
  it('нет статуса → null', () => {
    assert.equal(statusInfo(vac({ status: null })), null);
  });
  /* Подложки затемнены 27.07 до 4.5:1 с белым текстом бейджа: прежние #E45756/#3FA34D/#8a8f98
     давали 3.62/3.20/3.25 при кегле 11px bold, то есть ниже нормы AA. */
  it('отказ → красный', () => {
    assert.equal(statusInfo(vac({ status: 'DISCARD' })).label, 'Отказ');
    assert.equal(statusInfo(vac({ status: 'DISCARD' })).color, '#DE3433');
  });
  it('приглашение/интервью → зелёный', () => {
    assert.equal(statusInfo(vac({ status: 'INTERVIEW' })).color, '#34863F');
  });
  it('отклик без ответа → серый', () => {
    assert.equal(statusInfo(vac({ status: 'RESPONSE' })).color, '#717781');
  });
  it('незнакомый код → показываем как есть', () => {
    assert.equal(statusInfo(vac({ status: 'WEIRD' })).label, 'WEIRD');
  });
});

describe('filterVacancies — призраки (выпавшие из выдачи) видны в чат-фильтрах', () => {
  const ghost = vac({ id: 'g', _synthetic: true, chat: { needs_reply: true } });
  const real  = vac({ id: 'r', chat: { needs_reply: true } });
  it('без чат-фильтра призрак скрыт', () => {
    assert.deepEqual(filterVacancies([ghost, real], flt()).map(v => v.id), ['r']);
  });
  it('в фильтре «ждут ответа» призрак показан', () => {
    assert.deepEqual(
      filterVacancies([ghost, real], flt({ chatFilter: 'wait' })).map(v => v.id).sort(),
      ['g', 'r']);
  });
});

describe('filterVacancies — «Личные»: живой диалог, без фризов (ack / bot_interview)', () => {
  const human  = vac({ id: 'h', chat: { needs_reply: true, sender: 'human', kind: 'question' } });
  const frozen = vac({ id: 'f', chat: { needs_reply: true, sender: 'human', kind: 'ack' } });
  const botIv  = vac({ id: 'g', chat: { needs_reply: true, sender: 'human', kind: 'bot_interview' } });
  const bot    = vac({ id: 'b', chat: { needs_reply: true, sender: 'bot', kind: 'question' } });
  const closed = vac({ id: 'c', chat: { needs_reply: true, sender: 'human', kind: 'question', can_write: false } });
  const all = [human, frozen, botIv, bot, closed];
  it('personal: только человек с живым текстом; фриз/бот/закрытый чат скрыты', () => {
    assert.deepEqual(filterVacancies(all, flt({ chatFilter: 'personal' })).map(v => v.id), ['h']);
  });
  it('personal: бот-интервью (Сбер/ГигаРекрутер) — фриз, в список дел не попадает', () => {
    /* письмо приходит от имени человека, но диалога нет: интервью проходят у бота
       в Telegram/Max, а вакансия после этого обычно морозится */
    assert.equal(isFrozenChat(botIv.chat), true);
    assert.equal(filterVacancies([botIv], flt({ chatFilter: 'personal' })).length, 0);
  });
  it('wait: фризы остаются видны (это широкий фильтр, не «Личные»)', () => {
    assert.equal(filterVacancies(all, flt({ chatFilter: 'wait' })).length, 5);
  });
});

describe('isFrozenChat — тупиковые виды чата', () => {
  it('фриз: заглушка «свяжемся» и бот-интервью', () => {
    assert.equal(isFrozenChat({ kind: 'ack' }), true);
    assert.equal(isFrozenChat({ kind: 'bot_interview' }), true);
  });
  it('не фриз: живой вопрос, редирект к человеку, отсутствие чата', () => {
    assert.equal(isFrozenChat({ kind: 'question' }), false);
    assert.equal(isFrozenChat({ kind: 'redirect' }), false);
    assert.equal(isFrozenChat(null), false);
    assert.equal(isFrozenChat(undefined), false);
  });
});

describe('filterVacancies — «С контактами»: рекрутёр оставил телефон/телеграм', () => {
  const withTg = vac({ id: 't', chat: { contact: '@hr_nick' } });
  const withPhone = vac({ id: 'p', chat: { needs_reply: true, contact: '+7 912 345-67-89' } });
  const без = vac({ id: 'n', chat: { needs_reply: true, contact: '' } });
  it('contact: только с контактом, независимо от needs_reply', () => {
    assert.deepEqual(
      filterVacancies([withTg, withPhone, без], flt({ chatFilter: 'contact' })).map(v => v.id).sort(),
      ['p', 't']);
  });
});

describe('cardColor — цвет тела карточки (CRM-приоритет над ручной пометкой)', () => {
  it('отказ HH → rejected (красная), даже поверх ручного applied', () => {
    assert.equal(cardColor(vac({ status: 'DISCARD' })), 'rejected');
    assert.equal(cardColor(vac({ status: 'DISCARD_BY_EMPLOYER' })), 'rejected');
  });
  it('приглашение/интервью → applied (зелёная)', () => {
    assert.equal(cardColor(vac({ status: 'INTERVIEW' })), 'applied');
    assert.equal(cardColor(vac({ status: 'INVITATION' })), 'applied');
  });
  it('нет терминального статуса → "" (цвет ставит вызывающий по ручной пометке)', () => {
    assert.equal(cardColor(vac({ status: 'RESPONSE' })), '');
    assert.equal(cardColor(vac({ status: null })), '');
  });
});

describe('cardTone — тон рамки: жёлтое бот-интервью против реального исхода с HH', () => {
  const botIv = { needs_reply: true, kind: 'bot_interview' };
  it('бот-интервью без исхода → botiv (жёлтая вместо зелёной «откликнулся»)', () => {
    assert.equal(cardTone(vac({ status: 'RESPONSE', chat: botIv })), 'botiv');
    assert.equal(cardTone(vac({ status: null, chat: botIv })), 'botiv');
  });
  it('отказ на бот-интервью → rejected: рамка красная, жёлтое предупреждение уступает', () => {
    assert.equal(cardTone(vac({ status: 'DISCARD', chat: botIv })), 'rejected');
    assert.equal(cardTone(vac({ status: 'DISCARD_BY_EMPLOYER', chat: botIv })), 'rejected');
  });
  it('INTERVIEW/INVITATION на бот-интервью → всё равно botiv, зелёная рамка врала', () => {
    /* ЖИВОЙ КЕЙС 26.07: HH сам переводит отклик в INTERVIEW, когда бот зовёт на интервью —
       у всех 24 бот-интервью в ленте был статус INTERVIEW, поэтому cardColor давал зелёный
       и жёлтая рамка не появлялась ни на одной карточке. «Приглашение» здесь = приглашение
       бота, не движение по вакансии. */
    assert.equal(cardTone(vac({ status: 'INTERVIEW', chat: botIv })), 'botiv');
    assert.equal(cardTone(vac({ status: 'INVITATION', chat: botIv })), 'botiv');
  });
  it('приглашение без бот-интервью → applied (обычный зелёный путь не тронут)', () => {
    assert.equal(cardTone(vac({ status: 'INTERVIEW', chat: { kind: 'question' } })), 'applied');
  });
  it('мёртвая анкета → frozen: вакансию сняли, это не отказ работодателя', () => {
    assert.equal(cardTone(vac({ status: 'RESPONSE', form_dead: true })), 'frozen');
    assert.equal(cardTone(vac({ status: null, form_dead: true })), 'frozen');
  });
  it('отказ HH сильнее мёртвой анкеты: реальный исход важнее протухшей вакансии', () => {
    assert.equal(cardTone(vac({ status: 'DISCARD', form_dead: true })), 'rejected');
  });
  it('мёртвая анкета сильнее бот-интервью и приглашения', () => {
    assert.equal(cardTone(vac({ status: 'INTERVIEW', form_dead: true, chat: botIv })), 'frozen');
  });
  it('обычный чат → "" (как cardColor: тон по ручной пометке)', () => {
    assert.equal(cardTone(vac({ status: 'RESPONSE', chat: { kind: 'question' } })), '');
    assert.equal(cardTone(vac({ status: 'RESPONSE' })), '');
  });
});

/* Контракт кнопки ✓/✕: «ручная отметка против CRM-статуса» — одно правило на рендер ленты и
   на живой клик. `status` красит тело и активную кнопку, `tone` — рамку. Ожидания записаны
   по правилу `cardColor(v)`/`cardTone(v)` важнее отметки, отметка важнее пустоты. */
describe('cardPaint — ручная отметка против CRM-статуса', () => {
  it('ручная отметка без терминального статуса идёт и в тело, и в рамку', () => {
    assert.deepEqual(cardPaint(vac({ status: 'RESPONSE' }), 'applied'),
                     { status: 'applied', tone: 'applied' });
  });
  it('терминальный статус HH сильнее ручной отметки', () => {
    assert.deepEqual(cardPaint(vac({ status: 'DISCARD' }), 'applied'),
                     { status: 'rejected', tone: 'rejected' });
  });
  it('две роли разведены: бот-интервью даёт жёлтую рамку при зелёном теле', () => {
    assert.deepEqual(cardPaint(vac({ status: 'INTERVIEW', chat: { kind: 'bot_interview' } }), null),
                     { status: 'applied', tone: 'botiv' });
  });
  it('ни статуса, ни отметки -> null в обоих полях, а не undefined', () => {
    assert.deepEqual(cardPaint(vac({ status: null }), null), { status: null, tone: null });
  });
});

describe('cityMatches — выпадающий список городов с поиском внутри', () => {
  it('точный выбор из списка не тянет однокоренные города', () => {
    assert.equal(cityMatches('Москва', 'Москва', true), true);
    assert.equal(cityMatches('Московский', 'Москва', true), false);
  });
  it('набранный фрагмент ищет по подстроке, регистр не важен', () => {
    assert.equal(cityMatches('Санкт-Петербург', 'сан'), true);
    assert.equal(cityMatches('Санкт-Петербург', 'ПЕТЕР'), true);
    assert.equal(cityMatches('Казань', 'сан'), false);
  });
  it('пустой запрос и пустой город: фильтра нет / не падаем', () => {
    assert.equal(cityMatches('Москва', ''), true);
    assert.equal(cityMatches('Москва', '   '), true);
    assert.equal(cityMatches(null, 'москва'), false);
  });
  it('в фильтрации: фрагмент подбирает несколько городов, точный выбор — один', () => {
    const data = [vac({ id: 'm', city: 'Москва' }), vac({ id: 'mk', city: 'Московский' })];
    assert.deepEqual(filterVacancies(data, flt({ city: 'моск' })).map(v => v.id), ['m', 'mk']);
    assert.deepEqual(
      filterVacancies(data, flt({ city: 'Москва', cityExact: true })).map(v => v.id), ['m']);
  });
});

describe('filterVacancies — фильтр по статусу отклика', () => {
  const vacs = [
    vac({ id: 'a', status: 'RESPONSE' }),
    vac({ id: 'b', status: 'DISCARD' }),
    vac({ id: 'c', status: 'INTERVIEW' }),
    vac({ id: 'd', status: null, needs_form: true }),
    vac({ id: 'e', status: null }),
  ];
  const ids = f => filterVacancies(vacs, flt(f)).map(v => v.id);
  it('all → все', () => assert.deepEqual(ids({ status: 'all' }).sort(), ['a', 'b', 'c', 'd', 'e']));
  it('invited → только приглашения/интервью', () => assert.deepEqual(ids({ status: 'invited' }), ['c']));
  it('discard → только отказы', () => assert.deepEqual(ids({ status: 'discard' }), ['b']));
  it('response → только без ответа', () => assert.deepEqual(ids({ status: 'response' }), ['a']));
  it('form → только формы', () => assert.deepEqual(ids({ status: 'form' }), ['d']));
});

/* Шкала задана спекой: тон линейно едет 10 (красный) -> 210 (синий) на отрезке 0..90 дней,
   дальше клампится. Неравенства «hue <= 30 / >= 200 / растёт» держали и уехавшую шкалу
   (например 20..200: «горячий красный» уже не красный), а монотонность вдобавок сравнивала
   функцию саму с собой (аудит 09.08.2026). Светлота НЕ проверяется: она подбирается под
   подложку и в спеке названа плавающей. */
describe('ageColor — температурный градиент возраста', () => {
  const hue = c => Number(c.match(/hsl\((\d+)/)[1]);
  it('свежая (0 дн) — горячий красный, тон 10', () => {
    assert.equal(hue(ageColor(0)), 10);
  });
  it('10 дн — тон 32 (10 + 200·10/90)', () => {
    assert.equal(hue(ageColor(10)), 32);
  });
  it('60 дн — тон 143 (10 + 200·60/90)', () => {
    assert.equal(hue(ageColor(60)), 143);
  });
  it('старая (90 дн) — холодный синий, тон 210', () => {
    assert.equal(hue(ageColor(90)), 210);
  });
  it('старше 90 дн — тон клампится на 210', () => {
    assert.equal(hue(ageColor(120)), 210);
  });
  it('нет даты → нейтральный серый', () => {
    assert.equal(ageColor(null), '#9096a0');
  });
});

describe('fmtSal — форматирование зарплаты (месячная, валюта показа)', () => {
  it('диапазон from—to, RUR→₽, суффикс /мес', () => {
    assert.equal(ws(fmtSal(vac({ sal_from: 150000, sal_to: 200000 }))), '150 000 — 200 000 ₽/мес');
  });
  it('только from → «от N ₽/мес»', () => {
    assert.equal(ws(fmtSal(vac({ sal_from: 150000 }))), 'от 150 000 ₽/мес');
  });
  it('только to → «до N ₽/мес»', () => {
    assert.equal(ws(fmtSal(vac({ sal_to: 200000 }))), 'до 200 000 ₽/мес');
  });
  it('нет вилки → пустая строка', () => {
    assert.equal(fmtSal(vac()), '');
  });
  it('валюта без курса — показываем в оригинале, сумму не трогаем', () => {
    // нет FX_RATES -> конверсии нет: та же тысяча и символ исходной валюты. Проверка
    // одного лишь символа пропускала «от 0 $/мес» и пересчёт по несуществующему курсу
    // (аудит 09.08.2026).
    assert.equal(ws(fmtSal(vac({ sal_from: 1000, currency: 'USD' }))), 'от 1 000 $/мес');
  });
  it('с курсами: конвертит RUR→USD + /мес', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(ws(fmtSal(vac({ sal_from: 90000, currency: 'RUR' }), 'USD')), 'от 1 000 $/мес');
    delete globalThis.FX_RATES;
  });
});

/* Зарплата в НЕСКОЛЬКИХ валютах показа (мультивыбор, 23.09.2026): владелец выбрал несколько
   валют — вилка печатается в каждой. Порядок частей = порядок набора (он же порядок чекбоксов
   в разметке), а не порядок валют в данных: пользователь видит свой выбор как выбрал. */
describe('fmtSalMulti — вилка в нескольких валютах показа', () => {
  it('один рубль — ровно как fmtSal, без лишнего разделителя', () => {
    assert.equal(ws(fmtSalMulti(vac({ sal_from: 150000 }), ['RUB'])), 'от 150 000 ₽/мес');
  });
  it('рубль + доллар: две части через « · », порядок набора сохранён', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(ws(fmtSalMulti(vac({ sal_from: 180000 }), ['RUB', 'USD'])),
      'от 180 000 ₽/мес · от 2 000 $/мес');
    delete globalThis.FX_RATES;
  });
  it('порядок набора важен и обратный тоже работает', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(ws(fmtSalMulti(vac({ sal_from: 180000 }), ['USD', 'RUB'])),
      'от 2 000 $/мес · от 180 000 ₽/мес');
    delete globalThis.FX_RATES;
  });
  it('вакансия без вилки — пустая строка, а не « · » из разделителей', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(fmtSalMulti(vac(), ['RUB', 'USD']), '');
    delete globalThis.FX_RATES;
  });
  it('в наборе валюта без курса — она печатается в оригинале, остальные сконвертированы', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };           // курса EUR нет
    assert.equal(ws(fmtSalMulti(vac({ sal_from: 180000 }), ['RUB', 'EUR'])),
      'от 180 000 ₽/мес · от 180 000 ₽/мес');
    delete globalThis.FX_RATES;
  });
});

describe('convert / resolveCur — валюты', () => {
  it('resolveCur — алиасы RUR/USDT/BYR + upper', () => {
    assert.equal(resolveCur('RUR'), 'RUB');
    assert.equal(resolveCur('USDT'), 'USD');
    assert.equal(resolveCur('byr'), 'BYN');
  });
  it('convert через базу USD', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90, EUR: 0.9 };
    assert.equal(Math.round(convert(90, 'RUB', 'USD')), 1);       // 90₽ -> $1
    assert.equal(Math.round(convert(1, 'USD', 'RUB')), 90);       // $1 -> 90₽
    assert.equal(convert(100, 'RUR', 'RUB'), 100);               // алиас -> без изменения
    delete globalThis.FX_RATES;
  });
  it('нет курса валюты → без конверсии', () => {
    assert.equal(convert(100, 'ZZZ', 'USD'), 100);
    assert.equal(convert(null, 'USD', 'RUB'), null);
  });
});

describe('filterVacancies — зарплата в выбранной валюте', () => {
  it('фильтр minSal сравнивает в первой валюте показа', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    const data = [
      vac({ id: 'ru', sal_mid: 180000, currency: 'RUR' }),   // = $2000
      vac({ id: 'us', sal_mid: 1000, currency: 'USD' }),     // = $1000
    ];
    const ids = filterVacancies(data,
      flt({ displayCurs: ['USD'], minSal: 1500, maxSal: 100000, salMax: 100000 })).map(v => v.id);
    assert.deepEqual(ids, ['ru']);                            // только >$1500
    delete globalThis.FX_RATES;
  });
});

describe('fmtK — короткий формат тысяч', () => {
  it('≥1000 → «Nк» с усечением', () => {
    assert.equal(fmtK(150000), '150к');
    assert.equal(fmtK(1500), '1к');   /* 1.5 -> усечение к 1 */
  });
  it('<1000 → как есть', () => {
    assert.equal(fmtK(500), '500');
    assert.equal(fmtK(0), '0');
  });
});

/* hashId выбирает цвет чипа/аватара карточки, поэтому «стабилен» здесь — про стабильность
   МЕЖДУ прогонами и версиями: смена алгоритма перекрашивает всю ленту разом. Внутрипрогонное
   `hashId('abc') === hashId('abc')` этого не ловило — чистая функция сравнивалась сама
   с собой и упасть не могла (аудит 09.08.2026). Значения ниже посчитаны ПО СПЕКЕ
   (rolling hash по code units с множителем 31, усечение до int32 через |0, затем abs),
   а не сняты с вывода функции:
     'abc'    -> ((97*31+98)*31+99)                    = 96354
     'xyz'    -> ((120*31+121)*31+122)                 = 119193
     'Python' -> переполняет int32 на последнем шаге:  2405637372 |0 = -1889329924 -> abs */
describe('hashId — детерминированный хеш', () => {
  it('«abc» -> 96354', () => {
    assert.equal(hashId('abc'), 96354);
  });
  it('«xyz» -> 119193', () => {
    assert.equal(hashId('xyz'), 119193);
  });
  it('переполнение int32 отдаёт модуль, а не отрицательное число', () => {
    assert.equal(hashId('Python'), 1889329924);
  });
  it('пустая строка -> 0', () => {
    assert.equal(hashId(''), 0);
  });
  it('разные входы → разные хеши', () => {
    assert.equal(hashId('Java'), 2301506);
  });
});

describe('tagClr — цвета технологий', () => {
  it('известная технология → её цвета', () => {
    assert.deepEqual(tagClr('Python'), ['#3572A5', '#fff']);
  });
  it('неизвестная → дефолтная палитра', () => {
    assert.deepEqual(tagClr('COBOL'), ['#3a3d4a', '#bbb']);
  });
});

describe('tagInk — цвет технологии, читаемый как текст на тёмной карточке', () => {
  /* Палитра языков GitHub — заливочная: Ruby #701516 и PHP #4F5D95 как ТЕКСТ на #1a1d27
     нечитаемы. Светлота поднимается до контраста 4.5:1, тон сохраняется — язык узнаётся. */
  it('тёмный тон осветляется, оттенок остаётся', () => {
    assert.equal(tagInk('#701516'), '#e05c5e');     /* Ruby: тёмно-бордовый -> читаемый красный */
    assert.equal(tagInk('#4F5D95'), '#7582b6');     /* PHP */
    assert.equal(tagInk('#3572A5'), '#498cc4');     /* Python */
  });
  it('уже светлый тон не трогаем', () => {
    assert.equal(tagInk('#f1e05a'), '#f1e05a');     /* JavaScript */
    assert.equal(tagInk('#00ADD8'), '#00add8');     /* Go */
  });
});

describe('countActiveFilters — счётчик на свёрнутой панели', () => {
  const flt2 = over => flt({ ...over });
  it('состояние по умолчанию → 0', () => {
    assert.equal(countActiveFilters(flt2()), 0);
  });
  it('считаются группы, а не отдельные пилюли', () => {
    assert.equal(countActiveFilters(flt2({ langs: new Set(['Python', 'Go', 'Rust']) })), 1);
    assert.equal(countActiveFilters(flt2({ langs: new Set(['Python']), city: 'Москва' })), 2);
  });
  it('сортировка, валюта и сортировка по совпадению фильтрами не считаются', () => {
    assert.equal(countActiveFilters(flt2({ sort: 'desc', matchSort: true, displayCurs: ['USD'] })), 0);
  });
  it('суженная вилка зарплаты — активный фильтр', () => {
    assert.equal(countActiveFilters(flt2({ minSal: 50_000 })), 1);
    assert.equal(countActiveFilters(flt2({ maxSal: 100_000, salMax: 1_000_000 })), 1);
  });
  /* Незаполненный ползунок (`maxSal` ещё null) — не суженная вилка. Правило «зарплатный фильтр
     включён» одно на счётчик и на фильтрацию; именно на этом входе две прежние копии
     расходились — выдача резалась бы, а бейдж фильтров молчал (аудит 2026-09-23). */
  it('незаполненный ползунок фильтром не считается', () => {
    assert.equal(countActiveFilters(flt2({ maxSal: null })), 0);
  });
});

describe('matchColor / matchInk — светофор, а не термометр', () => {
  /* До 27.07 шкала была температурной и читалась наоборот: 100% выходил тревожно-красным,
     а слабые 25% — приятно-зелёными. Рядом с зелёным «✓ Отклик» и красным «✕ Отказ» это
     вводило в заблуждение. Первый вариант разворота вышел блёклым (узкий диапазон
     210->145 при низкой насыщенности) — значение переставало читаться по цвету, поэтому
     размах и сочность вернули, а контраст держат чернила. */
  it('0% красный, середина янтарная, 100% зелёный', () => {
    assert.equal(matchColor(0), 'hsl(0, 74%, 44%)');
    assert.equal(matchColor(50), 'hsl(73, 74%, 44%)');
    assert.equal(matchColor(100), 'hsl(145, 74%, 44%)');
  });
  it('выход за границы шкалы не ломает цвет', () => {
    assert.equal(matchColor(-10), 'hsl(0, 74%, 44%)');
    assert.equal(matchColor(140), 'hsl(145, 74%, 44%)');
  });
  /* Ровно эта подмена и была скрытой бедой яркой шкалы: белым по жёлто-зелёному выходило
     2.17:1. Чернила выбираются по контрасту, худшая точка всей шкалы — 4.51:1. */
  it('чернила по контрасту: на красном белые, на янтаре и зелёном тёмные', () => {
    assert.equal(matchInk(0), '#fff');
    assert.equal(matchInk(14), '#fff');
    assert.equal(matchInk(25), '#07080b');
    assert.equal(matchInk(100), '#07080b');
  });
});

describe('filterVacancies — фильтрация + сортировка (чистая)', () => {
  const data = [
    vac({ id: 'a', name: 'Python backend', employer: 'Альфа', techs: ['Python'], sal_mid: 200000, city: 'Москва', exp: '1–3 года', exp_id: 'between1And3' }),
    vac({ id: 'b', name: 'Java dev', employer: 'Сбер', techs: ['Java'], sal_mid: 300000, city: 'Казань', exp: '3–6 лет', exp_id: 'between3And6', remote_any: false, schedule: 'fullDay' }),
    vac({ id: 'c', name: 'Go SRE', employer: 'Яндекс', techs: ['Go'], sal_mid: null, city: 'Москва', exp: '6+ лет', exp_id: 'moreThan6' }),
  ];
  const ids = arr => arr.map(v => v.id);

  it('поиск по названию/компании (все токены)', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ search: ['альфа'] }))), ['a']);
    assert.deepEqual(ids(filterVacancies(data, flt({ search: ['python', 'backend'] }))), ['a']);
    assert.deepEqual(ids(filterVacancies(data, flt({ search: ['нет-такого'] }))), []);
  });
  it('фильтр по языкам', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ langs: new Set(['Java']) }))), ['b']);
  });
  /* Чип опыта несёт КОД грейда, а не подпись: подписи жили в трёх местах (config.EXP_LABELS,
     шаблон, resume.js) и правка любой из них молча ломала отбор — аудит 08.08.2026. */
  it('фильтр по опыту — по коду грейда', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ exps: new Set(['moreThan6']) }))), ['c']);
  });
  it('подпись грейда чипом уже не ловится', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ exps: new Set(['6+ лет']) }))), []);
  });
  it('resumeOnly оставляет только подходящие', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ resumeOnly: true }))), ['a']);
  });
  it('фильтр по городу', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ city: 'Казань' }))), ['b']);
  });
  it('schedule=remote отбрасывает офис', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ schedule: 'remote' }))).sort(), ['a', 'c']);
  });
  it('зарплатный диапазон: вакансии без вилки выпадают при minSal>0', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ minSal: 250000 }))), ['b']);
  });
  it('сортировка по зарплате asc — nulls в конце', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ sort: 'asc' }))), ['a', 'b', 'c']);
  });
  it('сортировка desc — nulls всё равно в конце', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ sort: 'desc' }))), ['b', 'a', 'c']);
  });
  it('matchSort — по % совпадения, независимо от «под меня»', () => {
    /* После введения оси языка (14.08.2026) чужой язык умножает балл на 0.1:
         a: Python, 1–3, remote  -> (30 + 25 + 15) × 1.0 × 0.5 = 35
         b: Java,   3–6, офис    -> ( 0 + 15 +  0) × 0.1 × 0.5 = 1
         c: Go,     6+,  remote  -> ( 0 +  6 + 15) × 0.1 × 0.5 = 1
       (×0.5 — роль не задана в фикстуре, нейтральный множитель.)
       b и c СРАВНЯЛИСЬ на единице, и порядок между ними держит стабильность сортировки,
       то есть исходный. Проверяемое утверждение — «подходящая вакансия наверху», а не
       порядок внутри пары нулей. */
    assert.deepEqual(ids(filterVacancies(data, flt({ matchSort: true }))), ['a', 'b', 'c']);
  });
  it('matchSort + зарплата desc — совпадение основной, зарплата вторичный', () => {
    /* три вакансии с равным % (одинаковый стек/опыт/удалёнка), различие в зарплате */
    const eq = [
      vac({ id: 'lo', techs: ['Python'], exp: '1–3 года', remote_any: true, sal_mid: 100000 }),
      vac({ id: 'hi', techs: ['Python'], exp: '1–3 года', remote_any: true, sal_mid: 300000 }),
      vac({ id: 'mid', techs: ['Python'], exp: '1–3 года', remote_any: true, sal_mid: 200000 }),
    ];
    /* % одинаков → вторичный ключ зарплата desc: hi, mid, lo */
    assert.deepEqual(
      filterVacancies(eq, flt({ matchSort: true, sort: 'desc' })).map(v => v.id),
      ['hi', 'mid', 'lo'],
    );
  });
});

/* Решение 08.08.2026: «удалёнка» = remote + гибрид (domain/schedule.py::is_remote_like),
   один ответ на ленту, отчёты и графики. До него кнопка «Офис» пропускала flexible, а
   analyzer.py::_REMOTE считал его удалёнкой — 16 857 вакансий в двух бакетах сразу. */
describe('filterVacancies — формат работы: гибрид считается удалёнкой', () => {
  const data = [
    vac({ id: 'rem', schedule: 'remote' }),
    vac({ id: 'hyb', schedule: 'flexible' }),
    vac({ id: 'off', schedule: 'fullDay' }),
  ];
  const ids = arr => arr.map(v => v.id);

  it('«Удалённо» показывает и remote, и гибрид', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ schedule: 'remote' }))), ['rem', 'hyb']);
  });
  it('«Офис» гибрид НЕ показывает', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ schedule: 'office' }))), ['off']);
  });
  it('«Все» показывает все три формата', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ schedule: 'all' }))), ['rem', 'hyb', 'off']);
  });
  it('пустой формат (призрак из журнала) — не удалёнка', () => {
    assert.equal(isRemoteLike(''), false);
  });
  it('remote и flexible — удалёнка, fullDay — нет', () => {
    assert.equal(isRemoteLike('remote'), true);
    assert.equal(isRemoteLike('flexible'), true);
    assert.equal(isRemoteLike('fullDay'), false);
  });
});

/* Фолбэк подписей формата обязан ДОСЛОВНО совпадать с Schedule.label (Python — источник).
   Совпадение с Python пришпилено стражем tests/backend/presentation/test_feed_bridge.py;
   здесь фиксируем сами литералы и то, что мёртвых кодов больше нет. */
describe('SCHED_LABELS — подписи формата (офлайн-фолбэк)', () => {
  it('три кода домена с подписями из Schedule.label', () => {
    assert.deepEqual(SCHED_LABELS, {
      remote: 'Удалённо', flexible: 'Гибрид', fullDay: 'Офис',
    });
  });
});

/* Регрессия 08.08.2026: офлайн-фолбэк STATE_LABELS разъехался с chat.py по двум ключам
   («Звонок» вместо «Телефон-интервью», «Закрыта» вместо «Вакансия закрыта»), и JS-тесты
   закрепляли ФОЛБЭК, а не источник. Подпись видна пользователю на бейдже карточки. */
describe('statusInfo — подписи статусов из фолбэка совпадают с Python', () => {
  it('PHONE_INTERVIEW -> «Телефон-интервью»', () => {
    assert.equal(statusInfo(vac({ status: 'PHONE_INTERVIEW' })).label, 'Телефон-интервью');
  });
  it('DISCARD_VACANCY_CLOSED -> «Вакансия закрыта»', () => {
    assert.equal(statusInfo(vac({ status: 'DISCARD_VACANCY_CLOSED' })).label, 'Вакансия закрыта');
  });
  it('DISCARD_BY_APPLICANT -> «Вы отказались»', () => {
    assert.equal(statusInfo(vac({ status: 'DISCARD_BY_APPLICANT' })).label, 'Вы отказались');
  });
});

describe('filterVacancies — роль + гейт не-IT', () => {
  const data = [
    vac({ id: 'be', role: 'Backend' }),
    vac({ id: 'qa', role: 'QA' }),
    vac({ id: 'no', role: 'Не-IT' }),
  ];
  const ids = arr => arr.map(v => v.id).sort();

  it('по умолчанию не-IT скрыт', () => {
    assert.deepEqual(ids(filterVacancies(data, flt())), ['be', 'qa']);
  });
  it('showNonIt=true показывает не-IT', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ showNonIt: true }))), ['be', 'no', 'qa']);
  });
  it('фильтр по роли оставляет только выбранную', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ roles: new Set(['QA']) }))), ['qa']);
  });
  it('роль «Не-IT» в чипах + showNonIt — показывает только не-IT', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ roles: new Set(['Не-IT']), showNonIt: true }))), ['no']);
  });
});

describe('appliedInRange — календарный диапазон откликов', () => {
  it('без границ — любой отклик проходит', () => {
    assert.equal(appliedInRange(vac({ applied: { ts: '2026-07-05T10:00:00' } }), '', ''), true);
  });
  it('нет отклика (нет .applied) — false', () => {
    assert.equal(appliedInRange(vac(), '', ''), false);
  });
  it('уважает нижнюю и верхнюю границу (сравнение ISO-дат)', () => {
    const v = vac({ applied: { ts: '2026-07-05T10:00:00' } });
    assert.equal(appliedInRange(v, '2026-07-06', ''), false);          // раньше from
    assert.equal(appliedInRange(v, '', '2026-07-04'), false);          // позже to
    assert.equal(appliedInRange(v, '2026-07-05', '2026-07-05'), true); // ровно в границах
  });
});

describe('filterVacancies — режим «Мои отклики»', () => {
  const a = vac({ id: 'a', applied: { ts: '2026-07-05T10:00:00' } });
  const b = vac({ id: 'b', applied: { ts: '2026-07-01T10:00:00' } });
  const c = vac({ id: 'c' });                                          // без отклика
  const syn = vac({ id: 'd', _synthetic: true, applied: { ts: '2026-07-06T10:00:00' } });

  it('только отклики в диапазоне, сортировка по дате ↓ (включая синтетические)', () => {
    const out = filterVacancies([a, b, c, syn],
      flt({ status: 'mine', dateFrom: '2026-07-04', dateTo: '2026-07-07' }));
    assert.deepEqual(out.map(v => v.id), ['d', 'a']);   // b вне диапазона, c без отклика; d(06)>a(05)
  });
  it('синтетические карточки скрыты в обычном режиме', () => {
    const out = filterVacancies([a, syn], flt({ status: 'all' }));
    assert.deepEqual(out.map(v => v.id), ['a']);        // syn._synthetic отфильтрован
  });
  /* Баг 16.09.2026: под «Показать (N)» фильтр зарплат и сортировка не действовали (режим был
     самостоятельным с ранним return). Теперь «Мои отклики» — лупа, КОМПОНУЕТСЯ с фильтрами. */
  it('фильтр зарплат компонуется с «Мои отклики»', () => {
    const hi = vac({ id: 'hi', sal_mid: 200000, applied: { ts: '2026-07-05T10:00:00' } });
    const lo = vac({ id: 'lo', sal_mid: 90000, applied: { ts: '2026-07-06T10:00:00' } });
    const out = filterVacancies([hi, lo], flt({ status: 'mine', minSal: 150000 }));
    assert.deepEqual(out.map(v => v.id), ['hi']);       // lo (90к) отсеян порогом 150к
  });
  it('сортировка «свежие» (date_new) компонуется с «Мои отклики»', () => {
    const oldv = vac({ id: 'o', age: 90, applied: { ts: '2026-07-06T10:00:00' } });
    const newv = vac({ id: 'n', age: 2, applied: { ts: '2026-07-01T10:00:00' } });
    // по дате отклика было бы [o, n]; date_new сортирует по свежести вакансии -> [n, o]
    const out = filterVacancies([oldv, newv], flt({ status: 'mine', sort: 'date_new' }));
    assert.deepEqual(out.map(v => v.id), ['n', 'o']);
  });
  it('без явной сортировки «Мои отклики» — по дате отклика ↓', () => {
    const out = filterVacancies([b, a], flt({ status: 'mine' }));   // a=05, b=01
    assert.deepEqual(out.map(v => v.id), ['a', 'b']);
  });
});

describe('filterVacancies — сортировка по дате появления вакансии', () => {
  const data = [
    vac({ id: 'old', age: 90 }),
    vac({ id: 'new', age: 2 }),
    vac({ id: 'mid', age: 30 }),
    vac({ id: 'nodate', age: null }),
  ];
  const ids = s => filterVacancies(data, flt({ sort: s })).map(v => v.id);
  it('date_new — свежие (малый age) первыми, без даты в конце', () => {
    assert.deepEqual(ids('date_new'), ['new', 'mid', 'old', 'nodate']);
  });
  it('date_old — старые первыми, без даты в конце', () => {
    assert.deepEqual(ids('date_old'), ['old', 'mid', 'new', 'nodate']);
  });
  /* age: 0 — «вакансия сегодня». Общая фабрика компараторов считает пустым только
     null/undefined/'', поэтому ноль остаётся валидным ключом: с проверкой на `!age`
     сегодняшняя уезжала бы в конец к «без даты», то есть «Свежие» начинались бы
     не со свежей вакансии. */
  it('age 0 — «сегодня», а не «без даты»: у свежих первым, в хвост уходит только nodate', () => {
    const withToday = [...data, vac({ id: 'today', age: 0 })];
    assert.deepEqual(
      filterVacancies(withToday, flt({ sort: 'date_new' })).map(v => v.id),
      ['today', 'new', 'mid', 'old', 'nodate']);
    assert.deepEqual(
      filterVacancies(withToday, flt({ sort: 'date_old' })).map(v => v.id),
      ['old', 'mid', 'new', 'today', 'nodate']);
  });
});

describe('filterVacancies — сортировка по ответу HR', () => {
  /* Ответ HR по СТАРОЙ вакансии тонул в ленте: «Свежие» сортируют по дате публикации,
     а не переписки. Ключ самостоятельный, дата вакансии на него не влияет. */
  const data = [
    vac({ id: 'stale-vac-fresh-reply', age: 120, hr_ts: '2026-07-28T18:40:00+03:00' }),
    vac({ id: 'fresh-vac-old-reply', age: 1, hr_ts: '2026-07-20T09:00:00+03:00' }),
    vac({ id: 'mid-reply', age: 30, hr_ts: '2026-07-25T12:00:00+03:00' }),
    vac({ id: 'no-reply', age: 2, hr_ts: '' }),
  ];
  const ids = filterVacancies(data, flt({ sort: 'reply_new' })).map(v => v.id);

  it('свежий ответ первым, даже если вакансия старая', () => {
    assert.deepEqual(ids, ['stale-vac-fresh-reply', 'mid-reply', 'fresh-vac-old-reply', 'no-reply']);
  });
});

describe('filterVacancies — совпадение отсеивает гост-вакансии (>60 дней)', () => {
  const data = [vac({ id: 'fresh', fresh: 'fresh' }), vac({ id: 'ghost', fresh: 'ghost' })];
  it('matchSort — гост отброшен', () => {
    assert.deepEqual(filterVacancies(data, flt({ matchSort: true })).map(v => v.id), ['fresh']);
  });
  it('resumeOnly — гост отброшен', () => {
    // Полный список, а не `!includes('ghost')`: отрицание проходило и на пустой выдаче,
    // то есть на регрессии «resumeOnly выкосил вообще всё» (аудит 09.08.2026). У обеих
    // вакансий дефолтный стек Python и remote_any — по резюме подходят обе.
    assert.deepEqual(filterVacancies(data, flt({ resumeOnly: true })).map(v => v.id), ['fresh']);
  });
  it('без совпадения — гост остаётся', () => {
    assert.deepEqual(filterVacancies(data, flt({})).map(v => v.id).sort(), ['fresh', 'ghost']);
  });
});

describe('filterVacancies — фильтр по порталу (source)', () => {
  const data = [vac({ id: 'h', source: 'hh' }), vac({ id: 'x', source: 'hirify' })];
  const ids = s => filterVacancies(data, flt({ source: s })).map(v => v.id);
  it('all — оба портала', () => assert.deepEqual(ids('all').sort(), ['h', 'x']));
  it('hh — только hh', () => assert.deepEqual(ids('hh'), ['h']));
  it('hirify — только hirify', () => assert.deepEqual(ids('hirify'), ['x']));
});

describe('chatAgeLabel — давность последнего сообщения работодателя', () => {
  const now = Date.parse('2026-07-23T12:00:00+03:00');
  it('сегодня / вчера / N дн', () => {
    assert.equal(chatAgeLabel('2026-07-23T09:00:00+03:00', now), 'сегодня');
    assert.equal(chatAgeLabel('2026-07-22T09:00:00+03:00', now), 'вчера');
    assert.equal(chatAgeLabel('2026-07-14T09:12:03+03:00', now), '9 дн');
  });
  it('пустая/битая/будущая метка -> пусто, не NaN', () => {
    assert.equal(chatAgeLabel('', now), '');
    assert.equal(chatAgeLabel('garbage', now), '');
    assert.equal(chatAgeLabel('2026-07-25T09:00:00+03:00', now), '');
  });
});

/* Регрессия 01.08.2026: чат по вакансии 135759424 («QA-инженер (тестировщик)»,
   Константинов Семен Павлович) не находился НИ ПО ОДНОМУ фильтру ленты. Вакансия выпала
   из сборки feed-data.js, «призрак» из журнала создавался с employer: '', а поиск идёт по
   `name + employer` -> запрос по компании не матчился никогда. */
describe('journalById — свёртка журнала откликов', () => {
  it('на вакансию берётся САМЫЙ РАННИЙ отклик', () => {
    const byId = journalById([
      { id: '7', ts: '2026-07-31T13:13:44+04:00', via: 'cron', status: 'applied' },
      { id: '7', ts: '2026-07-20T10:00:00+04:00', via: 'manual', status: 'applied' },
    ]);
    assert.equal(byId['7'].ts, '2026-07-20T10:00:00+04:00');
    assert.equal(byId['7'].via, 'manual');
  });

  it('переносит employer из журнала', () => {
    const byId = journalById([{
      id: '135759424', ts: '2026-07-31T13:13:44+04:00', via: 'cron', status: 'applied',
      name: 'QA-инженер (тестировщик)', url: 'https://hh.ru/vacancy/135759424',
      employer: 'Константинов Семен Павлович',
    }]);
    assert.equal(byId['135759424'].employer, 'Константинов Семен Павлович');
  });

  it('записи без id отбрасываются', () => {
    assert.deepEqual(journalById([{ ts: '2026-07-31T13:13:44+04:00' }, null]), {});
  });

  /* Регрессия 08.08.2026: дубли схлопывались сравнением ISO-СТРОК, а в журнале сосуществуют
     смещения +04:00 (append_applied) и +03:00 (метки HH). «09:30+03:00» = 06:30Z позже, чем
     «10:00+04:00» = 06:00Z, но строкой меньше — «первым откликом» выбирался поздний. */
  it('разные смещения: побеждает реально РАННИЙ момент, а не меньшая строка', () => {
    const byId = journalById([
      { id: '7', ts: '2026-07-20T09:30:00+03:00', via: 'hh' },      // 06:30Z
      { id: '7', ts: '2026-07-20T10:00:00+04:00', via: 'cron' },    // 06:00Z — раньше
    ]);
    assert.equal(byId['7'].ts, '2026-07-20T10:00:00+04:00');
    assert.equal(byId['7'].via, 'cron');
  });

  it('порядок записей не влияет на результат', () => {
    const byId = journalById([
      { id: '7', ts: '2026-07-20T10:00:00+04:00', via: 'cron' },
      { id: '7', ts: '2026-07-20T09:30:00+03:00', via: 'hh' },
    ]);
    assert.equal(byId['7'].via, 'cron');
  });

  it('битая метка не вытесняет разобранную', () => {
    const byId = journalById([
      { id: '7', ts: '2026-07-20T10:00:00+04:00', via: 'cron' },
      { id: '7', ts: 'не-дата', via: 'мусор' },
    ]);
    assert.equal(byId['7'].via, 'cron');
  });
});

describe('syntheticCard — карточка-призрак из журнала', () => {
  const a = {
    ts: '2026-07-31T13:13:44+04:00', via: 'cron', status: 'applied',
    name: 'QA-инженер (тестировщик)', url: 'https://hh.ru/vacancy/135759424',
    employer: 'Константинов Семен Павлович',
  };

  it('сохраняет работодателя из журнала', () => {
    assert.equal(syntheticCard('135759424', a).employer, 'Константинов Семен Павлович');
  });

  /* chat вешает оверлей (main.js::initOverlay) уже поверх карточки — воспроизводим тот же
     порядок: сначала призрак из журнала, затем свёртка переписки. */
  const ghostWithChat = () => [{ ...syntheticCard('135759424', a),
                                 chat: { needs_reply: true, sender: 'human' } }];

  it('поиск по компании находит призрака', () => {
    const found = filterVacancies(ghostWithChat(),
                                  flt({ search: ['константинов'], chatFilter: 'wait' }));
    assert.deepEqual(found.map(v => v.id), ['135759424']);
  });

  it('нет работодателя в журнале -> пустая строка, не undefined', () => {
    assert.equal(syntheticCard('42', { ts: '2026-07-31T13:13:44+04:00' }).employer, '');
  });

  it('нет имени в журнале -> подпись по id', () => {
    assert.equal(syntheticCard('42', {}).name, 'Вакансия 42');
    assert.equal(syntheticCard('42', {}).url, 'https://hh.ru/vacancy/42');
  });

  it('скрыт в общем списке, показан под чат-фильтром', () => {
    assert.deepEqual(filterVacancies(ghostWithChat(), flt({})).map(v => v.id), []);
    assert.deepEqual(filterVacancies(ghostWithChat(), flt({ chatFilter: 'wait' })).map(v => v.id),
                     ['135759424']);
  });
});

/* Регрессия 01.08.2026: подпись портала считалась тернарником на ДВА портала
   (`v.source === 'hirify' ? 'hirify.me' : 'hh.ru'`), поэтому talanto и getmatch
   подписывались в модалке как «hh.ru». */
describe('portalSite — подпись портала в карточке', () => {
  it('каждый портал получает свой домен', () => {
    assert.equal(portalSite('hh'), 'hh.ru');
    assert.equal(portalSite('hirify'), 'hirify.me');
    assert.equal(portalSite('talanto'), 'talanto.work');
    assert.equal(portalSite('getmatch'), 'getmatch.ru');
  });

  it('пустой источник -> hh (историческое умолчание карточек без поля)', () => {
    assert.equal(portalSite(''), 'hh.ru');
    assert.equal(portalSite(undefined), 'hh.ru');
  });

  it('неизвестный портал отдаёт своё имя, а не чужую подпись', () => {
    assert.equal(portalSite('новый_портал'), 'новый_портал');
  });
});


/* ИНЦИДЕНТ 07.08.2026 (латентный, найден аудитом): отказ определялся как
   `s.startsWith('DISCARD')`, а Python исключает `DISCARD_BY_APPLICANT` из DISCARD_STATES
   намеренно — это НАШ отказ, а не работодателя. Карточка красилась красным «Отказ», хотя
   подпись рядом говорила «Вы отказались», а воронка ту же вакансию клала в «без исхода».
   Наборы теперь инжектятся из Python; здесь проверяется фолбэк. */
describe('isDiscard / isInvited — наборы состояний едины с Python', () => {
  for (const s of ['DISCARD', 'DISCARD_BY_EMPLOYER', 'DISCARD_VACANCY_CLOSED']) {
    it(`${s} — отказ работодателя`, () => {
      assert.equal(isDiscard(s), true);
    });
  }

  it('DISCARD_BY_APPLICANT — НЕ отказ работодателя, это наш собственный', () => {
    assert.equal(isDiscard('DISCARD_BY_APPLICANT'), false);
  });

  it('префикс DISCARD сам по себе больше ничего не решает', () => {
    assert.equal(isDiscard('DISCARD_SOMETHING_NEW'), false);
  });

  for (const s of ['INVITATION', 'PHONE_INTERVIEW', 'INTERVIEW', 'ASSESSMENT', 'HIRED', 'CONSIDER']) {
    it(`${s} — приглашение`, () => {
      assert.equal(isInvited(s), true);
    });
  }

  it('RESPONSE — ни отказ, ни приглашение', () => {
    assert.equal(isDiscard('RESPONSE'), false);
    assert.equal(isInvited('RESPONSE'), false);
  });

  it('мусор на входе не роняет', () => {
    assert.equal(isDiscard(null), false);
    assert.equal(isInvited(undefined), false);
  });
});

/* Инцидент 08.08.2026: вилка без названной валюты сравнивалась как рубли.
   В срезе 109 596 вакансий таких 169 — talanto (27) и web3 (142), суммы вида 1300 и 10000,
   очевидно долларовые. $10 000/мес весили 10 000 ₽: уезжали в конец сортировки и прятались
   фильтром «зарплата от», то есть терялись самые дорогие удалённые вакансии выдачи.
   Контракт тот же, что у `net/rates.py::to_rub`: нет единицы измерения -> нет числа. */
describe('comparableSalary — сравнимая вилка или null', () => {
  it('валюта записи совпадает с целевой -> число как есть, курсы не нужны', () => {
    assert.equal(comparableSalary(vac({ sal_mid: 250000, currency: 'RUR' }), 'RUB'), 250000);
  });

  it('названная валюта с курсом -> конвертируется', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(comparableSalary(vac({ sal_mid: 10000, currency: 'USD' }), 'RUB'), 900000);
    delete globalThis.FX_RATES;
  });

  it('валюта не названа -> null, а НЕ исходное число', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(comparableSalary(vac({ sal_mid: 10000, currency: '' }), 'RUB'), null);
    delete globalThis.FX_RATES;
  });

  it('валюта названа, но курса нет -> null', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(comparableSalary(vac({ sal_mid: 35000000, currency: 'UZS' }), 'RUB'), null);
    delete globalThis.FX_RATES;
  });

  it('зарплаты нет вовсе -> null', () => {
    assert.equal(comparableSalary(vac({ sal_mid: null, currency: 'USD' }), 'RUB'), null);
  });

  it('фильтр «зарплата до» не выбрасывает вилку без валюты как копеечную', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    const noCur = vac({ id: 'w3', sal_mid: 10000, currency: '' });
    const cheap = vac({ id: 'ru', sal_mid: 10000, currency: 'RUR' });
    const f = flt({ minSal: 0, maxSal: 5000, salMax: 2090000, displayCurs: ['RUB'] });
    const ids = filterVacancies([noCur, cheap], f).map(v => v.id);
    assert.deepEqual(ids, ['w3']);   // рублёвые 10 000 не проходят потолок 5 000, безвалютная — не сравнивается
    delete globalThis.FX_RATES;
  });
});

/* Инцидент 10.08.2026: фильтр Python + arbeitnow + опыт давал НОЛЬ вакансий даже при всех
   нажатых кнопках, а без фильтра по опыту — 538. Пустой `exp_id` не совпадал ни с одним из
   четырёх доменных кодов, поэтому «выбрано всё» переставало быть равносильно «фильтр снят».
   Задето 27 880 карточек: web3 и arbeitnow поголовно, talanto — 22 625 из 59 672.
   Лечение — ПЯТЫЙ чип с пустым кодом (`feed.py::EXP_UNKNOWN`), а не подстановка
   `noExperience`: у talanto среди безгрейдовых полно senior-вакансий. */
describe('filterVacancies — опыт: карточка без грейда', () => {
  const noGrade = vac({ id: 'w3', exp_id: '', exp: '' });
  const junior  = vac({ id: 'jr', exp_id: 'between1And3' });

  it('все пять чипов выбраны -> видно и безгрейдовую, и обычную', () => {
    const f = flt({ exps: new Set(['noExperience', 'between1And3', 'between3And6', 'moreThan6', '']) });
    assert.deepEqual(filterVacancies([noGrade, junior], f).map(v => v.id), ['w3', 'jr']);
  });

  it('выбраны только доменные коды -> безгрейдовая скрыта', () => {
    const f = flt({ exps: new Set(['noExperience', 'between1And3', 'between3And6', 'moreThan6']) });
    assert.deepEqual(filterVacancies([noGrade, junior], f).map(v => v.id), ['jr']);
  });

  it('выбран только чип «не указан» -> видно ТОЛЬКО безгрейдовую', () => {
    const f = flt({ exps: new Set(['']) });
    assert.deepEqual(filterVacancies([noGrade, junior], f).map(v => v.id), ['w3']);
  });

  it('ни один чип не выбран -> фильтр снят, видно обе', () => {
    assert.deepEqual(filterVacancies([noGrade, junior], flt()).map(v => v.id), ['w3', 'jr']);
  });
});

/* Форма оформления (10.08.2026). Карточка несёт СПИСОК форм, потому что вакансия бывает
   «по ТК РФ или как самозанятый»: она обязана попадать в оба фильтра сразу. Порталы
   структурного поля не отдают, форма читается из текста описания — поэтому у большинства
   карточек список ПУСТ, и его ловит чип с пустым кодом (`feed.py::EMP_UNKNOWN`). */
describe('filterVacancies — форма оформления', () => {
  const tk    = vac({ id: 'tk',   emp_ids: ['labor_code'] });
  const both  = vac({ id: 'both', emp_ids: ['labor_code', 'self_employed'] });
  const gph   = vac({ id: 'gph',  emp_ids: ['civil_contract'] });
  const none  = vac({ id: 'none', emp_ids: [] });
  const all   = [tk, both, gph, none];

  it('ни один чип не выбран -> фильтр снят, видно все', () => {
    assert.deepEqual(filterVacancies(all, flt()).map(v => v.id), ['tk', 'both', 'gph', 'none']);
  });

  it('чип «ТК» -> видно и вакансию с одной формой, и вакансию с двумя', () => {
    const f = flt({ emps: new Set(['labor_code']) });
    assert.deepEqual(filterVacancies(all, f).map(v => v.id), ['tk', 'both']);
  });

  it('чип «Самозанятый» -> видно вакансию, где ТК идёт первой из двух форм', () => {
    const f = flt({ emps: new Set(['self_employed']) });
    assert.deepEqual(filterVacancies(all, f).map(v => v.id), ['both']);
  });

  it('чип «Не указано» -> видно ТОЛЬКО вакансию с пустым списком', () => {
    const f = flt({ emps: new Set(['']) });
    assert.deepEqual(filterVacancies(all, f).map(v => v.id), ['none']);
  });

  it('все пять чипов выбраны -> равносильно снятому фильтру', () => {
    const f = flt({ emps: new Set(['labor_code', 'self_employed', 'sole_trader', 'civil_contract', '']) });
    assert.deepEqual(filterVacancies(all, f).map(v => v.id), ['tk', 'both', 'gph', 'none']);
  });

  it('карточка без поля emp_ids не роняет фильтр и считается «не указано»', () => {
    const legacy = vac({ id: 'old' });
    assert.deepEqual(filterVacancies([legacy], flt({ emps: new Set(['']) })).map(v => v.id), ['old']);
    assert.deepEqual(filterVacancies([legacy], flt({ emps: new Set(['labor_code']) })).map(v => v.id), []);
  });

  it('группа «оформление» считается активным фильтром', () => {
    assert.equal(countActiveFilters(flt({ emps: new Set(['labor_code']) })), 1);
    assert.equal(countActiveFilters(flt()), 0);
  });
});

describe('employmentLabel', () => {
  it('две формы -> подписи через точку', () => {
    assert.equal(employmentLabel({ emp_ids: ['labor_code', 'self_employed'] }),
      'ТК РФ/РБ · Самозанятый');
  });

  it('форма не названа -> пустая строка, а не «ТК» по умолчанию', () => {
    assert.equal(employmentLabel({ emp_ids: [] }), '');
    assert.equal(employmentLabel({}), '');
  });
});

/* Инцидент 10.08.2026. Под фильтром «С контактами» первым стоял «Сетевой инженер» с ответом
   в 09:25, а «Учитель кружка программирования» с ответом в 14:13 — самым свежим за день —
   оказался в середине списка. Сортировка была исправна: у учителя ПУСТОЙ `hr_ts`, потому что
   ключ печётся из чат-данных, скачанных на момент сборки (лента собралась в 14:40, крон чата
   привёз ответ в 17:12). Оверлей serve обновляет `v.chat`, но `hr_ts` не трогает.
   Лечение — `hrReplyTime`: живой чат приоритетнее испечённого ключа. */
describe('сортировка «Ответы HR» — живой чат против испечённого ключа', () => {
  const baked = vac({ id: 'net', name: 'Сетевой инженер', hr_ts: '2026-08-10T09:25:06+03:00' });
  const live = vac({
    id: 'teacher', name: 'Учитель кружка программирования', hr_ts: '',
    chat: { sender: 'human', ts: '2026-08-10T14:13:12+03:00', contact: '@hr' },
  });

  it('свежий ответ из живого чата поднимается выше испечённого', () => {
    const f = flt({ sort: 'reply_new' });
    assert.deepEqual(filterVacancies([baked, live], f).map(v => v.id), ['teacher', 'net']);
  });

  it('призрак с живым чатом больше не падает в конец', () => {
    // под «С контактами» карточка без чата не видна вовсе, поэтому у «сетевого» он тоже есть:
    // последнее сообщение — отказ работодателя, время совпадает с испечённым ключом
    const net = vac({ id: 'net', hr_ts: '2026-08-10T09:25:06+03:00',
      chat: { sender: 'human', ts: '2026-08-10T09:25:06+03:00', contact: '+7 900' } });
    const ghost = syntheticCard('g', {});
    ghost.chat = { sender: 'human', ts: '2026-08-10T18:00:00+03:00', contact: '+7 900' };
    const f = flt({ sort: 'reply_new', chatFilter: 'contact' });
    assert.deepEqual(filterVacancies([net, ghost], f).map(v => v.id), ['g', 'net']);
  });
});

describe('hrReplyTime — какой ответ считается ответом HR', () => {
  it('живой ответ человека побеждает испечённый ключ', () => {
    assert.equal(
      hrReplyTime({ hr_ts: '2026-08-01T10:00:00+03:00',
        chat: { sender: 'human', ts: '2026-08-10T14:13:12+03:00' } }),
      '2026-08-10T14:13:12+03:00');
  });

  it('шаблонное письмо работодателя — тоже ответ (как в feed.py::_last_hr_replies)', () => {
    assert.equal(hrReplyTime({ hr_ts: '', chat: { sender: 'template', ts: '2026-08-10T12:00:00+03:00' } }),
      '2026-08-10T12:00:00+03:00');
  });

  it('автоответ бота вакансию не поднимает', () => {
    assert.equal(hrReplyTime({ hr_ts: '', chat: { sender: 'bot', ts: '2026-08-10T20:00:00+03:00' } }), '');
  });

  it('последнее слово за нами -> берём испечённый ключ, а не время своего письма', () => {
    // analyze() при нашем последнем сообщении оставляет sender пустым
    assert.equal(hrReplyTime({ hr_ts: '2026-08-01T10:00:00+03:00', chat: { sender: '', ts: '2026-08-10T19:00:00+03:00' } }),
      '2026-08-01T10:00:00+03:00');
  });

  it('чата нет вовсе -> испечённый ключ, пусто -> пустая строка', () => {
    assert.equal(hrReplyTime({ hr_ts: '2026-08-01T10:00:00+03:00' }), '2026-08-01T10:00:00+03:00');
    assert.equal(hrReplyTime({}), '');
  });
});

/* 10.08.2026: «Ответы HR» показывает призраков. Включая эту сортировку, человек просит
   «покажи, где ответили» — а вакансии, выпавшие из выдачи, оставались невидимыми до тех
   пор, пока он не добавит ещё и чат-фильтр. Но только тех, где ответ ЕСТЬ: иначе в выдачу
   высыпался бы весь журнал откликов по выпавшим вакансиям. */
describe('«Ответы HR» открывает призраков с ответом', () => {
  const real = vac({ id: 'real', hr_ts: '2026-08-01T10:00:00+03:00' });

  const ghostAnswered = () => {
    const g = syntheticCard('ga', {});
    g.chat = { sender: 'human', ts: '2026-08-10T18:00:00+03:00', contact: '' };
    return g;
  };
  const ghostSilent = () => syntheticCard('gs', {});   /* отклик был, ответа нет */

  it('без чат-фильтра сортировка «Ответы HR» показывает призрака с ответом первым', () => {
    const f = flt({ sort: 'reply_new' });
    assert.deepEqual(
      filterVacancies([real, ghostAnswered()], f).map(v => v.id), ['ga', 'real']);
  });

  it('призрак БЕЗ ответа остаётся скрытым и в этой сортировке', () => {
    const f = flt({ sort: 'reply_new' });
    assert.deepEqual(filterVacancies([real, ghostSilent()], f).map(v => v.id), ['real']);
  });

  it('в остальных сортировках призрак с ответом по-прежнему скрыт', () => {
    for (const sort of ['none', 'date_new', 'desc']) {
      assert.deepEqual(
        filterVacancies([real, ghostAnswered()], flt({ sort })).map(v => v.id), ['real'],
        `сортировка ${sort}`);
    }
  });

  it('чат-фильтр по-прежнему показывает призраков независимо от сортировки', () => {
    const g = ghostAnswered();
    g.chat.contact = '+7 900';
    const f = flt({ sort: 'none', chatFilter: 'contact' });
    assert.deepEqual(filterVacancies([g], f).map(v => v.id), ['ga']);
  });
});


/* ── Возраст среза ──────────────────────────────────────────────────────────────
   Сбор отказывает МОЛЧА: санити-гейт отменяет запись кеша целиком, лента назавтра
   пересобирается из замороженного среза и выглядит живой. 16-19.08.2026 так простояло
   трое суток — заметили по остановившимся откликам, а не по ленте.
   Порог сравнивается через `>=`: ровно на границе баннер уже горит, иначе «36 ч» было бы
   недостижимым состоянием между двумя тиками таймера. */
describe('staleAge — возраст среза против порога', () => {
  const HOUR = 3600;
  const now = 1_770_878_400_000;                    /* мс эпохи, фиксированные */
  const agoH = h => now / 1000 - h * HOUR;

  it('свежий срез — молчание', () => {
    assert.equal(staleAge(agoH(5), 36, now), null);
  });

  it('старше порога — возраст в часах и момент сбора', () => {
    const notice = staleAge(agoH(79), 36, now);
    assert.equal(Math.round(notice.hours), 79);
    assert.equal(notice.at.getTime(), agoH(79) * 1000);
  });

  it('ровно на пороге баннер уже горит', () => {
    assert.ok(staleAge(agoH(36), 36, now));
  });

  it('на волосок до порога — ещё нет', () => {
    assert.equal(staleAge(agoH(36) + 1, 36, now), null);
  });

  /* Метки нет — это НЕ «протухло»: так выглядит file:// без feed-data.js и кеш от версии
     без метки. Ноль отдельным случаем: 0 — это 1970, и наивное `if (collectedAt)` там
     сработало бы правильно, а `typeof === 'number'` без проверки знака — нет. */
  for (const [id, value] of [
    ['метки нет', undefined], ['null', null], ['ноль (1970)', 0], ['отрицательное', -1],
    ['строка', '1770878400'], ['NaN', NaN],
  ]) {
    it(`${id} -> молчание, а не вечный баннер`, () => {
      assert.equal(staleAge(value, 36, now), null);
    });
  }
});


describe('effectiveStatus — статус карточки под фильтром профиля (RFC-004)', () => {
  const byAcct = { main: 'DISCARD', acc2: 'RESPONSE' };   /* отказ у основного, отклик у второго */
  it('фильтр acc2 -> статус acc2 (а не отказ основного) — баг 15.09.2026', () => {
    assert.equal(effectiveStatus(byAcct, new Set(['acc2'])), 'RESPONSE');
  });
  it('фильтр основного -> его статус', () => {
    assert.equal(effectiveStatus(byAcct, new Set(['main'])), 'DISCARD');
  });
  it('без фильтра при конфликте -> приоритет приглашение > отказ > прочее', () => {
    assert.equal(effectiveStatus({ main: 'RESPONSE', acc2: 'INVITATION' }, new Set()), 'INVITATION');
    assert.equal(effectiveStatus(byAcct, new Set()), 'DISCARD');
  });
  it('нет статусов у выбранного профиля -> null', () => {
    assert.equal(effectiveStatus({ main: 'DISCARD' }, new Set(['acc2'])), null);
    assert.equal(effectiveStatus(null, new Set()), null);
  });
});

describe('effectiveChat — переписка и её дата под фильтром профиля (RFC-004)', () => {
  const cMain = { ts: 'old', label: 'отказ' };      /* чат основного: старый (19 дней назад) */
  const cAcc2 = { ts: 'new', label: 'отказ' };      /* чат acc2: его собственная дата */
  const byAcct = { main: cMain, acc2: cAcc2 };
  it('фильтр acc2 -> чат (и дата) acc2, а не основного — баг 16.09.2026 «отказ 19 дней назад»', () => {
    assert.equal(effectiveChat(byAcct, new Set(['acc2'])), cAcc2);
  });
  it('фильтр основного -> его чат', () => {
    assert.equal(effectiveChat(byAcct, new Set(['main'])), cMain);
  });
  it('без фильтра при конфликте -> основной (носитель ленты, прежнее main-wins)', () => {
    assert.equal(effectiveChat(byAcct, new Set()), cMain);
  });
  it('без основного среди кандидатов -> первый по коду (детерминированно)', () => {
    assert.equal(effectiveChat({ acc3: cAcc2, acc2: cMain }, new Set()), cMain);   /* acc2 < acc3 */
  });
  it('нет чата у выбранного профиля / нет данных -> null', () => {
    assert.equal(effectiveChat({ main: cMain }, new Set(['acc2'])), null);
    assert.equal(effectiveChat(null, new Set()), null);
  });
});

describe('journalById — набор аккаунтов на вакансию (RFC-004)', () => {
  it('легаси-строка без account относится к основному', () => {
    const j = journalById([{ id: '1', ts: '2026-09-15T10:00:00+04:00' }]);
    assert.deepEqual(j['1'].accounts, ['main']);
  });
  it('две записи от разных аккаунтов на одну вакансию — конфликт (оба в accounts)', () => {
    const j = journalById([
      { id: '1', ts: '2026-09-15T10:00:00+04:00', account: 'main' },
      { id: '1', ts: '2026-09-15T09:00:00+04:00', account: 'acc2' },
    ]);
    assert.deepEqual(j['1'].accounts, ['acc2', 'main']);
  });
  it('byAcct — ранняя запись КАЖДОГО профиля отдельно (RFC-004)', () => {
    const j = journalById([
      { id: '1', ts: '2026-08-27T10:00:00+04:00', via: 'cron', account: 'main' },
      { id: '1', ts: '2026-09-15T10:00:00+04:00', via: 'cron', account: 'acc2' },
    ]);
    assert.equal(j['1'].byAcct.main.ts, '2026-08-27T10:00:00+04:00');
    assert.equal(j['1'].byAcct.acc2.ts, '2026-09-15T10:00:00+04:00');
  });
});

describe('effectiveApplied — дата/via отклика под фильтром профиля (RFC-004)', () => {
  const byAcct = {
    main: { ts: '2026-08-27T10:00:00+04:00', via: 'cron', account: 'main' },   /* старее */
    acc2: { ts: '2026-09-15T10:00:00+04:00', via: 'cron', account: 'acc2' },   /* новее */
  };
  const accounts = ['acc2', 'main'];
  it('фильтр acc2 -> дата acc2, а не самая ранняя main — баг 16.09.2026', () => {
    const e = effectiveApplied(byAcct, new Set(['acc2']), accounts);
    assert.equal(e.ts, '2026-09-15T10:00:00+04:00');
    assert.deepEqual(e.accounts, accounts);            /* полный набор — для метки-конфликта */
  });
  it('фильтр main -> дата main', () => {
    assert.equal(effectiveApplied(byAcct, new Set(['main']), accounts).ts, '2026-08-27T10:00:00+04:00');
  });
  it('без фильтра -> самый ранний отклик (прежнее поведение)', () => {
    assert.equal(effectiveApplied(byAcct, new Set(), accounts).ts, '2026-08-27T10:00:00+04:00');
  });
  it('выбранный профиль не откликался -> null (карточка отсеется по accounts)', () => {
    assert.equal(effectiveApplied({ main: byAcct.main }, new Set(['acc2']), ['main']), null);
    assert.equal(effectiveApplied(null, new Set(), []), null);
  });
});

describe('crmStats — статистика откликов по аккаунтам и меткам (RFC-004)', () => {
  it('приглашение/отказ/без исхода разносятся по аккаунтам', () => {
    const applied = [
      { id: '1', account: 'main' }, { id: '2', account: 'main' }, { id: '3', account: 'acc2' },
    ];
    const statuses = { 1: { main: 'INVITATION' }, 2: { main: 'DISCARD' } };   /* 3 — без исхода */
    assert.deepEqual(crmStats(applied, statuses), {
      main: { applied: 2, invited: 1, rejected: 1, other: 0 },
      acc2: { applied: 1, invited: 0, rejected: 0, other: 1 },
    });
  });
  it('конфликт: одна вакансия у двух аккаунтов — статус учитывается обоим', () => {
    // отказ ТОЛЬКО у основного (баг 15.09.2026: раньше отказ основного шёл и в счётчик acc2)
    const applied = [{ id: '1', account: 'main' }, { id: '1', account: 'acc2' }];
    assert.deepEqual(crmStats(applied, { 1: { main: 'DISCARD', acc2: 'RESPONSE' } }), {
      main: { applied: 1, invited: 0, rejected: 1, other: 0 },
      acc2: { applied: 1, invited: 0, rejected: 0, other: 1 },
    });
  });
});

/* ── Панель профилей (24.09.2026): A/B резюме, пульс аккаунта, «ждут ответа» ──────────────
   Владелец неделю спрашивал в чат «отклики были? синк был? почему мало?» и «какое резюме
   работает» — лента на это не отвечала. Время форматируется в ЯВНОМ поясе (Europe/Samara,
   +04:00), иначе тесты разъехались бы на CI в UTC. */
const TZ = 'Europe/Samara';

describe('abCompare — сравнение резюме за ОБЩИЙ период', () => {
  const NOW = Date.parse('2026-09-24T12:00:00Z');   /* зрелость 3 дн -> граница 21.09 12:00Z */
  it('окно — от первого отклика младшего профиля до «старше 3 дней»; статус — свой у каждого', () => {
    const applied = [
      { id: 'm0', account: 'main', ts: '2026-09-10T08:00:00Z' },   /* до старта B — вне сравнения */
      { id: 'm1', account: 'main', ts: '2026-09-16T10:00:00Z' },
      { id: 'm2', account: 'main', ts: '2026-09-18T10:00:00Z' },
      { id: 'm3', account: 'main', ts: '2026-09-22T10:00:00Z' },   /* моложе 3 дней — ответа ещё не ждём */
      { id: 'a1', account: 'acc2', ts: '2026-09-15T10:00:00Z' },   /* старт B = начало окна */
      { id: 'a2', account: 'acc2', ts: '2026-09-17T10:00:00Z' },
      { id: 'a3', account: 'acc2', ts: '2026-09-23T10:00:00Z' },
    ];
    const statuses = { m1: { main: 'INTERVIEW' }, m2: { main: 'DISCARD' },
      a1: { acc2: 'DISCARD' }, a2: { acc2: 'RESPONSE' } };
    assert.deepEqual(abCompare(applied, statuses, NOW), {
      fromMs: Date.parse('2026-09-15T10:00:00Z'),
      toMs: Date.parse('2026-09-21T12:00:00Z'),
      thin: true,
      rows: {
        main: { applied: 2, invited: 1, rejected: 1, other: 0, invitedPct: 50, rejectedPct: 50 },
        acc2: { applied: 2, invited: 0, rejected: 1, other: 1, invitedPct: 0, rejectedPct: 50 },
      },
    });
  });
  it('вакансия с откликом от обоих: каждому — его статус, а не общий', () => {
    const applied = [
      { id: 'p', account: 'main', ts: '2026-09-14T10:00:00Z' },    /* до окна */
      { id: 'q', account: 'acc2', ts: '2026-09-15T00:00:00Z' },    /* старт B */
      { id: 'x', account: 'main', ts: '2026-09-16T10:00:00Z' },
      { id: 'x', account: 'acc2', ts: '2026-09-16T11:00:00Z' },
    ];
    const statuses = { x: { main: 'DISCARD', acc2: 'INTERVIEW' } };
    assert.deepEqual(abCompare(applied, statuses, NOW).rows, {
      main: { applied: 1, invited: 0, rejected: 1, other: 0, invitedPct: 0, rejectedPct: 100 },
      acc2: { applied: 2, invited: 1, rejected: 0, other: 1, invitedPct: 50, rejectedPct: 0 },
    });
  });
  it('30 зрелых откликов на профиль — выборка уже не «мало данных»', () => {
    const day = i => `2026-09-16T${String(i % 24).padStart(2, '0')}:00:00Z`;
    const applied = [
      ...Array.from({ length: 30 }, (_, i) => ({ id: `m${i}`, account: 'main', ts: day(i) })),
      ...Array.from({ length: 30 }, (_, i) => ({ id: `a${i}`, account: 'acc2', ts: day(i) })),
    ];
    assert.equal(abCompare(applied, {}, NOW).thin, false);
  });
  it('один профиль — сравнивать не с чем: null', () => {
    assert.equal(abCompare([{ id: '1', account: 'main', ts: '2026-09-16T10:00:00Z' }], {}, NOW), null);
  });
  it('второй профиль начал 2 дня назад — зрелых откликов нет: null', () => {
    const applied = [
      { id: '1', account: 'main', ts: '2026-09-16T10:00:00Z' },
      { id: '2', account: 'acc2', ts: '2026-09-22T10:00:00Z' },
    ];
    assert.equal(abCompare(applied, {}, NOW), null);
  });
});

describe('abText — подписи блока A/B', () => {
  it('период в датах пояса, доли в процентах, предупреждение о малой выборке', () => {
    const ab = { fromMs: Date.parse('2026-09-15T10:00:00Z'), toMs: Date.parse('2026-09-21T12:00:00Z'),
      thin: true,
      rows: {
        main: { applied: 2, invited: 1, rejected: 1, other: 0, invitedPct: 50, rejectedPct: 50 },
        acc2: { applied: 2, invited: 0, rejected: 1, other: 1, invitedPct: 0, rejectedPct: 50 },
      } };
    const accounts = [{ code: 'main', label: 'основной' }, { code: 'acc2', label: 'Резюме B' }];
    assert.deepEqual(abText(ab, accounts, TZ), {
      head: 'A/B · отклики 15.09–21.09, старше 3 дн',
      lines: ['основной: 2 · 📩 1 (50%) · ✖ 1 (50%)', 'Резюме B: 2 · 📩 0 (0%) · ✖ 1 (50%)'],
      note: 'мало данных (<30 на профиль) — разница может быть случайной',
    });
  });
});

describe('pulseLine — строка состояния аккаунта', () => {
  const NOW = Date.parse('2026-09-24T09:00:00Z');   /* 13:00 по Самаре */
  it('сегодняшние события — только время', () => {
    const acc = { today: 10, window24: 28, window_cap: 45, window_free_at: null,
      last_applied: '2026-09-24T06:52:14+00:00', last_sync: '2026-09-24T07:33:01+00:00' };
    assert.equal(pulseLine(acc, NOW, TZ), 'сегодня 10 · 24ч 28/45 · отклик 10:52 · синк 11:33');
  });
  it('окно заполнено — пауза до часа, когда HH снова примет отклик', () => {
    const acc = { today: 12, window24: 45, window_cap: 45, window_free_at: '2026-09-24T13:10:00+00:00',
      last_applied: '2026-09-24T08:40:00+00:00', last_sync: null };
    assert.equal(pulseLine(acc, NOW, TZ), 'сегодня 12 · 24ч 45/45 ⏸ до 17:10 · отклик 12:40');
  });
  it('пауза через полночь и события прошлых дней — с датой', () => {
    const acc = { today: 0, window24: 46, window_cap: 45, window_free_at: '2026-09-24T21:30:00+00:00',
      last_applied: '2026-09-23T13:53:16+00:00', last_sync: '2026-09-22T07:33:01+00:00' };
    assert.equal(pulseLine(acc, NOW, TZ),
      'сегодня 0 · 24ч 46/45 ⏸ до 25.09 01:30 · отклик 23.09 17:53 · синк 22.09 11:33');
  });
  it('сервер старой версии (без пульса) — пустая строка, а не «undefined»', () => {
    assert.equal(pulseLine({ code: 'main', label: 'основной', session: 'ok', applied: 3 }, NOW, TZ), '');
  });
});

describe('waitingByAccount — «ждут ответа» по каждому профилю', () => {
  /* Считается как чип «👤 Личные» (isPersonalChat), а не по голому needs_reply: тот ложен только
     у отказа, и первая версия панели показала «💬 747» — боты и заглушки «свяжемся» вперемешку
     с живыми письмами (замер на живой ленте 24.09.2026). Счётчик, который всегда сотни, не
     сигнал. */
  const human = { needs_reply: true, sender: 'human' };
  it('живое письмо, ждущее ответа, — по чату СВОЕГО профиля, мимо фильтра профилей', () => {
    const vs = [
      { chatByAcct: { main: human, acc2: { needs_reply: false, sender: 'human' } } },
      { chatByAcct: { acc2: human } },
      {},
    ];
    assert.deepEqual(waitingByAccount(vs), { main: 1, acc2: 1 });
  });
  it('бот, заглушка-фриз и закрытый чат ответа не ждут', () => {
    const vs = [
      { chatByAcct: { acc2: { needs_reply: true, sender: 'bot' } } },
      { chatByAcct: { acc2: { needs_reply: true, sender: 'human', kind: 'ack' } } },
      { chatByAcct: { acc2: { needs_reply: true, sender: 'human', can_write: false } } },
    ];
    assert.deepEqual(waitingByAccount(vs), {});
  });
});

/* Подпись свёрнутой выпадашки «Язык»/«Роль» (24.09.2026: 14 + 18 пилюль ушли в выпадающие
   списки — решение владельца). Порядок — порядок разметки, а не кликов: иначе подпись
   прыгала бы при каждом выборе. */
describe('pickSummary — подпись свёрнутого мультивыбора', () => {
  const ORDER = ['Go', 'Python', 'Rust'];
  it('ничего не выбрано — «все»', () => {
    assert.equal(pickSummary(new Set(), ORDER), 'все');
  });
  it('один-два — перечислением в порядке разметки', () => {
    assert.equal(pickSummary(new Set(['Rust', 'Go']), ORDER), 'Go, Rust');
  });
  it('больше двух — два первых и сколько ещё', () => {
    assert.equal(pickSummary(new Set(['Rust', 'Python', 'Go']), ORDER), 'Go, Python +1');
  });
});

describe('filterVacancies — фильтр по аккаунту (RFC-004)', () => {
  const base = { search: [], roles: new Set(), langs: new Set(), exps: new Set(), emps: new Set(),
    status: 'all', schedule: 'all', source: 'all', minSal: 0, maxSal: 1e9, salMax: 1e9,
    showNonIt: true, displayCurs: ['RUB'] };
  const vs = [
    { id: '1', name: 'A', employer: '', techs: [], role: 'Backend', applied: { accounts: ['main'] } },
    { id: '2', name: 'B', employer: '', techs: [], role: 'Backend', applied: { accounts: ['acc2'] } },
    { id: '3', name: 'C', employer: '', techs: [], role: 'Backend', applied: { accounts: ['acc2', 'main'] } },
  ];
  it('один выбранный профиль — только его отклики (плюс общие)', () => {
    const out = filterVacancies(vs, { ...base, accountFilter: new Set(['acc2']) });
    assert.deepEqual(out.map(v => v.id).sort(), ['2', '3']);
  });
  it('несколько профилей — объединение (мультиселект)', () => {
    const out = filterVacancies(vs, { ...base, accountFilter: new Set(['main', 'acc2']) });
    assert.deepEqual(out.map(v => v.id).sort(), ['1', '2', '3']);
  });
  it('пустой набор — фильтр выключен, показываем все', () => {
    const out = filterVacancies(vs, { ...base, accountFilter: new Set() });
    assert.deepEqual(out.map(v => v.id).sort(), ['1', '2', '3']);
  });
});

/* Офлайн-фолбэк подписей исхода отклика. Лента открывается и из `file://`, где
   `feed-data.js` (и вместе с ним инжект `APPLY_LABELS_PY`) не подгружен, поэтому словарь
   обязан быть полным и дословным. До аудита 2026-09-22 метки жили только в `main.js` без
   моста и разъехались с сервером: `server.py::_apply_post` отдаёт статус `taken` (вакансию
   уже взял другой аккаунт), метки для него не было — пользователь видел сырое английское
   слово; `captcha` не был размечен вовсе. */
describe('APPLY_LABELS — подписи исхода отклика (офлайн-фолбэк)', () => {
  /* [код, подпись] — коды `ApplyOutcome` + транспортные статусы `TransportStatus` одним
     словарём (Python: `apply/outcome.py::APPLY_LABELS`). Значения выписаны литералами до
     запуска: это вход спеки, а не снимок вывода реализации. */
  const cases = [
    ['applied', '✅ Отклик отправлен'],
    ['already', 'уже откликались'],
    ['form', '📝 нужна форма — в очереди'],
    ['skip', '✖ пропущено (внешний/архив/опросник)'],
    ['unconfirmed', '⚠ не подтвердилось — клик ушёл, ответа HH нет'],
    ['captcha', '⛔ капча HH — нужен вход руками'],
    ['queued', '➕ в очереди крона'],
    ['busy', '⏳ занято — идёт крон-отклик, попробуйте через пару минут'],
    ['no-session', '⚠ нет сессии — hh.py autoclick --login'],
    ['taken', '🚫 вакансию уже взял другой аккаунт'],
    ['error', '⚠ ошибка'],
  ];
  for (const [code, label] of cases) {
    it(`${code} -> «${label}»`, () => {
      assert.equal(APPLY_LABELS[code], label);
    });
  }

  it('словарь полон: ровно одиннадцать кодов и ни одного лишнего', () => {
    assert.deepEqual(Object.keys(APPLY_LABELS), cases.map(([code]) => code));
  });
});
