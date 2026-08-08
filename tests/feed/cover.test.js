/* Юнит-тесты генерации сопроводительных писем (src/feed/cover.js).
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { coverLetter, coverTemplates } from '../../src/feed/cover.js';

/* Письму нужны только name/employer/techs — минимальная фабрика. */
function vac(over = {}) {
  return { name: 'Python Backend', employer: 'Acme', techs: ['Python'], ...over };
}

/* Шаблоны из профиля (resume_profile.json::feed_cover_templates -> FEED_COVER_TEMPLATES_PY).
   Короткий шаблон вида '{stack}' изолирует одну подстановку от текста писем. */
function withProfile(tpls, fn) {
  globalThis.FEED_COVER_TEMPLATES_PY = tpls;
  try { fn(); } finally { delete globalThis.FEED_COVER_TEMPLATES_PY; }
}

describe('coverLetter — генерация письма', () => {
  /* Дефолтных шаблонов ПЯТЬ, и это число видит пользователь: view.js печатает
     «Вариант N из M» и крутит idx по длине набора. Литерал вместо COVER_TEMPLATES.length:
     потеря шаблона при правке уменьшала обе стороны равенства одновременно, тест оставался
     зелёным, а ротация писем незаметно сужалась — одному работодателю уезжали бы одинаковые
     письма (аудит 09.08.2026). Удалил/добавил шаблон — поправь и это число. */
  it('дефолтных шаблонов ровно пять', () => {
    assert.equal(coverTemplates().length, 5);
  });
  it('idx 5 возвращается к первому шаблону, idx 6 — ко второму', () => {
    const v = vac();
    assert.equal(coverLetter(v, 5), coverLetter(v, 0));      /* цикл по модулю пяти */
    assert.equal(coverLetter(v, 6), coverLetter(v, 1));
  });
  it('подставляет роль, компанию и совпавший стек', () => {
    const txt = coverLetter(vac({ name: 'Python Dev', employer: 'Сбер', techs: ['Python', 'Docker'] }), 0);
    assert.ok(txt.includes('вакансия «Python Dev» в компании Сбер'));
    assert.ok(txt.includes('В вашем стеке вижу Python и Docker — именно с этим работаю каждый день.'));
  });
  /* Стек-фраза целиком, а не по вхождению «C#/.NET»: шаблон подставляется в {stack},
     поэтому проверяем ровно ту строку, которую увидит работодатель. */
  it('C# рендерится как «C#/.NET»', () => {
    withProfile(['{stack}'], () => {
      assert.equal(coverLetter(vac({ techs: ['C#'] }), 0),
                   'В вашем стеке вижу C#/.NET — именно с этим работаю каждый день.');
    });
  });
  it('без совпавшего стека — дефолтная фраза', () => {
    withProfile(['{stack}'], () => {
      assert.equal(coverLetter(vac({ techs: ['COBOL'] }), 0),
                   'Мой основной стек — Python, FastAPI, Docker и PostgreSQL/MySQL.');
    });
  });
});

/* Шаблоны из resume_profile.json (feed_cover_templates -> FEED_COVER_TEMPLATES_PY).
   Форк репозитория должен менять письма профилем, а не правкой src/feed/cover.js. */
describe('шаблоны писем из профиля', () => {
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
  /* Литерал 5, а не COVER_TEMPLATES.length: сравнение длины массива с длиной того же
     массива — тавтология, она молчала бы и о полностью потерянном наборе. */
  it('пустой список в профиле -> дефолты (форк без ключа ничего не теряет)', () => {
    withProfile([], () => {
      assert.equal(coverTemplates().length, 5);
      assert.ok(coverLetter(vac(), 0).startsWith('Здравствуйте!'));
    });
  });
  it('без инжекта вообще — дефолты', () => {
    assert.equal(coverTemplates().length, 5);
  });
});
