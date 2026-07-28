# RFC-003: LLM-заполнение форм-опросников (inline авто-submit, no-execution)

**Статус:** Implemented Phase 1 (2026-07-22, Тип 1 внутри HH) · **Связано:** `security.md`, `apply.md`, RFC-002

> **Поправка 2026-07-22 (решение владельца):** форма — ЧАСТЬ отклика, не отдельный ручной шаг.
> Заполнение переехало inline в `autoclick.apply_one` (гейт `FORMS_LLM`); при полноте тул сам жмёт
> «Откликнуться». Прежняя модель «человек жмёт submit» (`--fill`/`_field_consent`) снята. Что это
> меняет для безопасности — в §«Инвариант» и §«Риск b»: машинный инвариант ДЕРЖИТСЯ, человек-рубеж
> заменён на **completeness-gate** (шлём, только если КАЖДОЕ поле закрыто из фактов/словаря).

## Инвариант безопасности (машинный — держится контуром, не послушанием модели)

**Машинная безопасность = ДВА рантайм-факта** (независимый критик-агент подтвердил: путь
«вредоносная форма → исполнение кода на машине» построить не удалось) — **не изменились поправкой**:
1. **Нет tool-канала** — `openrouter.py::chat_json` шлёт `httpx.post` БЕЗ `tools`/`functions`/
   `tool_choice`, возвращает строку `message.content` или `None`. Выход не `import`/`eval`/dispatch.
2. **Стоки — только `print()` и Playwright `.fill()`/`.select_option()`/`.check()`** — DOM-литералы,
   значение не парсится как код/шелл/имя файла. В `form_fill`/`form_read`/`forms` нет
   `eval`/`exec`/`compile`/`os.system`/`os.popen`/`subprocess`/`__import__` (AST-гвард `test_form_fill`).

Prompt injection worst case = плохой ТЕКСТ, ушедший работодателю в поле анкеты. **Машина не тронута
ни при каком раскладе.** Что заменило человек-submit как рубеж против выдумки/неполноты — §«Риск b».

## Поток (Тип 1) — inline в отклике

```
apply_one (крон):
  кнопка «тест/опрос» ИЛИ /applicant/vacancy_response + task-question
   ├─ FORMS_LLM НЕ задан (дефолт) --> ApplyOutcome.FORM --> store.add_form   [как раньше, крон-путь без изменений]
   └─ FORMS_LLM=1 --> forms.try_autofill(page, cand):
        form_read.extract_fields  →  на поле: match_answer(словарь) → suggest(TEXT/TEXTAREA) → answer_field(LLM) → DECLINE
        ├─ пусто извлечено ИЛИ хоть один пробел --> лог пробелов --> return False --> ApplyOutcome.FORM (в очередь)
        └─ ВСЕ поля закрыты --> _fill_field(.fill/.check/.select_option) + _fill_cover + click «Откликнуться»
                              --> apply_one верифицирует «Вы откликнулись» --> ApplyOutcome.APPLIED

hh.py forms  — тот же авто-путь по НАКОПЛЕННОЙ очереди (бэклог / закрытые словарём пробелы); --dry = превью.
```

**Поправка 28.07: флаг включён в кроне.** Ветка «FORMS_LLM НЕ задан» выше — уже не про боевое
расписание: `cron/cron_apply.bat` выставляет `FORMS_LLM=1`. Причина — очередь росла быстрее, чем
вычерпывалась: каждый слот докидывал в неё новые опросники, за ночь 59 -> 60 при 21 снятом вручную.
Исходное «не в кроне» writing-time опиралось на пустой словарь; сейчас `form_answers` (141 запись)
закрывает 177 полей из 179, и пробелы стали исключением. Гейт как VO не менялся, изменилось только
значение в боевом окружении; инвариант «шлём ТОЛЬКО при полноте» действует по-прежнему.

## Модули (слой application)
- `form_read.py` — `FieldType`/`FormField` VO (`options`/`opt_values`), `extract_fields(page)`
  (task-body: question + radio/checkbox группы, дедуп, лимиты, всё в `contextlib.suppress`).
- `form_fill.py` — `match_answer` (словарь `form_answers` → подпись+own, membership для radio/checkbox),
  `answer_field` (LLM грунт на резюме — личные факты), `answer_quiz` (LLM ЭКСПЕРТНО на технические
  вопросы-с-вариантами QA/фреймворки — возвращает НОМЕР варианта → индекс→опция, робастно к длинным
  подписям; личное без факта → DECLINE; fallback после `answer_field` только для option-полей),
  `_sanitize` (control/длина/фенсы всегда; `<>`/URL для TEXTAREA
  разрешены — сток DOM-литерал; TEXT строже), `_match_option` (select/radio строго из опций),
  `build_resume_ctx` (allowlist фактов + `resume.md` через `_scrub_pii` — БЕЗ гражданства/города/
  контактов/зарплаты). Salary/office_city/citizenship — не уходят провайдеру.
