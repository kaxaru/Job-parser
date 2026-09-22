@echo off
rem Слот приоритетного пула acc2 (RFC-004). Три слота в сутки — три отдельные задачи-триггера
rem на hh_apply_acc2 (10:45, 14:15, 17:45), каждый зовёт эту обёртку.
rem Лимит за прогон — 20 (лимиты и потолок суток объяснены в cron_apply_account.bat).
call "%~dp0cron_apply_account.bat" acc2 20
