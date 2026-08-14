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

/* ── Бейдж «% совпадения» ──────────────────────────────────────────────────────────────
   ДВЕ ОСИ, и они отвечают на разные вопросы (переделка 14.08.2026):
     * стек   — насколько я к этой работе ГОТОВ;
     * роль   — насколько я туда ХОЧУ, множителем.
   Пока роль в формуле не участвовала, дата-инженер обгонял бэкендера: замер по кешу дал
   30 533 вакансии с Python, из них 10 657 (35 %) — Data/ML, Аналитик и Data Eng, причём
   по числу попаданий в ядро Data Eng ВЫШЕ «Разработчика» (1.57 против 1.35) — там те же
   Kafka, ClickHouse и PostgreSQL. Никакая настройка весов стека это не различает.

   Что было не так со старой формулой (сумма весов с отсечкой на 60):
     * это счётчик совпадений, а число техов в карточке задаёт МНОГОСЛОВНОСТЬ ПОРТАЛА
       (devitjobs даёт по 10-20 `technologies`, hh — единицы), а не соответствие вакансии;
     * не было знаменателя: «Python + 12 незнакомых технологий» и «Python» соло давали
       одинаковый балл, хотя разница ровно в том, справлюсь ли я;
     * потолок 60 схлопывал верх — всё сильное становилось неразличимым;
     * ступенька опыта роняла 15 очков разом на границе «1-3» / «3-6».

   Ярусы стека и множители ролей инжектятся из Python (config.RESUME_STACK_TIERS /
   RESUME_ROLE_FIT, поверх них resume_profile.json). Хардкод ниже — ДОСЛОВНАЯ копия
   ДЕФОЛТА из config, а не профиля пользователя: профиль перекрывает дефолт и в офлайн-копию
   не попадает. Расхождение копии с конфигом ловит страж
   tests/backend/presentation/test_feed_bridge.py — иначе тесты фронта проверяли бы одну
   модель, а лента считала другой. */
const TIERS = (typeof RESUME_TIERS_PY !== 'undefined' && RESUME_TIERS_PY) || {
  Python: 1, FastAPI: 1, SQLAlchemy: 1, Alembic: 1, PostgreSQL: 1, Redis: 1,
  RabbitMQ: 1, Kafka: 1, ClickHouse: 1, MySQL: 1, Airflow: 1, Celery: 1,
  Django: 0.5, RAG: 0.5, LangChain: 0.5,
  Docker: 0.2, Kubernetes: 0.2, Nginx: 0.2, AWS: 0.2, GCP: 0.2, Azure: 0.2,
  'Yandex Cloud': 0.2, 'GitLab CI': 0.2, 'GitHub Actions': 0.2, Terraform: 0.2,
  Ansible: 0.2, MongoDB: 0.2, Elasticsearch: 0.2, SQLite: 0.2, MSSQL: 0.2,
  'Oracle DB': 0.2,
};
const ROLE_FIT = (typeof RESUME_ROLE_FIT_PY !== 'undefined' && RESUME_ROLE_FIT_PY) || {
  Backend: 1, 'Разработчик': 1, Architect: 1,
  GenAI: 0.8, Fullstack: 0.6,
  DevOps: 0.3, 'Data Eng': 0.3, 'Data/ML': 0.3, 'Аналитик': 0.3,
  QA: 0.15, Frontend: 0.15, Mobile: 0.15, Security: 0.15, Embedded: 0.15,
  Gamedev: 0.15, 'Менеджер': 0.15, 'Дизайнер': 0.15,
  'Не-IT': 0,
};
/* Сколько попаданий в ядро считать полной вовлечённостью: вакансия никогда не перечисляет
   весь стек владельца, медиана по кешу — 1-2 теха из ядра. */
const CORE_SAT = (typeof RESUME_CORE_SAT_PY !== 'undefined' && RESUME_CORE_SAT_PY) || 3;
/* Языки: какие теги вообще СЧИТАЮТСЯ языком (домен, config.LANG_KEYS) и какие из них МОИ
   (якорь профиля — RESUME_CORE ∩ LANG_KEYS). Второе выводится из ядра, а не задаётся
   отдельно: ядро уже отвечает на «на чём я пишу» для жёсткого фильтра, и второй источник
   того же ответа неминуемо разъехался бы. */
const LANG_KEYS = (typeof LANG_KEYS_PY !== 'undefined' && LANG_KEYS_PY) || [
  '1С', 'C#', 'C++', 'Go', 'Java', 'JavaScript', 'Kotlin', 'PHP', 'Python',
  'Ruby', 'Rust', 'Scala', 'Swift', 'TypeScript',
];
const RESUME_LANGS = (typeof RESUME_LANGS_PY !== 'undefined' && RESUME_LANGS_PY) || ['Python'];
const LANG_FIT = (typeof RESUME_LANG_FIT_PY !== 'undefined' && RESUME_LANG_FIT_PY)
  || { own: 1, none: 0.5, foreign: 0.1 };
