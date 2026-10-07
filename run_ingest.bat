@echo off
REM Phase 1: rebuild processed data for the last 56 days.
REM Output is shown here and saved to reports\ingest_log.txt
cd /d "%~dp0"
REM Use the project environment (.venv) if it exists, otherwise Anaconda's Python.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\ProgramData\anaconda3\python.exe"
echo Using Python: %PY%
echo Deleting old processed data (it is rebuilt from the archive)...
if exist data\processed rmdir /s /q data\processed
if not exist reports mkdir reports
powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%PY%' -u -m evops.ingest --days 56 2>&1 | Tee-Object -FilePath 'reports\ingest_log.txt'"
echo.
echo Finished. Log saved to reports\ingest_log.txt - you can close this window.
pause