- `forms.py` — `try_autofill(page, cand)` (ядро: резолв всех полей → completeness-gate → заполнить+
  письмо+«Откликнуться»; пробел → False), `run(dry, only, headless, cover_mode)` (процессор очереди),
  `_is_hh` origin-гард, `_resolve` (словарь→suggest→LLM), `_fill_field` (DOM-литерал; «Свой вариант» →
  парный `textarea[name=…_text]`), `_fill_cover` (письмо в поле формы).
- `config.py`: `FORMS_ENABLED` (env `FORMS_LLM`, truthy-set, **OFF по умолчанию**), `FORM_MODEL`/
  `FORM_TIMEOUT`/`FORM_MAX_TOKENS`/`FORM_MAX_ANSWER_LEN`. `hh.py`: `Mode.FORMS`, `--dry`.
- `followup.remove_form_vacancy(id)` + `store.remove_form` — убрать из очереди после отправки.
- **Кеш структуры анкет** `data/forms_cache.json` (`followup.cache_form`/`load_form_cache`/
  `cached_form_ids`, фасад `store.*`): `forms.sweep()` (CLI `hh.py forms --sweep`) разово снимает
  вопросы/опции всех форм очереди в кеш, пропуская уже собранные (`--refresh` — пересобрать). Мёртвые
  формы (0 полей, истёкшие вакансии) кешируются как `empty` и повторно не открываются. submit НЕ жмётся.
- **Дренаж бэклога:** `forms.clean_queue()` (CLI `hh.py forms --clean`, без браузера) убирает из очереди
  мёртвые (кеш `empty`/`error`) и уже-откликнутые (applied_log/marks) — вакансии остаются в feed
  («в архив»). `hh.py forms --limit N` — максимум ОТПРАВЛЕННЫХ за запуск (батчи, анти-бот/суточный
  лимит). `run()` пропускает уже-откликнутые — защита от повторного отклика между чисткой и дренажем.
- `resume_profile.json::form_answers` — словарь типовых ответов (офис/курс/часы/переезд/военник/ТК/
  формат/образование): `{q:regex, a:подпись, own?:текст «свой вариант»}`. Пополняется по логам
  пробелов — это и есть контур настройки. Подпись обязана быть среди опций формы (membership),
  поэтому одну и ту же тему можно закрыть несколькими записями с разными подписями.
- **Зарплата — ТОЛЬКО детерминированный код** (`form_fill.is_salary_q`/`detect_grade`/`salary_target`/
  `salary_option`), НЕ LLM: зарплатный вопрос никогда не уходит провайдеру (приватность). Грейд по
  названию вакансии -> нижняя граница `answers.salary_by_grade` -> если пол вакансии (`Salary.frm`)
  выше, берём его -> опция-диапазон (наибольший вход <= цели). Не разобрали -> пробел (человек), в LLM
  НЕ идём. «Зарплатная карта» (способ выплаты) — не сюда, обычным путём словаря/LLM.

## Риски (ситуация → закрывающий рубеж)
- **(a) Инъекция → исполнение кода.** НЕВОЗМОЖНО (§Инвариант): нет tool-канала + стоки print/DOM-литерал.
- **(b) Инъекция → выдумка / неполное извлечение → авто-отправка.** Человек-submit снят поправкой.
  Замена: **completeness-gate** — шлём ТОЛЬКО если каждое поле закрыто из фактов/словаря (`match_answer`/
  `suggest`/`answer_field`), radio/checkbox строго `in options` (membership), пусто/пробел → НЕ шлём,
  лог + очередь. Ответы грунтованы фактами (не свободная фантазия), select-инъекция невозможна.
  **Остаточный риск принят владельцем** («Авто-submit при полноте»): открытый текст (TEXTAREA) может
  быть неточен и уходит без просмотра — компенсируется грунтовкой + `--dry`-превью очереди.
- **(c) Чужой URL уводит сессионный браузер.** origin-гард `hh.ru` (Фаза 1); non-persistent контекст (Фаза 2).
- **(d) Утечка резюме в провайдера.** allowlist фактов + `_scrub_pii(resume.md)` (тест
  `test_resume_md_pii_scrubbed_from_ctx`): гражданство/город/дата рожд./контакты/зарплата не уходят.
- **(e) Инъекция опций select/radio.** membership: только `in options`, иначе DECLINE → пробел → очередь.
- **Повторная отправка.** `remove_form_vacancy` + `mark_applied` после подтверждённого submit.
- **Сервис недоступен/OFF/нет ключа** → поле не резолвится → пробел → в очередь (никогда не «пустой» submit).

## Фаза 2 (отложена, Тип 2 внешние формы) — assistive-only
`ChatKind.SCREENING`/`REDIRECT` (telegram/чужой сайт): тул сам URL не открывает (человек подтверждает);
извлечение — в **отдельном non-persistent `browser.new_context()`** (не HH-профиль — иначе кража сессии);
**submit внешних форм тул НЕ делает**. Extract+draft, человек навигирует и отправляет сам.

## Верификация
`hh.py forms --dry` → поля+резолвинг очереди без действий; `FORMS_LLM=1 hh.py forms` → авто-обработка
очереди; inline — `FORMS_LLM=1 hh.py autoclick --apply-limit 1 --headed` на вакансии с анкетой;
`pytest -q` (флаг OFF → зелёные, +51 form-тест: security data-flow, PII-скраб, completeness-gate).
