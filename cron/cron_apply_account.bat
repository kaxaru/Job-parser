@echo off
rem Крон-обёртка автооткликов ДОПОЛНИТЕЛЬНОГО аккаунта hh (RFC-004). Код аккаунта — %1,
rem лимит откликов за прогон — %2 (по умолчанию 10).
rem Отличия от cron_apply.bat (основной): задаётся HR_ACCOUNT, НЕ ставится FORMS_LLM (анкеты
rem у не-основного выключены принудительно) и НЕ ставится HR_LOGIN_PHONE — ночью SMS-вход не
rem дёргаем: протухшая сессия -> прогон остановится с баннером, вход добираешь вручную
rem   set HR_LOGIN_PHONE=... ^& set HR_ACCOUNT=%~1 ^& "%PY%" hh.py autoclick --login
rem Транспорт откликов — браузер; с основным сериализуется через autoclick.lock (сдвиг сетки).
rem Шаги: автоотклики, затем инкрементальный синк статусов (best-effort — обход тысяч чатов
rem не должен задерживать отклики).
rem ЛИМИТЫ: 10 за прогон (было 20 с 22.09.2026 — «10 мало»; откатано 23.09.2026 по замеру).
rem Замер по журналу acc2: HH перестаёт подтверждать отклики, когда за СКОЛЬЗЯЩИЕ 24ч их
rem накопилось ~48 — 7 из 7 прошли при базе 42, следующая попытка (база 48) отказана, и все
rem 33 после неё тоже; при базе 28 отклик снова прошёл. Суточная квота этого упора не видит
rem (в тот момент показывала 189 свободных из 200). Поэтому цель прогона считается как
rem min(лимит, дневной остаток, остаток окна) — см. config.HH_APPLY_ROLLING_CAP и
rem autoclick._apply_batch. Три слота x 10 = до 30/сутки, то есть до упора не доводит.
rem `--daily-cap 60` остаётся верхней страховкой (перезапуск слота, ручные прогоны).
rem Логи — logs\cron_apply_%1.log. Отключить задачу: schtasks /Delete /TN hh_apply_%1 /F
if "%~1"=="" (echo Usage: cron_apply_account.bat ^<account-code^> [limit]& exit /b 2)
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
set "HR_ACCOUNT=%~1"
set "LIMIT=%~2"
if "%LIMIT%"=="" set "LIMIT=10"
"%PY%" hh.py autoclick --apply-limit %LIMIT% --daily-cap 60 >> logs\cron_apply_%~1.log 2>&1
"%PY%" hh.py autoclick --sync-status >> logs\cron_apply_%~1.log 2>&1
