@echo off
REM Download and convert every archive day since 28 Jan 2026 that is not done yet.
REM Days already converted are skipped, so this is safe to re-run.
REM Output is shown here and saved to reports\backfill_log.txt
cd /d "%~dp0"
REM Use the project environment (.venv) if it exists, otherwise Anaconda's Python.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\ProgramData\anaconda3\python.exe"
echo Using Python: %PY%
if not exist reports mkdir reports
powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%PY%' -u -m evops.ingest --start 2026-01-28 2>&1 | Tee-Object -FilePath 'reports\backfill_log.txt'"
echo.
echo Finished. Log saved to reports\backfill_log.txt - you can close this window.
pause
