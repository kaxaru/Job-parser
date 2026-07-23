/* Сопроводительные письма — ПРАВЬ ШАБЛОНЫ ЗДЕСЬ.
   Чистая логика без DOM/IO (тестируется в Node — tests/feed/cover.test.js).
   Зависит только от полей вакансии {name, employer, techs}.

   Под своё резюме меняй:
   - RESUME_TECH_SET / TECH_LABEL — какие технологии из стека вакансии упоминать в письме и как подписывать;
   - COVER_TEMPLATES — сами тексты (role/company/stack подставляются автоматически в coverLetter). */

/* Технологии, которые упоминаем в письме, если они есть в стеке вакансии (+ подписи). */
const RESUME_TECH_SET = new Set(
  ['Python', 'FastAPI', 'PostgreSQL', 'MySQL', 'SQLite', 'Docker', 'C#', 'React']);
const TECH_LABEL = { 'C#': 'C#/.NET' };

function humanList(arr) {
  if (arr.length <= 1) return arr[0] || '';
  return `${arr.slice(0, -1).join(', ')} и ${arr[arr.length - 1]}`;
}
function matchedTechs(v) {
  return v.techs.filter(t => RESUME_TECH_SET.has(t)).map(t => TECH_LABEL[t] || t);
}

/* Шаблоны письма (строго по фактам personal/resume.md). Каждый — функция ({role, company, stack}) => текст;
   подстановки делает coverLetter ниже. Добавляй/удаляй варианты свободно — длина массива учитывается. */
export const COVER_TEMPLATES = [
  ({ role, company, stack }) =>
`Здравствуйте!

Заинтересовала вакансия ${role}${company}. Я backend-разработчик на Python: проектирую REST API на FastAPI (роутинг, валидация через Pydantic, авторизация, версионирование), работаю с PostgreSQL, MySQL и SQLite, поднимаю окружение в Docker. ${stack}

Собираю решение целиком — от парсинга и обработки данных до бизнес-логики, API и готового результата. Пишу чистый типизированный код, быстро разбираюсь в чужой кодовой базе и довожу задачи до production.

Буду рад обсудить, чем могу быть полезен вашей команде. Спасибо за внимание!`,

  ({ role, company, stack }) =>
`Здравствуйте!

Откликаюсь на вакансию ${role}${company}. Специализируюсь на Python-бэкенде и автоматизации: строю data-пайплайны, пишу парсеры сайтов и API, проектирую REST-сервисы на FastAPI с валидацией через Pydantic. ${stack}

Люблю доводить рутину до автоматизма и отдавать результат «под ключ» — от сбора данных до готового отчёта. Настраиваю окружение в Docker/docker-compose на Linux, работаю с PostgreSQL, MySQL и SQLite.

Будет интересно применить это у вас — расскажу подробнее на созвоне. Спасибо!`,

  ({ role, company, stack }) =>
`Здравствуйте!

Вакансия ${role}${company} привлекла возможностью отвечать за полный цикл разработки. Сейчас пишу backend на Python/FastAPI, проектирую схемы БД и миграции, упаковываю сервисы в Docker. ${stack}

Раньше работал с C# (.NET + Entity Framework + React), поэтому хорошо понимаю и серверную, и клиентскую часть, и ценность чистой архитектуры. Беру задачу от прототипа до стабильного production и не бросаю на полпути.

Буду рад обсудить, как могу усилить вашу команду. Спасибо за внимание!`,

  ({ role, company, stack }) =>
`Здравствуйте!

Хочу присоединиться к вам на позицию ${role}${company}. Коротко обо мне: Python-разработчик — REST API на FastAPI, базы PostgreSQL/MySQL/SQLite, Docker и Linux, чистый типизированный код. ${stack}

Быстро вникаю в новый код и инструменты, довожу задачи до прода. Готов показать примеры работ и обсудить детали в удобное время.

Спасибо, жду ответа!`,

  ({ role, company, stack }) =>
`Здравствуйте!

Заинтересовала вакансия ${role}${company}. Последнее время развиваюсь в backend-разработке на Python: REST API на FastAPI, автоматизация процессов, парсинг и обработка данных, окружение в Docker. ${stack}

Ищу команду, где смогу расти в архитектуре и прокачивать инженерные навыки, поэтому ваша вакансия мне особенно интересна. Умею собирать решение целиком и доводить его до стабильного состояния, беру ответственность за результат.

Буду рад рассказать, чем могу быть полезен именно вам. Спасибо!`,
];

export function coverLetter(v, idx) {
  const role    = v.name ? `«${v.name}»` : 'разработчика';
  const company = v.employer ? ` в компании ${v.employer}` : '';
  const matched = matchedTechs(v);
  const stack   = matched.length
    ? `В вашем стеке вижу ${humanList(matched)} — именно с этим работаю каждый день.`
    : 'Мой основной стек — Python, FastAPI, Docker и PostgreSQL/MySQL.';
  return COVER_TEMPLATES[idx % COVER_TEMPLATES.length]({ role, company, stack });
}
