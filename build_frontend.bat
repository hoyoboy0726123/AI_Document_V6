@echo off
cd /d "%~dp0"
echo [Build] Building frontend and deploying to backend\frontend_dist
echo.

REM ASCII ONLY - see the header of launch_AI_Document_V3.bat for why.
REM Re-run this after every frontend code change, then restart the server,
REM otherwise LAN users keep seeing the previously built bundle.

cd frontend
call npm run build
if errorlevel 1 (
    echo.
    echo [ERROR] Frontend build failed. See messages above.
    pause
    exit /b 1
)

cd /d "%~dp0"
if exist "backend\frontend_dist" rmdir /s /q "backend\frontend_dist"
xcopy /e /i /q "frontend\dist" "backend\frontend_dist" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy dist to backend\frontend_dist.
    pause
    exit /b 1
)

echo.
echo [Build] Done. Now run launch_AI_Document_V3.bat to start the server.
pause
