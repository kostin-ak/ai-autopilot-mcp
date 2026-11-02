@echo off
chcp 65001 > nul
echo ========================================================
echo  Запуск AI Desktop Autopilot Daemon...
echo ========================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Виртуальное окружение .venv не найдено.
    echo Сначала запустите install.bat
    pause
    exit /b 1
)

".venv\Scripts\python.exe" autopilot_daemon.py
pause
