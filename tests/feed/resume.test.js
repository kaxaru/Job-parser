/* Юнит-тесты профиля резюме (src/feed/resume.js): кнопка-фильтр + бейдж %.
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { matchesResume, resumeMatch } from '../../src/feed/resume.js';

/* Профилю нужны только techs/exp/remote_any — минимальная фабрика. */
function vac(over = {}) {
  return { exp: '1–3 года', techs: ['Python'], remote_any: true, ...over };
}

describe('matchesResume — жёсткий фильтр «по резюме» (кнопка)', () => {
  it('remote + опыт≤3 + Python-ядро → true', () => {
    assert.equal(matchesResume(vac({ techs: ['Python', 'Docker'] })), true);
  });
  it('без удалёнки → false (RESUME_REQUIRE_REMOTE=true)', () => {
    assert.equal(matchesResume(vac({ remote_any: false })), false);
  });
  it('опыт вне RESUME_EXPS → false', () => {
    assert.equal(matchesResume(vac({ exp: '6+ лет' })), false);
  });
  it('нет технологий из RESUME_CORE → false', () => {
    assert.equal(matchesResume(vac({ techs: ['Java', 'Go'] })), false);
  });

  /* ИНЦИДЕНТ 07.08.2026: у arbeitnow и web3 грейда в API нет вовсе — 1326 и 1740 карточек
     с пустым exp, — и фильтр выбрасывал оба портала целиком, хотя resumeMatch тот же
     неизвестный опыт считает нейтральным (12 баллов). Отсекаем неподходящий грейд,
     а не отсутствие данных о нём. */
  for (const empty of ['', null, undefined]) {
    it(`неизвестный опыт (${JSON.stringify(empty)}) → true, а не отсев`, () => {
      assert.equal(matchesResume(vac({ exp: empty })), true);
    });
  }
  it('неизвестный опыт не отменяет остальные условия — без Python всё равно false', () => {
    assert.equal(matchesResume(vac({ exp: '', techs: ['Java'] })), false);
  });
  it('неизвестный опыт не отменяет требование удалёнки', () => {
    assert.equal(matchesResume(vac({ exp: '', remote_any: false })), false);
  });
});

describe('resumeMatch — % совпадения (стек60 + опыт25 + удалёнка15)', () => {
  it('считает компоненты и сумму', () => {
    const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL'], exp: '1–3 года' }));
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
    assert.equal(resumeMatch(vac({ exp: '???' })).exp, 12);
  });
  it('не удалёнка → remote 0', () => {
    assert.equal(resumeMatch(vac({ remote_any: false })).remote, 0);
  });
});
