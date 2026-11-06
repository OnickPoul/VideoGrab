# -*- coding: utf-8 -*-
"""VideoGrab — минималистичный загрузчик видео и аудио по ссылке.

Оболочка над yt-dlp (1800+ сайтов + прямые ссылки на медиафайлы) и ffmpeg.
Возможности: выбор качества, обрезка до скачивания, выбор папки.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog

# В режиме --windowed у exe нет консоли: stdout/stderr равны None.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))

import customtkinter as ctk  # noqa: E402
import yt_dlp  # noqa: E402
from yt_dlp.utils import DownloadCancelled, download_range_func  # noqa: E402

APP_NAME = "VideoGrab"
CONFIG_PATH = Path.home() / ".videograb.json"
BEST = "Лучшее доступное"
AUDIO_FORMATS = {"MP3": "mp3", "M4A": "m4a", "WAV": "wav", "Оригинал": None}
BROWSERS = {"Не использовать": None, "Firefox": "firefox", "Chrome": "chrome",
            "Edge": "edge", "Brave": "brave", "Opera": "opera"}


# ───────────────────────────── Ядро (без GUI) ─────────────────────────────

def base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def find_deno() -> str | None:
    """JS-рантайм, без которого yt-dlp не умеет разбирать YouTube."""
    exe = "deno.exe" if os.name == "nt" else "deno"
    bundled = base_dir() / exe
    if bundled.exists():
        return str(bundled)
    try:
        import deno
        return deno.find_deno_bin()
    except Exception:
        return shutil.which("deno")


def parse_time(text: str) -> float | None:
    """'90', '1:30', '01:02:03.5' -> секунды. Пустая строка -> None."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)  # ValueError при мусоре
    if total < 0:
        raise ValueError(text)
    return total


def fmt_time(sec: float) -> str:
    sec = round(max(float(sec), 0.0), 1)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ГБ"


def video_heights(info: dict) -> list[int]:
    hs = {f["height"] for f in info.get("formats") or []
          if f.get("height") and f.get("vcodec") not in (None, "none")}
    return sorted(hs, reverse=True)


def audio_bitrates(info: dict) -> list[int]:
    rates = {int(f["abr"]) for f in info.get("formats") or []
             if f.get("abr") and f.get("acodec") not in (None, "none")}
    return sorted(rates, reverse=True)


