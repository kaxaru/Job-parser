@echo off
rem Крон-обёртка автооткликов ДОПОЛНИТЕЛЬНОГО аккаунта hh (RFC-004). Код аккаунта — %1,
rem лимит откликов за прогон — %2 (по умолчанию 20).
rem Отличия от cron_apply.bat (основной): задаётся HR_ACCOUNT, НЕ ставится FORMS_LLM (анкеты
rem у не-основного выключены принудительно) и НЕ ставится HR_LOGIN_PHONE — ночью SMS-вход не
rem дёргаем: протухшая сессия -> прогон остановится с баннером, вход добираешь вручную
rem   set HR_LOGIN_PHONE=... ^& set HR_ACCOUNT=%~1 ^& "%PY%" hh.py autoclick --login
rem Транспорт откликов — браузер; с основным сериализуется через autoclick.lock (сдвиг сетки).
rem Шаги: автоотклики, затем инкрементальный синк статусов (best-effort — обход тысяч чатов
rem не должен задерживать отклики).
rem ЛИМИТЫ подняты 22.09.2026 (было 10/сутки, решение владельца: «10 мало»). Слотов три
rem (см. задачу hh_apply_acc2), поэтому 3 x 20 = до 60/сутки; `--daily-cap` держит потолок
rem даже при перезапуске слота (RestartOnFailure). Фактический упор — ПУЛ: пока он не пуст,
rem откликов столько, сколько в нём вакансий, дальше — сколько добавляет рынок.
rem Точностный потолок HH — HH_DAILY_APPLY_CAP (200/сутки на аккаунт); 60 выбран как
rem компромисс между скоростью и анти-детектом (второй аккаунт того же человека).
rem Логи — logs\cron_apply_%1.log. Отключить задачу: schtasks /Delete /TN hh_apply_%1 /F
if "%~1"=="" (echo Usage: cron_apply_account.bat ^<account-code^> [limit]& exit /b 2)
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
set "HR_ACCOUNT=%~1"
set "LIMIT=%~2"
if "%LIMIT%"=="" set "LIMIT=20"
"%PY%" hh.py autoclick --apply-limit %LIMIT% --daily-cap 60 >> logs\cron_apply_%~1.log 2>&1
"%PY%" hh.py autoclick --sync-status >> logs\cron_apply_%~1.log 2>&1
