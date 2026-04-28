@echo off
REM Build WhipOnIdle.exe on Windows.
REM Output: dist\WhipOnIdle\WhipOnIdle.exe
REM         dist\WhipOnIdle-windows.zip — share this with coworkers.
setlocal

cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip >nul
python -m pip install -r requirements-dev.txt

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

pyinstaller whip_app.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo Build failed.
    exit /b 1
)

REM Zip for distribution
powershell -NoProfile -Command "Compress-Archive -Path 'dist\WhipOnIdle\*' -DestinationPath 'dist\WhipOnIdle-windows.zip' -Force"

echo.
echo Built:   dist\WhipOnIdle\WhipOnIdle.exe
echo Zipped:  dist\WhipOnIdle-windows.zip
endlocal
