@echo off
rem 執行旋轉文字重抽並「確實記錄離開碼」。
rem 直接用 `python ... | grep` 會拿到 grep 的離開碼，python 無聲死掉也看不出來
rem —— 實測就是這樣漏掉了真正的中止原因。
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" -u scripts\reextract_rotated_docs.py %* >"logs\reextract.log" 2>&1
echo EXITCODE=%ERRORLEVEL%>"logs\reextract_exit.txt"
type "logs\reextract_exit.txt"