/* Роль, которой нет в карте (новая в ROLE_PATTERNS, а профиль не обновлён) -> 0.5:
   нейтрально. Ноль молча выкинул бы целый класс вакансий из верха ленты. */
const ROLE_FIT_UNKNOWN = 0.5;

/* Ключи — доменные КОДЫ грейда (domain/experience.py::Experience), а не подписи. Раньше
   тут стояли строки 'Без опыта'/'1–3 года'/…, то есть третья копия config.EXP_LABELS (после
   самого конфига и чипов шаблона): правка подписи молча обнуляла бы баллы за опыт у ВСЕХ
   карточек. Карточка несёт код в `exp_id` (feed.py::build_feed), набор кодов пришпилен
   стражем test_feed_bridge.py.
   Доли, а не очки: «больше 6 лет» — это ПОЖЕЛАНИЕ работодателя, а не запрет (тем же
   доводом грейд убрали из жёсткого фильтра 07.08.2026), поэтому не ноль. */
const EXP_FIT = { noExperience: 1, between1And3: 1, between3And6: 0.6, moreThan6: 0.25 };
const EXP_FIT_UNKNOWN = 0.5;   /* поля нет у arbeitnow/web3 — это качество данных портала */

/* Доля ИХ требований, которую я закрываю, и доля МОЕГО ядра, которую вакансия задействует.
   Сводятся гармоническим средним: высокий балл требует ОБОИХ. Одна только первая доля
   поощряла бы вакансии с одним-единственным знакомым тегом, одна вторая — вернула бы
   портальную многословность. */
function stackFit(techs) {
  if (!techs?.length) return { fit: 0, covered: 0, core: 0 };
  let gain = 0, core = 0;
  for (const t of techs) {
    const w = TIERS[t] || 0;
    gain += w;
    if (w >= 1) core++;
  }
  const covered = gain / techs.length;
  const engaged = Math.min(1, core / CORE_SAT);
  const fit = (covered + engaged) ? (2 * covered * engaged) / (covered + engaged) : 0;
  return { fit, covered, core };
}

/* ЯЗЫК — первая проверка, и она главнее стека.
   Три исхода, и средний важен: «язык не назван» это НЕ «язык чужой» — у части вакансий
   стек в тексте не перечислен вовсе, и карать их наравне с Java-вакансией нельзя.
   Без этой оси «Ведущий разработчик 1С» (1С, PostgreSQL, Kafka, RabbitMQ) набирал 91 %:
   три ядровых попадания давали насыщение, а 1С весом 0 растворялся в знаменателе. */
function langFit(v) {
  /* ТАЙТЛ ГЛАВНЕЕ ОПИСАНИЯ. Тот же принцип, по которому роль считается по тайтлу:
     тело вакансии называет чужие технологии в требованиях и в рассказе о компании.
     Стоило это конкретной ошибки — «Инженер-разработчик C++» получал ×1.0 за «свой
     язык», потому что Python нашёлся в ОПИСАНИИ. Молчит тайтл — смотрим описание;
     там же лечится обратный случай: «Software Engineer (Cloud)» про язык не говорит,
     но по стеку это Python-вакансия, и ронять её нельзя. */
  const inTitle = v.title_langs || [];
  if (inTitle.length) {
    return inTitle.some(t => RESUME_LANGS.includes(t)) ? LANG_FIT.own : LANG_FIT.foreign;
  }
  const langs = (v.techs || []).filter(t => LANG_KEYS.includes(t));
  if (langs.some(t => RESUME_LANGS.includes(t))) return LANG_FIT.own;
  return langs.length ? LANG_FIT.foreign : LANG_FIT.none;
}

export function resumeMatch(v) {
  const s = stackFit(v.techs);
  const stack = 60 * s.fit;
  const exp = 25 * (v.exp_id in EXP_FIT ? EXP_FIT[v.exp_id] : EXP_FIT_UNKNOWN);
  const remote = v.remote_any ? 15 : 0;
  const role = v.role in ROLE_FIT ? ROLE_FIT[v.role] : ROLE_FIT_UNKNOWN;
  const lang = langFit(v);
  const pct = Math.round((stack + exp + remote) * lang * role);
  return {
    pct,
    stack: Math.round(stack), exp: Math.round(exp), remote,
    lang, role,                            /* множители — показываются в подсказке бейджа */
    covered: s.covered, core: s.core,      /* для отладки и тестов формулы */
  };
}
