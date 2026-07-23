@echo off
rem Крон-обёртка: поднятие резюме в поиске (HH разрешает раз в 4 часа).
rem ЗАДАЧА hh_bump ОТКЛЮЧЕНА — поднятие слито в hh_apply, раздельные задачи дрались за
rem autoclick.lock. Файл оставлен для ручного запуска: --bump-only обходит кулдаун-гейт.
rem Логи — logs\cron_bump.log.
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
"%PY%" hh.py autoclick --bump-only >> logs\cron_bump.log 2>&1
