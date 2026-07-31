@echo off
rem 在獨立的主控台視窗啟動前端開發伺服器。
rem 與 run_backend.bat 同樣的理由：由工具工作階段以隱藏視窗啟動的程序會被回收，
rem 表現為服務無聲中止。
title AI_Document_V5 Frontend (port 5175)
cd /d "%~dp0frontend"
set VITE_API_TARGET=http://127.0.0.1:8001
call npm run dev -- --host --port 5175
echo.
echo === 前端已結束（離開碼 %ERRORLEVEL%）。視窗保留以便查看錯誤訊息。 ===
pause