def height_label(h: int) -> str:
    names = {4320: " · 8K", 2160: " · 4K", 1440: " · 2K", 1080: " · Full HD", 720: " · HD"}
    return f"{h}p{names.get(h, '')}"


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _common_opts(browser: str | None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "logger": _SilentLogger(), "socket_timeout": 20}
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    deno = find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def probe_duration(media_url: str) -> float | None:
    """Длительность прямого медиафайла, если сайт её не сообщил (читаем через ffmpeg)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not media_url.startswith(("http://", "https://")):
        return None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", media_url], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=25, creationflags=flags)
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def fetch_info(url: str, browser: str | None = None) -> dict:
    opts = _common_opts(browser)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise yt_dlp.utils.DownloadError("Unsupported URL: пустой плейлист")
        info = entries[0]
    if not info.get("duration"):
        direct = info.get("url") or next((f.get("url") for f in info.get("formats") or []
                                          if f.get("protocol") in ("http", "https")), None)
        info["duration"] = probe_duration(direct) if direct else None
    return info


@dataclass
class Job:
    url: str
    outdir: str
    mode: str = "video"            # "video" | "audio"
    height: int | None = None      # потолок по высоте, None = лучшее
    abr: int | None = None         # потолок по битрейту источника, None = лучшее
    audio_codec: str | None = "mp3"  # None = оставить как есть
    start: float | None = None
    end: float | None = None
    browser: str | None = None


def build_opts(job: Job) -> dict:
    opts = _common_opts(job.browser)
    tags = []
    if job.mode == "video" and job.height:
        tags.append(f"{job.height}p")
    if job.start is not None or job.end is not None:
        end = "конец" if job.end is None else f"{job.end:g}"
        tags.append(f"фрагмент {job.start or 0:g}-{end}")
    suffix = f" [{', '.join(tags)}]" if tags else ""
    opts.update({
        "outtmpl": os.path.join(job.outdir, f"%(title).150B{suffix}.%(ext)s"),
        "overwrites": True,
        "merge_output_format": "mp4",
        "format_sort": ["res", "ext:mp4:m4a"],  # по возможности H.264/AAC — дружит с монтажками
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
    })
    if job.mode == "audio":
        cap = f"bestaudio[abr<={job.abr}]/" if job.abr else ""
        opts["format"] = f"{cap}bestaudio/best"
        if job.audio_codec:
            quality = {"mp3": "0", "m4a": "256"}.get(job.audio_codec, "0")
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": job.audio_codec,
                                       "preferredquality": quality}]
    else:
        h = f"[height<={job.height}]" if job.height else ""
        opts["format"] = f"bv*{h}+ba/b{h}/bv*{h}/b"
    if job.start is not None or job.end is not None:
        end = job.end if job.end is not None else float("inf")
        opts["download_ranges"] = download_range_func(None, [(job.start or 0.0, end)])
        opts["force_keyframes_at_cuts"] = True  # точный рез, а не по ближайшему кадру
    return opts


def run_job(job: Job, emit, cancel: threading.Event) -> str | None:
    """Скачивает файл. emit(kind, *args) — отправка статуса в интерфейс."""
    def on_progress(d):
        if cancel.is_set():
            raise DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            audio_part = (d.get("info_dict") or {}).get("vcodec") == "none"
            bits = ["Загрузка аудио" if audio_part else "Загрузка"]
            frac = min(done / total, 1.0) if total else None
            if frac is not None:
                bits[0] += f" {frac * 100:.0f}%"
            elif done:
                bits.append(fmt_size(done))
            if d.get("speed"):
                bits.append(f"{fmt_size(d['speed'])}/с")
            if d.get("eta") is not None:
                bits.append(f"осталось {fmt_dur(d['eta'])}")
            emit("progress", frac, "  ·  ".join(bits))
        elif d.get("status") == "finished":
            emit("progress", None, "Обработка файла…")

    def on_post(d):
        if d.get("status") == "started":
            emit("progress", None, "Обработка файла…")

    opts = build_opts(job)
    opts["progress_hooks"] = [on_progress]
    opts["postprocessor_hooks"] = [on_post]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(job.url, download=True))
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    downloads = info.get("requested_downloads") or []
    return downloads[0].get("filepath") if downloads else None


def friendly_error(err: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).replace("ERROR: ", "").strip()
    low = text.lower()
    if "unsupported url" in low:
        return ("Не нашёл на этой странице видео или аудио. Сайт может не поддерживаться, "
                "а сам файл — быть скрытым за оплатой или входом в аккаунт.")
    if any(k in low for k in ("sign in", "log in", "login", "private", "confirm you", "members-only")):
        return ("Сайт просит войти в аккаунт или подтвердить, что вы не робот. "
                "Выберите свой браузер в поле «Cookies браузера» и повторите.")
    if any(k in low for k in ("getaddrinfo", "urlopen error", "timed out", "connection")):
        return "Нет связи с сайтом. Проверьте интернет и ссылку."
    if "ffmpeg" in low and "not found" in low:
        return "Не найден ffmpeg — он нужен для объединения дорожек и обрезки."
    if "requested format is not available" in low:
        return "Выбранное качество недоступно. Попробуйте «Лучшее доступное»."
    if "unable to download" in low or "http error 403" in low or "http error 404" in low:
        return "Сайт отказал в доступе к файлу (" + text[:160] + ")."
    return "Не получилось: " + text[:300]


def reveal_in_folder(path: str):
    path = os.path.abspath(path)
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ───────────────────────────── Интерфейс ─────────────────────────────

BG = ("#F2F3F5", "#16181C")
CARD = ("#FFFFFF", "#20232A")
FIELD = ("#ECEEF1", "#2A2E36")
SEG_ON = ("#FFFFFF", "#444A55")
HOVER = ("#DFE2E6", "#343943")
LINE = ("#D9DCE1", "#2F333B")
TEXT = ("#15181D", "#ECEEF1")
MUTED = ("#6B7280", "#8E96A3")
ACCENT = ("#2F6FED", "#4C8DF6")
ACCENT_HOVER = ("#2459C4", "#3A78E0")
OK = ("#1E8E5A", "#4CC38A")
BAD = ("#C0392B", "#EF7A6E")
FAMILY = "Segoe UI"


class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("system")
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        self.geometry("720x780")
        self.minsize(660, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.info: dict | None = None
        self.url_ok = ""
        self.duration = 0.0
        self.qmap: dict[str, int | None] = {BEST: None}
        self.last_file: str | None = None
        self._indeterminate = False
        self.sl: dict[str, ctk.CTkSlider] = {}
        self.en: dict[str, ctk.CTkEntry] = {}

        cfg = load_config()
        self.outdir = ctk.StringVar(value=cfg.get("outdir") or str(Path.home() / "Downloads"))
        self.trim_on = ctk.BooleanVar(value=False)

        self._build()
        self._lock(True)
        self.after(80, self._poll)

    # ── построение окна ──
    def font(self, size=13, weight="normal"):
        return ctk.CTkFont(family=FAMILY, size=size, weight=weight)

    def _menu(self, parent, values, command=None, width=170):
        return ctk.CTkOptionMenu(
            parent, values=values, command=command, width=width, height=36, corner_radius=10,
            fg_color=FIELD, button_color=FIELD, button_hover_color=HOVER, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_hover_color=FIELD, dropdown_text_color=TEXT,
            font=self.font(), dropdown_font=self.font())

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        px = 30

        ctk.CTkLabel(self, text=APP_NAME, font=self.font(28, "bold"), text_color=TEXT
                     ).grid(row=0, column=0, sticky="w", padx=px, pady=(26, 0))
        ctk.CTkLabel(self, text="Видео и аудио по ссылке: выберите качество, обрежьте и сохраните.",
                     font=self.font(13), text_color=MUTED
                     ).grid(row=1, column=0, sticky="w", padx=px, pady=(2, 0))

        # ссылка
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=px, pady=(20, 0))
        row.grid_columnconfigure(0, weight=1)
        self.url = ctk.CTkEntry(row, height=44, corner_radius=10, fg_color=CARD, border_color=LINE,
                                border_width=1, text_color=TEXT, font=self.font(14),
                                placeholder_text="Вставьте ссылку на видео или аудио")
        self.url.grid(row=0, column=0, sticky="ew")
        self.url.bind("<Return>", lambda e: self.analyze())
        ctk.CTkButton(row, text="Вставить", width=96, height=44, corner_radius=10, font=self.font(),
                      fg_color=FIELD, hover_color=HOVER, text_color=TEXT, command=self.paste
                      ).grid(row=0, column=1, padx=(8, 0))
        self.btn_find = ctk.CTkButton(row, text="Найти", width=84, height=44, corner_radius=10,
                                      font=self.font(13, "bold"), fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, text_color="white", command=self.analyze)
        self.btn_find.grid(row=0, column=2, padx=(8, 0))

        self.info_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=640,
                                     font=self.font(13), text_color=MUTED)
        self.info_lbl.grid(row=3, column=0, sticky="ew", padx=px + 2, pady=(12, 0))

        # параметры
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        card.grid(row=4, column=0, sticky="ew", padx=px, pady=(14, 0))
        card.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=18, pady=16)
        inner.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        self.mode = ctk.CTkSegmentedButton(
            top, values=["Видео", "Аудио"], command=self._mode_changed, height=36, corner_radius=10,
            fg_color=FIELD, unselected_color=FIELD, unselected_hover_color=HOVER,
            selected_color=SEG_ON, selected_hover_color=SEG_ON, text_color=TEXT, font=self.font())
        self.mode.set("Видео")
        self.mode.grid(row=0, column=0)
        self.quality = self._menu(top, [BEST], width=190)
        self.quality.grid(row=0, column=1, padx=(10, 0))
        self.afmt = self._menu(top, list(AUDIO_FORMATS), width=120)
        self.afmt.set("MP3")
        self.afmt.grid(row=0, column=2, padx=(10, 0))
        self.afmt.grid_remove()

        sw_row = ctk.CTkFrame(inner, fg_color="transparent")
        sw_row.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        sw_row.grid_columnconfigure(0, weight=1)
        self.sw_trim = ctk.CTkSwitch(sw_row, text="Обрезать фрагмент", variable=self.trim_on,
                                     command=self._toggle_trim, progress_color=ACCENT,
                                     button_color=("#FFFFFF", "#D5D9E0"), fg_color=("#C9CDD3", "#3A3F48"),
                                     text_color=TEXT, font=self.font())
        self.sw_trim.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(sw_row, text="Cookies браузера", text_color=MUTED, font=self.font(12)
                     ).grid(row=0, column=1, padx=(0, 8))
        self.cookies = self._menu(sw_row, list(BROWSERS), width=150)
        self.cookies.grid(row=0, column=2)

        self.trim_box = ctk.CTkFrame(inner, fg_color="transparent")
        self.trim_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.trim_box.grid_columnconfigure(1, weight=1)
        for r, (key, label) in enumerate((("start", "Начало"), ("end", "Конец"))):
            ctk.CTkLabel(self.trim_box, text=label, width=60, anchor="w", text_color=MUTED,
                         font=self.font()).grid(row=r, column=0, pady=5)
            s = ctk.CTkSlider(self.trim_box, from_=0, to=1, height=16, fg_color=FIELD,
                              progress_color=FIELD, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                              command=lambda v, k=key: self._on_slide(k, v))
            s.set(0 if key == "start" else 1)
            s.grid(row=r, column=1, sticky="ew", padx=10)
            e = ctk.CTkEntry(self.trim_box, width=100, height=32, justify="center", corner_radius=8,
                             fg_color=FIELD, border_width=0, text_color=TEXT, font=self.font())
            e.grid(row=r, column=2)
            e.bind("<Return>", lambda ev, k=key: self._on_entry(k))
            e.bind("<FocusOut>", lambda ev, k=key: self._on_entry(k))
            self.sl[key], self.en[key] = s, e
        ctk.CTkLabel(self.trim_box, text="Время вводится как 1:30 или 01:02:03.5. Лишнее не скачивается.",
                     text_color=MUTED, font=self.font(12), anchor="w"
                     ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.trim_box.grid_remove()

        # папка
        frow = ctk.CTkFrame(self, fg_color="transparent")
        frow.grid(row=5, column=0, sticky="ew", padx=px, pady=(14, 0))
        frow.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(frow, textvariable=self.outdir, height=40, corner_radius=10, fg_color=CARD,
                     border_color=LINE, border_width=1, text_color=TEXT, font=self.font()
                     ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(frow, text="Папка…", width=96, height=40, corner_radius=10, font=self.font(),
                      fg_color=FIELD, hover_color=HOVER, text_color=TEXT, command=self.pick_folder
                      ).grid(row=0, column=1, padx=(8, 0))

        # прогресс
        self.bar = ctk.CTkProgressBar(self, height=6, corner_radius=3, fg_color=LINE, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.grid(row=6, column=0, sticky="ew", padx=px, pady=(22, 0))
        self.status = ctk.CTkLabel(self, text="Вставьте ссылку и нажмите «Найти».", anchor="w",
                                   justify="left", wraplength=650, font=self.font(13), text_color=MUTED)
        self.status.grid(row=7, column=0, sticky="ew", padx=px + 2, pady=(8, 0))

        # кнопки
        brow = ctk.CTkFrame(self, fg_color="transparent")
        brow.grid(row=8, column=0, sticky="ew", padx=px, pady=(16, 26))
        brow.grid_columnconfigure(0, weight=1)
        self.btn_dl = ctk.CTkButton(brow, text="Скачать", height=48, corner_radius=12,
                                    font=self.font(15, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    text_color="white", text_color_disabled=MUTED,
                                    command=self.start_download)
        self.btn_dl.grid(row=0, column=0, sticky="ew")
        self.btn_side = ctk.CTkButton(brow, text="Отмена", width=170, height=48, corner_radius=12,
                                      font=self.font(14), fg_color=FIELD, hover_color=HOVER,
                                      text_color=TEXT, command=self.cancel_download)
        self.btn_side.grid(row=0, column=1, padx=(10, 0))
        self.btn_side.grid_remove()

    # ── вспомогательное ──
    def _lock(self, locked: bool):
        state = "disabled" if locked else "normal"
        for w in (self.mode, self.quality, self.afmt, self.sw_trim):
            w.configure(state=state)
        self.btn_dl.configure(state=state, fg_color=FIELD if locked else ACCENT)

    def _browser(self) -> str | None:
        return BROWSERS.get(self.cookies.get())

    def _progress(self, frac, text=None, color=None):
        if frac is None:
            if not self._indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self._indeterminate = True
        else:
            if self._indeterminate:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self._indeterminate = False
            self.bar.set(frac)
        if text is not None:
            self.status.configure(text=text, text_color=color or MUTED)

    def _idle(self, text, color=None, frac=0.0):
        self.busy = False
        self._progress(frac, text, color)
        self.btn_find.configure(state="normal")
        self._lock(self.info is None)

    # ── действия ──
    def paste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        if text:
            self.url.delete(0, "end")
            self.url.insert(0, text)
            self.analyze()

    def pick_folder(self):
        path = filedialog.askdirectory(initialdir=self.outdir.get() or None, title="Куда сохранять")
        if path:
            self.outdir.set(os.path.normpath(path))

    def analyze(self):
        url = self.url.get().strip()
        if not url or self.busy:
            return
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
            url = "https://" + url
            self.url.delete(0, "end")
            self.url.insert(0, url)
        self.busy = True
        self.info = None
        self.btn_side.grid_remove()
        self._lock(True)
        self.btn_find.configure(state="disabled")
        self.info_lbl.configure(text="")
        self._progress(None, "Ищу видео и аудио по ссылке…")

        def work():
            try:
                self.q.put(("info", url, fetch_info(url, self._browser_cached)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", friendly_error(e)))

        self._browser_cached = self._browser()
        threading.Thread(target=work, daemon=True).start()

    def _on_info(self, url, info):
        self.info, self.url_ok = info, url
        self.duration = float(info.get("duration") or 0)
        title = info.get("title") or "Без названия"
        source = info.get("extractor_key") or ""
        source = "" if source.lower() == "generic" else source
        meta = ", ".join(x for x in (source, f"длительность {fmt_dur(self.duration)}" if self.duration else "") if x)
        self.info_lbl.configure(text=title + (f"\n{meta}" if meta else ""), text_color=TEXT)

        self.heights, self.abrs = video_heights(info), audio_bitrates(info)
        audio_exts = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "wma", "weba", "aiff"}
        has_video = bool(self.heights) or any(
            f.get("vcodec") != "none" and f.get("ext") not in audio_exts
            for f in (info.get("formats") or [info]))
        self.mode.set("Видео" if has_video else "Аудио")
        self.last_file = None

        if self.duration:
            steps = min(max(int(self.duration * 10), 1), 20000)
            for k, v in (("start", 0), ("end", self.duration)):
                self.sl[k].configure(from_=0, to=self.duration, number_of_steps=steps, state="normal")
                self.sl[k].set(v)
            self._sync()
        else:
            for k in ("start", "end"):
                self.sl[k].configure(state="disabled")
                self.en[k].delete(0, "end")
            self.en["start"].insert(0, "00:00.0")
        self._idle("Выберите качество и нажмите «Скачать».")
        self._mode_changed(self.mode.get())

    def _mode_changed(self, mode):
        if mode == "Аудио":
            labels = [f"{r} кбит/с" for r in self.abrs]
            self.qmap = {BEST: None, **{l: r for l, r in zip(labels, self.abrs)}}
            self.afmt.grid()
        else:
            labels = [height_label(h) for h in self.heights]
            self.qmap = {BEST: None, **{l: h for l, h in zip(labels, self.heights)}}
            self.afmt.grid_remove()
        self.quality.configure(values=list(self.qmap))
        self.quality.set(BEST)

    # обрезка
    def _toggle_trim(self):
        if self.trim_on.get():
            self.trim_box.grid()
        else:
            self.trim_box.grid_remove()

    def _on_slide(self, key, _value):
        a, b = self.sl["start"].get(), self.sl["end"].get()
        if key == "start" and a > b:
            self.sl["end"].set(a)
        if key == "end" and b < a:
            self.sl["start"].set(b)
        self._sync()

    def _sync(self):
        for k in ("start", "end"):
            self.en[k].delete(0, "end")
            self.en[k].insert(0, fmt_time(self.sl[k].get()))

    def _on_entry(self, key):
        raw = self.en[key].get()
        try:
            t = parse_time(raw)
        except ValueError:
            t = "bad"
        if not self.duration:           # длительность неизвестна: слайдеров нет, доверяем вводу
            if t in ("bad",):
                self.en[key].delete(0, "end")
            return
        if t == "bad":
            self._sync()
            return
        if t is None:
            t = 0.0 if key == "start" else self.duration
        t = min(t, self.duration)
        self.sl[key].set(t)
        self._on_slide(key, t)

    def _read_trim(self):
        """-> (start, end) или (None, None), если обрезка не нужна. ValueError с текстом для пользователя."""
        if not self.trim_on.get():
            return None, None
        try:
            start = parse_time(self.en["start"].get()) or 0.0
            end = parse_time(self.en["end"].get())
        except ValueError:
            raise ValueError("Время указано неверно. Пример: 1:30 или 00:01:30.5") from None
        if self.duration and end is not None and end >= self.duration - 0.05:
            end = None
        if self.duration and start >= self.duration:
            raise ValueError("Начало фрагмента позже конца ролика.")
        if end is not None and end <= start:
            raise ValueError("Конец фрагмента должен быть позже начала.")
        if start < 0.05:
            start = None
        return start, end

    # загрузка
    def start_download(self):
        if self.busy or not self.info:
            return
        try:
            start, end = self._read_trim()
        except ValueError as e:
            self._progress(0, str(e), BAD)
            return
        outdir = self.outdir.get().strip()
        try:
            Path(outdir).mkdir(parents=True, exist_ok=True)
        except Exception:
            self._progress(0, "Не удалось открыть папку сохранения. Выберите другую.", BAD)
            return
        save_config({"outdir": outdir})

        audio = self.mode.get() == "Аудио"
        value = self.qmap.get(self.quality.get())
        job = Job(url=self.url_ok, outdir=outdir, mode="audio" if audio else "video",
                  height=None if audio else value, abr=value if audio else None,
                  audio_codec=AUDIO_FORMATS[self.afmt.get()], start=start, end=end,
                  browser=self._browser())

        self.busy = True
        self.cancel.clear()
        self.last_file = None
        self._lock(True)
        self.btn_find.configure(state="disabled")
        self.btn_side.configure(text="Отмена", command=self.cancel_download)
        self.btn_side.grid()
        self._progress(None, "Подключаюсь…")

        def work():
            try:
                path = run_job(job, lambda *a: self.q.put(a), self.cancel)
                self.q.put(("done", path))
            except Exception as e:  # noqa: BLE001
                if self.cancel.is_set() or isinstance(e, DownloadCancelled):
                    self.q.put(("cancelled",))
                elif (start is not None or end is not None) and "ffmpeg exited" in str(e):
                    self.q.put(("error", "Сервер не позволяет скачать только фрагмент. Снимите «Обрезать "
                                         "фрагмент» и скачайте целиком — обрезать можно в монтажной программе."))
                else:
                    self.q.put(("error", friendly_error(e)))

        threading.Thread(target=work, daemon=True).start()

    def cancel_download(self):
        self.cancel.set()
        self.btn_side.configure(state="disabled")
        self._progress(None, "Отменяю…")

    def _on_progress(self, frac, text):
        self._progress(frac, text)

    def _on_done(self, path):
        self.last_file = path
        self.btn_side.configure(state="normal")
        if path and os.path.exists(path):
            self.btn_side.configure(text="Показать в папке", command=lambda: reveal_in_folder(path))
            self.btn_side.grid()
            self._idle(f"Готово: {os.path.basename(path)}", OK, 1.0)
        else:
            self.btn_side.grid_remove()
            self._idle("Готово. Файл сохранён в выбранной папке.", OK, 1.0)

    def _on_cancelled(self):
        self.btn_side.grid_remove()
        self.btn_side.configure(state="normal")
        self._idle("Загрузка отменена.")

    def _on_error(self, text):
        self.btn_side.grid_remove()
        self.btn_side.configure(state="normal")
        self._idle(text, BAD)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                getattr(self, "_on_" + msg[0])(*msg[1:])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_close(self):
        self.cancel.set()
        self.destroy()


def selftest(out_path: str) -> int:
    """Проверка, что в сборку упакованы ffmpeg и JS-рантайм (запускается из CI: VideoGrab.exe --selftest файл)."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    res: dict = {"yt_dlp": yt_dlp.version.__version__, "ffmpeg": find_ffmpeg(), "deno": find_deno()}
    try:
        import yt_dlp_ejs  # noqa: F401
        res["yt_dlp_ejs"] = True
    except Exception:
        res["yt_dlp_ejs"] = False
    for name, flag in (("ffmpeg", "-version"), ("deno", "--version")):
        try:
            r = subprocess.run([res[name], flag], capture_output=True, timeout=60, creationflags=flags)
            res[name + "_runs"] = r.returncode == 0
        except Exception:
            res[name + "_runs"] = False
    Path(out_path).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0 if all(res[k] for k in ("yt_dlp_ejs", "ffmpeg_runs", "deno_runs")) else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    App().mainloop()
