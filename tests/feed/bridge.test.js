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

const {
  SCHED_LABELS, STATUS_BTNS, filterVacancies, isFrozenChat, isRemoteLike, statusInfo,
} = await import('../../src/feed/model.js');

const vac = over => ({
  id: '1', name: 'x', employer: '', city: '', techs: [], sal_mid: null, currency: 'RUR',
  exp: '', exp_id: '', schedule: 'remote', remote_any: true, role: 'Backend',
  status: null, needs_form: false, ...over,
});
const flt = over => ({
  search: [], resumeOnly: false, langs: new Set(), roles: new Set(), showNonIt: false,
  exps: new Set(), minSal: 0, maxSal: 1e6, salMax: 1e6, city: '', schedule: 'all',
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

  it('MARK_VALUES_PY задаёт набор кнопок пометок', () => {
    assert.deepEqual(STATUS_BTNS.map(b => b.act), ['applied']);
  });

  it('CHAT_FROZEN_PY задаёт набор тупиковых чатов', () => {
    assert.equal(isFrozenChat({ kind: 'ack' }), true);
    assert.equal(isFrozenChat({ kind: 'bot_interview' }), false);   /* фолбэк дал бы true */
  });
});
