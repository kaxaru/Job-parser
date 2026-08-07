/* Юнит-тесты доменной модели ленты (src/feed/model.js).
   Запуск: node --test tests/feed/  (или `npm test`). Без зависимостей — node:test + node:assert.
   model.js — ЧИСТАЯ логика (без DOM/IO), поэтому тестируется прямым импортом в Node. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  ageColor, appliedInRange, cardColor, cardTone, chatAgeLabel, cityMatches, convert,
  countActiveFilters, esc, filterVacancies, fmtK, fmtSal, hashId, isFrozenChat, matchColor, matchInk,
  isDiscard, isInvited,
  journalById, portalSite, resolveCur, statusInfo, syntheticCard, tagClr, tagInk,
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
    assert.equal(countActiveFilters(flt2({ sort: 'desc', matchSort: true, displayCur: 'USD' })), 0);
  });
  it('суженная вилка зарплаты — активный фильтр', () => {
    assert.equal(countActiveFilters(flt2({ minSal: 50_000 })), 1);
    assert.equal(countActiveFilters(flt2({ maxSal: 100_000, salMax: 1_000_000 })), 1);
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
