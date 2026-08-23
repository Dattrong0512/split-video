@echo off
setlocal
cd /d "%~dp0"

py -m pip install -r requirements.txt
if errorlevel 1 goto :error

py -m PyInstaller --noconfirm --clean --onefile --windowed --name "Split Video - Ready" --icon "assets\split_video.ico" --collect-all faster_whisper --add-data "assets;assets" --distpath "release" --workpath ".pyinstaller-build" main.py
if errorlevel 1 goto :error

echo.
echo Da tao xong: release\Split Video - Ready.exe
pause
exit /b 0

:error
echo.
echo Co loi khi dong goi. Hay xem thong bao o tren.
pause
exit /b 1
