/* Юнит-тесты профиля резюме (src/feed/resume.js): кнопка-фильтр + бейдж %.
   Запуск: npm test (node:test). Чистая логика — без DOM/IO. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { matchesResume, resumeMatch } from '../../src/feed/resume.js';

/* Профилю нужны только techs/exp_id/remote_any — минимальная фабрика.
   exp_id — доменный КОД грейда (Experience.hh_id), не подпись: подписи из EXP_LABELS
   переименовываются, коды — нет (аудит 08.08.2026, EXP_SCORE перекочевал на коды). */
function vac(over = {}) {
  return { exp_id: 'between1And3', techs: ['Python'], remote_any: true,
           role: 'Backend', ...over };
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

/* Формула (переделка 14.08.2026):
     pct = (60·stackF1 + 25·expFit + 15·geoFit) · roleFit
     stackF1 = гармоническое среднее двух долей:
       covered = Σ вес(тех) / число техов вакансии   — какую часть ИХ списка я закрываю
       engaged = min(1, попаданий в ядро / 3)        — насколько задействовано МОЁ ядро
   Ожидаемые значения ниже посчитаны ПО ЭТОЙ ЗАПИСИ, а не сняты с вывода кода. */
describe('resumeMatch — две оси: готовность (стек) × желание (роль)', () => {
  it('считает компоненты и сумму', () => {
    /* covered = (1+1+0.2+1)/4 = 0.8 · engaged = 3/3 = 1 · F1 = 1.6/1.8 = 0.8889
       stack = 60·0.8889 = 53.3 · exp 25 · remote 15 · роль Backend ×1 -> 93.3 */
    const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL'] }));
    assert.equal(m.stack, 53);
    assert.equal(m.exp, 25);
    assert.equal(m.remote, 15);
    assert.equal(m.pct, 93);
  });

  it('чужие технологии в списке СНИЖАЮТ балл — это доля, а не счётчик', () => {
    /* Та же тройка ядра, но вакансия перечислила ещё пять чужих технологий.
       covered = 3/8 = 0.375 · engaged = 1 · F1 = 0.75/1.375 = 0.5455 -> stack 32.7
       Старая формула дала бы РОВНО столько же, сколько без этих пяти: она их не видела. */
    const m = resumeMatch(vac({
      techs: ['Python', 'FastAPI', 'PostgreSQL', 'Java', 'React', 'Android', 'iOS', 'PHP'],
    }));
    assert.equal(m.stack, 33);
    assert.equal(m.pct, 73);
  });

  it('многословность портала балл НЕ надувает: насыщение на трёх ядрах', () => {
    /* три ядровых теха и пять — одинаковы: covered=1, engaged=1 в обоих случаях.
       Ровно это защищает от devitjobs с его 10-20 `technologies` против единиц у hh. */
    const three = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'PostgreSQL'] }));
    const five = resumeMatch(vac({
      techs: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Kafka'],
    }));
    assert.equal(three.stack, 60);
    assert.equal(five.stack, 60);
  });

  it('один знакомый тех не даёт полного стека — нужны обе доли', () => {
    /* covered = 1/1 = 1, но engaged = 1/3 -> F1 = 0.5 -> stack 30.
       Без второй доли вакансия с единственным «Python» получала бы максимум. */
    assert.equal(resumeMatch(vac({ techs: ['Python'] })).stack, 30);
  });

  it('смежный ярус весит половину ядра', () => {
    /* covered = (1+0.5)/2 = 0.75 · engaged = 1/3 · F1 = 0.5/1.0833 = 0.4615 -> 27.7 */
    assert.equal(resumeMatch(vac({ techs: ['Python', 'Django'] })).stack, 28);
  });

  it('технологий нет вовсе -> стек 0, но опыт и удалёнка считаются', () => {
    /* язык не назван -> ×0.5 (не «чужой»): (0 + 25 + 15) × 0.5 × 1 = 20 */
    const m = resumeMatch(vac({ techs: [] }));
    assert.equal(m.stack, 0);
    assert.equal(m.lang, 0.5);
    assert.equal(m.pct, 20);
  });

  /* ── Ось языка: ПЕРВАЯ проверка, главнее стека ──────────────────────────────────── */
  it('чужой язык обрушивает балл, даже если все базы «мои»', () => {
    /* РЕГРЕССИЯ 14.08.2026, вакансия hh 136213467 «Ведущий разработчик 1С»:
       техи 1С/PostgreSQL/Kafka/RabbitMQ давали ТРИ ядровых попадания -> насыщение,
       а 1С весом 0 растворялся в знаменателе. Балл был 91 %. По кешу таких вакансий
       без единого «своего» языка с баллом >= 70 набиралось 498. */
    const m = resumeMatch(vac({ techs: ['1С', 'PostgreSQL', 'Kafka', 'RabbitMQ'] }));
    assert.equal(m.lang, 0.1);
    assert.ok(m.stack > 45, 'стек по-прежнему высокий — валит именно язык');
    assert.equal(m.pct, 9);
  });

  for (const [lang, mult] of [['Java', 0.1], ['Go', 0.1], ['PHP', 0.1], ['C#', 0.1]]) {
    it(`${lang} без Python -> язык ×${mult}`, () => {
      assert.equal(resumeMatch(vac({ techs: [lang, 'PostgreSQL', 'Redis'] })).lang, mult);
    });
  }

  it('Python рядом с чужим языком -> язык всё равно мой', () => {
    /* Фуллстек «Python + TypeScript» не должен караться за второй язык. */
    assert.equal(resumeMatch(vac({ techs: ['Python', 'TypeScript'] })).lang, 1);
  });

  it('язык из ТАЙТЛА главнее языка из описания', () => {
    /* РЕГРЕССИЯ 14.08.2026: «Инженер-разработчик C++» получал ×1.0, потому что Python
       нашёлся в ОПИСАНИИ. Тот же принцип, что у роли: верим тайтлу, не телу вакансии. */
    const m = resumeMatch(vac({ techs: ['Python', 'C++', 'PostgreSQL'], title_langs: ['C++'] }));
    assert.equal(m.lang, 0.1);
  });

  it('тайтл называет МОЙ язык -> описание уже не важно', () => {
    const m = resumeMatch(vac({ techs: ['Python', 'Java'], title_langs: ['Python'] }));
    assert.equal(m.lang, 1);
  });

  it('тайтл про язык молчит -> смотрим описание', () => {
    /* «Software Engineer (Cloud)» с Python в стеке ронять нельзя: тайтл просто
       не называет язык, а вакансия — питоновская. */
    const m = resumeMatch(vac({ techs: ['Python', 'PostgreSQL'], title_langs: [] }));
    assert.equal(m.lang, 1);
  });

  it('язык не назван -> ×0.5, а не ×0.1: это не «чужой», это «неизвестно»', () => {
    assert.equal(resumeMatch(vac({ techs: ['PostgreSQL', 'Kafka'] })).lang, 0.5);
  });

  it('не удалёнка → remote 0', () => {
    assert.equal(resumeMatch(vac({ remote_any: false })).remote, 0);
  });

  /* ── Ось желания: множитель роли ────────────────────────────────────────────────── */
  for (const [role, mult, pct] of [['Backend', 1, 93], ['Разработчик', 1, 93],
                                   ['GenAI', 0.8, 75], ['Fullstack', 0.6, 56],
                                   ['Data/ML', 0.3, 28], ['DevOps', 0.3, 28],
                                   ['Не-IT', 0, 0]]) {
    it(`роль ${role} даёт множитель ×${mult} -> ${pct}%`, () => {
      const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL'], role }));
      assert.equal(m.role, mult);
      assert.equal(m.pct, pct);
    });
  }

  it('роль вне карты -> нейтральные ×0.5, а не ноль', () => {
    /* Новая роль в ROLE_PATTERNS при необновлённом профиле не должна молча выбрасывать
       целый класс вакансий из верха ленты. */
    const m = resumeMatch(vac({ techs: ['Python', 'FastAPI', 'Docker', 'PostgreSQL'],
                               role: 'Роль-которой-нет' }));
    assert.equal(m.role, 0.5);
    assert.equal(m.pct, 47);
  });

  it('стек не спасает нежеланную роль', () => {
    /* Замер, ради которого роль и введена: у Data Eng попаданий в ядро БОЛЬШЕ, чем
       у «Разработчика» (1.57 против 1.35) — там те же Kafka/ClickHouse/PostgreSQL. */
    const dataEng = resumeMatch(vac({ techs: ['Python', 'Kafka', 'ClickHouse', 'Airflow'],
                                     role: 'Data Eng' }));
    const backend = resumeMatch(vac({ techs: ['Python', 'FastAPI'], role: 'Backend' }));
    assert.equal(dataEng.stack, 60);       /* стек идеальный */
    assert.ok(backend.pct > dataEng.pct);  /* и всё равно ниже бэкенда */
  });

  /* ── Ось опыта: доли вместо ступенек ─────────────────────────────────────────────── */
  for (const [expId, points] of [['noExperience', 25], ['between1And3', 25],
                                 ['between3And6', 15], ['moreThan6', 6]]) {
    it(`код ${expId} даёт ${points} баллов за опыт`, () => {
      assert.equal(resumeMatch(vac({ exp_id: expId })).exp, points);
    });
  }
  it('«больше 6 лет» не обнуляет опыт: это пожелание, а не запрет', () => {
    assert.ok(resumeMatch(vac({ exp_id: 'moreThan6' })).exp > 0);
  });
  it('подпись грейда вместо кода -> нейтральные 12.5', () => {
    /* Скоринг ключуется по КОДУ: подпись из config.EXP_LABELS переименовывается,
       и раньше это молча обнуляло бы баллы за опыт у всех карточек. */
    assert.equal(resumeMatch(vac({ exp_id: '1–3 года' })).exp, 13);
  });
  it('грейда нет вовсе (arbeitnow / web3) -> нейтральные 12.5', () => {
    assert.equal(resumeMatch({ techs: [], remote_any: false, role: 'Backend' }).exp, 13);
  });
});
