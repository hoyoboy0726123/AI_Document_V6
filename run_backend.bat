@echo off
rem 在獨立的主控台視窗啟動後端。
rem 用批次檔而非把整串指令塞進 `start`：含 && 的長指令交給 start 解析會被拆壞，
rem 結果是視窗開了、python 卻沒執行。
rem
rem 額外把 stderr 與離開碼寫進 logs\：實測後端會無聲中止（cmd 仍停在 pause、
rem python 卻已不在），而 Python 層沒有任何 traceback。離開碼是判斷「原生層
rem 崩潰（如 0xC0000005 存取違規）」還是「正常結束」的唯一線索。
title AI_Document_V5 Backend (port 8001)
cd /d "%~dp0backend"
if not exist "logs" mkdir "logs"

".venv\Scripts\python.exe" -u -m uvicorn app.main:app --host 127.0.0.1 --port 8001 2>"logs\backend_stderr.log"
set RC=%ERRORLEVEL%

echo.
echo === 後端已結束，離開碼 %RC% ===
>"logs\backend_exit_code.txt" echo %RC%
echo 離開碼已寫入 backend\logs\backend_exit_code.txt
echo stderr 已寫入 backend\logs\backend_stderr.log
echo 視窗保留以便查看訊息。
pause
