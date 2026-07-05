@echo off
rem Double-click me: read-only Redis connectivity check (PING + INFO, writes nothing).
cd /d "%~dp0"
".venv\Scripts\python.exe" "backend\check_redis.py"
echo.
pause
