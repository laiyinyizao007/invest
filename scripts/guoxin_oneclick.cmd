@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=..\invest\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\guoxin_oneclick.py %*
echo.
echo Exit code %errorlevel%
pause
