# -*- coding: utf-8 -*-
"""cctv.paths — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import threading
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent  # مجلد المشروع (فوق حزمة cctv)
DATA_DIR = Path(os.environ.get("CCTV_DATA_DIR", str(BASE_DIR / "cctv_v18_data"))).resolve()
SNAP_DIR = DATA_DIR / "snapshots"
CLIPS_DIR = DATA_DIR / "clips"
PLATES_DIR = DATA_DIR / "plates"
CONFIG_FILE = DATA_DIR / "config.json"
EVENTS_FILE = DATA_DIR / "events_log.json"
EVENTS_ARCHIVE = DATA_DIR / "events_archive.jsonl"  # v17 #R21: الأحداث اللي خرجت من الفهرس السريع تُحفظ هنا (سطر لكل حدث)

DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAP_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)
PLATES_DIR.mkdir(exist_ok=True)
BIN_DIR = DATA_DIR / "bin"        # v16 #R16: go2rtc.exe
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"    # v16 #R18: تسجيل المقاطع بنسخ البث بدون فك ترميز
FFPROBE_EXE = BIN_DIR / "ffprobe.exe"
SEG_DIR = DATA_DIR / "segments"        # v16 #R18: مقاطع دوّارة قصيرة لكل كاميرا (آخر ~70 ثانية)
STATIC_DIR = (DATA_DIR / "static") if (DATA_DIR / "static").exists() else BASE_DIR / "assets" / "static"  # v16 #R16: الخط وChart.js محلياً (تشتغل بدون إنترنت)

MAX_CAMERAS = 32
CLIP_FPS = 12  # v16 #R15: كان 8، والإطارات صارت تُسحب بخيط مستقل عن YOLO
PRE_BUFFER_SEC = 5
POST_BUFFER_SEC = 10
AI_MAX_IMAGE_DIM = 1024
MAX_PENDING_AI_PER_CAM = 16
MAX_STORED_EVENTS = 250

# قفل مركزي لحماية عمليات قراءة/كتابة الملفات على القرص بشكل ذري وآمن بين الخيوط
DISK_IO_LOCK = threading.RLock()

