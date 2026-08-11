@echo off
cd /d "%~dp0"
echo [Launcher] AI_Document_V6 - production mode (single port 8001, LAN accessible)
echo.

REM ---------------------------------------------------------------------------
REM ASCII ONLY. Do not put non-ASCII characters anywhere in this file, not even
REM in REM comments: CMD reads .bat with the system ANSI code page (cp950 on
REM zh-TW Windows). UTF-8 bytes misread as cp950 can decode into '&' or other
REM command separators, which REM does NOT protect against - it corrupts the
REM parser and silently breaks if/echo blocks.
REM See DEPLOY_NOTES.md for the Chinese explanation.
REM ---------------------------------------------------------------------------

REM Production mode: the backend serves the built frontend directly
REM (backend/standalone_launcher.py mounts frontend_dist). Same origin, so no
REM vite proxy is needed and vite/esbuild are not in the serving path at all.
REM For hot-reload development use launch_dev.bat instead.

if not exist "backend\frontend_dist\index.html" (
    echo [ERROR] backend\frontend_dist\index.html not found.
    echo         Run build_frontend.bat first to build the frontend.
    echo.
    pause
    exit /b 1
)

REM OLLAMA_NUM_CTX: env var takes priority over .env; 8192 context window.
REM OLLAMA_TIMEOUT: large models are slow to cold-load / swap; avoid timeouts.
set OLLAMA_NUM_CTX=8192
set OLLAMA_TIMEOUT=300

echo   Local: http://127.0.0.1:8001
echo   LAN  : http://^<this-machine-ip^>:8001   (run "ipconfig" to find it)
echo.
echo   Press Ctrl+C to stop.
echo.

cd backend
.venv\Scripts\python.exe -u -m uvicorn standalone_launcher:app --host 0.0.0.0 --port 8001
pause
