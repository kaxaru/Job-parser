/* Юнит-тесты профиля резюме (src/feed/resume.js): кнопка-фильтр + бейдж %.
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { matchesResume, resumeMatch } from '../../src/feed/resume.js';

/* Профилю нужны только techs/exp_id/remote_any — минимальная фабрика.
   exp_id — доменный КОД грейда (Experience.hh_id), не подпись: подписи из EXP_LABELS
   переименовываются, коды — нет (аудит 08.08.2026, EXP_SCORE перекочевал на коды). */
function vac(over = {}) {
  return { exp_id: 'between1And3', techs: ['Python'], remote_any: true, ...over };
}

describe('matchesResume — жёсткий фильтр «по резюме» (кнопка)', () => {
  it('remote + опыт≤3 + Python-ядро → true', () => {
    assert.equal(matchesResume(vac({ techs: ['Python', 'Docker'] })), true);
  });
  it('без удалёнки → false (RESUME_REQUIRE_REMOTE=true)', () => {
    assert.equal(matchesResume(vac({ remote_any: false })), false);
  });
  it('нет технологий из RESUME_CORE → false', () => {
    assert.equal(matchesResume(vac({ techs: ['Java', 'Go'] })), false);
  });

  /* Грейд из ЖЁСТКОГО фильтра убран (07.08.2026). Он резал вслепую: у arbeitnow и web3
     грейда в API нет вовсе (1326 и 1740 карточек с пустым exp — оба портала отсекались
     целиком), у talanto/hirify/getmatch заполнен не везде — там терялось ещё 1328.
     Видимость карточки грейд больше не определяет; на приоритет он влияет через
     resumeMatch (до 25 баллов). */
  for (const expId of ['moreThan6', 'between3And6', 'noExperience', 'between1And3',
                       '', null, undefined]) {
    it(`грейд ${JSON.stringify(expId)} не влияет на видимость`, () => {
      assert.equal(matchesResume(vac({ exp_id: expId })), true);
    });
  }
  it('снятие грейда не отменяет требование стека', () => {
    assert.equal(matchesResume(vac({ exp_id: 'moreThan6', techs: ['Java'] })), false);
  });
  it('снятие грейда не отменяет требование удалёнки', () => {
    assert.equal(matchesResume(vac({ exp_id: 'moreThan6', remote_any: false })), false);
  });
});

describe('resumeMatch — % совпадения (стек60 + опыт25 + удалёнка15)', () => {
  it('считает компоненты и сумму', () => {
    const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL'], exp_id: 'between1And3' }));
    assert.equal(m.stack, 53);   /* 30+12+6+5 */
    assert.equal(m.exp, 25);
    assert.equal(m.remote, 15);
    assert.equal(m.pct, 93);
  });
  it('стек клампится на 60', () => {
    const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL', 'MySQL', 'SQLite', 'C#', 'React'] }));
    assert.equal(m.stack, 60);
  });
  it('неизвестный опыт → нейтральные 12', () => {
    assert.equal(resumeMatch(vac({ exp_id: '???' })).exp, 12);
  });
  it('не удалёнка → remote 0', () => {
    assert.equal(resumeMatch(vac({ remote_any: false })).remote, 0);
  });

  /* Скоринг ключуется по КОДУ грейда: подпись из config.EXP_LABELS может быть
     переименована (и уже жила в трёх копиях) — баллы это менять не должно. */
  for (const [expId, points] of [['noExperience', 25], ['between1And3', 25],
                                 ['between3And6', 10], ['moreThan6', 0]]) {
    it(`код ${expId} даёт ${points} баллов за опыт`, () => {
      assert.equal(resumeMatch(vac({ exp_id: expId })).exp, points);
    });
  }
  it('подпись грейда вместо кода баллов не даёт — только нейтральные 12', () => {
    assert.equal(resumeMatch(vac({ exp_id: '1–3 года' })).exp, 12);
  });
  it('грейда нет вовсе (arbeitnow / web3) → нейтральные 12', () => {
    assert.equal(resumeMatch({ techs: [], remote_any: false }).exp, 12);
  });
});
