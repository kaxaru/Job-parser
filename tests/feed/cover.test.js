/* Юнит-тесты генерации сопроводительных писем (src/feed/cover.js).
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { COVER_TEMPLATES, coverLetter, coverTemplates } from '../../src/feed/cover.js';

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

/* Шаблоны из resume_profile.json (feed_cover_templates -> FEED_COVER_TEMPLATES_PY).
   Форк репозитория должен менять письма профилем, а не правкой src/feed/cover.js. */
describe('шаблоны писем из профиля', () => {
  function withProfile(tpls, fn) {
    globalThis.FEED_COVER_TEMPLATES_PY = tpls;
    try { fn(); } finally { delete globalThis.FEED_COVER_TEMPLATES_PY; }
  }

  it('профиль полностью заменяет дефолтные шаблоны', () => {
    withProfile(['Привет! Вакансия {role}{company}. {stack}'], () => {
      assert.equal(
        coverLetter(vac({ name: 'Go Dev', employer: 'Acme', techs: ['Python'] }), 0),
        'Привет! Вакансия «Go Dev» в компании Acme. '
        + 'В вашем стеке вижу Python — именно с этим работаю каждый день.',
      );
    });
  });
  it('счётчик вариантов равен числу шаблонов профиля, а не дефолтов', () => {
    withProfile(['первый {role}', 'второй {role}'], () => {
      assert.equal(coverTemplates().length, 2);
    });
  });
  it('ротация idx идёт по набору профиля', () => {
    withProfile(['первый {role}', 'второй {role}'], () => {
      assert.equal(coverLetter(vac(), 0), 'первый «Python Backend»');
      assert.equal(coverLetter(vac(), 1), 'второй «Python Backend»');
      assert.equal(coverLetter(vac(), 2), 'первый «Python Backend»');
    });
  });
  it('подстановка повторяется по всему тексту, а не только в первом вхождении', () => {
    withProfile(['{role} — снова {role}'], () => {
      assert.equal(coverLetter(vac({ name: 'Dev' }), 0), '«Dev» — снова «Dev»');
    });
  });
  it('пустой список в профиле -> дефолты (форк без ключа ничего не теряет)', () => {
    withProfile([], () => {
      assert.equal(coverTemplates().length, COVER_TEMPLATES.length);
      assert.ok(coverLetter(vac(), 0).startsWith('Здравствуйте!'));
    });
  });
  it('без инжекта вообще — дефолты', () => {
    assert.equal(coverTemplates().length, COVER_TEMPLATES.length);
  });
});
