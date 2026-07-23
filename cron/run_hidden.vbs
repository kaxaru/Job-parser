' Запуск крон-.bat СО СКРЫТЫМ окном (0 = hidden, False = не ждать).
' Задачи планировщика идут как Interactive (в сессии пользователя), из-за чего .bat
' открывал видимое окно cmd и висел им весь прогон (у hh_chat это ~час: синк + loop).
' Обёртка гасит окно, не требуя смены LogonType на Password (тот запрашивал бы пароль).
' Аргумент — полный путь к .bat. Пример действия задачи:
'   wscript.exe "D:\...\cron\run_hidden.vbs" "D:\...\cron\cron_chat.bat"
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
sh.Run "cmd /c """ & WScript.Arguments(0) & """", 0, False
