@echo off
chcp 65001 > nul
echo ========================================================
echo  Установка зависимостей AI Desktop Autopilot (MCP)
echo ========================================================
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден в переменной PATH.
    echo Установите Python 3.11-3.13 и отметьте галочку "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Создаю виртуальное окружение .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Виртуальное окружение уже существует, пропускаю.
)

echo [2/3] Обновляю pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/3] Устанавливаю проект и зависимости ...
".venv\Scripts\python.exe" -m pip install -e ".[win32]"
if errorlevel 1 (
    echo [ОШИБКА] Установка зависимостей завершилась сбоем.
    pause
    exit /b 1
)

echo.
echo [УСПЕХ] Зависимости установлены в .venv
echo Точки запуска: .venv\Scripts\autopilot-daemon.exe и autopilot-mcp.exe
echo Запустите start_daemon.bat для старта демона.
pause
