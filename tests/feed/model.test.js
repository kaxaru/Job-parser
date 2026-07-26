/* Юнит-тесты доменной модели ленты (src/feed/model.js).
   Запуск: node --test tests/feed/  (или `npm test`). Без зависимостей — node:test + node:assert.
   model.js — ЧИСТАЯ логика (без DOM/IO), поэтому тестируется прямым импортом в Node. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  ageColor, appliedInRange, cardColor, cardTone, chatAgeLabel, convert, esc, filterVacancies, fmtK, fmtSal,
  hashId, isFrozenChat, matchColor, resolveCur, statusInfo, tagClr,
} from '../../src/feed/model.js';

/* Фабрика вакансии с дефолтами — переопределяем только нужные поля в каждом тесте. */
function vac(over = {}) {
  return {
    id: '1', name: 'Python Backend', employer: 'Acme', url: '', city: 'Москва',
    sal_from: null, sal_to: null, sal_mid: null, currency: 'RUR',
    exp: '1–3 года', schedule: 'remote', techs: ['Python'], remote_any: true,
    role: 'Backend', status: null, needs_form: false,
    ...over,
  };
}
/* Полное состояние фильтров (как в store) — тесты меняют точечно. */
function flt(over = {}) {
  return {
    search: [], resumeOnly: false, langs: new Set(), roles: new Set(),
    showNonIt: false, exps: new Set(),
    minSal: 0, maxSal: 1_000_000, salMax: 1_000_000,
    city: '', schedule: 'all', status: 'all', source: 'all', displayCur: 'RUB',
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

describe('statusInfo — бейдж статуса отклика (API)', () => {
  it('нет статуса → null', () => {
    assert.equal(statusInfo(vac({ status: null })), null);
  });
  it('отказ → красный', () => {
    assert.equal(statusInfo(vac({ status: 'DISCARD' })).label, 'Отказ');
    assert.equal(statusInfo(vac({ status: 'DISCARD' })).color, '#E45756');
  });
  it('приглашение/интервью → зелёный', () => {
    assert.equal(statusInfo(vac({ status: 'INTERVIEW' })).color, '#3FA34D');
  });
  it('отклик без ответа → серый', () => {
    assert.equal(statusInfo(vac({ status: 'RESPONSE' })).color, '#8a8f98');
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
  it('приглашение после бот-интервью → applied: движение реальное, красим зелёным', () => {
    assert.equal(cardTone(vac({ status: 'INVITATION', chat: botIv })), 'applied');
  });
  it('обычный чат → "" (как cardColor: тон по ручной пометке)', () => {
    assert.equal(cardTone(vac({ status: 'RESPONSE', chat: { kind: 'question' } })), '');
    assert.equal(cardTone(vac({ status: 'RESPONSE' })), '');
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

describe('ageColor — температурный градиент возраста', () => {
  const hue = c => Number(c.match(/hsl\((\d+)/)[1]);
  it('свежая (0 дн) — горячий красный (низкий hue)', () => {
    assert.ok(hue(ageColor(0)) <= 30);
  });
  it('старая (90+ дн) — холодный синий (высокий hue)', () => {
    assert.ok(hue(ageColor(90)) >= 200);
    assert.equal(ageColor(120), ageColor(90));   // клампинг сверху
  });
  it('монотонность: старее → холоднее (больше hue)', () => {
    assert.ok(hue(ageColor(10)) < hue(ageColor(60)));
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
  it('валюта без курса — показываем в оригинале (символ $)', () => {
    assert.ok(fmtSal(vac({ sal_from: 1000, currency: 'USD' })).includes('$'));   // нет FX_RATES -> оригинал
  });
  it('с курсами: конвертит RUR→USD + /мес', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    assert.equal(ws(fmtSal(vac({ sal_from: 90000, currency: 'RUR' }), 'USD')), 'от 1 000 $/мес');
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
  it('фильтр minSal сравнивает в displayCur', () => {
    globalThis.FX_RATES = { USD: 1, RUB: 90 };
    const data = [
      vac({ id: 'ru', sal_mid: 180000, currency: 'RUR' }),   // = $2000
      vac({ id: 'us', sal_mid: 1000, currency: 'USD' }),     // = $1000
    ];
    const ids = filterVacancies(data,
      flt({ displayCur: 'USD', minSal: 1500, maxSal: 100000, salMax: 100000 })).map(v => v.id);
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

describe('hashId — детерминированный хеш', () => {
  it('стабилен и неотрицателен', () => {
    assert.equal(hashId('abc'), hashId('abc'));
    assert.ok(hashId('xyz') >= 0);
  });
  it('разные входы → разные хеши (обычно)', () => {
    assert.notEqual(hashId('Python'), hashId('Java'));
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

describe('matchColor — hsl от % совпадения', () => {
  it('крайние значения в границах hue', () => {
    assert.equal(matchColor(100), 'hsl(15, 70%, 42%)');   /* тёплый */
    assert.equal(matchColor(0), 'hsl(210, 70%, 42%)');    /* холодный */
  });
});

describe('filterVacancies — фильтрация + сортировка (чистая)', () => {
  const data = [
    vac({ id: 'a', name: 'Python backend', employer: 'Альфа', techs: ['Python'], sal_mid: 200000, city: 'Москва', exp: '1–3 года' }),
    vac({ id: 'b', name: 'Java dev', employer: 'Сбер', techs: ['Java'], sal_mid: 300000, city: 'Казань', exp: '3–6 лет', remote_any: false, schedule: 'fullDay' }),
    vac({ id: 'c', name: 'Go SRE', employer: 'Яндекс', techs: ['Go'], sal_mid: null, city: 'Москва', exp: '6+ лет' }),
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
  it('фильтр по опыту', () => {
    assert.deepEqual(ids(filterVacancies(data, flt({ exps: new Set(['6+ лет']) }))), ['c']);
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
    /* a: Python+1–3+remote=70 | c: 6+ лет+remote=15 | b: Java+3–6+офис=10 */
    assert.deepEqual(ids(filterVacancies(data, flt({ matchSort: true }))), ['a', 'c', 'b']);
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
});

describe('filterVacancies — совпадение отсеивает гост-вакансии (>60 дней)', () => {
  const data = [vac({ id: 'fresh', fresh: 'fresh' }), vac({ id: 'ghost', fresh: 'ghost' })];
  it('matchSort — гост отброшен', () => {
    assert.deepEqual(filterVacancies(data, flt({ matchSort: true })).map(v => v.id), ['fresh']);
  });
  it('resumeOnly — гост отброшен', () => {
    assert.ok(!filterVacancies(data, flt({ resumeOnly: true })).map(v => v.id).includes('ghost'));
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
