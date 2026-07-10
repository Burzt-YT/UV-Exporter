@echo off
setlocal enabledelayedexpansion

rem Clears Python's compiled-bytecode cache for this project: every
rem __pycache__ folder (at any depth) plus any stray top-level .pyc/.pyo
rem files, so a stale cached module can't silently keep running instead of
rem your latest saved edits.
rem
rem Run this from anywhere -- it always targets the folder this .bat file
rem itself lives in (%~dp0), not the current working directory.

set "ROOT=%~dp0"
set /a dirs_removed=0
set /a files_removed=0

echo Clearing Python bytecode cache under:
echo   %ROOT%
echo.

for /f "delims=" %%D in ('dir /ad /b /s "%ROOT%__pycache__" 2^>nul') do (
    echo Removing folder: %%D
    rd /s /q "%%D" 2>nul
    set /a dirs_removed+=1
)

for /f "delims=" %%F in ('dir /b /s "%ROOT%*.pyc" 2^>nul') do (
    del /f /q "%%F" 2>nul
    set /a files_removed+=1
)

for /f "delims=" %%F in ('dir /b /s "%ROOT%*.pyo" 2^>nul') do (
    del /f /q "%%F" 2>nul
    set /a files_removed+=1
)

echo.
echo Done. Removed !dirs_removed! __pycache__ folder(s) and !files_removed! stray .pyc/.pyo file(s).
echo.
pause
