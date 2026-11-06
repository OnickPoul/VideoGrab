# VideoGrab

Минималистичный загрузчик видео и аудио (оболочка над yt-dlp + ffmpeg).

## Запуск без сборки
    pip install -r requirements.txt
    python app.py

## Сборка exe
- Локально на Windows: двойной клик по `build.bat` → `dist\VideoGrab\VideoGrab.exe`
- В облаке: загрузите папку в репозиторий GitHub → вкладка Actions → «Сборка VideoGrab.exe» → Run workflow → скачайте артефакт `VideoGrab-windows`.

Если сайт перестал скачиваться — пересоберите (подтянется свежий yt-dlp) или выполните `pip install -U yt-dlp`.
