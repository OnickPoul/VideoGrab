@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === VideoGrab: сборка exe ===
where py >nul 2>nul || (echo Не найден Python. Установите Python 3.11+ с python.org и поставьте галочку "Add to PATH". & pause & exit /b 1)

py -3 -m venv .venv || goto :err
call .venv\Scripts\activate.bat || goto :err
python -m pip install -U pip || goto :err
pip install -r requirements.txt pyinstaller || goto :err

for /f "delims=" %%i in ('python -c "import deno; print(deno.find_deno_bin())"') do set "DENO=%%i"
echo JS-рантайм: %DENO%

pyinstaller --noconfirm --clean --windowed --name VideoGrab ^
  --collect-all customtkinter --collect-all imageio_ffmpeg --collect-all yt_dlp_ejs ^
  --add-binary "%DENO%;." app.py || goto :err

echo.
echo Готово! Программа: dist\VideoGrab\VideoGrab.exe
echo Всю папку dist\VideoGrab можно копировать и архивировать целиком.
pause
exit /b 0

:err
echo.
echo Что-то пошло не так. Скопируйте текст ошибки выше.
pause
exit /b 1
