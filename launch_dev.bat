@echo off
cd /d "%~dp0"
echo [Dev] Development mode - vite dev server with hot reload
echo.
echo   NOTE: dev server binds to 127.0.0.1 only, NOT exposed to the LAN.
echo   vite 5's dev server has unpatched path-traversal issues, so for
echo   LAN users run build_frontend.bat + launch_AI_Document_V3.bat instead.
echo.

REM ASCII ONLY - see the header of launch_AI_Document_V3.bat for why.
REM OLLAMA_NUM_CTX: env var takes priority over .env; 8192 context window.
REM OLLAMA_TIMEOUT: large models are slow to cold-load / swap; avoid timeouts.
REM No --host on purpose: binding the dev server to 0.0.0.0 would expose the
REM unpatched vite/esbuild dev-server issues to the whole LAN.

start cmd /k "cd backend & set OLLAMA_NUM_CTX=8192& set OLLAMA_TIMEOUT=300& .venv\Scripts\python.exe -u -m uvicorn app.main:app --host 127.0.0.1 --port 8001"
start cmd /k "cd frontend & set VITE_API_TARGET=http://127.0.0.1:8001& npm run dev -- --port 5175"

echo   Backend : http://127.0.0.1:8001
echo   Frontend: http://localhost:5175
pause
