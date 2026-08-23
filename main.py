from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import asyncio
import base64
import hashlib
import uuid
from datetime import timedelta
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import imageio_ffmpeg
import edge_tts
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw, ImageTk
from pydub import AudioSegment
from yt_dlp import YoutubeDL


APP_NAME = "Split Video"
PREVIEW_SIZE = (720, 405)
MIN_RECT = 12
GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"
WHISPER_MODEL_DEFAULT = "small"
VIETNAMESE_VOICE_DEFAULT = "vi-VN-HoaiMyNeural"


@dataclass
class SubtitleCue:
    start: float
    end: float
    original: str
    vietnamese: str
    speaker: str = "S1"
    gender: str = "unknown"


def ffmpeg_filter_path(path: Path) -> str:
    """Escape an absolute Windows path used inside an FFmpeg filter option."""
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def ass_time(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


def srt_time(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    milliseconds = int(round((value - int(value)) * 1000))
    if milliseconds == 1000:
        seconds += 1
        milliseconds = 0
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


def work_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SplitVideo"
    base.mkdir(parents=True, exist_ok=True)
    return base


def timestamp(value: float) -> str:
    value = max(0.0, float(value))
    hours, rem = divmod(int(value), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def valid_time(text: str) -> float:
    text = text.strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    bits = text.split(":")
    if len(bits) not in (2, 3):
        raise ValueError("Thời gian phải có dạng giây, MM:SS hoặc HH:MM:SS.")
    nums = [float(bit) for bit in bits]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


@dataclass
class Rect:
    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0

    def clamp(self) -> "Rect":
        self.left = min(max(self.left, 0.0), 1.0)
        self.right = min(max(self.right, 0.0), 1.0)
        self.top = min(max(self.top, 0.0), 1.0)
        self.bottom = min(max(self.bottom, 0.0), 1.0)
        if self.right - self.left < 0.02:
            self.right = min(1.0, self.left + 0.02)
        if self.bottom - self.top < 0.02:
            self.bottom = min(1.0, self.top + 0.02)
        return self

    def is_full(self) -> bool:
        return self.left <= 0.001 and self.top <= 0.001 and self.right >= 0.999 and self.bottom >= 0.999


class RectangleEditor(tk.Canvas):
    """Canvas preview where crop or blur region can be edited by drag handles."""

    def __init__(self, master: tk.Misc, mode_changed):
        super().__init__(master, width=PREVIEW_SIZE[0], height=PREVIEW_SIZE[1], bg="#111827", highlightthickness=0)
        self.mode_changed = mode_changed
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.image_box = (0, 0, *PREVIEW_SIZE)
        self.crop = Rect()
        self.blur = Rect(0.68, 0.05, 0.96, 0.14)
        self.subtitle = Rect(0.10, 0.73, 0.90, 0.93)
        self.extra_blurs: list[Rect] = []
        self.active = "crop"
        self.dragging: str | None = None
        self.start = (0, 0)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set_active(self, active: str) -> None:
        self.active = active
        self.mode_changed(active)
        self.redraw()

    def set_image(self, image: Image.Image) -> None:
        self.image = image.copy()
        self.redraw()

    def reset_crop(self) -> None:
        self.crop = Rect()
        self.redraw()

    def reset_blur(self) -> None:
        self.blur = Rect(0.68, 0.05, 0.96, 0.14)
        self.redraw()

    def reset_subtitle(self) -> None:
        self.subtitle = Rect(0.10, 0.73, 0.90, 0.93)
        self.redraw()

    def _display_rect(self) -> tuple[int, int, int, int]:
        cw, ch = PREVIEW_SIZE
        if not self.image:
            return 0, 0, cw, ch
        ratio = min(cw / self.image.width, ch / self.image.height)
        w, h = int(self.image.width * ratio), int(self.image.height * ratio)
        return (cw - w) // 2, (ch - h) // 2, (cw + w) // 2, (ch + h) // 2

    def _rect_pixels(self, rect: Rect) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.image_box
        return (x0 + rect.left * (x1 - x0), y0 + rect.top * (y1 - y0),
                x0 + rect.right * (x1 - x0), y0 + rect.bottom * (y1 - y0))

    def redraw(self) -> None:
        self.delete("all")
        self.image_box = self._display_rect()
        if self.image:
            x0, y0, x1, y1 = self.image_box
            img = self.image.copy()
            img.thumbnail((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(img)
            self.create_image(x0, y0, image=self.photo, anchor="nw")
        else:
            self.create_text(PREVIEW_SIZE[0] // 2, PREVIEW_SIZE[1] // 2, text="Mở video hoặc dán liên kết Douyin để xem trước", fill="#d1d5db", font=("Segoe UI", 12))
            return
        self._draw_rect(self.crop, "#22d3ee", "CROP", self.active == "crop")
        self._draw_rect(self.blur, "#fb7185", "LÀM MỜ", self.active == "blur")
        self._draw_rect(self.subtitle, "#a3e635", "PHỤ ĐỀ", self.active == "subtitle")
        for region in self.extra_blurs:
            self._draw_rect(region, "#fda4af", "MỜ THÊM", False)

    def _draw_rect(self, rect: Rect, color: str, title: str, selected: bool) -> None:
        x0, y0, x1, y1 = self._rect_pixels(rect)
        width = 3 if selected else 1
        self.create_rectangle(x0, y0, x1, y1, outline=color, width=width)
        self.create_rectangle(x0, max(0, y0 - 22), min(PREVIEW_SIZE[0], x0 + 76), y0, fill=color, outline="")
        self.create_text(x0 + 38, y0 - 11, text=title, fill="#111827", font=("Segoe UI", 8, "bold"))
        if selected:
            for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                self.create_rectangle(px - 5, py - 5, px + 5, py + 5, fill=color, outline="#ffffff")

    def _target(self) -> Rect:
        if self.active == "crop":
            return self.crop
        if self.active == "subtitle":
            return self.subtitle
        return self.blur

    def _press(self, event: tk.Event) -> None:
        if not self.image:
            return
        rect = self._target()
        x0, y0, x1, y1 = self._rect_pixels(rect)
        handle = 12
        points = {"tl": (x0, y0), "tr": (x1, y0), "bl": (x0, y1), "br": (x1, y1)}
        for name, (px, py) in points.items():
            if abs(event.x - px) <= handle and abs(event.y - py) <= handle:
                self.dragging = name
                self.start = (event.x, event.y)
                return
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            self.dragging = "move"
            self.start = (event.x, event.y)

    def _drag(self, event: tk.Event) -> None:
        if not self.dragging or not self.image:
            return
        x0, y0, x1, y1 = self.image_box
        w, h = x1 - x0, y1 - y0
        rect = self._target()
        nx = min(max((event.x - x0) / w, 0.0), 1.0)
        ny = min(max((event.y - y0) / h, 0.0), 1.0)
        if self.dragging == "move":
            dx, dy = (event.x - self.start[0]) / w, (event.y - self.start[1]) / h
            rw, rh = rect.right - rect.left, rect.bottom - rect.top
            rect.left = min(max(rect.left + dx, 0), 1 - rw)
            rect.top = min(max(rect.top + dy, 0), 1 - rh)
            rect.right, rect.bottom = rect.left + rw, rect.top + rh
            self.start = (event.x, event.y)
        else:
            if "l" in self.dragging: rect.left = min(nx, rect.right - 0.02)
            if "r" in self.dragging: rect.right = max(nx, rect.left + 0.02)
            if "t" in self.dragging: rect.top = min(ny, rect.bottom - 0.02)
            if "b" in self.dragging: rect.bottom = max(ny, rect.top + 0.02)
        rect.clamp()
        self.redraw()

    def _release(self, _event: tk.Event) -> None:
        self.dragging = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x900")
        self.minsize(980, 820)
        icon = resource_path("assets/split_video.ico")
        if icon.exists():
            self.iconbitmap(default=str(icon))
        self.configure(bg="#0f172a")
        self.source: Path | None = None
        self.duration = 0.0
        self.preview_time = 0.0
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Videos" / "Split Video"))
        self.url = tk.StringVar()
        self.cookie_browser = tk.StringVar(value="Chrome")
        self.cookie_file: Path | None = None
        self.status = tk.StringVar(value="Sẵn sàng. Mở video cục bộ hoặc dán liên kết Douyin.")
        self.progress = tk.DoubleVar(value=0)
        self.zoom = tk.DoubleVar(value=1.0)
        self._last_zoom = 1.0
        self.blur_enabled = tk.BooleanVar(value=False)
        self.blur_regions: list[Rect] = []
        self.watermark_text = tk.StringVar()
        self.watermark_texts: list[str] = []
        self.watermark_image: Path | None = None
        self.watermark_scale = tk.DoubleVar(value=0.25)
        self.gemini_key = tk.StringVar(value=os.environ.get("GEMINI_API_KEY", ""))
        self.gemini_model = tk.StringVar(value=os.environ.get("GEMINI_MODEL", GEMINI_MODEL_DEFAULT))
        self.whisper_model = tk.StringVar(value=os.environ.get("WHISPER_MODEL", WHISPER_MODEL_DEFAULT))
        self.dub_enabled = tk.BooleanVar(value=True)
        self.burn_subtitles = tk.BooleanVar(value=True)
        self.speaker_voice_vars: dict[str, tk.StringVar] = {}
        self.speaker_controls: ttk.Frame | None = None
        self.subtitle_cues: list[SubtitleCue] = []
        self.dubbing_audio: Path | None = None
        self.subtitle_job_dir: Path | None = None
        self.export_button: ttk.Button | None = None
        self.ai_button: ttk.Button | None = None
        self._style()
        self._build_ui()
        self.after(100, self._poll_events)

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#0f172a")
        style.configure("Panel.TFrame", background="#172033")
        style.configure("TLabel", background="#172033", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#0f172a", foreground="#f8fafc", font=("Segoe UI", 23, "bold"))
        style.configure("Hint.TLabel", background="#0f172a", foreground="#94a3b8", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 7))
        style.configure("Accent.TButton", background="#06b6d4", foreground="#082f49")
        style.map("Accent.TButton", background=[("active", "#22d3ee")])
        style.configure("TEntry", fieldbackground="#0b1220", foreground="#f8fafc", padding=7)
        style.configure("TProgressbar", troughcolor="#0b1220", background="#22d3ee")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Split Video", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Douyin → nhận diện lời thoại → Gemini hiệu đính/dịch Việt → che subtitle gốc → phụ đề & lồng tiếng Việt • render CPU", style="Hint.TLabel").pack(anchor="w", pady=(2, 16))

        source = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        source.pack(fill="x")
        ttk.Label(source, text="Liên kết Douyin / video nguồn").grid(row=0, column=0, sticky="w")
        ttk.Entry(source, textvariable=self.url, width=76).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(5, 0))
        ttk.Button(source, text="Tải video", style="Accent.TButton", command=self.download).grid(row=1, column=1, pady=(5, 0))
        ttk.Button(source, text="Mở file…", command=self.open_file).grid(row=1, column=2, padx=(8, 0), pady=(5, 0))
        self.ai_button = ttk.Button(source, text="Nhận diện AI…", style="Accent.TButton", command=self.create_vietnamese_subtitles)
        self.ai_button.grid(row=1, column=3, padx=(8, 0), pady=(5, 0))
        cookies = ttk.Frame(source, style="Panel.TFrame")
        cookies.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(cookies, text="Cookie Douyin:").pack(side="left")
        ttk.Combobox(cookies, textvariable=self.cookie_browser, values=("Chrome", "Edge", "Firefox", "Không dùng cookie"), state="readonly", width=18).pack(side="left", padx=(6, 8))
        ttk.Button(cookies, text="Chọn cookies.txt…", command=self.choose_cookie_file).pack(side="left")
        self.cookie_file_label = ttk.Label(cookies, text="Dùng cookie trình duyệt đang chọn", foreground="#94a3b8")
        self.cookie_file_label.pack(side="left", padx=(7, 0))
        ttk.Label(source, textvariable=self.status, foreground="#67e8f9", anchor="w").grid(row=3, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        source.columnconfigure(0, weight=1)

        main = ttk.Frame(outer)
        main.pack(fill="both", expand=True, pady=14)
        left = ttk.Frame(main, style="Panel.TFrame", padding=14)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(main, style="Panel.TFrame", padding=14, width=330)
        right.pack(side="left", fill="y", padx=(14, 0))

        ttk.Label(left, text="Xem trước và chỉnh vùng").pack(anchor="w")
        self.editor = RectangleEditor(left, self._mode_note)
        self.editor.pack(pady=(8, 8))
        controls = ttk.Frame(left, style="Panel.TFrame")
        controls.pack(fill="x")
        ttk.Button(controls, text="Chỉnh Crop", command=lambda: self.editor.set_active("crop")).pack(side="left")
        ttk.Button(controls, text="Chỉnh vùng mờ", command=lambda: self.editor.set_active("blur")).pack(side="left", padx=7)
        ttk.Button(controls, text="Khung phụ đề", command=lambda: self.editor.set_active("subtitle")).pack(side="left")
        ttk.Button(controls, text="Đặt lại Crop", command=self.editor.reset_crop).pack(side="left")
        ttk.Button(controls, text="Đặt lại vùng mờ", command=self.editor.reset_blur).pack(side="left", padx=7)
        ttk.Button(controls, text="Đặt lại phụ đề", command=self.editor.reset_subtitle).pack(side="left")
        ttk.Button(controls, text="Thêm vùng mờ", command=self.add_blur_region).pack(side="left")
        zoom_row = ttk.Frame(left, style="Panel.TFrame")
        zoom_row.pack(fill="x", pady=(10, 0))
        ttk.Label(zoom_row, text="Zoom khung hình").pack(side="left")
        ttk.Scale(zoom_row, from_=1.0, to=2.0, variable=self.zoom, orient="horizontal", command=lambda _v: self._apply_zoom()).pack(side="left", fill="x", expand=True, padx=10)
        ttk.Label(zoom_row, text="Kéo cạnh/góc khung để crop; kéo trong khung để di chuyển.").pack(side="left")

        ttk.Label(right, text="Xuất video hoàn chỉnh").pack(anchor="w")
        ttk.Label(right, text="Video sẽ được xử lý từ đầu đến cuối, không chia thành nhiều phần.", foreground="#94a3b8", wraplength=290).pack(anchor="w", pady=(2, 10))
        ttk.Separator(right).pack(fill="x", pady=(0, 12))
        ttk.Label(right, text="Thư mục lưu").pack(anchor="w")
        output = ttk.Frame(right, style="Panel.TFrame")
        output.pack(fill="x", pady=(5, 12))
        ttk.Entry(output, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(output, text="…", width=3, command=self.choose_output).pack(side="left", padx=(5, 0))
        ttk.Label(right, text="Làm mờ watermark sẽ áp dụng cho tất cả video đã chọn.\nChỉ dùng với nội dung bạn có quyền chỉnh sửa.", foreground="#fbbf24", justify="left", wraplength=290).pack(anchor="w", pady=(0, 13))
        ttk.Checkbutton(right, text="Bật làm mờ vùng đã khoanh", variable=self.blur_enabled).pack(anchor="w", pady=(0, 10))
        ttk.Separator(right).pack(fill="x", pady=(0, 10))
        ttk.Label(right, text="Phụ đề & lồng tiếng Việt (AI)").pack(anchor="w")
        ttk.Label(right, text="Whisper nhận diện bằng CPU; Gemini chỉ hiệu đính/dịch và gán người nói. Khóa API chỉ giữ trong bộ nhớ của phiên này.", foreground="#94a3b8", justify="left", wraplength=290).pack(anchor="w", pady=(2, 5))
        ttk.Label(right, text="Khóa Gemini").pack(anchor="w")
        key_entry = ttk.Entry(right, textvariable=self.gemini_key, show="●")
        key_entry.pack(fill="x", pady=(3, 5))
        ai_options = ttk.Frame(right, style="Panel.TFrame")
        ai_options.pack(fill="x", pady=(0, 5))
        ttk.Label(ai_options, text="Whisper").pack(side="left")
        ttk.Combobox(ai_options, textvariable=self.whisper_model, values=("base", "small", "medium"), width=8, state="readonly").pack(side="left", padx=(5, 9))
        ttk.Label(ai_options, text="Gemini").pack(side="left")
        ttk.Entry(ai_options, textvariable=self.gemini_model, width=16).pack(side="left", padx=(5, 0))
        ttk.Button(right, text="1. Nhận diện, dịch & phân vai", command=self.create_vietnamese_subtitles).pack(fill="x", pady=(1, 5))
        ttk.Checkbutton(right, text="Render phụ đề Việt vào video", variable=self.burn_subtitles).pack(anchor="w")
        ttk.Checkbutton(right, text="Thay âm thanh bằng lồng tiếng Việt", variable=self.dub_enabled).pack(anchor="w", pady=(0, 5))
        self.speaker_controls = ttk.Frame(right, style="Panel.TFrame")
        self.speaker_controls.pack(fill="x", pady=(0, 8))
        ttk.Label(self.speaker_controls, text="Sau khi AI phân vai, chọn giọng cho từng người ở đây.", foreground="#94a3b8", wraplength=290).pack(anchor="w")
        ttk.Separator(right).pack(fill="x", pady=(0, 10))
        ttk.Label(right, text="Watermark bổ sung (tùy chọn)").pack(anchor="w")
        text_row = ttk.Frame(right, style="Panel.TFrame")
        text_row.pack(fill="x", pady=(4, 4))
        ttk.Entry(text_row, textvariable=self.watermark_text).pack(side="left", fill="x", expand=True)
        ttk.Button(text_row, text="Thêm chữ", command=self.add_text_watermark).pack(side="left", padx=(5, 0))
        image_row = ttk.Frame(right, style="Panel.TFrame")
        image_row.pack(fill="x", pady=(0, 4))
        ttk.Button(image_row, text="Chọn ảnh watermark…", command=self.choose_watermark_image).pack(side="left")
        self.watermark_image_label = ttk.Label(image_row, text="Chưa chọn ảnh", foreground="#94a3b8")
        self.watermark_image_label.pack(side="left", padx=(6, 0))
        ttk.Label(image_row, text=" Kích thước").pack(side="left", padx=(8, 0))
        ttk.Scale(image_row, from_=0.10, to=0.60, variable=self.watermark_scale, orient="horizontal", length=80).pack(side="left", padx=(4, 0))
        self.export_button = ttk.Button(right, text="Xuất video", style="Accent.TButton", command=self.render)
        self.export_button.pack(fill="x")

        bottom = ttk.Frame(outer, style="Panel.TFrame", padding=(14, 10))
        bottom.pack(fill="x")
        ttk.Progressbar(bottom, variable=self.progress, maximum=100).pack(fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w", pady=(7, 0))

    def _mode_note(self, mode: str) -> None:
        notes = {
            "crop": "Đang chỉnh vùng crop (xanh dương).",
            "blur": "Đang chỉnh vùng làm mờ subtitle/watermark gốc (hồng).",
            "subtitle": "Đang chỉnh khung hiển thị phụ đề Việt (xanh lá).",
        }
        self.status.set(notes.get(mode, "Đang chỉnh khung hình."))

    def add_text_watermark(self) -> None:
        text = self.watermark_text.get().strip()
        if not text:
            messagebox.showwarning(APP_NAME, "Nhập nội dung watermark chữ trước.")
            return
        self.watermark_texts.append(text)
        self.watermark_text.set("")
        self.status.set(f"Đã thêm {len(self.watermark_texts)} watermark chữ.")

    def choose_watermark_image(self) -> None:
        path = filedialog.askopenfilename(title="Chọn ảnh watermark", filetypes=[("Ảnh", "*.png *.jpg *.jpeg *.webp"), ("Mọi file", "*.*")])
        if path:
            self.watermark_image = Path(path)
            self.watermark_image_label.configure(text=self.watermark_image.name)

    def add_blur_region(self) -> None:
        if not self.editor.image:
            messagebox.showwarning(APP_NAME, "Hãy mở video trước khi thêm vùng mờ.")
            return
        current = self.editor.blur
        self.blur_regions.append(Rect(current.left, current.top, current.right, current.bottom))
        self.editor.extra_blurs = list(self.blur_regions)
        self.editor.blur = Rect(0.68, 0.05, 0.96, 0.14)
        self.blur_enabled.set(True)
        self.editor.redraw()
        self.status.set(f"Đã lưu vùng mờ {len(self.blur_regions)}. Hãy kéo vùng hồng mới để thêm vùng tiếp theo.")

    def choose_output(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if path:
            self.output_dir.set(path)

    def open_file(self) -> None:
        file = filedialog.askopenfilename(title="Chọn video", filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm"), ("Mọi file", "*.*")])
        if file:
            self._load_source(Path(file))

    def choose_cookie_file(self) -> None:
        path = filedialog.askopenfilename(title="Chọn file cookies Netscape", filetypes=[("cookies.txt", "*.txt"), ("Mọi file", "*.*")])
        if path:
            self.cookie_file = Path(path)
            self.cookie_file_label.configure(text=self.cookie_file.name)

    def download(self) -> None:
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "Hãy dán liên kết video trước.")
            return
        self._run_background("Đang tải video Douyin bằng cookie mới…", self._download_worker, url, self.cookie_browser.get(), self.cookie_file)

    def _download_worker(self, url: str, browser: str, cookie_file: Path | None) -> None:
        out_dir = work_directory() / "downloads"
        out_dir.mkdir(exist_ok=True)
        cache_index = out_dir / "index.json"
        cache: dict[str, str] = {}
        try:
            if cache_index.exists():
                loaded = json.loads(cache_index.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cache = {str(key): str(value) for key, value in loaded.items()}
        except (OSError, json.JSONDecodeError):
            cache = {}
        cached_file = Path(cache.get(url, "")) if cache.get(url) else None
        if cached_file and cached_file.exists():
            self.events.put(("status", f"Đã có sẵn video này, dùng lại file cache: {cached_file.name}"))
            self.events.put(("loaded", cached_file))
            return
        download_prefix = f"{uuid.uuid4().hex[:10]}-"
        options = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": str(out_dir / f"{download_prefix}%(id)s.%(ext)s"),
            "noplaylist": True,
            "geo_bypass": True,
            "progress_hooks": [self._download_hook],
            "quiet": True,
            "no_warnings": True,
        }
        if cookie_file and cookie_file.exists():
            options["cookiefile"] = str(cookie_file)
        elif browser != "Không dùng cookie":
            options["cookiesfrombrowser"] = (browser.lower(),)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                file = Path(ydl.prepare_filename(info))
                if not file.exists() or file.suffix.lower() != ".mp4":
                    candidates = list(out_dir.glob(f"{download_prefix}{info.get('id', '')}*.mp4"))
                    if candidates:
                        file = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
        except Exception as exc:
            message = str(exc)
            if "Fresh cookies" in message or "cookies" in message.lower():
                selected = "file cookies.txt" if cookie_file else f"{browser}"
                raise RuntimeError(
                    f"Douyin cần cookie mới từ {selected}. Mở Douyin trong {browser}, xem video này, tải lại trang rồi thử lại. "
                    "Nếu vẫn lỗi, xuất cookie dạng Netscape cookies.txt và chọn file đó trong ứng dụng."
                ) from exc
            raise
        if not file.exists():
            raise RuntimeError("Tải xong nhưng không tìm thấy file video đầu ra.")
        cache[url] = str(file)
        try:
            cache_index.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # Cache is an optimization; a read-only profile should not invalidate a successful download.
            pass
        self.events.put(("loaded", file))

    def _download_hook(self, info: dict) -> None:
        if info.get("status") == "downloading":
            total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
            if total:
                self.events.put(("progress", max(1, min(96, info.get("downloaded_bytes", 0) * 100 / total))))

    def create_vietnamese_subtitles(self) -> None:
        if not self.source:
            self.status.set("Chưa có video. Hãy tải video hoặc mở file trước.")
            messagebox.showwarning(APP_NAME, "Hãy tải hoặc mở video trước.")
            return
        key = self.gemini_key.get().strip()
        if not key:
            self.status.set("Thiếu khóa Gemini. Nhập khóa ở phần “Phụ đề & lồng tiếng Việt (AI)”.")
            messagebox.showwarning(APP_NAME, "Nhập khóa Gemini (hoặc đặt GEMINI_API_KEY) trước.")
            return
        model = self.gemini_model.get().strip() or GEMINI_MODEL_DEFAULT
        whisper = self.whisper_model.get().strip() or WHISPER_MODEL_DEFAULT
        if self.ai_button:
            self.ai_button.configure(state="disabled", text="Đang nhận diện…")
        self.status.set("Đã nhận lệnh. Đang tải/khởi động Whisper CPU; lần đầu có thể mất vài phút…")
        self._run_background("Đang tách tiếng nói và nhận diện subtitle bằng CPU…", self._subtitle_worker, self.source, key, model, whisper)

    def _subtitle_worker(self, source: Path, api_key: str, gemini_model: str, whisper_model: str) -> None:
        job_id = hashlib.sha256(f"{source.resolve()}:{source.stat().st_mtime_ns}".encode()).hexdigest()[:16]
        job_dir = work_directory() / "subtitle_jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        speech_audio = job_dir / "speech_for_ai.mp3"
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.events.put(("status", "Đang tách audio 16 kHz cho Whisper và Gemini…"))
        subprocess.run([ffmpeg, "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k", str(speech_audio)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.events.put(("status", f"Whisper {whisper_model} đang nhận diện lời thoại bằng CPU… Lần đầu sẽ tải model."))
        model = WhisperModel(whisper_model, device="cpu", compute_type="int8", cpu_threads=max(1, os.cpu_count() or 1), num_workers=1, download_root=str(work_directory() / "whisper_models"))
        segments, _info = model.transcribe(str(speech_audio), vad_filter=True, beam_size=5, condition_on_previous_text=True)
        cues = [SubtitleCue(float(s.start), max(float(s.end), float(s.start) + 0.12), s.text.strip(), s.text.strip()) for s in segments if s.text and s.text.strip()]
        if not cues:
            raise RuntimeError("Không nhận diện được lời thoại. Hãy kiểm tra video có âm thanh rõ ràng hay không.")
        self.events.put(("status", "Gemini đang nghe audio, sửa transcript, dịch Việt và phân biệt người nói…"))
        corrected = self._gemini_correct_translate_and_assign(cues, speech_audio, api_key, gemini_model)
        self._write_srt(job_dir / "subtitles_vi.srt", corrected)
        self.events.put(("subtitles_ready", (job_dir, corrected)))

    @staticmethod
    def _json_from_model(text: str) -> object:
        cleaned = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", text.strip(), flags=re.IGNORECASE)
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start < 0 or end < start:
            raise ValueError("Không tìm thấy JSON.")
        return json.loads(cleaned[start:end + 1])

    def _gemini_correct_translate_and_assign(self, cues: list[SubtitleCue], audio: Path, api_key: str, model: str) -> list[SubtitleCue]:
        audio_bytes = audio.read_bytes()
        if len(audio_bytes) > 13_000_000:
            raise RuntimeError("Audio quá dài để gửi trực tiếp tới Gemini. Hãy cắt video ngắn hơn.")
        items = [{"id": i, "text": cue.original} for i, cue in enumerate(cues)]
        prompt = (
            "Bạn là biên tập viên phụ đề và điều phối lồng tiếng tiếng Việt. Audio đính kèm là nguồn sự thật về người nói. "
            "Transcript bên dưới do Whisper tạo ra, có thể sai từ. Hãy nghe audio, sửa nhận dạng sai khi có thể và dịch từng câu sang tiếng Việt tự nhiên, chính xác. "
            "Phân biệt người nói bằng giọng nói: dùng nhãn nhất quán S1, S2,... và gender chỉ là male, female hoặc unknown. "
            "KHÔNG thêm, xóa, gộp, tách hoặc đổi id; mỗi id đúng một lần; không tự tạo mốc thời gian. "
            "Chỉ trả JSON thuần: [{\"id\":0,\"text_vi\":\"...\",\"speaker\":\"S1\",\"gender\":\"female\"}].\\n"
            + json.dumps(items, ensure_ascii=False)
        )
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}, {"inlineData": {"mimeType": "audio/mpeg", "data": base64.b64encode(audio_bytes).decode("ascii")}}]}], "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max(2048, len(cues) * 80)}}
        request = Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
        try:
            with urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Gemini từ chối yêu cầu ({exc.code}): {exc.read().decode('utf-8', errors='replace')[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Không kết nối được Gemini: {exc.reason}") from exc
        try:
            text = "".join(part.get("text", "") for part in result["candidates"][0]["content"]["parts"])
            rows = self._json_from_model(text)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Gemini không trả về phụ đề JSON có thể dùng. Hãy thử model Gemini khác.") from exc
        if not isinstance(rows, list):
            raise RuntimeError("Gemini trả về phụ đề sai định dạng.")
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), int)}
        if set(by_id) != set(range(len(cues))):
            raise RuntimeError("Gemini đã thiếu hoặc đổi id. Để bảo toàn timecode, kết quả này không được dùng.")
        corrected: list[SubtitleCue] = []
        for index, cue in enumerate(cues):
            row = by_id[index]
            vietnamese = str(row.get("text_vi", "")).strip()
            if not vietnamese:
                raise RuntimeError(f"Gemini không tạo bản dịch cho câu {index + 1}.")
            speaker = re.sub(r"[^A-Za-z0-9_-]", "", str(row.get("speaker", "S1")))[:24] or "S1"
            gender = str(row.get("gender", "unknown")).lower()
            corrected.append(SubtitleCue(cue.start, cue.end, cue.original, vietnamese, speaker, gender if gender in {"male", "female", "unknown"} else "unknown"))
        return corrected

    @staticmethod
    def _write_srt(path: Path, cues: list[SubtitleCue]) -> None:
        lines: list[str] = []
        for index, cue in enumerate(cues, 1):
            lines.extend((str(index), f"{srt_time(cue.start)} --> {srt_time(cue.end)}", cue.vietnamese, ""))
        path.write_text("\\n".join(lines), encoding="utf-8-sig")

    def _populate_speaker_voice_controls(self, cues: list[SubtitleCue]) -> None:
        if not self.speaker_controls:
            return
        for child in self.speaker_controls.winfo_children():
            child.destroy()
        self.speaker_voice_vars = {}
        speakers: dict[str, str] = {}
        for cue in cues:
            speakers.setdefault(cue.speaker, cue.gender)
        if not speakers:
            ttk.Label(self.speaker_controls, text="Sau khi AI phân vai, chọn giọng cho từng người ở đây.", foreground="#94a3b8", wraplength=290).pack(anchor="w")
            return
        ttk.Label(self.speaker_controls, text="Giọng lồng tiếng theo người nói").pack(anchor="w")
        values = ("Nữ — Hoài My", "Nam — Nam Minh")
        for speaker, gender in speakers.items():
            row = ttk.Frame(self.speaker_controls, style="Panel.TFrame")
            row.pack(fill="x", pady=(3, 0))
            ttk.Label(row, text=f"{speaker} ({'nữ' if gender == 'female' else 'nam' if gender == 'male' else 'chưa rõ'})", width=13).pack(side="left")
            value = tk.StringVar(value=values[1] if gender == "male" else values[0])
            self.speaker_voice_vars[speaker] = value
            ttk.Combobox(row, textvariable=value, values=values, state="readonly", width=18).pack(side="left", fill="x", expand=True)

    def _load_source(self, source: Path) -> None:
        if not source.exists():
            messagebox.showerror(APP_NAME, "Không tìm thấy file video.")
            return
        self.source = source
        try:
            self.blur_regions = []
            self.editor.extra_blurs = []
            self.subtitle_cues = []
            self.dubbing_audio = None
            self.subtitle_job_dir = None
            self._populate_speaker_voice_controls([])
            self.duration = self._probe_duration(source)
            self.preview_time = min(1.0, self.duration / 2)
            frame = self._extract_frame(source, self.preview_time)
            self.editor.set_image(frame)
            self.status.set(f"Đã nạp: {source.name} • {timestamp(self.duration)}")
            self.progress.set(100)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Không thể đọc video:\n{exc}")

    def _probe_duration(self, source: Path) -> float:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not match:
            raise RuntimeError("FFmpeg không xác định được thời lượng.")
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)

    def _extract_frame(self, source: Path, at: float) -> Image.Image:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        temp = work_directory() / "preview.jpg"
        command = [ffmpeg, "-y", "-ss", str(at), "-i", str(source), "-frames:v", "1", "-q:v", "3", str(temp)]
        subprocess.run(command, capture_output=True, check=True)
        with Image.open(temp) as image:
            return image.convert("RGB").copy()

    def _apply_zoom(self) -> None:
        # Zoom is applied to the crop frame and therefore stays fully CPU/FFmpeg based.
        if not self.editor.image:
            return
        factor = float(self.zoom.get())
        if abs(factor - self._last_zoom) < 0.005:
            return
        rect = self.editor.crop
        cx, cy = (rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2
        ratio = self._last_zoom / factor
        w, h = (rect.right - rect.left) * ratio, (rect.bottom - rect.top) * ratio
        rect.left, rect.right = cx - w / 2, cx + w / 2
        rect.top, rect.bottom = cy - h / 2, cy + h / 2
        rect.clamp()
        self._last_zoom = factor
        self.editor.redraw()

    def _reset_split_points(self) -> None:
        count = max(1, min(10, int(self.split_count.get() or 3)))
        self.split_count.set(count)
        self.split_points = [self.duration * i / count for i in range(count + 1)] if self.duration else []
        self._draw_timeline()

    def _on_split_count_changed(self, *_args) -> None:
        try:
            count = max(1, min(10, int(self.split_count.get())))
            self.split_count.set(count)
        except (TypeError, ValueError, tk.TclError):
            self.split_count.set(3)
        self._reset_split_points()

    def _update_export_label(self) -> None:
        if self.export_button:
            try:
                self.export_button.configure(text=f"Xuất {max(1, min(10, int(self.split_count.get())))} video")
            except (ValueError, tk.TclError):
                self.export_button.configure(text="Xuất video")

    def _draw_timeline(self) -> None:
        if not self.timeline:
            return
        canvas = self.timeline
        canvas.delete("all")
        width = max(250, canvas.winfo_width() or 300)
        height = 100
        canvas.create_rectangle(10, 38, width - 10, 62, fill="#1e3a5f", outline="#334155")
        if not self.duration or len(self.split_points) < 2:
            canvas.create_text(width // 2, 50, text="Mở video để hiện vạch cắt", fill="#94a3b8", font=("Segoe UI", 9))
            return
        canvas.create_text(10, 18, text="00:00", anchor="w", fill="#94a3b8", font=("Segoe UI", 8))
        canvas.create_text(width - 10, 18, text=timestamp(self.duration), anchor="e", fill="#94a3b8", font=("Segoe UI", 8))
        for index, point in enumerate(self.split_points):
            x = 10 + (width - 20) * point / self.duration
            if index not in (0, len(self.split_points) - 1):
                canvas.create_line(x, 28, x, 72, fill="#22d3ee", width=4)
                canvas.create_polygon(x - 7, 28, x + 7, 28, x, 20, fill="#22d3ee", outline="")
                canvas.create_text(x, 86, text=timestamp(point), fill="#e2e8f0", font=("Segoe UI", 8))

    def _timeline_press(self, event: tk.Event) -> None:
        if not self.duration or len(self.split_points) < 3:
            return
        width = max(250, self.timeline.winfo_width() or 300) if self.timeline else 300
        candidates = [(abs(event.x - (10 + (width - 20) * point / self.duration)), i) for i, point in enumerate(self.split_points[1:-1], 1)]
        distance, index = min(candidates)
        if distance <= 14:
            self.timeline_handle = index

    def _timeline_drag(self, event: tk.Event) -> None:
        if self.timeline_handle is None or not self.timeline or not self.duration:
            return
        width = max(250, self.timeline.winfo_width() or 300)
        point = min(self.duration, max(0.0, (event.x - 10) * self.duration / (width - 20)))
        index = self.timeline_handle
        gap = max(0.25, self.duration * 0.005)
        self.split_points[index] = max(self.split_points[index - 1] + gap, min(point, self.split_points[index + 1] - gap))
        self._draw_timeline()

    def render(self) -> None:
        if not self.source:
            messagebox.showwarning(APP_NAME, "Hãy tải hoặc mở video trước.")
            return
        if not self.duration or self.duration <= 0:
            messagebox.showwarning(APP_NAME, "Không xác định được thời lượng video.")
            return
        segments = [(0.0, self.duration)]
        destination = Path(self.output_dir.get()).expanduser()
        texts = list(self.watermark_texts)
        if self.watermark_text.get().strip():
            texts.append(self.watermark_text.get().strip())
        cues = list(self.subtitle_cues)
        burn_subtitles = bool(self.burn_subtitles.get()) and bool(cues)
        dub_enabled = bool(self.dub_enabled.get()) and bool(cues)
        if (self.burn_subtitles.get() or self.dub_enabled.get()) and not cues:
            messagebox.showwarning(APP_NAME, "Hãy bấm “1. Nhận diện, dịch & phân vai” trước, hoặc tắt phụ đề/lồng tiếng Việt.")
            return
        voice_map = {speaker: self._voice_id(value.get()) for speaker, value in self.speaker_voice_vars.items()}
        crop = Rect(self.editor.crop.left, self.editor.crop.top, self.editor.crop.right, self.editor.crop.bottom)
        subtitle_rect = Rect(self.editor.subtitle.left, self.editor.subtitle.top, self.editor.subtitle.right, self.editor.subtitle.bottom)
        regions = [Rect(r.left, r.top, r.right, r.bottom) for r in self.blur_regions]
        regions.append(Rect(self.editor.blur.left, self.editor.blur.top, self.editor.blur.right, self.editor.blur.bottom))
        self._run_background("Đang chuẩn bị xuất video…", self._render_worker, self.source, segments, destination, bool(self.blur_enabled.get()), texts, self.watermark_image, float(self.watermark_scale.get()), crop, regions, subtitle_rect, cues, burn_subtitles, dub_enabled, voice_map, self.duration, self.subtitle_job_dir)

    @staticmethod
    def _voice_id(label: str) -> str:
        return "vi-VN-NamMinhNeural" if label.startswith("Nam") else VIETNAMESE_VOICE_DEFAULT

    def _render_worker(self, source: Path, segments: list[tuple[float, float]], destination: Path, blur_enabled: bool, watermark_texts: list[str], watermark_image: Path | None, watermark_scale: float, crop: Rect, selected_regions: list[Rect], subtitle_rect: Rect, cues: list[SubtitleCue], burn_subtitles: bool, dub_enabled: bool, voice_map: dict[str, str], source_duration: float, job_dir: Path | None) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        blur_regions = selected_regions if blur_enabled else []
        render_dir = job_dir or (work_directory() / "render_jobs" / hashlib.sha256(f"{source}:{time.time_ns()}".encode()).hexdigest()[:16])
        render_dir.mkdir(parents=True, exist_ok=True)
        dub_audio: Path | None = None
        if dub_enabled:
            dub_audio = self._synthesize_dubbing(cues, source_duration, voice_map, render_dir)
        if cues:
            self._write_srt(destination / f"{source.stem}.vi.srt", cues)
        total_parts = len(segments)
        for index, (start, end) in enumerate(segments, 1):
            out = destination / f"{source.stem}_part_{index}.mp4"
            vf = self._filter_graph(crop, blur_regions, watermark_texts)
            if burn_subtitles:
                ass_file = render_dir / f"subtitle_part_{index}.ass"
                self._write_ass(ass_file, cues, subtitle_rect, crop, start, end)
                vf += f",ass=filename='{ffmpeg_filter_path(ass_file)}'"
            command = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source)]
            has_image = bool(watermark_image and watermark_image.exists())
            if has_image:
                command += ["-i", str(watermark_image)]
            if dub_audio:
                # A second input is seeked by exactly the same offset, so the dubbed cue starts on its original timecode.
                command += ["-ss", f"{start:.3f}", "-i", str(dub_audio)]
            command += ["-t", f"{end - start:.3f}"]
            if has_image:
                command += ["-filter_complex", f"[0:v]{vf}[video];[1:v]scale=iw*{max(0.1, min(0.6, watermark_scale)):.4f}:-1[wm];[video][wm]overlay=20:20[outv]", "-map", "[outv]", "-map", "0:a?"]
            else:
                command += ["-vf", vf, "-map", "0:v:0", "-map", "0:a?"]
            if dub_audio:
                audio_input_index = 2 if has_image else 1
                map_position = command.index("-map", command.index("-map") + 1) if has_image else command.index("-map") + 2
                command[map_position + 1] = f"{audio_input_index}:a:0"
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "0",
                       "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
            self.events.put(("status", f"Đang xuất video {index}/{total_parts} bằng CPU…"))
            # FFmpeg writes verbose diagnostics continuously. Sending them to DEVNULL prevents a full
            # pipe from pausing long CPU renders while the interface is polling progress.
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while process.poll() is None:
                self.events.put(("progress", ((index - 1) + 0.5) / total_parts * 100))
                time.sleep(0.5)
            if process.returncode != 0:
                raise RuntimeError(f"FFmpeg không thể xuất video {index}. Hãy kiểm tra file nguồn và ổ đĩa còn trống.")
            self.events.put(("progress", index / total_parts * 100))
        self.events.put(("done", (destination, total_parts)))

    @staticmethod
    def _atempo_filter(speed: float) -> str:
        parts: list[str] = []
        while speed > 2.0:
            parts.append("atempo=2.0")
            speed /= 2.0
        while speed < 0.5:
            parts.append("atempo=0.5")
            speed /= 0.5
        parts.append(f"atempo={speed:.6f}")
        return ",".join(parts)

    def _synthesize_dubbing(self, cues: list[SubtitleCue], duration: float, voice_map: dict[str, str], job_dir: Path) -> Path:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        AudioSegment.converter = ffmpeg
        tracks_dir = job_dir / "dub_tracks"
        tracks_dir.mkdir(exist_ok=True)
        final_audio = AudioSegment.silent(duration=max(1, int(duration * 1000) + 300), frame_rate=24000).set_channels(1)
        for index, cue in enumerate(cues, 1):
            self.events.put(("status", f"Đang tạo lồng tiếng Việt {index}/{len(cues)} ({cue.speaker})…"))
            raw = tracks_dir / f"{index:04d}.mp3"
            stretched = tracks_dir / f"{index:04d}.wav"
            asyncio.run(edge_tts.Communicate(cue.vietnamese, voice=voice_map.get(cue.speaker, VIETNAMESE_VOICE_DEFAULT)).save(str(raw)))
            original = AudioSegment.from_file(raw)
            target_ms = max(250, int((cue.end - cue.start) * 1000))
            speed = max(0.25, min(8.0, len(original) / target_ms))
            subprocess.run([ffmpeg, "-y", "-i", str(raw), "-af", self._atempo_filter(speed), "-ar", "24000", "-ac", "1", str(stretched)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            final_audio = final_audio.overlay(AudioSegment.from_file(stretched), position=max(0, int(cue.start * 1000)))
        output = job_dir / "dubbing_vi.mp3"
        final_audio.export(output, format="mp3", bitrate="128k")
        return output

    @staticmethod
    def _write_ass(path: Path, cues: list[SubtitleCue], subtitle_rect: Rect, crop: Rect, start: float, end: float) -> None:
        cx = min(0.96, max(0.04, ((subtitle_rect.left + subtitle_rect.right) / 2 - crop.left) / (crop.right - crop.left)))
        cy = min(0.96, max(0.04, ((subtitle_rect.top + subtitle_rect.bottom) / 2 - crop.top) / (crop.bottom - crop.top)))
        font_size = max(28, min(72, int((subtitle_rect.bottom - subtitle_rect.top) / (crop.bottom - crop.top) * 220)))
        header = "[Script Info]\\nScriptType: v4.00+\\nPlayResX: 1920\\nPlayResY: 1080\\nScaledBorderAndShadow: yes\\n\\n[V4+ Styles]\\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\\n"
        style = f"Style: Vietnamese,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H99000000,-1,0,0,0,100,100,0,0,3,2,1,2,30,30,30,1\\n\\n[Events]\\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\\n"
        events: list[str] = []
        for cue in cues:
            if cue.end <= start or cue.start >= end:
                continue
            text = cue.vietnamese.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
            events.append(f"Dialogue: 0,{ass_time(max(cue.start, start) - start)},{ass_time(min(cue.end, end) - start)},Vietnamese,,0,0,0,,{{\\an2\\pos({int(cx * 1920)},{int(cy * 1080)})}}{text}")
        path.write_text(header + style + "\\n".join(events) + "\\n", encoding="utf-8-sig")

    @staticmethod
    def _filter_graph(crop: Rect, blur_regions: list[Rect], watermark_texts: list[str] | None = None) -> str:
        prefix = ""
        if not crop.is_full():
            prefix = f"crop=trunc(iw*{crop.right - crop.left:.7f}/2)*2:trunc(ih*{crop.bottom - crop.top:.7f}/2)*2:trunc(iw*{crop.left:.7f}/2)*2:trunc(ih*{crop.top:.7f}/2)*2,"
        # blur region coordinates are converted from source-normalized to post-crop normalized coordinates.
        if not blur_regions:
            graph = prefix.rstrip(",") or "null"
            return App._append_text_watermarks(graph, watermark_texts or [])
        regions = []
        for blur in blur_regions:
            bx0 = (blur.left - crop.left) / (crop.right - crop.left)
            by0 = (blur.top - crop.top) / (crop.bottom - crop.top)
            bx1 = (blur.right - crop.left) / (crop.right - crop.left)
            by1 = (blur.bottom - crop.top) / (crop.bottom - crop.top)
            bx0, by0, bx1, by1 = [min(max(v, 0.0), 1.0) for v in (bx0, by0, bx1, by1)]
            if bx1 - bx0 > 0.01 and by1 - by0 > 0.01:
                regions.append((bx0, by0, bx1 - bx0, by1 - by0))
        if not regions:
            graph = prefix.rstrip(",") or "null"
            return App._append_text_watermarks(graph, watermark_texts or [])
        labels = "[base]" + "".join(f"[r{i}]" for i in range(len(regions)))
        graph = prefix + f"split={len(regions) + 1}{labels};"
        current = "base"
        for i, (x, y, w, h) in enumerate(regions):
            graph += f"[r{i}]crop=iw*{w:.7f}:ih*{h:.7f}:iw*{x:.7f}:ih*{y:.7f},gblur=sigma=12[b{i}];"
            if i < len(regions) - 1:
                out = f"o{i}"
                graph += f"[{current}][b{i}]overlay=main_w*{x:.7f}:main_h*{y:.7f}[{out}];"
                current = out
            else:
                graph += f"[{current}][b{i}]overlay=main_w*{x:.7f}:main_h*{y:.7f}"
        return App._append_text_watermarks(graph.rstrip(";"), watermark_texts or [])

    @staticmethod
    def _append_text_watermarks(graph: str, texts: list[str]) -> str:
        for index, text in enumerate(texts):
            safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            graph += f",drawtext=text='{safe}':x=20:y={20 + index * 42}:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.45"
        return graph

    def _run_background(self, initial: str, target, *args) -> None:
        self.progress.set(0)
        self.status.set(initial)
        def runner() -> None:
            try:
                target(*args)
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=runner, daemon=True).start()

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.progress.set(float(payload))
            elif kind == "status":
                self.status.set(str(payload))
            elif kind == "loaded":
                self._load_source(Path(payload))
            elif kind == "subtitles_ready":
                job_dir, cues = payload
                self.subtitle_job_dir = Path(job_dir)
                self.subtitle_cues = list(cues)
                self._populate_speaker_voice_controls(self.subtitle_cues)
                people = len({cue.speaker for cue in self.subtitle_cues})
                self.status.set(f"Đã tạo {len(self.subtitle_cues)} phụ đề Việt, phát hiện {people} người nói. Kéo khung xanh lá rồi xuất video.")
                if self.ai_button:
                    self.ai_button.configure(state="normal", text="Nhận diện AI…")
                self.progress.set(100)
                messagebox.showinfo(APP_NAME, f"Đã nhận diện {len(self.subtitle_cues)} câu và {people} người nói.\n\nBạn có thể chọn giọng cho từng người, chỉnh vùng mờ subtitle gốc và kéo khung xanh lá để đặt phụ đề Việt.")
            elif kind == "done":
                destination, total_parts = payload
                self.status.set(f"Hoàn tất {total_parts} video! Lưu tại: {destination}")
                self.progress.set(100)
                messagebox.showinfo(APP_NAME, f"Đã xuất xong {total_parts} video.\n\nThư mục: {destination}")
            elif kind == "error":
                self.status.set("Có lỗi. Xem thông báo để biết chi tiết.")
                if self.ai_button:
                    self.ai_button.configure(state="normal", text="Nhận diện AI…")
                messagebox.showerror(APP_NAME, str(payload))
        self.after(100, self._poll_events)


if __name__ == "__main__":
    App().mainloop()
