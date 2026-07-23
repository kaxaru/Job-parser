/* Юнит-тесты генерации сопроводительных писем (src/feed/cover.js).
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { COVER_TEMPLATES, coverLetter } from '../../src/feed/cover.js';

/* Письму нужны только name/employer/techs — минимальная фабрика. */
function vac(over = {}) {
  return { name: 'Python Backend', employer: 'Acme', techs: ['Python'], ...over };
}

describe('coverLetter — генерация письма', () => {
  it('ротация шаблонов по idx (mod длины массива)', () => {
    const v = vac();
    const n = COVER_TEMPLATES.length;
    assert.equal(coverLetter(v, 0), coverLetter(v, n));      /* цикл по модулю */
    assert.notEqual(coverLetter(v, 0), coverLetter(v, 1));
  });
  it('подставляет роль, компанию и совпавший стек', () => {
    const txt = coverLetter(vac({ name: 'Python Dev', employer: 'Сбер', techs: ['Python', 'Docker'] }), 0);
    assert.ok(txt.includes('«Python Dev»'));
    assert.ok(txt.includes('Сбер'));
    assert.ok(txt.includes('Python') && txt.includes('Docker'));
  });
  it('C# рендерится как «C#/.NET»', () => {
    const txt = coverLetter(vac({ techs: ['C#'] }), 0);
    assert.ok(txt.includes('C#/.NET'));
  });
  it('без совпавшего стека — дефолтная фраза', () => {
    const txt = coverLetter(vac({ techs: ['COBOL'] }), 0);
    assert.ok(txt.includes('Мой основной стек'));
  });
});
