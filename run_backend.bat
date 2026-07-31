@echo off
rem 在獨立的主控台視窗啟動後端。
rem 用批次檔而非把整串指令塞進 `start`：含 && 的長指令交給 start 解析會被拆壞，
rem 結果是視窗開了、python 卻沒執行。
title AI_Document_V5 Backend (port 8001)
cd /d "%~dp0backend"
".venv\Scripts\python.exe" -u -m uvicorn app.main:app --host 127.0.0.1 --port 8001
echo.
echo === 後端已結束（離開碼 %ERRORLEVEL%）。視窗保留以便查看錯誤訊息。 ===
pause
