@echo off
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Khong tim thay Python tren may nay.
  echo Cai Python 3.11 tro len tai https://www.python.org/downloads/
  echo hoac mo PowerShell va chay:  winget install Python.Python.3.13
  echo Khi cai nho tick "Add python.exe to PATH".
  pause
  exit /b 1
)

%PY% savekeeper.py %*
if errorlevel 1 pause
