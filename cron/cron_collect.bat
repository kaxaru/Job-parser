@echo off
rem Крон-обёртка: ежедневный пересбор вакансий со ВСЕХ источников config.py::SOURCES
rem (на 08.08.2026 их девять). Раньше здесь стояло «hh + hirify» — из-за этого прогон
rem казался вчетверо короче, чем он есть, и упирался в ExecutionTimeLimit PT2H неожиданно.
rem Запуск ежедневно в 12:00. Это ВНУТРИ окна откликов (10:00-23:30), и так можно: сбор ходит
rem через curl/HTTP, браузер и autoclick.lock не трогает, а запись vacancies_raw.json атомарная
rem (tmp + replace) — идущий рядом прогон откликов прочитает либо старый файл целиком, либо
rem новый, но не половину. Свежие данные подхватит ближайший слот после сбора.
rem
rem БЕЗ --force НАМЕРЕННО: флаг обходит санити-гейт COLLECT_MIN_RATIO, который не даёт
rem затереть полный кеш деградированным срезом при блокировке источника. Для запуска без
rem присмотра эту защиту снимать нельзя — один сбойный прогон уничтожил бы данные.
rem
rem CACHE_TTL_HOURS=20: при ежедневном запуске возраст кеша попадает ровно на границу 24ч,
rem и поведение стало бы непредсказуемым (то соберёт, то «кеш свежий»). 20ч гарантируют,
rem что суточный прогон всегда собирает, а ручной перезапуск через час — нет.
rem
rem Логи — logs\cron_collect.log. Отключить: schtasks /Delete /TN hh_collect /F
rem Портабельно: корень репо выводится из расположения .bat (%~dp0 = <repo>\cron\), venv —
rem в РОДИТЕЛЬСКОМ каталоге репо (..\.venv3). Если venv создан ВНУТРИ репо (.venv3\ по README) —
rem заменить "\..\.venv3" на "\.venv3" в строке set PY.
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
set CACHE_TTL_HOURS=20
rem UTF-8 stdout: шаги ниже (search-loader, ETL) печатают кириллицу обычным print(); при
rem cp1251-редиректе в лог это роняет прогон UnicodeEncodeError. Задаём один раз на весь .bat.
set PYTHONIOENCODING=utf-8
rem 0') Уборка logs/ (24.09.2026): cron_*.log > 20 МБ -> *.1 (одно поколение) и per-PID
rem     логи loguru старше 14 дней (hrwork/infrastructure/storage/logfiles.py). Вывод НЕ в
rem     cron_collect.log: cmd открывает файл редиректа ДО старта процесса, и ротировать
rem     занятый собственным редиректом лог Windows не даст. Итог — в per-PID логе loguru.
"%PY%" -m hrwork.infrastructure.storage.logfiles >nul 2>&1
"%PY%" hh.py collect >> logs\cron_collect.log 2>&1

rem ── Пересборка статистики ПОСЛЕ сбора (все шаги ниже — best-effort; читают уже записанный
rem    выше vacancies_raw.json, т.е. работают «с учётом кеша» сбора, без повторного скрейпа) ──

rem -1) Лента (data\feed-data.js) — ПЕРВОЙ, до Docker-блока: это основной рабочий интерфейс,
rem    а шаги 0/1/3 ниже занимают минуты (--wait до 240с, MSSQL-заливка ~10 мин) и могут
rem    упасть на выключенном Docker. Без этого шага лента застывала на дате РУЧНОГО прогона
rem    `hh.py feed`: 01.08.2026 в ней не было 5331 собранной вакансии и 68 вакансий с живыми
rem    чатами (инцидент: чат «работодатель без названия» на QA не находился ни по одному
rem    фильтру). Оверлей serve чинит только status/forms/chats, но не СОСТАВ ленты,
rem    поэтому одним оверлеем не обойтись.
rem    Браузер и autoclick.lock не трогает — с откликами не конфликтует.
"%PY%" hh.py feed >> logs\cron_collect.log 2>&1
rem Освежить пул acc2 (eligible.json) браузерно-независимо (RFC-004): основной вычитает его
rem целиком и должен отступать даже когда сессия acc2 в дауне. setlocal изолирует HR_ACCOUNT
rem от остальных шагов сбора (иначе dashboard/etl упали бы как «режим только для основного»).
setlocal
set "HR_ACCOUNT=acc2"
"%PY%" hh.py autoclick --dry-pool --write-eligible >> logs\cron_collect.log 2>&1
endlocal

