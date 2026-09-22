/* Мост Python -> JS: константы, инжектированные в feed-data.js, обязаны ПОБЕЖДАТЬ
   офлайн-фолбэки model.js. Фолбэк — страховка для file:// без feed-data.js, а не второй
   источник правды; когда он молча выигрывал, стороны расходились (SCHED_LABELS совпадал
   с доменом в одном коде из трёх, а кнопка «Офис» пропускала гибрид — аудит 08.08.2026).

   Инжект читается на ИМПОРТЕ модуля (const инициализируется один раз), поэтому глобалы
   ставятся до динамического import, а файл живёт отдельно от model.test.js: node --test
   даёт каждому файлу свой процесс, и статический импорт соседа кеш бы уже прогрел. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

/* Значения нарочно НЕ равны фолбэкам: тест обязан падать, если модуль читает хардкод. */
globalThis.SCHED_LABELS_PY = { remote: 'ИЗ-ПАЙТОНА', flexible: 'ГИБРИД-PY', fullDay: 'ОФИС-PY' };
globalThis.REMOTE_LIKE_PY = ['flexible'];          /* только гибрид — фолбэк дал бы и remote */
globalThis.STATE_LABELS_PY = { DISCARD: 'ОТКАЗ-PY' };
globalThis.MARK_VALUES_PY = ['applied'];           /* фолбэк — две пометки, инжект одна */
globalThis.CHAT_FROZEN_PY = ['ack'];               /* фолбэк содержит ещё bot_interview */
globalThis.CHAT_BOT_INTERVIEW_PY = 'bot-iv-PY';    /* код ЖЁЛТОГО фриза из Python, не фолбэк */
/* Возраст среза: порог 2 ч против фолбэка 36 — срез «12 ч назад» протухший ТОЛЬКО по
   инжекту. Метка стоит в прошлом относительно момента проверки, а не Date.now() теста. */
globalThis.COLLECTED_AT_PY = 1_770_878_400 - 12 * 3600;
globalThis.STALE_HOURS_PY = 2;
/* Подписи исхода отклика: инжект — единственный ключ, фолбэк их десять. Сервер отдаёт
   транспортные статусы (`taken`/`busy`/…), поэтому коды в ленте обязаны приходить из
   Python, а не жить литералом в main.js (аудит 2026-09-22, §3.2). */
globalThis.APPLY_LABELS_PY = { taken: 'ИЗ-ПАЙТОНА' };

const {
  APPLY_LABELS, SCHED_LABELS, STATUS_BTNS, cardTone, filterVacancies, isFrozenChat, isRemoteLike,
  staleNow, statusInfo,
} = await import('../../src/feed/model.js');

const vac = over => ({
  id: '1', name: 'x', employer: '', city: '', techs: [], sal_mid: null, currency: 'RUR',
  exp: '', exp_id: '', schedule: 'remote', remote_any: true, role: 'Backend',
  status: null, needs_form: false, ...over,
});
const flt = over => ({
  search: [], resumeOnly: false, langs: new Set(), roles: new Set(), showNonIt: false,
  exps: new Set(), emps: new Set(), minSal: 0, maxSal: 1e6, salMax: 1e6, city: '', schedule: 'all',
  status: 'all', source: 'all', displayCur: 'RUB', dateFrom: '', dateTo: '',
  sort: 'none', matchSort: false, ...over,
});

describe('мост Python -> JS: инжект побеждает офлайн-фолбэк', () => {
  it('SCHED_LABELS берутся из SCHED_LABELS_PY', () => {
    assert.deepEqual(SCHED_LABELS,
      { remote: 'ИЗ-ПАЙТОНА', flexible: 'ГИБРИД-PY', fullDay: 'ОФИС-PY' });
  });

  it('REMOTE_LIKE_PY задаёт, что считается удалёнкой', () => {
    assert.equal(isRemoteLike('flexible'), true);
    assert.equal(isRemoteLike('remote'), false);   /* фолбэк вернул бы true */
  });

  /* Фильтр строится ОТ моста, а не от литерала 'remote' в model.js. */
  it('кнопка «Удалённо» следует за инжектом, а не за литералом', () => {
    const data = [vac({ id: 'rem', schedule: 'remote' }), vac({ id: 'hyb', schedule: 'flexible' })];
    assert.deepEqual(filterVacancies(data, flt({ schedule: 'remote' })).map(v => v.id), ['hyb']);
    assert.deepEqual(filterVacancies(data, flt({ schedule: 'office' })).map(v => v.id), ['rem']);
  });

  it('STATE_LABELS_PY задаёт подпись бейджа', () => {
    assert.equal(statusInfo(vac({ status: 'DISCARD' })).label, 'ОТКАЗ-PY');
  });

  it('APPLY_LABELS_PY задаёт подпись исхода отклика', () => {
    assert.equal(APPLY_LABELS.taken, 'ИЗ-ПАЙТОНА');   /* литерал фолбэка — «🚫 вакансию…» */
  });

  it('MARK_VALUES_PY задаёт набор кнопок пометок', () => {
    assert.deepEqual(STATUS_BTNS.map(b => b.act), ['applied']);
  });

  it('CHAT_FROZEN_PY задаёт набор тупиковых чатов', () => {
    assert.equal(isFrozenChat({ kind: 'ack' }), true);
    assert.equal(isFrozenChat({ kind: 'bot_interview' }), false);   /* фолбэк дал бы true */
  });

  /* Жёлтый тон — сравнение с КОДОМ из Python, а не с литералом: набор CHAT_FROZEN_PY
     отвечает лишь «тупик», а какой фриз жёлтый — отдельный факт. Инжект здесь НЕ равен
     фолбэку 'bot_interview', поэтому тест падает, если модель вернётся к литералу
     (аудит 2026-09-22, §3.2). */
  it('CHAT_BOT_INTERVIEW_PY задаёт код жёлтого фриза', () => {
    assert.equal(cardTone({ ...vac(), status: null, chat: { kind: 'bot-iv-PY' } }), 'botiv');
    assert.equal(cardTone({ ...vac(), status: null, chat: { kind: 'bot_interview' } }), '');
  });

  /* Оба конца моста разом: метка сбора из cache_meta.json и порог из config. С фолбэком
     36 ч срез «12 ч назад» молчал бы — баннер зажигает именно инжект. */
  it('COLLECTED_AT_PY и STALE_HOURS_PY задают возраст среза и порог', () => {
    const mark = 1_770_878_400 - 12 * 3600;            /* == COLLECTED_AT_PY */
    assert.equal(Math.round(staleNow(1_770_878_400_000).hours), 12);
    assert.equal(staleNow((mark + 3600) * 1000), null);   /* час после сбора — ещё свежо */
  });
});
