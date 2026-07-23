@echo off
rem Крон-обёртка: синхронизация из чатов HH каждые 3 часа (10:45, 13:45, 16:45, 19:45, 22:45).
rem Один проход по всем чатам -> статусы откликов (response_status.json) + дожурналивание
rem новых откликов (applied_log.jsonl) с реальной датой, включая сделанные РУКАМИ на hh.ru.
rem Браузер НЕ запускается и autoclick.lock НЕ берётся — работает по кукам из hh_state.json,
rem поэтому наложение на hh_apply безвредно. Смещение +45 мин осталось с тех времён, когда
rem синк ещё брал общий lock; сейчас не нужно, но и не мешает.
rem Логи — logs\cron_sync.log. Отключить: schtasks /Delete /TN hh_sync /F
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
"%PY%" hh.py autoclick --sync-status >> logs\cron_sync.log 2>&1
