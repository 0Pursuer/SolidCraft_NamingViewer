@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
py -3 -c "import sys; raise SystemExit(sys.version_info[:2].__lt__((3, 10)))" >nul 2>&1
if not errorlevel 1 goto run_py
python -c "import sys; raise SystemExit(sys.version_info[:2].__lt__((3, 10)))" >nul 2>&1
if not errorlevel 1 goto run_python
if not exist "%ProgramFiles%\Python312\python.exe" goto no_python
"%ProgramFiles%\Python312\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2].__lt__((3, 10)))" >nul 2>&1
if not errorlevel 1 goto run_installed
:no_python
echo 需要 Python 3.10 或更新版本。
pause
exit /b 1
:run_py
py -3 server.py --open %*
goto done
:run_python
python server.py --open %*
goto done
:run_installed
"%ProgramFiles%\Python312\python.exe" server.py --open %*
:done
set "viewer_exit=%errorlevel%"
if not "%viewer_exit%"=="0" pause
exit /b %viewer_exit%
