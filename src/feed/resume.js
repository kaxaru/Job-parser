/* Профиль резюме — ПРАВЬ ПОД СВОЁ РЕЗЮМЕ. Чистая логика без DOM/IO
   (тестируется в Node — tests/feed/resume.test.js). personal/resume.md — справочный текст,
   реальные правила задаются ЗДЕСЬ. Используется:
   - кнопкой «по резюме»   → matchesResume (жёсткий фильтр: показать/скрыть);
   - бейджем «% совпадения» → resumeMatch  (мягкий скоринг 0..100). */

/* ── Кнопка «по резюме»: жёсткий фильтр (стек + опыт + удалёнка). ──
   Ядро+опыт приходят из Python (config.RESUME_*) через feed-data.js — единый источник с
   autoclick-фильтром. Хардкод — фолбэк для офлайна/тестов (feed-data.js не подгружен). */
const RESUME_CORE = (typeof RESUME_CORE_PY !== 'undefined' && RESUME_CORE_PY) || ['Python', 'FastAPI'];
const RESUME_REQUIRE_REMOTE = true;                      /* true — показывать только удалёнку */

/* Условий ДВА: стек и удалёнка. Грейда здесь НЕТ намеренно (решение 07.08.2026).
   Он стоял третьим (`Без опыта` / `1–3 года`) и резал выдачу вслепую: у arbeitnow и web3
   грейда в API нет вовсе, и оба портала отсекались целиком; у talanto/hirify/getmatch он
   заполнен не везде, и там фильтр молча терял 1328 карточек. Сам по себе грейд в вакансии —
   пожелание работодателя, а не запрет, и как ЖЁСТКОЕ условие он давал больше ложных
   отсевов, чем пользы.
   Грейд по-прежнему участвует в мягком скоринге (resumeMatch, до 25 баллов) — там ему
   и место: он влияет на приоритет карточки, а не на её видимость. */
export function matchesResume(v) {
  if (RESUME_REQUIRE_REMOTE && !v.remote_any) return false;
  return v.techs.some(t => RESUME_CORE.includes(t));
}

/* ── Бейдж «% совпадения»: мягкий скоринг, стек 60 + опыт 25 + удалёнка 15 = 100. ── */
const RESUME_WEIGHTS = {
  Python: 30, FastAPI: 12, Docker: 6, PostgreSQL: 5, MySQL: 3, SQLite: 2,
  'C#': 1, React: 1,
};
/* Ключи — доменные КОДЫ грейда (domain/experience.py::Experience), а не подписи. Раньше
   тут стояли строки 'Без опыта'/'1–3 года'/…, то есть третья копия config.EXP_LABELS (после
   самого конфига и чипов шаблона): правка подписи молча обнуляла бы баллы за опыт у ВСЕХ
   карточек — скоринг тихо стал бы «неизвестный опыт -> 12». Карточка несёт код в `exp_id`
   (feed.py::build_feed), набор кодов пришпилен стражем test_feed_bridge.py. */
const EXP_SCORE = { noExperience: 25, between1And3: 25, between3And6: 10, moreThan6: 0 };

export function resumeMatch(v) {
  let stack = 0;
  for (const t of v.techs) stack += RESUME_WEIGHTS[t] || 0;
  stack = Math.min(stack, 60);
  /* грейда нет или портал его не отдал -> нейтральные 12 (не 0: пустое поле у arbeitnow
     и web3 — это качество данных портала, а не несоответствие резюме) */
  const exp    = (v.exp_id in EXP_SCORE) ? EXP_SCORE[v.exp_id] : 12;
  const remote = v.remote_any ? 15 : 0;
  return { pct: Math.round(stack + exp + remote), stack, exp, remote };
}
