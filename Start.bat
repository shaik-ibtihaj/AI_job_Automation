@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" goto usepython
"%~dp0.venv\Scripts\python.exe" "%~dp0src\bootstrap.py" %*
goto finished
:usepython
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 goto usepy
python "%~dp0src\bootstrap.py" %*
goto finished
:usepy
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 goto nopython
py -3 "%~dp0src\bootstrap.py" %*
goto finished
:nopython
echo ERROR: Python 3.11 or newer is required. Install Python and enable its launcher or PATH option.
set "run_result=1"
goto done
:finished
set "run_result=%errorlevel%"
if "%run_result%"=="2" echo Workflow completed with capability or approval blockers. Read output\last_run_report.txt.
if not "%run_result%"=="0" if not "%run_result%"=="2" echo Workflow stopped with an error. Read the displayed message and output\last_run_report.txt if available.
:done
if "%~1"=="" pause
exit /b %run_result%
