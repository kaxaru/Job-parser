@echo off
rem Крон-обёртка: поднятие резюме + автоотклики (РЕАЛЬНЫЕ заявки работодателям) в ОДНОМ
rem браузерном прогоне. Раздельные задачи (hh_apply/hh_bump) делили один профиль и только
rem дрались за autoclick.lock, поэтому слиты — флага --apply-only больше нет.
rem Поднятие гейтится кулдауном (bump_state.json, лимит HH раз в 4ч): на слотах, где ещё
rem рано, тяжёлая /applicant/resumes не грузится вовсе.
rem Окно 10:00-23:30, каждые 90 мин = 10 прогонов x 20 = ровно 200/день (лимит HH),
rem дневной потолок дополнительно enforced в apply_quota.json.
rem Логи — logs\cron_apply.log. Отключить: schtasks /Delete /TN hh_apply /F
cd /d "%~dp0.."
set "REPO=%CD%"
set "PY=%REPO%\..\.venv3\Scripts\python.exe"
"%PY%" hh.py autoclick --apply-limit 20 >> logs\cron_apply.log 2>&1
