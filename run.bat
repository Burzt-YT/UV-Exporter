@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/ and make sure
    echo "Add python.exe to PATH" is checked during install, then run this again.
    pause
    exit /b 1
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo Installing required package: PySide6 ...
    python -m pip install --upgrade pip >nul
    python -m pip install PySide6
    if errorlevel 1 (
        echo [ERROR] Failed to install PySide6. Check your internet connection / pip setup.
        pause
        exit /b 1
    )
)

python -c "import msgpack" >nul 2>nul
if errorlevel 1 (
    echo Installing required package: msgpack ^(for .cdae support^) ...
    python -m pip install msgpack
    if errorlevel 1 (
        echo [ERROR] Failed to install msgpack. Check your internet connection / pip setup.
        pause
        exit /b 1
    )
)

python -c "import zstandard" >nul 2>nul
if errorlevel 1 (
    echo Installing required package: zstandard ^(for compressed .cdae support^) ...
    python -m pip install zstandard
    if errorlevel 1 (
        echo [ERROR] Failed to install zstandard. Check your internet connection / pip setup.
        pause
        exit /b 1
    )
)

python main.py
if errorlevel 1 (
    echo.
    echo [ERROR] The app exited with an error. See the message above.
    pause
)

endlocal
