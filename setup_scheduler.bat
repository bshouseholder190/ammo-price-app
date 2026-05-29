@echo off
:: Creates a Windows Task Scheduler job that runs ammo_alert.py once daily at 8:00 AM.
:: Run this bat file once as Administrator, then forget about it.

set SCRIPT_DIR=%~dp0
set PYTHON_PATH=python
set TASK_NAME=AmmoAlertDaily

echo Creating scheduled task "%TASK_NAME%"...
echo Script directory: %SCRIPT_DIR%

schtasks /create /tn "%TASK_NAME%" ^
  /tr "\"%PYTHON_PATH%\" \"%SCRIPT_DIR%ammo_alert.py\"" ^
  /sc DAILY ^
  /st 08:00 ^
  /f

if %ERRORLEVEL% == 0 (
    echo.
    echo Success! Task "%TASK_NAME%" will run daily at 8:00 AM.
    echo.
    echo To change the time: open Task Scheduler and edit "%TASK_NAME%"
    echo To run it now:      schtasks /run /tn "%TASK_NAME%"
    echo To remove it:       schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo.
    echo ERROR: Could not create task. Try running this bat file as Administrator.
)
pause