rem 0) Автоподъём Docker-стека, от которого зависят шаги 1 и 3 (search-индекс + DWH). Поднимаем
rem    ТОЛЬКО данные+BI (postgres/clickhouse/mssql/metabase), без Airflow/Loki/Grafana. --wait
rem    блокирует до healthcheck'ов (mssql/postgres встают ~30-60с), --wait-timeout ограничивает
rem    ожидание. Требует запущенного Docker Desktop; если демон выключен — команда залогирует
rem    ошибку, шаги 1/3 тоже деградируют, но собранные данные и шаг 2 (дашборд) не пострадают.
cd /d "%REPO%\dwh_demo"
docker compose up -d --wait --wait-timeout 240 hh-postgres clickhouse mssql metabase >> ..\logs\cron_collect.log 2>&1
cd /d "%REPO%"

rem 1) Полнотекстовый индекс поиска (search_demo.vacancies) свежим срезом — иначе застывает
rem    на старом снимке. Требует hh-postgres (Docker).
"%PY%" dwh_demo\search_demo\load.py >> logs\cron_collect.log 2>&1

rem 2) Plotly-дашборд основного проекта (data\dashboard.html) — иначе показывает старый срез.
"%PY%" hh.py dashboard >> logs\cron_collect.log 2>&1

rem 3) DWH подпроекта dwh_demo: перезалить факт во все три движка (idempotent TRUNCATE+reload
rem    из текущего raw). Metabase-дашборды берут данные ЖИВЫМ SQL, поэтому re-provision не нужен —
rem    достаточно обновить факт. Требует поднятого Docker-стека (PG/CH/MSSQL); если он выключен,
rem    шаг залогирует ошибку и НЕ отменит собранные данные. MSSQL-заливка долгая (~10 мин).
cd /d "%REPO%\dwh_demo"
"%PY%" -m etl all >> ..\logs\cron_collect.log 2>&1
cd /d "%REPO%"

rem 3a) Выгрузка для КОНТЕЙНЕРНОГО пути (Airflow в dwh_demo/docker-compose.yml). Шаг 3 выше
rem     работает на хосте и читает data\ напрямую; контейнерам же смонтирован ТОЛЬКО
rem     data\export (09.08.2026: раньше монтировался весь data\ вместе с hh_state.json,
rem     browser_profile\ и контактами рекрутёров — см. dwh_demo/docs/audits/). Значит нужные
rem     два файла надо туда положить, иначе Airflow смонтирует пустоту.
rem     Копия АТОМАРНАЯ (во временное имя, затем move): контейнер читает каталог, и половина
rem     файла в нём хуже вчерашнего файла целиком.
if not exist "%REPO%\data\export" mkdir "%REPO%\data\export"
copy /y "%REPO%\data\vacancies_raw.json" "%REPO%\data\export\vacancies_raw.json.tmp" >nul 2>&1
if exist "%REPO%\data\export\vacancies_raw.json.tmp" move /y "%REPO%\data\export\vacancies_raw.json.tmp" "%REPO%\data\export\vacancies_raw.json" >nul
copy /y "%REPO%\data\fx_rates.json" "%REPO%\data\export\fx_rates.json.tmp" >nul 2>&1
if exist "%REPO%\data\export\fx_rates.json.tmp" move /y "%REPO%\data\export\fx_rates.json.tmp" "%REPO%\data\export\fx_rates.json" >nul

rem 4) Погасить BI-часть стенда. MSSQL, ClickHouse и Metabase нужны только шагам 0-3 и просмотру
rem    витрин, а круглосуточно держали ~4,6 ГБ памяти WSL ради часа работы в сутки (замер
rem    10.09.2026: mssql 2,06, metabase 1,44, clickhouse 1,15 ГБ). hh-postgres ОСТАВЛЯЕМ: на нём
rem    живёт /search в ленте (hrwork/infrastructure/search.py). Именно stop, а не down: контейнеры
rem    и тома целы, завтрашний шаг 0 поднимет их тем же up --wait, а unless-stopped сам их не
rem    вернёт. Ключ -t 60 вместо дефолтных 10 с - запас ClickHouse, которому сразу после заливки
rem    нужно время на чистую остановку. MSSQL им не спасти: PID 1 в его образе - launch_sqlservr.sh,
rem    который запускает sqlservr фоном без проброса SIGTERM, поэтому по таймауту всегда SIGKILL
rem    (так он гасился и раньше при каждом рестарте Docker). База поднимается штатным
rem    восстановлением за 4-9 с (логи 29.08 и 09.09.2026), а данные в ней - реплика, которую шаг 3
rem    перезаливает целиком. Витрины нужны вне крона - поднять руками из dwh_demo:
rem    docker compose up -d --wait clickhouse mssql metabase
cd /d "%REPO%\dwh_demo"
docker compose stop -t 60 clickhouse mssql metabase >> ..\logs\cron_collect.log 2>&1
cd /d "%REPO%"
