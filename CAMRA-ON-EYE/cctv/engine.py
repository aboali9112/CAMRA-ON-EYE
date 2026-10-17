# -*- coding: utf-8 -*-
"""cctv.engine — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import sys
import os
import re
import time
import json
import shutil
import base64
import threading
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
import requests
from .ai import parse_ai_json_response
from .camera import SingleCameraWorker
from .cloud import CloudStorageManager
from .config import atomic_write_json, load_config, load_events, make_default_cam_analysis, redact_secrets, save_config, save_events
from .detector import DetectionHub, LocalObjectDetector
from .geometry import build_ai_verification_crop, classify_geometry_target, extract_and_enhance_plate
from .go2rtc import Go2RtcManager
from .guard import LoadGuard
from .isapi import IsapiAlertStreamListener
from .notify import MultiProviderNotifier
from .paths import AI_MAX_IMAGE_DIM, CLIPS_DIR, CLIP_FPS, DATA_DIR, EVENTS_FILE, MAX_CAMERAS, MAX_STORED_EVENTS, PLATES_DIR, POST_BUFFER_SEC, PRE_BUFFER_SEC, SNAP_DIR, EVENTS_ARCHIVE, DISK_IO_LOCK
from .plates import HAS_FAST_ALPR, enhance_plate_crop, get_plate_reader, read_best_plate
from .recorder import FfmpegSegmentRecorder
from .rtsp import query_hikvision_isapi_full
from .reliability import lpr_enabled, ready_models, assess_plate, normalize_plate
from .evidence import wait_recorded_frame, evidence_caption


class MultiCamMatrixEngine:
    def __init__(self, auto_start=True):
        self.engine_start_ts = time.time()
        self.cfg = load_config()
        self.preview = os.environ.get("CCTV_PREVIEW") == "1"
        self._plate_sem = threading.BoundedSemaphore(2)
        if self.preview:
            self.cfg["cameras"] = []
        # v15 #1: load_events يرجع (events, ok)
        self.events, self.events_loaded_ok = load_events(EVENTS_FILE, return_tuple=True)
        self.workers = {}
        self.lock = threading.RLock()
        self.events_lock = threading.RLock()
        self._event_seq = 0

        self.last_ai_latency_ms = 0
        self.last_alert_delay_ms = 0
        self.last_telegram_ms = 0
        self.ai_connection_state = "untested"
        self.ai_last_completed_state = "untested"
        self.ai_last_error = ""
        self.active_ai_requests = 0

        self.telegram_cooldown_lock = threading.Lock()
        self._notify_sem = threading.BoundedSemaphore(3)  # v16 #R5
        self.last_telegram_sent_per_cam = {}

        # v15 #5 & #8 & #9 & #11: تهيئة الكاشف المحلي، نظام الإشعارات، التخزين السحابي، ومستمع ISAPI
        self.local_detector = LocalObjectDetector(self.cfg.get("yolo_device", "auto"))
        self.notifier = MultiProviderNotifier(self)
        self.cloud_manager = CloudStorageManager(self)
        self.isapi_listener = IsapiAlertStreamListener(self)
        self.go2rtc = Go2RtcManager(self)  # v16 #R16
        self.detect_hub = DetectionHub(self.local_detector)  # v16 #R18
        self.load_guard = LoadGuard(self, start_thread=("--selftest" not in sys.argv and not self.preview))  # v16 #R18

        if "--selftest" not in sys.argv and not self.preview:
            if auto_start:
                FfmpegSegmentRecorder.kill_stale()  # v16 #R18
            self.sync_workers(auto_start=auto_start)
            if auto_start:
                self.isapi_listener.start()
                threading.Thread(target=self.go2rtc.start, daemon=True).start()
            self.cleanup_thread = threading.Thread(target=self._storage_cleanup_loop, daemon=True)
            self.cleanup_thread.start()
            self._recover_incomplete_events()  # v16 #R7
            if HAS_FAST_ALPR and self.cfg.get("plate_reader", "fast_alpr") == "fast_alpr":
                get_plate_reader()  # v16 #R12: تحميل نماذج اللوحات بالخلفية من أول تشغيل

    @staticmethod
    def archive_events(evs):
        '''# v17 #R21: الأحداث اللي تخرج من الفهرس السريع (250) تُلحق بأرشيف JSONL دائم؛ ملفاتها باقية والإحصائيات تشوفها'''
        if not evs:
            return 0
        try:
            with DISK_IO_LOCK:
                with open(EVENTS_ARCHIVE, "a", encoding="utf-8") as f:
                    for e in evs:
                        f.write(json.dumps({k: v for k, v in e.items() if not str(k).startswith("_") or k in ("_snap_path",)}, ensure_ascii=False) + "\n")
            return len(evs)
        except Exception:
            return 0

    @staticmethod
    def load_archived_events(since_ts=0.0, limit=5000):
        '''يقرأ الأرشيف (الأحدث أولاً) مع فلترة بالوقت'''
        out = []
        try:
            if not EVENTS_ARCHIVE.exists():
                return out
            with DISK_IO_LOCK:
                lines = EVENTS_ARCHIVE.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if MultiCamMatrixEngine._event_epoch(e) >= since_ts:
                    out.append(e)
                    if len(out) >= limit:
                        break
        except Exception:
            pass
        return out

    @staticmethod
    def _event_is_busy(ev):
        '''# v16 #R8: حدث لسا شغّال (تحليل/تسجيل/إرسال/رفع)'''
        return (
            ev.get("ai_status") in ("pending", "analyzing")
            or ev.get("clip_status") == "recording"
            or ev.get("telegram_status") in ("sending", "waiting_evidence")
            or ev.get("plate_status") in ("waiting_hq", "reading")
            or ev.get("cloud_status") in ("queued", "uploading")
        )

    def _recover_incomplete_events(self):
        '''# v16 #R7: بعد إعادة التشغيل نكمّل أو نغلق الأعمال المعلّقة بدل ما تظل عالقة للأبد'''
        max_age = float(self.cfg.get("recovery_max_age_sec", 1800) or 1800)
        now = time.time()
        to_requeue = []
        to_renotify = []
        to_reupload = []
        with self.events_lock:
            for ev in self.events:
                eid = ev.get("id")
                if not eid:
                    continue
                if ev.get("plate_status") in ("waiting_hq", "reading"):
                    ev.update(plate_status="failed", plate_error="توقفت قراءة اللوحة بسبب إعادة تشغيل البرنامج")
                if ev.get("evidence_status") == "waiting":
                    ev.update(evidence_status="fallback", evidence_source="analysis_fallback", evidence_reason="restart")
                if ev.get("clip_status") == "recording":
                    clip_json = CLIPS_DIR / f"{eid}.json"
                    done = None
                    if clip_json.exists():
                        try:
                            done = json.loads(clip_json.read_text(encoding="utf-8"))
                        except Exception:
                            done = None
                    if isinstance(done, dict) and done.get("clip_status"):
                        ev.update({
                            "clip_status": done.get("clip_status"),
                            "clip_status_msg": done.get("clip_status_msg", ""),
                            "mp4_url": done.get("mp4_url", ""),
                            "browser_playable": bool(done.get("browser_playable", False)),
                            "clip_duration": done.get("duration_sec", 0.0)
                        })
                    else:
                        ev.update({"clip_status": "failed", "clip_status_msg": "توقف تسجيل المقطع بسبب إعادة تشغيل البرنامج"})
                _age = now - float(ev.get("detected_ts") or 0.0)
                # v16 #R14: الإرسال والرفع المقطوعين يكملون بعد إعادة التشغيل (لو الحدث حديث)
                if ev.get("telegram_status") == "sending":
                    ev.update(telegram_status="unknown", telegram_status_msg="أعيد تشغيل البرنامج أثناء الإرسال؛ القبول غير معروف، لم تتم إعادة الإرسال")
                elif ev.get("telegram_status") == "waiting_evidence":
                    if (_age <= max_age and Path(ev.get("_snap_path") or "").is_file()
                            and ev.get("confirmed") and ev.get("important") and not ev.get("is_error")):
                        ev.update({"telegram_status": "pending_retry", "telegram_status_msg": "إعادة الإرسال بعد إعادة تشغيل البرنامج..."})
                        to_renotify.append(eid)
                    else:
                        ev.update({"telegram_status": "failed", "telegram_status_msg": "انقطع الإرسال بسبب إعادة تشغيل البرنامج"})
                if ev.get("cloud_status") in ("queued", "uploading"):
                    if _age <= max_age and eid not in to_renotify:
                        ev.update({"cloud_status": "queued"})
                        to_reupload.append(eid)
                    elif eid not in to_renotify:
                        ev.update({"cloud_status": "failed", "cloud_error": "انقطع الرفع بسبب إعادة تشغيل البرنامج"})
                if ev.get("ai_status") in ("pending", "analyzing"):
                    age = now - float(ev.get("detected_ts") or 0.0)
                    cid = int(ev.get("cam_id", 0) or 0)
                    if age <= max_age and cid in self.workers and Path(ev.get("_snap_path") or "").is_file():
                        ev.update({"ai_status": "pending", "description": "أُعيد جدولة التحليل بعد إعادة تشغيل البرنامج..."})
                        to_requeue.append((cid, eid))
                    else:
                        ev.update({"ai_status": "failed", "is_error": True,
                                   "ai_error_reason": "توقف التحليل بسبب إعادة تشغيل البرنامج",
                                   "description": "توقف التحليل بسبب إعادة تشغيل البرنامج (الصورة والمقطع محفوظين)"})
            save_events(self.events)
        for cid, eid in to_requeue:
            try:
                self.workers[cid].enqueue_event_for_ai(eid)
            except Exception:
                pass
        for eid in to_reupload:
            try:
                ok_up = self.cloud_manager.enqueue_event_upload(eid)
            except Exception:
                ok_up = False
            if not ok_up and (self.get_event_by_id(eid) or {}).get("cloud_status") == "queued":
                self.update_event_fields(eid, cloud_status="failed", cloud_error="تعذر استكمال الرفع بعد إعادة التشغيل (التخزين السحابي غير مفعّل؟)")
        for eid in to_renotify:
            ev = self.get_event_by_id(eid)
            if ev:
                self._dispatch_finalize_async(eid, ev, Path(ev.get("_snap_path")), True, True, False,
                                              ev.get("trigger") or "", ev.get("description") or "", time.time())
        return len(to_requeue)

    def _storage_cleanup_loop(self):
        '''خيط تنظيف يعمل كل ساعة بعد انقضاء أول 10 دقائق من التشغيل'''
        while True:
            time.sleep(3600)
            try:
                self.cleanup_storage()
            except Exception:
                pass

    # v15 #1: حماية cleanup_storage من حذف الملفات عند فراغ السجل أو تلفه أو خلال أول 10 دقائق
    def cleanup_storage(self, now_ts=None, bypass_startup_grace=False):
        now = float(now_ts) if now_ts is not None else time.time()

        # 1. لا يعمل أي تنظيف في أول 10 دقائق (600 ثانية) من التشغيل
        if not bypass_startup_grace and (now - self.engine_start_ts) < 600.0:
            return {"ok": True, "skipped": "startup_grace_10m", "deleted_files": 0}

        # 2. إذا لم يُقرأ سجل الأحداث بنجاح (ok=False)، يتوقف التنظيف بالكامل مع كتابة تحذير في السجل
        if not getattr(self, "events_loaded_ok", True):
            print("[WARNING] cleanup_storage: تم إيقاف التنظيف بالكامل لأن سجل الأحداث لم يُقرأ بنجاح (ok=False).")
            return {"ok": False, "skipped": "events_load_failed", "deleted_files": 0}

        with self.events_lock:
            current_events = list(self.events)
            valid_ids = {str(ev.get("id", "")) for ev in current_events if ev.get("id")}
            cloud_uploaded_map = {
                str(ev.get("id", "")): float(ev.get("cloud_uploaded_ts") or 0.0)
                for ev in current_events
                if ev.get("cloud_status") == "uploaded" and ev.get("cloud_uploaded_ts")
            }

        # إذا كان السجل فارغاً، لا نحذف أي ملفات تلقائياً
        if len(current_events) == 0:
            return {"ok": True, "skipped": "empty_events_log", "deleted_files": 0}

        retention_days = max(1, int(self.cfg.get("retention_days", 7) or 7))
        max_storage_mb = max(50, int(self.cfg.get("max_storage_mb", 2000) or 2000))
        orphan_cleanup_enabled = bool(self.cfg.get("orphan_cleanup_enabled", False))
        # لا يُحذف ملف يتيم إلا إذا كان orphan_cleanup_enabled=True، والسجل سليم وفيه 5 أحداث على الأقل، وعمر الملف > 24 ساعة
        allow_orphan_delete = orphan_cleanup_enabled and self.events_loaded_ok and (len(current_events) >= 5)
        if allow_orphan_delete:
            # An archived event still owns its media. Archive read failure disables
            # orphan cleanup rather than treating unread records as deletable.
            try:
                if EVENTS_ARCHIVE.exists():
                    with EVENTS_ARCHIVE.open(encoding="utf-8") as archive:
                        for line in archive:
                            if line.strip():
                                entry = json.loads(line)
                                if entry.get("id"):
                                    valid_ids.add(str(entry["id"]))
            except (OSError, ValueError, AttributeError):
                allow_orphan_delete = False

        cloud_cfg = self.cfg.get("cloud_storage") if isinstance(self.cfg.get("cloud_storage"), dict) else {}
        cloud_del_days = int(cloud_cfg.get("delete_local_after_days", 0) or 0)

        cutoff_ts = now - (retention_days * 86400.0)
        orphan_min_age_cutoff = now - 86400.0  # 24 ساعة
        recent_grace_ts = now - 120.0

        remaining_files = []
        deleted_count = 0

        for d in (SNAP_DIR, CLIPS_DIR, PLATES_DIR):
            if not d.exists():
                continue
            for fpath in list(d.iterdir()):
                if not fpath.is_file() or fpath.name.startswith("."):
                    continue
                try:
                    st = fpath.stat()
                except Exception:
                    continue
                mtime = st.st_mtime
                if mtime >= recent_grace_ts:
                    remaining_files.append((mtime, st.st_size, fpath))
                    continue
                stem = fpath.stem
                for pref in ("ai_in_", "ai_crop_", "plate_", "hq_"):
                    if stem.startswith(pref):
                        stem = stem[len(pref):]

                is_orphan = (stem not in valid_ids)
                is_expired = (mtime < cutoff_ts)
                should_delete_orphan = allow_orphan_delete and is_orphan and (mtime < orphan_min_age_cutoff)

                should_delete_cloud_local = False
                if cloud_del_days > 0 and stem in cloud_uploaded_map:
                    if (now - cloud_uploaded_map[stem]) >= (cloud_del_days * 86400.0):
                        should_delete_cloud_local = True

                if is_expired or should_delete_orphan or should_delete_cloud_local:
                    try:
                        fpath.unlink()
                        deleted_count += 1
                    except Exception:
                        pass
                else:
                    remaining_files.append((mtime, st.st_size, fpath))

        max_bytes = max_storage_mb * 1024 * 1024
        total_bytes = sum(sz for _, sz, _ in remaining_files)
        if total_bytes > max_bytes:
            # v16 #R10: ما نحذف ملف جديد (آخر دقيقتين) أو ملف لحدث لسا شغّال
            busy_ids = {str(e.get("id", "")) for e in current_events if self._event_is_busy(e)}
            remaining_files.sort(key=lambda x: x[0])
            for mtime, sz, fpath in remaining_files:
                if total_bytes <= max_bytes:
                    break
                _stem = fpath.stem
                for _pref in ("ai_in_", "ai_crop_", "plate_", "hq_"):
                    if _stem.startswith(_pref):
                        _stem = _stem[len(_pref):]
                if mtime >= recent_grace_ts or _stem in busy_ids:
                    continue
                try:
                    fpath.unlink()
                    total_bytes -= sz
                    deleted_count += 1
                except Exception:
                    pass
        return {"ok": True, "deleted_files": deleted_count}

    # v15 #10: إحصائيات التخزين التفصيلية وتنفيذ المسح المحمي بتأكيد المستخدم
    def get_storage_stats(self):
        def _dir_stats(d: Path):
            if not d.exists():
                return 0, 0
            cnt, sz = 0, 0
            for f in d.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    try:
                        cnt += 1
                        sz += f.stat().st_size
                    except Exception:
                        pass
            return cnt, sz

        snap_cnt, snap_bytes = _dir_stats(SNAP_DIR)
        clip_cnt, clip_bytes = _dir_stats(CLIPS_DIR)
        plate_cnt, plate_bytes = _dir_stats(PLATES_DIR)
        total_bytes = snap_bytes + clip_bytes + plate_bytes

        try:
            disk_free = shutil.disk_usage(str(DATA_DIR)).free
        except Exception:
            disk_free = 0

        with self.events_lock:
            ev_count = len(self.events)
            newest_ts = self.events[0].get("timestamp", "--") if self.events else "لا يوجد"
            oldest_ts = self.events[-1].get("timestamp", "--") if self.events else "لا يوجد"

        return {
            "snapshots_count": snap_cnt,
            "snapshots_mb": round(snap_bytes / (1024.0 * 1024.0), 2),
            "clips_count": clip_cnt,
            "clips_mb": round(clip_bytes / (1024.0 * 1024.0), 2),
            "plates_count": plate_cnt,
            "plates_mb": round(plate_bytes / (1024.0 * 1024.0), 2),
            "total_files": snap_cnt + clip_cnt + plate_cnt,
            "total_mb": round(total_bytes / (1024.0 * 1024.0), 2),
            "free_disk_gb": round(disk_free / (1024.0 ** 3), 2),
            "events_count": ev_count,
            "oldest_event": oldest_ts,
            "newest_event": newest_ts
        }

    @staticmethod
    def _event_epoch(ev):
        '''# v16 #5: وقت الحدث من detected_ts أو من timestamp النصي، نفس القيمة للمعاينة والحذف'''
        try:
            v = float(ev.get("detected_ts") or 0.0)
            if v > 0:
                return v
        except Exception:
            pass
        try:
            return datetime.strptime(str(ev.get("timestamp") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return 0.0

    def clear_storage_by_mode(self, mode="events_only", days=7, preview=False):
        mode = str(mode or "events_only").strip()
        cutoff = time.time() - (max(1, int(days or 7)) * 86400.0)
        files_to_remove = []

        if mode in ("all", "older_than"):
            for d in (SNAP_DIR, CLIPS_DIR, PLATES_DIR):
                if not d.exists():
                    continue
                for f in d.iterdir():
                    if not f.is_file() or f.name.startswith("."):
                        continue
                    try:
                        st = f.stat()
                    except Exception:
                        continue
                    if mode == "all" or st.st_mtime < cutoff:
                        files_to_remove.append((f, st.st_size))

        with self.events_lock:
            if mode in ("events_only", "all"):
                events_to_remove_count = len(self.events)
            elif mode == "older_than":
                events_to_remove_count = sum(1 for e in self.events if self._event_epoch(e) < cutoff)  # v16 #5
            else:
                events_to_remove_count = 0

        total_bytes = sum(sz for _, sz in files_to_remove)
        if preview:
            return {
                "ok": True,
                "preview": True,
                "mode": mode,
                "files_count": len(files_to_remove),
                "events_count": events_to_remove_count,
                "size_bytes": total_bytes,
                "size_mb": round(total_bytes / (1024.0 * 1024.0), 2)
            }

        deleted_files = 0
        deleted_bytes = 0
        for fpath, sz in files_to_remove:
            try:
                fpath.unlink()
                deleted_files += 1
                deleted_bytes += sz
            except Exception:
                pass

        with self.events_lock:
            if mode in ("events_only", "all"):
                self.events = []
            elif mode == "older_than":
                self.events = [e for e in self.events if self._event_epoch(e) >= cutoff]  # v16 #5
            self.events_loaded_ok = True
            save_events(self.events)

        return {
            "ok": True,
            "mode": mode,
            "deleted_files": deleted_files,
            "deleted_events": events_to_remove_count,
            "deleted_bytes": deleted_bytes,
            "deleted_mb": round(deleted_bytes / (1024.0 * 1024.0), 2)
        }

    def get_effective_ai_state(self):
        if self.active_ai_requests > 0:
            return "analyzing"
        return self.ai_connection_state

    def get_active_model_label(self):
        prov = self.cfg.get("ai_provider", "openrouter")
        if prov not in ("gemini", "ollama"):  # v16 #R13
            return self.cfg.get("universal_model", "qwen/qwen2.5-vl-72b-instruct:free")
        if prov == "gemini":
            return self.cfg.get("gemini_model", "gemini-3.8-flash")
        return self.cfg.get("ollama_model", "llava")

    def get_ai_health(self):
        prov = self.cfg.get("ai_provider", "gemini")
        active_model = self.get_active_model_label()
        state = self.get_effective_ai_state()
        has_key = bool((self.cfg.get("gemini_api_key") if prov == "gemini" else self.cfg.get("universal_api_key") or self.cfg.get("ollama_url") or "").strip())
        lat = self.last_ai_latency_ms
        lat_txt = f"{lat}ms" if lat > 0 else ""
        last_used = getattr(self, "ai_last_model_used", "") or active_model

        if state in ("last_succeeded", "verified_vision"):
            working = True
            label = f"يعمل ({last_used})"
            dot = "dot-online"
        elif state == "analyzing":
            working = True
            label = f"جارٍ التحليل ({active_model})"
            dot = "dot-warning"
        elif state in ("last_failed", "error", "vision_unsupported"):
            working = False
            label = f"متوقف ({state})"
            dot = "dot-danger"
        else:
            working = has_key
            label = f"جاهز ({active_model})" if has_key else "مفتاح غير مدخل"
            dot = "dot-online" if has_key else "dot-warning"

        return {
            "provider": prov,
            "model": active_model,
            "last_model_used": last_used,
            "working": working,
            "state": state,
            "status_label": label,
            "dot": dot,
            "latency_ms": lat,
            "last_error": self.ai_last_error,
            "tooltip": f"مزود الرؤية: {prov} | النموذج: {last_used} | الحالة: {label}" + (f" | زمن الاستجابة: {lat_txt}" if lat_txt else "")
        }

    def get_telegram_health(self):
        notifiers_tg = (self.cfg.get("notifiers") or {}).get("telegram") or {}
        token = (notifiers_tg.get("bot_token") or self.cfg.get("telegram_bot_token") or "").strip()
        chat_id = (notifiers_tg.get("chat_id") or self.cfg.get("telegram_chat_id") or "").strip()
        enabled = bool(notifiers_tg.get("enabled", True))
        if not token or not chat_id:
            return {
                "configured": False,
                "enabled": enabled,
                "working": False,
                "bot_username": "",
                "status_label": "غير مدخل",
                "tooltip": "لم يتم إدخال Bot Token أو Chat ID لتيليجرام"
            }
        now = time.time()
        cache = getattr(self, "_tg_health_cache", None)
        if cache and (now - cache[0] < 60.0):
            return cache[1]
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=3.5)
            if r.status_code == 200:
                bot_un = (r.json().get("result") or {}).get("username", "")
                res = {
                    "configured": True,
                    "enabled": enabled,
                    "working": True,
                    "bot_username": bot_un,
                    "status_label": f"متصل (@{bot_un})" if bot_un else "يعمل ومستعد",
                    "tooltip": f"بوت تيليجرام يعمل (@{bot_un}) وجاهز لإرسال إشعارات الأحداث" if bot_un else "بوت تيليجرام متصل بنجاح"
                }
                self._tg_health_cache = (now, res)
                return res
            else:
                res = {
                    "configured": True,
                    "enabled": enabled,
                    "working": False,
                    "bot_username": "",
                    "status_label": f"خطأ ({r.status_code})",
                    "tooltip": f"فشل التحقق من بوت تيليجرام (رمز الخطأ {r.status_code})"
                }
                self._tg_health_cache = (now, res)
                return res
        except Exception as ex:
            res = {
                "configured": True,
                "enabled": enabled,
                "working": False,
                "bot_username": "",
                "status_label": "تعذر الاتصال",
                "tooltip": f"تعذر الاتصال بخوادم تيليجرام: {redact_secrets(ex, self.cfg)[:60]}"
            }
            self._tg_health_cache = (now, res)
            return res

    def get_system_libraries_status(self):
        now = time.time()
        cache = getattr(self, "_lib_status_cache", None)
        if cache and (now - cache[0] < 60.0):
            return cache[1]
        import importlib.util, importlib.metadata
        packages = [
            ("PyTorch (تسريع الذكاء)", "torch", "torch", "تسريع المعالجة العصبية وكرت الشاشة CUDA"),
            ("Ultralytics (YOLO)", "ultralytics", "ultralytics", "محرك كشف وتتبع الأشخاص والمركبات"),
            ("OpenCV (cv2)", "opencv-python", "cv2", "معالجة الفيديو وتدفق الكاميرات وقص الإطارات"),
            ("Fast-ALPR (قارئ اللوحات)", "fast-alpr", "fast_alpr", "التعرف البصري المحلي على أرقام وحروف اللوحات"),
            ("NumPy", "numpy", "numpy", "العمليات الرياضية ومعالجة مصفوفات البكسل"),
            ("Flask", "Flask", "flask", "خادم واجهة التطبيق التفاعلية"),
            ("Requests", "requests", "requests", "الاتصال السحابي بـ Gemini API و Telegram"),
            ("Werkzeug", "Werkzeug", "werkzeug", "بث بروتوكول WSGI وإدارة اتصالات الويب"),
        ]
        result = []
        for display_name, dist, mod, desc in packages:
            installed = importlib.util.find_spec(mod) is not None
            ver = "غير مثبت"
            extra = ""
            if installed:
                try:
                    ver = importlib.metadata.version(dist)
                except Exception:
                    ver = "مثبت"
                if mod == "torch":
                    try:
                        import torch
                        if torch.cuda.is_available():
                            extra = f"CUDA متاح ({torch.cuda.get_device_name(0)})"
                        else:
                            extra = "يعمل عبر المعالج CPU"
                    except Exception:
                        pass
                elif mod == "fast_alpr":
                    from . import plates
                    extra = f"جاهز ({plates._PLATE_READER_STATE})" if hasattr(plates, "_PLATE_READER_STATE") else "جاهز"
            result.append({
                "name": display_name,
                "module": mod,
                "version": ver,
                "installed": installed,
                "working": installed,
                "status_label": "يعمل بنجاح" if installed else "غير مثبت",
                "dot": "dot-online" if installed else "dot-danger",
                "extra": extra,
                "description": desc
            })
        self._lib_status_cache = (now, result)
        return result

    def get_system_tools_status(self):
        from .paths import BIN_DIR, BASE_DIR, DATA_DIR
        tools = [
            ("ffmpeg.exe", BIN_DIR / "ffmpeg.exe", "تسجيل المقاطع عالية الدقة بدون فك ترميز (صفر معالج)"),
            ("ffprobe.exe", BIN_DIR / "ffprobe.exe", "فحص واختبار سلامة ملفات ومقاطع الفيديو"),
            ("go2rtc.exe", BIN_DIR / "go2rtc.exe", "وسيط البث فائق السرعة WebRTC/RTSP"),
        ]
        tool_res = []
        for name, path, desc in tools:
            exists = path.is_file()
            tool_res.append({
                "name": name,
                "installed": exists,
                "status_label": "متاح وجاهز" if exists else "مفقود",
                "dot": "dot-online" if exists else "dot-danger",
                "description": desc
            })
        models = sorted(list(set([p.name for p in list(BASE_DIR.glob("*.pt")) + list(DATA_DIR.glob("*.pt"))])))
        return {
            "binaries": tool_res,
            "models": models
        }

    def get_event_by_id(self, event_id):
        with self.events_lock:
            for ev in self.events:
                if ev.get("id") == event_id:
                    return ev
        return None

    def update_event_fields(self, event_id, persist=True, **updates):
        with self.events_lock:
            target = None
            for ev in self.events:
                if ev.get("id") == event_id:
                    ev.update(updates)
                    target = dict(ev)
                    break
            if target is not None and persist:
                save_events(self.events)
            return target

    def mark_event_ai_overloaded(self, event_id):
        return self.update_event_fields(
            event_id,
            ai_status="overload_skipped",
            is_error=True,
            ai_error_reason="طابور التحليل ممتلئ (ضغط أحداث مرتفع) — تم حفظ اللقطة والمقطع دون حذف أي حدث قديم.",
            description="⚠ فشل التحليل بسبب ضغط الطابور، والمقطع محفوظ (15 ثانية)",
            model_used=self.get_active_model_label()
        )

    def probe_camera_hw(self, cam_id=1):
        with self.lock:
            cam = next((c for c in self.cfg.get("cameras", []) if int(c["id"]) == int(cam_id)), None)
            if not cam:
                return {"isapi_ok": False, "error_reason": "الكاميرا غير موجودة"}
            ip = cam.get("ip", "192.168.8.7").strip()
            http_port = str(cam.get("http_port", "80")).strip() or "80"
            user = cam.get("username", "admin").strip()
            pwd = cam.get("password", "")

        res = query_hikvision_isapi_full(ip, http_port, user, pwd)  # v16 #R19: كانت تنادي دالة غير موجودة (زر فحص الجهاز يفشل)
        if not res.get("isapi_ok"):
            return res

        with self.lock:
            ch_map = res.get("channels", {})
            for c in self.cfg.get("cameras", []):
                ch_key = str(c.get("channel", c.get("id", 1)))
                if ch_key in ch_map:
                    ch_info = ch_map[ch_key]
                    c["cam_hw_info"] = {
                        "cam_model": ch_info["cam_model"],
                        "cam_firmware": ch_info["cam_firmware"],
                        "cam_serial": ch_info["cam_serial"],
                        "nvr_model": ch_info["nvr_model"],
                        "nvr_firmware": ch_info["nvr_firmware"],
                        "rtsp_configured_kbps": ch_info.get("configured_kbps", 0),
                        "source_note": ch_info["source_note"]
                    }
                else:
                    c["cam_hw_info"] = {
                        "cam_model": f"غير متاح للقناة {ch_key}",
                        "cam_firmware": "--",
                        "cam_serial": res.get("base_serial", "--"),
                        "nvr_model": res.get("base_model", "--"),
                        "nvr_firmware": f"{res.get('base_firmware','--')} ({res.get('base_build_date','')})",
                        "rtsp_configured_kbps": 0,
                        "source_note": "قراءة من جهاز التسجيل الأساسي"
                    }

            self.cfg["nvr_device_info"] = {
                "device_name": res.get("device_name"),
                "nvr_model": res.get("base_model"),
                "nvr_firmware": res.get("base_firmware"),
                "nvr_build": res.get("base_build_date"),
                "nvr_serial": res.get("base_serial"),
                "nvr_mac": res.get("base_mac")
            }
            save_config(self.cfg)
        return res

    def sync_workers(self, auto_start=False):
        with self.lock:
            cams = self.cfg.get("cameras", [])[:MAX_CAMERAS]
            valid_ids = {int(c["id"]) for c in cams}
            for cid in list(self.workers.keys()):
                if cid not in valid_ids:
                    self.workers[cid].stop()
                    del self.workers[cid]

            _stream_keys = ("analysis_stream", "ip", "port", "channel", "username", "password", "conn_type", "custom_rtsp", "clip_source")
            for c in cams:
                cid = int(c["id"])
                if cid not in self.workers:
                    self.workers[cid] = SingleCameraWorker(c, self)
                else:
                    w = self.workers[cid]
                    old = w.cam if isinstance(w.cam, dict) else {}
                    changed = any(str(old.get(k, "")) != str(c.get(k, "")) for k in _stream_keys)
                    w.cam = c
                    # v17 #R22: تغيير بث التحليل أو بيانات الاتصال يعيد تشغيل الكاميرا فوراً (كان يحتاج إعادة تشغيل البرنامج)
                    if changed and w.running:
                        threading.Thread(target=w.start, daemon=True).start()
                if auto_start and c.get("enabled", True) and c.get("password"):
                    if not self.workers[cid].running:
                        self.workers[cid].start()
        # v16 #R16: أي تعديل على الكاميرات يتحدّث في go2rtc بدون إعادة تشغيله
        g2 = getattr(self, "go2rtc", None)
        if g2 is not None:
            g2.request_sync()

    def update_camera_roi(self, cam_id, roi_dict):
        cid = int(cam_id)
        with self.lock:
            for c in self.cfg.get("cameras", []):
                if int(c["id"]) == cid:
                    c["roi"] = roi_dict
                    break
            if cid in self.workers:
                self.workers[cid].cam["roi"] = roi_dict
            save_config(self.cfg)

    def start_camera(self, cam_id):
        cid = int(cam_id)
        if cid in self.workers:
            self.workers[cid].start()

    def stop_camera(self, cam_id):
        cid = int(cam_id)
        if cid in self.workers:
            self.workers[cid].stop()

    def start_all_enabled(self):
        for c in self.cfg.get("cameras", []):
            cid = int(c["id"])
            if c.get("enabled", True) and cid in self.workers:
                self.workers[cid].start()

    def stop_all(self):
        for w in self.workers.values():
            w.stop()

    def _record_15s_clip_immediately(self, event_id, worker, pre_items, detected_ts, clip_json_path, mp4_path, post_window_sec=None):
        post_sec = float(post_window_sec) if post_window_sec is not None else float(POST_BUFFER_SEC)
        collected = []
        seen_fids = set()

        for item in (pre_items or []):
            if len(item) == 3:
                fid, fts, jpg_bytes = item
            elif len(item) == 4:
                fid, fts, _, b64 = item
                jpg_bytes = base64.b64decode(b64) if b64 else b""
            else:
                continue
            if fid not in seen_fids and jpg_bytes:
                seen_fids.add(fid)
                collected.append((int(fid), float(fts), jpg_bytes))

        deadline = float(detected_ts) + post_sec
        stream_interrupted = False
        poll_interval = max(0.01, min(0.08, 1.0 / (CLIP_FPS * 2.0)))

        while time.time() < deadline:
            if (worker._stop_event and worker._stop_event.is_set()) or not worker.running or not worker.connected:
                stream_interrupted = True
                break
            with worker.clip_lock:
                recent_items = list(worker.clip_buffer)[-12:]
            for item in recent_items:
                fid, fts, jpg_bytes = item
                if fid not in seen_fids and jpg_bytes and fts <= deadline + 0.15:
                    seen_fids.add(fid)
                    collected.append((int(fid), float(fts), jpg_bytes))
            if worker._stop_event and worker._stop_event.wait(poll_interval):
                stream_interrupted = True
                break

        with worker.clip_lock:
            for item in list(worker.clip_buffer):
                fid, fts, jpg_bytes = item
                if fid not in seen_fids and jpg_bytes and (float(detected_ts) - PRE_BUFFER_SEC - 0.5) <= fts <= (deadline + 0.25):
                    seen_fids.add(fid)
                    collected.append((int(fid), float(fts), jpg_bytes))

        collected.sort(key=lambda x: (x[1], x[0]))

        if not collected:
            self.update_event_fields(
                event_id,
                clip_status="failed",
                clip_status_msg="لم تتوفر إطارات لتسجيل المقطع",
                mp4_url="",
                browser_playable=False,
                clip_duration=0.0
            )
            return

        first_ts = collected[0][1]
        last_ts = collected[-1][1]
        pre_actual = max(0.0, float(detected_ts) - first_ts)
        post_actual = max(0.0, last_ts - float(detected_ts))

        if len(collected) >= 2 and (last_ts - first_ts) > 0.05:
            actual_duration = round(last_ts - first_ts, 2)
            effective_fps = round((len(collected) - 1) / (last_ts - first_ts), 2)
        else:
            actual_duration = round(len(collected) / float(CLIP_FPS), 2)
            effective_fps = float(CLIP_FPS)

        writer_fps = max(1.0, min(30.0, effective_fps))

        if stream_interrupted or (post_sec >= 1.0 and post_actual < post_sec * 0.55):
            clip_status = "stream_interrupted"
            clip_status_msg = f"مقطع ناقص ({actual_duration}s) بسبب انقطاع البث أثناء التسجيل"
        elif PRE_BUFFER_SEC >= 1.0 and pre_actual < (PRE_BUFFER_SEC * 0.5):
            clip_status = "partial_pre_buffer"
            clip_status_msg = f"مقطع جزئي ({actual_duration}s — توفر {pre_actual:.1f}s فقط قبل الحدث)"
        else:
            clip_status = "complete"
            clip_status_msg = f"مكتمل ({actual_duration}s)"

        mp4_verified = False
        used_codec = ""
        mp4_error = ""
        tmp_mp4 = CLIPS_DIR / f".{mp4_path.name}.tmp.{threading.get_ident()}.mp4"
        try:
            first_bgr = cv2.imdecode(np.frombuffer(collected[0][2], dtype=np.uint8), cv2.IMREAD_COLOR)
            if first_bgr is not None:
                fh, fw = first_bgr.shape[:2]
                # v16 #R16: مقاس المقطع حسب إطار منتصف المقطع (لو أول الإطارات كانت من البث الخفيف مؤقتاً)
                _mid = cv2.imdecode(np.frombuffer(collected[len(collected) // 2][2], dtype=np.uint8), cv2.IMREAD_COLOR)
                if _mid is not None and _mid.shape[1] > fw:
                    fh, fw = _mid.shape[:2]
                    first_bgr = cv2.resize(first_bgr, (fw, fh))
                # v14 fix #10: ترتيب الترميز avc1 ثم H264 ثم mp4v وحفظ browser_playable والـ metadata فقط عند نجاح MP4
                # v16 #R3: نكتب الفيديو بمعدل ثابت حسب وقت وصول كل إطار (تكرار/إسقاط) عشان السرعة تطابق الواقع
                out_fps = float(CLIP_FPS)
                span = max(0.0, last_ts - first_ts)
                n_out = max(1, int(round(span * out_fps)) + 1)
                writer_fps = out_fps
                for fourcc_str in ("avc1", "H264", "mp4v"):
                    writer = cv2.VideoWriter(str(tmp_mp4), cv2.VideoWriter_fourcc(*fourcc_str), out_fps, (fw, fh))
                    if not writer.isOpened():
                        continue
                    src_idx = 0
                    cur_bgr = first_bgr
                    dec_idx = 0
                    for k in range(n_out):
                        t_k = first_ts + k / out_fps
                        while src_idx + 1 < len(collected) and collected[src_idx + 1][1] <= t_k + 1e-6:
                            src_idx += 1
                        if src_idx != dec_idx:
                            dec = cv2.imdecode(np.frombuffer(collected[src_idx][2], dtype=np.uint8), cv2.IMREAD_COLOR)
                            if dec is not None:
                                cur_bgr = dec if dec.shape[:2] == (fh, fw) else cv2.resize(dec, (fw, fh))
                            dec_idx = src_idx
                        writer.write(cur_bgr)
                    writer.release()
                    # v16 #R4: نتحقق إن الملف ينفتح ويُقرأ منه إطار فعلاً، مو بس من حجمه
                    valid = False
                    if tmp_mp4.exists() and tmp_mp4.stat().st_size > 256:
                        chk = cv2.VideoCapture(str(tmp_mp4))
                        try:
                            ok_read, chk_frame = chk.read()
                            valid = bool(ok_read and chk_frame is not None)
                        finally:
                            chk.release()
                    if valid:
                        os.replace(tmp_mp4, mp4_path)
                        mp4_verified = True
                        used_codec = fourcc_str
                        break
                    try:
                        tmp_mp4.unlink()
                    except Exception:
                        pass
        except Exception as ex:
            mp4_error = redact_secrets(ex, self.cfg)
        finally:
            if tmp_mp4.exists():
                try:
                    tmp_mp4.unlink()
                except Exception:
                    pass

        if not mp4_verified:
            clip_status = "failed"
            clip_status_msg = f"فشل إنشاء ملف MP4: {mp4_error or 'لم يقبل المشفر الإطارات'}"

        rel_timestamps = [round(max(0.0, it[1] - first_ts), 3) for it in collected]
        trigger_offset = round(max(0.0, float(detected_ts) - first_ts), 2)
        verified_mp4_url = f"/clips/{mp4_path.name}" if (mp4_verified and mp4_path.exists() and mp4_path.stat().st_size > 256) else ""
        browser_playable = bool(verified_mp4_url and used_codec in ("avc1", "H264"))

        clip_payload = {
            "ok": bool(verified_mp4_url or len(collected) > 0),
            "event_id": event_id,
            "fps": CLIP_FPS,
            "effective_fps": writer_fps,
            "duration_sec": actual_duration,
            "pre_duration_sec": round(pre_actual, 2),
            "post_duration_sec": round(post_actual, 2),
            "trigger_offset_sec": trigger_offset,
            "unique_frames": len(collected),
            "clip_status": clip_status,
            "clip_status_msg": clip_status_msg,
            "mp4_verified": bool(verified_mp4_url),
            "mp4_url": verified_mp4_url,
            "codec": used_codec,
            "browser_playable": browser_playable,
            "frame_timestamps_rel": rel_timestamps
        }
        if not browser_playable:  # v16 #R6: نحفظ الإطارات البديلة لو الـ MP4 ما يشتغل في المتصفح
            clip_payload["frames"] = [base64.b64encode(it[2]).decode("utf-8") for it in collected if it[2]]

        try:
            atomic_write_json(clip_json_path, clip_payload, indent=None)
        except Exception as ex:
            clip_status = "failed"
            clip_status_msg = f"فشل حفظ بيانات المقطع: {redact_secrets(ex, self.cfg)}"

        self.update_event_fields(
            event_id,
            clip_status=clip_status,
            clip_status_msg=clip_status_msg,
            mp4_url=verified_mp4_url,
            browser_playable=browser_playable,
            clip_duration=actual_duration,
            effective_fps=writer_fps,
            pre_duration_sec=round(pre_actual, 2),
            post_duration_sec=round(post_actual, 2)
        )

    def _record_clip_ffmpeg(self, event_id, worker, rec, pre_items, detected_ts, clip_json_path, mp4_path, post_window_sec=None):
        '''# v16 #R18: مقطع الحدث من مقاطع ffmpeg الدوّارة (نسخ بدون إعادة ترميز)'''
        post_sec = float(post_window_sec) if post_window_sec is not None else float(POST_BUFFER_SEC)
        res = rec.make_clip(detected_ts, PRE_BUFFER_SEC, post_sec, mp4_path)
        if not res.get("ok"):
            self.update_event_fields(event_id, clip_status_msg=f"ffmpeg: {res.get('error', '')[:120]} — نستخدم الطريقة القديمة")
            return self._record_15s_clip_immediately(event_id, worker, pre_items, detected_ts, clip_json_path, mp4_path, post_window_sec)
        duration = float(res["duration"])
        pre_actual = max(0.0, float(detected_ts) - float(res["first_start"]))
        post_actual = max(0.0, duration - pre_actual)
        if post_actual < post_sec * 0.55:
            clip_status, clip_status_msg = "stream_interrupted", f"مقطع ناقص ({duration}s) بسبب انقطاع البث أثناء التسجيل"
        elif pre_actual < PRE_BUFFER_SEC * 0.5:
            clip_status, clip_status_msg = "partial_pre_buffer", f"مقطع جزئي ({duration}s — توفر {pre_actual:.1f}s فقط قبل الحدث)"
        else:
            clip_status, clip_status_msg = "complete", f"مكتمل ({duration}s • {res['width']}x{res['height']} {res['codec']})"
        payload = {
            "ok": True, "event_id": event_id, "recorder": "ffmpeg", "fps": rec.fps, "effective_fps": rec.fps,
            "duration_sec": round(duration, 2), "pre_duration_sec": round(pre_actual, 2), "post_duration_sec": round(post_actual, 2),
            "trigger_offset_sec": round(pre_actual, 2), "unique_frames": int(duration * (rec.fps or 0)),
            "clip_status": clip_status, "clip_status_msg": clip_status_msg, "mp4_verified": True,
            "mp4_url": f"/clips/{mp4_path.name}", "codec": res["codec"], "resolution": f"{res['width']}x{res['height']}",
            # v17 #R21: H.264 يشتغل في كل المتصفحات؛ HEVC يحتاج دعم عتادي (Chrome/Edge على ويندوز 11 يدعمونه) — الواجهة تتحقق
            "browser_playable": True, "needs_hevc": (res["codec"] == "hevc"), "frame_timestamps_rel": []
        }
        try:
            atomic_write_json(clip_json_path, payload, indent=None)
        except Exception as ex:
            clip_status, clip_status_msg = "failed", f"فشل حفظ بيانات المقطع: {redact_secrets(ex, self.cfg)}"
        self.update_event_fields(
            event_id, clip_status=clip_status, clip_status_msg=clip_status_msg, mp4_url=payload["mp4_url"],
            browser_playable=True, clip_duration=round(duration, 2), effective_fps=rec.fps,
            pre_duration_sec=round(pre_actual, 2), post_duration_sec=round(post_actual, 2)
        )

    def register_and_dispatch_event(
        self,
        cam_id,
        disp_frame=None,
        raw_frame=None,
        trigger_reason="تحليل فوري",
        boxes=None,
        pre_frames=None,
        detected_ts=None,
        track_ids=None,
        post_window_sec=None,
        detector_meta=None
    ):
        '''
        # v15 #4 / # v15 #6: يسجل الحدث فوراً ويجهز صورتين لنموذج الرؤية:
        (أ) المشهد الكامل مرسوماً عليه الخط والمضلّع، والجسم المسبب بمربع أحمر عليه رقمه (#track_id).
        (ب) قصّة مكبّرة للجسم نفسه (المربع + 30%).
        '''
        cid = int(cam_id)
        worker = self.workers.get(cid)
        if not worker:
            return None

        # v16 #3: منع تكرار نفس الحدث لما يكون المصدر "الاثنين" (الكاشف المحلي + أحداث الكاميرا ISAPI)
        _src_backend = str((detector_meta or {}).get("detector_backend") or "")
        if _src_backend:
            _src_kind = "isapi" if _src_backend.startswith("isapi") else "local"
            _dedupe_sec = float(self.cfg.get("dual_source_dedupe_sec", 6) or 6)
            _now_chk = float(detected_ts) if detected_ts is not None else time.time()
            with self.events_lock:
                if not hasattr(self, "_last_src_event"):
                    self._last_src_event = {}
                _prev = self._last_src_event.get(cid)
                if _prev and _prev[1] != _src_kind and abs(_now_chk - _prev[0]) < _dedupe_sec:
                    return None
                self._last_src_event[cid] = (_now_chk, _src_kind)

        if disp_frame is None or raw_frame is None:
            with worker.frame_lock:
                if worker.raw_frame is not None:
                    if raw_frame is None:
                        raw_frame = worker.raw_frame.copy()
                    if disp_frame is None:
                        disp_frame = worker.raw_frame.copy()
                    if detected_ts is None:
                        detected_ts = worker.raw_frame_ts or time.time()

        if disp_frame is None:
            return None

        true_detected_ts = float(detected_ts) if detected_ts is not None else time.time()
        dt_obj = datetime.fromtimestamp(true_detected_ts)
        ts_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
        detected_iso = dt_obj.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        with self.events_lock:
            self._event_seq += 1
            seq_num = self._event_seq

        file_ts = dt_obj.strftime("%Y%m%d_%H%M%S_%f")[:19]
        event_id = f"cam{cid}_{file_ts}_{seq_num:04d}"
        snap_name = f"{event_id}.jpg"
        snap_path = SNAP_DIR / snap_name
        ai_input_path = SNAP_DIR / f"ai_in_{event_id}.jpg"
        ai_crop_path = SNAP_DIR / f"ai_crop_{event_id}.jpg"
        clip_json_path = CLIPS_DIR / f"{event_id}.json"
        mp4_path = CLIPS_DIR / f"{event_id}.mp4"

        vis_snap = disp_frame.copy()
        sh, sw = vis_snap.shape[:2]
        active_boxes = list(boxes) if boxes is not None else list(worker.motion_boxes)
        det_meta = dict(detector_meta or {})
        target_box = det_meta.get("target_box") or (active_boxes[0] if active_boxes else None)
        primary_tid = int(det_meta.get("track_id") or (track_ids[0] if track_ids else 1))
        geo_hint = classify_geometry_target(active_boxes, sw, sh)

        roi = worker.cam.get("roi") or {}
        if roi.get("zone_enabled"):
            poly_norm = roi.get("polygon")
            if poly_norm and len(poly_norm) >= 3:
                pts_px = np.array([[int(float(pt[0]) * sw), int(float(pt[1]) * sh)] for pt in poly_norm], dtype=np.int32)
                cv2.polylines(vis_snap, [pts_px], isClosed=True, color=(0, 230, 153), thickness=max(2, int(3 * (sh / 720.0))))
            else:
                zx1, zy1 = int(float(roi.get("zx1", 0.15)) * sw), int(float(roi.get("zy1", 0.15)) * sh)
                zx2, zy2 = int(float(roi.get("zx2", 0.85)) * sw), int(float(roi.get("zy2", 0.85)) * sh)
                cv2.rectangle(vis_snap, (zx1, zy1), (zx2, zy2), (0, 230, 153), max(2, int(3 * (sh / 720.0))))
        if roi.get("line_enabled"):
            lx1, ly1 = int(float(roi.get("lx1", 0.2)) * sw), int(float(roi.get("ly1", 0.55)) * sh)
            lx2, ly2 = int(float(roi.get("lx2", 0.8)) * sw), int(float(roi.get("ly2", 0.55)) * sh)
            cv2.line(vis_snap, (lx1, ly1), (lx2, ly2), (0, 212, 255), max(2, int(3 * (sh / 720.0))))

        # v15 #6: رسم الجسم المسبب للحدث بمربع أحمر (0, 0, 255) وعليه رقمه (#tid)
        if target_box and len(target_box) == 4:
            tbx, tby, tbw, tbh = [int(v) for v in target_box]
            cv2.rectangle(vis_snap, (tbx, tby), (tbx + tbw, tby + tbh), (0, 0, 255), 3)
            cv2.putText(
                vis_snap,
                f"#{primary_tid}",
                (max(4, tbx), max(22, tby - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 0, 255),
                2,
                cv2.LINE_AA
            )
        elif active_boxes:
            for (bx, by, bw, bh) in active_boxes[:3]:
                cv2.rectangle(vis_snap, (bx, by), (bx + bw, by + bh), (0, 0, 255), 2)

        # حفظ القصة المكبرة للجسم المسبب (+30%) لإرسالها كصورة ثانية للـ AI
        raw_at_det = raw_frame if raw_frame is not None else disp_frame
        crop_img = build_ai_verification_crop(raw_at_det, target_box, pad_ratio=0.30)
        if crop_img is not None:
            ok_cr, cr_buf = cv2.imencode(".jpg", crop_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if ok_cr:
                ai_crop_path.write_bytes(cr_buf.tobytes())

        cam_an = worker.cam.get("analysis") if isinstance(worker.cam.get("analysis"), dict) else {}
        lpr_setting_on = lpr_enabled(worker.cam)
        plate_candidate_url = ""
        ai_input_frame = vis_snap.copy()

        plate_text, plate_conf = "", 0.0
        _plate_job = None
        if lpr_setting_on and det_meta.get("detector_class") == "vehicle":
            _plate_job = (raw_at_det, target_box)

        ok_vis, vis_buf = cv2.imencode(".jpg", vis_snap, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if ok_vis:
            snap_path.write_bytes(vis_buf.tobytes())
        ok_ai, ai_buf = cv2.imencode(".jpg", ai_input_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if ok_ai:
            ai_input_path.write_bytes(ai_buf.tobytes())

        cam_name = worker.cam.get("name", f"كاميرا {cid}")
        initial_event = {
            "id": event_id,
            "cam_id": cid,
            "cam_name": cam_name,
            "detected_ts": round(true_detected_ts, 3),
            "detected_at": detected_iso,
            "timestamp": ts_str,
            "trigger": trigger_reason,
            "initial_trigger": trigger_reason,
            "track_ids": list(track_ids or []),
            "category": det_meta.get("detector_class", "general"),
            "subtype": det_meta.get("detector_subtype", ""),
            "detector_meta": det_meta,
            "confirmed": False,
            "confirmation_status": "pending",
            "ai_json": {},
            "description": "تم رصد الحدث وحفظ اللقطة — بانتظار تحقق الذكاء الاصطناعي...",
            "important": False,
            "is_person": False,
            "is_lpr": False,
            "plate_url": "",
            "plate_status": "not_requested",
            "plate_text": plate_text,
            "plate_conf": plate_conf,
            "_candidate_plate_url": plate_candidate_url,
            "_snap_path": str(snap_path),
            "_ai_input_path": str(ai_input_path),
            "_ai_crop_path": str(ai_crop_path),
            "is_error": False,
            "ai_status": "pending",
            "ai_error_reason": "",
            "fps_at_capture": worker.stream_fps,
            "ai_latency_ms": 0,
            "alert_delay_ms": 0,
            "model_used": "قيد الانتظار",
            "image_url": f"/snapshots/{snap_name}",
            "clip_id": event_id,
            "clip_status": "recording",
            "clip_status_msg": "جارٍ تسجيل المقطع (15 ثانية)...",
            "mp4_url": "",
            "browser_playable": False,
            "clip_duration": 0.0,
            "telegram_status": "not_requested",
            "telegram_status_msg": "لم يُطلب الإرسال",
            "telegram_accept_ms": None,
            "notify_results": {},
            "cloud_status": "disabled",
            "cloud_url": ""
        }

        with self.events_lock:
            self.events.insert(0, initial_event)
            # v14 fix #9: قص قائمة الأحداث في الذاكرة إلى MAX_STORED_EVENTS بعد كل insert
            if len(self.events) > MAX_STORED_EVENTS:
                # v16 #R8: نحذف الأقدم المكتمل بس؛ الحدث اللي لسا قيد التحليل/التسجيل/الإرسال يبقى
                _overflow = self.events[MAX_STORED_EVENTS:]
                self.events = self.events[:MAX_STORED_EVENTS] + [e for e in _overflow if self._event_is_busy(e)]
                self.archive_events([e for e in _overflow if not self._event_is_busy(e)])  # v17 #R21
            save_events(self.events)

        initial_pre = pre_frames if pre_frames is not None else worker.get_pre_buffer_snapshot(true_detected_ts)
        _rec = worker.recorder
        if _rec is not None and _rec.healthy:
            # v16 #R18: مقطع بنسخ البث (بدون فك ترميز) مع الرجوع للطريقة القديمة لو فشل
            rec_thread = threading.Thread(
                target=self._record_clip_ffmpeg,
                args=(event_id, worker, _rec, initial_pre, true_detected_ts, clip_json_path, mp4_path, post_window_sec),
                daemon=True
            )
        else:
            rec_thread = threading.Thread(
                target=self._record_15s_clip_immediately,
                args=(event_id, worker, initial_pre, true_detected_ts, clip_json_path, mp4_path, post_window_sec),
                daemon=True
            )
        rec_thread.start()

        if _plate_job is not None:
            self._start_plate_job(event_id, _plate_job[0], _plate_job[1], worker=worker, detected_ts=true_detected_ts,
                                  on_ready=lambda: worker.enqueue_event_for_ai(event_id))
        else:
            worker.enqueue_event_for_ai(event_id)
        return initial_event

    @staticmethod
    def _gemini_order(candidates, cooldown, dead, now, available=None):
        return ready_models(candidates, cooldown, dead, now, available)

    def _gemini_available_models(self, api_key):
        '''# v17 #R28: قائمة الموديلات المتاحة فعلاً للمفتاح (ListModels)، مخزنة ساعة؛ None لو تعذر الجلب'''
        cache = getattr(self, "_gemini_models_cache", None)
        if cache and time.time() - cache[0] < 3600:
            return cache[1]
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": api_key}, timeout=3)
            if r.status_code == 200:
                names = set()
                for m in (r.json().get("models") or []):
                    if "generateContent" in (m.get("supportedGenerationMethods") or []):
                        names.add(str(m.get("name", "")).replace("models/", ""))
                if names:
                    self._gemini_models_cache = (time.time(), names)
                    return names
        except Exception:
            pass
        self._gemini_models_cache = (time.time(), None)
        return None

    def _hq_frame_at_event(self, worker, detected_ts, event_id=None):
        '''# v17 #R27: إطار 4K/12MP عند لحظة الحدث من تسجيل ffmpeg (أو None)؛ ويحفظه كلقطة عالية الدقة للحدث'''
        rec = getattr(worker, "recorder", None) if worker is not None else None
        if rec is None or not rec.healthy:
            return None
        # المقطع المفتوح يحتاج ثواني قليلة عشان يُكتب الإطار على القرص
        hq = None
        for _ in range(2):
            hq = rec.grab_frame_at(detected_ts, timeout=2.0)
            if hq is not None:
                break
            time.sleep(0.5)
        if hq is not None and event_id:
            try:
                hq_name = f"hq_{event_id}.jpg"
                ok_hq, hq_buf = cv2.imencode(".jpg", hq, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                if ok_hq:
                    (SNAP_DIR / hq_name).write_bytes(hq_buf.tobytes())
                    self.update_event_fields(event_id, persist=False, hq_image_url=f"/snapshots/{hq_name}")
            except Exception:
                pass
        return hq

    def _start_plate_job(self, event_id, frame, box, worker=None, detected_ts=None, on_ready=None):
        ready_called = False
        started = time.monotonic()
        def publish_ready():
            nonlocal ready_called
            if not ready_called:
                ready_called = True
                if on_ready is not None:
                    on_ready()
        fallback = dict(evidence_source="analysis_fallback",
                        evidence_resolution=f"{frame.shape[1]}x{frame.shape[0]}" if frame is not None else "",
                        evidence_status="fallback")
        if not self._plate_sem.acquire(blocking=False):
            self.update_event_fields(event_id, plate_status="busy", plate_error="قارئ اللوحات مشغول",
                                     evidence_reason="worker_capacity", **fallback)
            publish_ready()
            return
        done = threading.Event()
        with self.events_lock:
            if not hasattr(self, "_plate_jobs"):
                self._plate_jobs = {}
            self._plate_jobs[event_id] = done
        self.update_event_fields(event_id, plate_status="waiting_hq", evidence_status="waiting")
        def _run():
            try:
                rec = getattr(worker, "recorder", None)
                wait_sec = max(0.5, min(30, float(self.cfg.get("plate_hq_wait_sec", 12))))
                src, reason, attempts = wait_recorded_frame(rec, detected_ts, timeout=wait_sec)
                recorded = src is not None
                if src is None:
                    src = frame
                    self.update_event_fields(event_id, evidence_reason=reason, evidence_attempts=attempts, **fallback)
                else:
                    name = f"hq_{event_id}.jpg"
                    ok, buf = cv2.imencode(".jpg", src, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                    if not ok:
                        raise RuntimeError("تعذر حفظ صورة التسجيل")
                    (SNAP_DIR / name).write_bytes(buf.tobytes())
                    self.update_event_fields(event_id, hq_image_url=f"/snapshots/{name}",
                        evidence_source="recording", evidence_resolution=f"{src.shape[1]}x{src.shape[0]}",
                        evidence_status="ready", evidence_reason="", evidence_attempts=attempts)
                self.update_event_fields(event_id, evidence_wait_ms=int((time.monotonic()-started)*1000))
                # AI is queued only after image readiness/fallback is decided. OCR
                # continues independently; the AI worker no longer races a 1s wait.
                publish_ready()
                if src is None:
                    self.update_event_fields(event_id, plate_status="failed", plate_error="صورة الحدث غير متاحة")
                    return
                if get_plate_reader() is None:
                    self.update_event_fields(event_id, plate_status="reader_unavailable",
                                             plate_error="قارئ اللوحات غير جاهز؛ صورة الحدث محفوظة للمراجعة")
                    return
                self.update_event_fields(event_id, plate_status="reading")
                samples, crops = [], []
                ocr_deadline = time.monotonic() + 8
                seen_frames = set()
                for index, offset in enumerate((0.0, -0.35, 0.35)):
                    if index:
                        if not recorded or time.monotonic() >= ocr_deadline:
                            break
                        src, _, _ = wait_recorded_frame(rec, detected_ts + offset,
                            timeout=min(2, max(0, ocr_deadline-time.monotonic())))
                        if src is None:
                            continue
                    import hashlib
                    key = hashlib.sha256(src.tobytes()).hexdigest()
                    if key in seen_frames:
                        continue
                    seen_frames.add(key)
                    src_box = box
                    if frame is not None and box:
                        sy, sx = src.shape[0]/frame.shape[0], src.shape[1]/frame.shape[1]
                        src_box = (int(box[0]*sx), int(box[1]*sy), int(box[2]*sx), int(box[3]*sy))
                    pr = read_best_plate(src, src_box)
                    crop = pr.get("crop")
                    quality_ok = False
                    if crop is not None and crop.size:
                        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                        quality_ok = crop.shape[1] >= 80 and crop.shape[0] >= 20 and cv2.Laplacian(gray, cv2.CV_64F).var() >= 35
                        crops.append((normalize_plate(pr.get("text")), float(pr.get("conf", 0)), crop))
                    samples.append(dict(text=pr.get("text", ""), conf=pr.get("conf", 0),
                                        quality_ok=bool(quality_ok), frame_key=key))
                    if pr.get("error"):
                        self.update_event_fields(event_id, plate_error=pr["error"])
                upd = assess_plate(samples)
                upd["is_lpr"] = upd["plate_status"] == "confirmed"
                upd["plate_frame_source"] = "recording" if recorded else "analysis_fallback"
                matching = [c for c in crops if c[0] == upd["plate_text"]] or crops
                if matching:
                    _, _, crop = max(matching, key=lambda c: c[1])
                    upd["plate_native_resolution"] = f"{crop.shape[1]}x{crop.shape[0]}"
                    img = enhance_plate_crop(crop)
                    name = f"plate_{event_id}.jpg"
                    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                    if ok:
                        (PLATES_DIR / name).write_bytes(buf.tobytes())
                        upd["_candidate_plate_url"] = f"/plates/{name}"
                        upd["plate_url"] = f"/plates/{name}"
                upd["plate_completed_ts"] = time.time()
                upd["plate_processing_ms"] = int((time.monotonic()-started)*1000)
                self.update_event_fields(event_id, **upd)
            except Exception as ex:
                self.update_event_fields(event_id, plate_status="failed", plate_error=redact_secrets(ex, self.cfg)[:160])
                publish_ready()
            finally:
                done.set()
                self._plate_sem.release()
                with self.events_lock:
                    self._plate_jobs.pop(event_id, None)
        threading.Thread(target=_run, daemon=True, name=f"evidence-{event_id}").start()

    def _wait_plate_job(self, event_id, timeout=1.0):  # v17 #R27: جلب إطار 12MP من التسجيل ياخذ ثواني
        job = getattr(self, "_plate_jobs", {}).get(event_id)
        if job is None:
            return
        job.wait(timeout)
        with self.events_lock:
            if job.is_set():
                self._plate_jobs.pop(event_id, None)

    def process_queued_event_ai(self, event_id):
        '''
        # v15 #6 & #7 & #8 & #11:
        يرسل صورتين للـ AI (المشهد الكامل + القصة المكبرة +30%) ويطلب رد JSON مهيكل،
        ويتحقق من شروط تأكيد الحدث (confirmed) قبل إرسال الإشعارات أو الرفع السحابي.
        '''
        ev = self.get_event_by_id(event_id)
        if not ev:
            return None

        _t_dequeued = time.time()  # v17 #R26: انتظار الطابور يُقاس هنا (قبل انتظار اللوحة وجلب الصورة عالية الدقة)
        # Recorded evidence is resolved before entering this queue; OCR can still be running.
        ev = self.get_event_by_id(event_id) or ev  # نعيد القراءة عشان تدخل نتيجة اللوحة
        cid = int(ev.get("cam_id", 1))
        worker = self.workers.get(cid)
        cam_cfg = worker.cam if worker else {}
        cam_an = cam_cfg.get("analysis") if isinstance(cam_cfg.get("analysis"), dict) else make_default_cam_analysis()
        cam_name = ev.get("cam_name", f"كاميرا {cid}")
        trigger_reason = ev.get("initial_trigger") or ev.get("trigger") or "رصد حركة"
        lpr_setting_on = lpr_enabled(cam_cfg)
        det_meta = ev.get("detector_meta") or {}
        ev_kind = str(det_meta.get("event_kind") or ("line_cross" if "خط" in trigger_reason else "zone_motion"))
        cross_dir = str(det_meta.get("crossing_direction") or "غير محدد")
        det_cls = str(det_meta.get("detector_class") or "other")
        det_conf = float(det_meta.get("detector_confidence") or 0.55)
        tid_num = int(det_meta.get("track_id") or 1)

        # v16 #R9: لقطة عالية الدقة من البث الرئيسي وقت الحدث (للصورة والقصة المكبرة واللوحات)
        hq_crop = self._grab_hq_for_event(ev, worker, det_cls, lpr_setting_on)

        # إذا كان خيار ai_verify معطلاً للكاميرا، نعتمد نتيجة الكاشف المحلي مباشرة
        if not bool(cam_an.get("ai_verify", True)):
            fallback_json = {
                "category": det_cls if det_cls in ("person", "vehicle", "animal", "other") else "other",
                "subtype": str(det_meta.get("detector_subtype") or ""),
                "in_zone": ev_kind == "zone_motion",
                "crossed_line": ev_kind == "line_cross",
                "count": 1,
                "plate": "",
                "confidence": det_conf,
                "description_ar": f"رصد محلي مباشر ({det_cls}) بدون تحقق AI سحابي"
            }
            _cat = fallback_json["category"]
            _important = _cat in set(cam_an.get("alert_classes") or ["person", "vehicle"])
            _labels = {"person": "🚶 رصد مرور شخص", "vehicle": "🚗 رصد مرور مركبة", "animal": "🐾 رصد حيوان"}
            _final_trigger = f"{_labels[_cat]} - {cam_name}" if _cat in _labels else f"❔ {trigger_reason}"
            _snap_path = Path(ev.get("_snap_path") or (SNAP_DIR / f"{event_id}.jpg"))
            self.update_event_fields(
                event_id,
                ai_status="succeeded",
                ai_json=fallback_json,
                confirmed=True,
                confirmation_status="confirmed",
                category=_cat,
                subtype=fallback_json["subtype"],
                trigger=_final_trigger,
                is_person=(_cat == "person"),
                important=_important,
                description=fallback_json["description_ar"],
                model_used=f"Local ({self.local_detector.backend})"
            )
            for _tmp in (ev.get("_ai_input_path"), ev.get("_ai_crop_path")):
                try:
                    if _tmp and Path(_tmp).is_file() and Path(_tmp) != _snap_path:
                        Path(_tmp).unlink()
                except Exception:
                    pass
            # v16 #R2: الإشعار والرفع السحابي يكملون حتى لو تحقق الـ AI معطّل لهالكاميرا
            self._dispatch_finalize_async(event_id, ev, _snap_path, True, _important, False,
                                          _final_trigger, fallback_json["description_ar"], time.time())
            return self.get_event_by_id(event_id)

        self.update_event_fields(event_id, ai_status="analyzing", description="جارٍ التحقق من الجسم عبر نموذج الرؤية (صورتان + JSON)...")

        ai_in_path = Path(ev.get("_ai_input_path") or ev.get("_snap_path") or (SNAP_DIR / f"{event_id}.jpg"))
        ai_crop_path = Path(ev.get("_ai_crop_path") or "")
        snap_path = Path(ev.get("_snap_path") or (SNAP_DIR / f"{event_id}.jpg"))
        saved_frame = cv2.imread(str(ai_in_path)) if ai_in_path.exists() else (cv2.imread(str(snap_path)) if snap_path.exists() else None)
        crop_frame = hq_crop if hq_crop is not None else (cv2.imread(str(ai_crop_path)) if (ai_crop_path and ai_crop_path.is_file()) else None)

        if saved_frame is None:
            err_txt = "فشل التحليل: تعذر قراءة صورة الحدث المحفوظة من القرص."
            return self.update_event_fields(
                event_id,
                ai_status="failed",
                is_error=True,
                ai_error_reason=err_txt,
                description=err_txt,
                model_used=self.get_active_model_label()
            )

        prompt = (
            f"أنت وكيل رؤية أمنية بصري للكاميرا «{cam_name}».\n"
            f"- نوع الحدث: {ev_kind} ({trigger_reason})\n"
            f"- اتجاه العبور: {cross_dir}\n"
            f"- تصنيف الكاشف المحلي للجسم رقم #{tid_num} (المحدد بالمربع الأحمر): الفئة={det_cls}\n"
            "- تحذير: الكاشف المحلي يخطئ أحياناً مع أوراق الشجر والظلال والأغراض الثابتة. لا تفترض صحته.\n"
            "- لو ما فيه جسم من هذي الفئة واضح فعلاً داخل المربع الأحمر (أو القصّة المكبّرة) أرجع category=\"none\" وconfidence منخفضة.\n"
            "- confidence = ثقتك أنت (0 إلى 1) إن الجسم حقيقي ومن الفئة المذكورة، مو رقم الكاشف.\n"
            "- لا تذكر رقم اللوحة في description_ar. يمكنك وضع قراءة واضحة فقط في plate كاقتراح للمراجعة؛ اتركه فارغاً إذا لم تكن الأحرف واضحة ولا تخمّن. اقتراحك لا يؤكد قراءة ANPR.\n"
            f"- تعليمات المالك: {self.cfg.get('custom_rules', '')}\n"
            "- ملاحظة: عبور الخط أو دخول المنطقة تم التحقق منه مسبقاً بالمتتبع. مهمتك تأكيد نوع الجسم داخل المربع الأحمر وأنه جسم حقيقي وليس ظلاً أو انعكاساً.\n"
            + "\n"
            "لديك صورتان مرفقتان:\n"
            "1) الصورة الأولى: المشهد الكامل وعليه الخط والمضلّع والجسم المسبب محاطاً بمربع أحمر ورقمه.\n"
            "2) الصورة الثانية: قصّة مكبّرة (+30%) للجسم المسبب للتحقق الدقيق.\n\n"
            "أجب بصيغة JSON فقط بدون أي نص إضافي بهذا الشكل الدقيق:\n"
            '{"category":"person|vehicle|animal|other|none","subtype":"car|truck|motorcycle|bicycle|bus|cat|dog|bird|...","in_zone":true,"crossed_line":true,"count":1,"plate":"","confidence":0.0,"description_ar":"وصف عربي مختصر ودقيق لما يفعله الجسم"}'
        )

        t_ai_start = time.time()
        _queue_wait_ms = int(max(0.0, _t_dequeued - float(ev.get("_ai_queued_ts") or _t_dequeued)) * 1000)  # v17 #R21/#R26
        ai_res = self.call_vision_ai_detailed(saved_frame, prompt, second_frame=crop_frame, request_json=True)
        t_ai_end = time.time()
        ai_latency_ms = int((t_ai_end - t_ai_start) * 1000)
        self.last_ai_latency_ms = ai_latency_ms

        ai_ok = bool(ai_res.get("ok"))
        ai_reply = redact_secrets(ai_res.get("reply", ""), self.cfg)
        actual_model = ai_res.get("model_used") or self.get_active_model_label()
        ai_err_reason = redact_secrets(ai_res.get("error_reason", ""), self.cfg)

        is_error = (not ai_ok) or ai_reply.startswith("خطأ") or ai_reply.startswith("[ملاحظة]")
        ai_json = parse_ai_json_response(
            ai_reply,
            default_in_zone=(ev_kind == "zone_motion"),
            default_crossed_line=(ev_kind == "line_cross")
        )
        parsed_cat = ai_json.get("category", "other")
        if parsed_cat == "none":
            parsed_cat_legacy = "normal"
        else:
            parsed_cat_legacy = parsed_cat

        ai_says_person = (not is_error) and (parsed_cat == "person")
        ai_says_vehicle = (not is_error) and (parsed_cat == "vehicle")
        ai_says_animal = (not is_error) and (parsed_cat == "animal")
        ai_says_normal = (not is_error) and (parsed_cat in ("none", "normal"))

        # v15 #6: شروط تأكيد الحدث (confirmed)
        detect_classes = set(cam_an.get("detect_classes") or ["person", "vehicle", "animal", "other"])
        alert_classes = set(cam_an.get("alert_classes") or ["person", "vehicle"])
        min_conf = float(cam_an.get("min_confidence", 0.45))
        # v17 #R20: تأكيد الـAI يحتاج ثقته هو (الافتراضي 0.60) مو ثقة الكاشف
        min_conf = max(min_conf, float(cam_an.get("ai_confirm_min_conf", 0.60) or 0.0))
        ai_conf = float(ai_json.get("confidence", 0.0))
        # v16 #2: الهندسة (عبور الخط / داخل المنطقة) يحكم فيها المتتبع المحلي أو الكاميرا لأنهم شافوا الحركة فعلاً.
        # الـ AI يتحقق من نوع الجسم والثقة فقط؛ صورة وحدة ما تثبت العبور، فقيم in_zone/crossed_line تبقى للعرض.
        geometry_matches = True
        is_confirmed = bool(
            (not is_error)
            and (parsed_cat in detect_classes)
            and (parsed_cat != "none")
            and (ai_conf >= min_conf)
            and geometry_matches
        )
        confirmation_status = "confirmed" if is_confirmed else ("failed" if is_error else "unconfirmed")

        # Reload the independently completing OCR result. AI text is never promoted to OCR evidence.
        ev = self.get_event_by_id(event_id) or ev
        candidate_plate_url = ev.get("_candidate_plate_url", "")
        plate_img_url = candidate_plate_url if lpr_setting_on and ai_says_vehicle else ""
        confirmed_lpr = bool(plate_img_url and ev.get("plate_status") == "confirmed")
        ai_json["plate_proposal"] = str(ai_json.get("plate") or "")
        ai_json["plate"] = ev.get("plate_text", "") if confirmed_lpr else ""
        ai_json["plate_source"] = "anpr_consensus" if confirmed_lpr else "unverified"
        desc_ar = ai_json.get("description_ar") or ai_reply
        for tmp in (ai_in_path, ai_crop_path):
            if tmp and tmp.is_file() and tmp != snap_path:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        if ai_says_person:
            final_trigger = f"🚶 رصد مرور شخص - {cam_name}"
            target_category = "person"
        elif confirmed_lpr:
            final_trigger = f"🚘 رصد مركبة ولوحة - {cam_name}"
            target_category = "vehicle"
        elif ai_says_vehicle:
            final_trigger = f"🚗 رصد مرور مركبة - {cam_name}"
            target_category = "vehicle"
        elif ai_says_animal:
            final_trigger = f"🐾 رصد حيوان - {cam_name}"
            target_category = "animal"
        elif ai_says_normal:
            final_trigger = trigger_reason
            target_category = "normal"
        else:
            final_trigger = f"❔ {trigger_reason}"
            target_category = "other" if parsed_cat == "other" else "unknown"

        is_important = bool(is_confirmed and (target_category in alert_classes))
        if not is_confirmed and not is_error:
            desc_ar = f"[غير مؤكد] {desc_ar}"

        self.update_event_fields(
            event_id,
            trigger=final_trigger,
            category=target_category,
            subtype=ai_json.get("subtype", ""),
            confirmed=is_confirmed,
            confirmation_status=confirmation_status,
            ai_json=ai_json,
            ai_plate_proposal=ai_json.get("plate_proposal", ""),
            description=desc_ar,
            important=is_important,
            is_person=ai_says_person,
            is_error=is_error,
            ai_status="failed" if is_error else "succeeded",
            ai_error_reason=ai_err_reason if is_error else "",
            ai_latency_ms=ai_latency_ms,
            ai_queue_wait_ms=_queue_wait_ms,
            model_requested=self.get_active_model_label(),
            model_used=actual_model,
            ai_fallback_reason=(getattr(self, "ai_fallback_note", "") if actual_model != self.get_active_model_label() else "")
        )

        # v16 #R5: الرفع السحابي والإشعارات يصيرون في خيط مستقل، فعامل التحليل يكمل للحدث اللي بعده
        self._dispatch_finalize_async(event_id, ev, snap_path, is_confirmed, is_important, is_error,
                                      final_trigger, desc_ar, t_ai_start)
        return self.get_event_by_id(event_id)

    def _grab_hq_for_event(self, ev, worker, det_cls, lpr_on):
        url = ev.get("hq_image_url")
        if not url:
            return None
        hq = cv2.imread(str(SNAP_DIR / Path(url).name))
        if hq is None:
            return None
        box = (ev.get("detector_meta") or {}).get("target_box")
        snap = cv2.imread(str(ev.get("_snap_path") or ""))
        if snap is None or not box:
            return None
        sx, sy = hq.shape[1]/snap.shape[1], hq.shape[0]/snap.shape[0]
        return build_ai_verification_crop(hq, (int(box[0]*sx), int(box[1]*sy), int(box[2]*sx), int(box[3]*sy)), pad_ratio=0.30)

    def _dispatch_finalize_async(self, event_id, ev, snap_path, is_confirmed, is_important, is_error,
                                 final_trigger, desc_ar, t_ai_start):
        '''# v16 #R5: ينفّذ الرفع والإشعار بالخلفية (بحد أقصى 3 أحداث بنفس الوقت)'''
        def _run():
            with self._notify_sem:
                try:
                    self._finalize_cloud_and_notify(event_id, ev, snap_path, is_confirmed, is_important, is_error,
                                                    final_trigger, desc_ar, t_ai_start)
                except Exception as ex:
                    self.update_event_fields(event_id, telegram_status="failed",
                                             telegram_status_msg=f"فشل الإرسال: {redact_secrets(ex, self.cfg)}")
        threading.Thread(target=_run, daemon=True).start()

    def _finalize_cloud_and_notify(self, event_id, ev, snap_path, is_confirmed, is_important, is_error,
                                   final_trigger, desc_ar, t_ai_start):
        if is_confirmed and is_important and not is_error:
            self.update_event_fields(event_id, telegram_status="waiting_evidence",
                                     telegram_status_msg="بانتظار نتيجة اللوحة قبل تجهيز التنبيه")
            self._wait_plate_job(event_id, timeout=max(0, min(15, float(self.cfg.get("plate_notify_wait_sec", 8)))))
        # v15 #11: رفع الحدث المؤكد سحابياً (إن كان التخزين السحابي مفعلاً) للحصول على رابط مؤقت قبل الإشعار أو بالخلفية
        cloud_cfg = self.cfg.get("cloud_storage") if isinstance(self.cfg.get("cloud_storage"), dict) else {}
        if cloud_cfg.get("enabled", False) and (is_confirmed or not cloud_cfg.get("confirmed_only", True)):
            if is_confirmed and is_important and not is_error:
                # v16 #4: نرفع الحدث قبل الإشعار (بحد أقصى pre_notify_timeout_sec) عشان رابط الصورة يوصل مع واتساب وntfy
                self._upload_before_notify(event_id, timeout_sec=float(cloud_cfg.get("pre_notify_timeout_sec", 15) or 15))
            else:
                self.cloud_manager.enqueue_event_upload(event_id)

        # v15 #6 & #8: لا تُرسل تنبيهات خارجية للأحداث غير المؤكدة (unconfirmed)
        updated_ev = self.get_event_by_id(event_id) or ev
        if is_confirmed and is_important and not is_error:
            self.update_event_fields(
                event_id,
                telegram_status="sending",
                telegram_status_msg="قيد الإرسال للمزودات المفعلة..."
            )
            ts_str = updated_ev.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            updated_ev = dict(updated_ev)
            caption_txt = f"🚨 {final_trigger} ({ts_str})\n\n" + evidence_caption(updated_ev, desc_ar)
            image_path = snap_path
            hq_url = updated_ev.get("hq_image_url")
            if hq_url and (SNAP_DIR / Path(hq_url).name).is_file():
                image_path = SNAP_DIR / Path(hq_url).name
            self.update_event_fields(event_id, notified_plate_status=updated_ev.get("plate_status"),
                notified_plate_text=updated_ev.get("plate_text", ""),
                notified_image_source="recording" if image_path != snap_path else "analysis_fallback")
            notify_res = self.notifier.dispatch_all_configured(
                updated_ev,
                image_path,
                caption_txt,
                cloud_url=(str(updated_ev.get("cloud_url") or "") if str(updated_ev.get("cloud_url") or "").startswith(("http://", "https://")) else "")  # v16 #4
            )
            tg_res = notify_res.get("telegram") or {
                "status": "skipped",
                "message": "لم يُرسل: تيليجرام غير مضبوط (لا يوجد توكن/معرّف محادثة)",
                "accept_ms": None
            }
            t_alert_done = time.time()
            det_ts_val = float(updated_ev.get("detected_ts") or t_ai_start)
            self.last_alert_delay_ms = int((t_alert_done - det_ts_val) * 1000)
            self.update_event_fields(
                event_id,
                notify_results=notify_res,
                telegram_status=tg_res.get("status", "not_requested"),
                telegram_status_msg=tg_res.get("message", ""),
                telegram_accept_ms=tg_res.get("accept_ms"),
                alert_delay_ms=self.last_alert_delay_ms
            )
        else:
            t_alert_done = time.time()
            det_ts_val = float(updated_ev.get("detected_ts") or t_ai_start)
            self.last_alert_delay_ms = int((t_alert_done - det_ts_val) * 1000)
            if is_error:
                skip_msg = "لم يُرسل: فشل تحليل الـAI"
            elif not is_confirmed:
                skip_msg = "لم يُرسل: الحدث غير مؤكد (الـAI ما أكّد نوع الجسم أو ثقته أقل من الحد)"
            else:
                skip_msg = "لم يُرسل: نوع الحدث خارج أنواع التنبيه (alert_classes) لهذه الكاميرا"
            self.update_event_fields(
                event_id,
                telegram_status="not_requested",
                telegram_status_msg=skip_msg,
                telegram_accept_ms=None,
                alert_delay_ms=self.last_alert_delay_ms
            )

    def _upload_before_notify(self, event_id, timeout_sec=15.0):
        '''# v16 #4: يرفع الحدث للسحابة وينتظر بحد أقصى timeout_sec؛ لو طوّل يكمل الرفع بالخلفية والإشعار يطلع بدون رابط'''
        done = threading.Event()

        def _run():
            try:
                self.update_event_fields(event_id, cloud_status="uploading")
                self.cloud_manager.upload_event_now(event_id)
            except Exception as ex:
                self.update_event_fields(event_id, cloud_status="failed", cloud_error=redact_secrets(ex, self.cfg))
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()
        done.wait(max(1.0, float(timeout_sec)))
        return done.is_set()

    def analyze_and_record_15s_event(self, cam_id, frame=None, trigger_reason="تحليل فوري", boxes=None, pre_frames=None, trigger_ts=None, wait_for_ai=False, post_window_sec=None):
        ev = self.register_and_dispatch_event(
            cam_id=cam_id,
            disp_frame=frame,
            raw_frame=frame,
            trigger_reason=trigger_reason,
            boxes=boxes,
            pre_frames=pre_frames,
            detected_ts=trigger_ts,
            post_window_sec=post_window_sec
        )
        if not ev:
            return None
        if wait_for_ai:
            ev_id = ev["id"]
            wait_deadline = time.time() + 30.0
            while time.time() < wait_deadline:
                cur = self.get_event_by_id(ev_id)
                if cur and cur.get("ai_status") in ("succeeded", "failed", "overload_skipped"):
                    return cur
                time.sleep(0.05)
        return self.get_event_by_id(ev["id"])

    def _encode_ai_resized_b64(self, frame):
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if max(h, w) > AI_MAX_IMAGE_DIM:
            sc = AI_MAX_IMAGE_DIM / float(max(h, w))
            ai_frame = cv2.resize(frame, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
        else:
            ai_frame = frame
        ok, buf = cv2.imencode(".jpg", ai_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return base64.b64encode(buf.tobytes()).decode("utf-8") if ok else None

    @staticmethod
    def _is_vision_unsupported_error(status_code, err_text):
        low = str(err_text or "").lower()
        vision_indicators = (
            "image_url",
            "image input",
            "does not support image",
            "doesn't support image",
            "not support image",
            "vision is not supported",
            "multimodal",
            "text-only",
            "text only",
            "unsupported content type",
            "invalid content type",
            "cannot read image",
            "no endpoints found that support image",
        )
        return any(ind in low for ind in vision_indicators)

    @staticmethod
    def _gemini_retry_delay(resp):
        '''# v16 #R13: يقرأ retryDelay من رد Google (مثل "37s") عشان نعرف متى نرجع نجرب الموديل'''
        try:
            for d in ((resp.json().get("error") or {}).get("details") or []):
                rd = str(d.get("retryDelay") or "").strip()
                if rd.endswith("s"):
                    return float(rd[:-1])
        except Exception:
            pass
        return 0.0

    # v15 #6: دعم إرسال صورتين (المشهد الكامل + القصة المكبرة) وطلب response_format={"type":"json_object"} وresponseMimeType="application/json"
    def call_vision_ai_detailed(self, frame, prompt_text, second_frame=None, request_json=False):
        provider = self.cfg.get("ai_provider", "openrouter")
        model_used = self.get_active_model_label()
        img_b64 = self._encode_ai_resized_b64(frame)
        crop_b64 = self._encode_ai_resized_b64(second_frame) if second_frame is not None else None
        if not img_b64:
            self.ai_connection_state = "last_failed"
            self.ai_last_error = "لا توجد صورة صالحة لإرسالها"
            return {
                "ok": False,
                "reply": "خطأ: لا توجد صورة صالحة لإرسالها لنموذج الرؤية.",
                "model_used": model_used,
                "ai_state": "last_failed",
                "error_reason": self.ai_last_error
            }

        with self.lock:
            self.active_ai_requests += 1

        try:
            # v16 #R13: أي مزود غير Gemini وOllama يُعامل كواجهة متوافقة مع OpenAI (OpenAI، OpenRouter، Groq، Mistral، xAI، DeepSeek، Qwen، LM Studio، أو أي رابط مخصص)
            if provider not in ("gemini", "ollama"):
                api_key = self.cfg.get("universal_api_key", "").strip()
                base_url = self.cfg.get("universal_base_url", "https://openrouter.ai/api/v1").strip().rstrip("/")
                model_name = self.cfg.get("universal_model", "qwen/qwen2.5-vl-72b-instruct:free").strip()
                model_used = model_name
                if not api_key and re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)", base_url):
                    api_key = "local-no-key"  # الخوادم المحلية (LM Studio / vLLM) ما تحتاج مفتاح

                if not api_key:
                    self.ai_connection_state = "untested"
                    return {
                        "ok": False,
                        "reply": "[ملاحظة] لم يتم إدخال مفتاح الـ API في الإعدادات بعد.",
                        "model_used": model_used,
                        "ai_state": "untested",
                        "error_reason": "مفتاح API غير مدخل"
                    }

                endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                user_content = [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                ]
                if crop_b64:
                    user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{crop_b64}"}})

                vision_payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": user_content}],
                    "max_tokens": 400,
                    "temperature": 0.15
                }
                if request_json:
                    vision_payload["response_format"] = {"type": "json_object"}

                try:
                    resp = requests.post(endpoint, headers=headers, json=vision_payload, timeout=28)
                    # بعض نماذج OpenRouter المجانية ترفض حقل response_format، نعيد المحاولة بدونه تلقائياً
                    if resp.status_code in (400, 422) and request_json and "response_format" in vision_payload:
                        vision_payload.pop("response_format", None)
                        resp = requests.post(endpoint, headers=headers, json=vision_payload, timeout=28)

                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices") or []
                        reply_txt = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
                        actual_resp_model = data.get("model") or model_name
                        if not reply_txt:
                            self.ai_connection_state = "last_failed"
                            self.ai_last_error = "رد فارغ من نموذج الرؤية"
                            return {
                                "ok": False,
                                "reply": "خطأ: استلم النظام رداً فارغاً من نموذج الرؤية.",
                                "model_used": actual_resp_model,
                                "ai_state": "last_failed",
                                "error_reason": self.ai_last_error
                            }
                        self.ai_connection_state = "last_succeeded"
                        self.ai_last_error = ""
                        return {
                            "ok": True,
                            "reply": reply_txt,
                            "model_used": actual_resp_model,
                            "ai_state": "last_succeeded",
                            "error_reason": ""
                        }
                    else:
                        raw_err = redact_secrets(resp.text[:260], self.cfg)
                        try:
                            err_json = resp.json()
                            if isinstance(err_json.get("error"), dict):
                                raw_err = redact_secrets(err_json["error"].get("message") or raw_err, self.cfg)
                            elif err_json.get("error"):
                                raw_err = redact_secrets(str(err_json["error"]), self.cfg)
                        except Exception:
                            pass

                        if self._is_vision_unsupported_error(resp.status_code, raw_err):
                            self.ai_connection_state = "vision_unsupported"
                            self.ai_last_error = f"النموذج {model_name} لا يدعم الصور ({raw_err})"
                            return {
                                "ok": False,
                                "reply": f"خطأ رؤية بصرية ({resp.status_code}): النموذج «{model_name}» لا يدعم قراءة الصور ({raw_err}).",
                                "model_used": model_name,
                                "ai_state": "vision_unsupported",
                                "error_reason": self.ai_last_error
                            }
                        self.ai_connection_state = "last_failed"
                        self.ai_last_error = f"HTTP {resp.status_code}: {raw_err}"
                        return {
                            "ok": False,
                            "reply": f"خطأ من خادم الرؤية ({resp.status_code}): {raw_err}",
                            "model_used": model_name,
                            "ai_state": "last_failed",
                            "error_reason": self.ai_last_error
                        }
                except Exception as ex:
                    clean_ex = redact_secrets(ex, self.cfg)
                    self.ai_connection_state = "last_failed"
                    self.ai_last_error = clean_ex
                    return {
                        "ok": False,
                        "reply": f"خطأ في الاتصال بخادم الرؤية: {clean_ex}",
                        "model_used": model_name,
                        "ai_state": "last_failed",
                        "error_reason": clean_ex
                    }

            elif provider == "gemini":
                api_key = self.cfg.get("gemini_api_key", "").strip() or self.cfg.get("universal_api_key", "").strip()
                preferred = self.cfg.get("gemini_model", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
                model_used = preferred
                if not api_key:
                    self.ai_connection_state = "untested"
                    return {
                        "ok": False,
                        "reply": "[ملاحظة] لم يتم إدخال مفتاح Gemini API بعد.",
                        "model_used": model_used,
                        "ai_state": "untested",
                        "error_reason": "مفتاح Gemini غير مدخل"
                    }

                # v16 #R13: قائمة احتياط قابلة للتعديل وكلها موديلات موجودة فعلاً (حذفنا 1.5 و2.0 اللي ترجع 404)
                # v17 #R28: بدائل موجودة فعلاً لهذا المفتاح (2.5-flash-lite كان يرجع 404 "no longer available")
                _fallbacks = self.cfg.get("gemini_fallback_models") or [
                    "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"
                ]
                candidates_models = [preferred] + [str(m).strip() for m in _fallbacks]
                seen = []
                for m in candidates_models:
                    if m and m not in seen:
                        seen.append(m)

                parts_list = [
                    {"text": prompt_text},
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
                ]
                if crop_b64:
                    parts_list.append({"inline_data": {"mime_type": "image/jpeg", "data": crop_b64}})

                payload = {"contents": [{"parts": parts_list}]}
                if request_json:
                    payload["generationConfig"] = {"responseMimeType": "application/json"}

                last_err = ""
                # v14 fix #11: نقل مفتاح Gemini من رابط الطلب ?key= إلى الترويسة x-goog-api-key
                gemini_headers = {
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json"
                }
                # v16 #R13: الموديل اللي نفدت حصته (429) أو غير موجود (404) يتوقف مؤقتاً، فالأحداث الجاية تروح مباشرة للموديل الشغّال
                if not hasattr(self, "_gemini_cooldown"):
                    self._gemini_cooldown = {}
                fallback_notes = []
                if not hasattr(self, "_gemini_dead"):
                    self._gemini_dead = set()
                _avail = self._gemini_available_models(api_key)
                _order, _skipped = self._gemini_order(seen, self._gemini_cooldown, self._gemini_dead, time.time(), _avail)
                fallback_notes.extend(_skipped)
                deadline = time.monotonic() + 18
                if not _order:
                    last_err = "لا يوجد نموذج جاهز؛ النماذج غير متاحة أو في فترة انتظار"
                for model_name in _order:
                    if time.monotonic() >= deadline:
                        last_err = "انتهت ميزانية وقت محاولات Gemini"
                        break
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
                    # v17 #R23: الموديل الأساسي يجرّب مرة وحدة بمهلة أقصر لو فيه بدائل؛ البديل الأخير ياخذ محاولتين
                    _is_last = (model_name == seen[-1])
                    for attempt in range(1):
                        try:
                            resp = requests.post(url, headers=gemini_headers, json=payload, timeout=(3, max(1, min(10, deadline-time.monotonic()))))
                            if resp.status_code == 200:
                                data = resp.json()
                                reply_txt = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                                self._gemini_cooldown.pop(model_name, None)
                                if hasattr(self, "_gemini_strikes"):
                                    self._gemini_strikes.pop(model_name, None)
                                self.ai_connection_state = "last_succeeded"
                                self.ai_last_error = ""
                                self.ai_last_model_used = data.get("modelVersion") or model_name
                                self.ai_fallback_note = ("، ".join(fallback_notes) + f" ← أجاب {model_name}") if fallback_notes else ""
                                return {
                                    "ok": True,
                                    "reply": reply_txt,
                                    "model_used": self.ai_last_model_used,
                                    "fallback_note": self.ai_fallback_note,
                                    "ai_state": "last_succeeded",
                                    "error_reason": ""
                                }
                            last_err = redact_secrets(f"({resp.status_code}) {resp.text[:160]}", self.cfg)
                            if resp.status_code == 429:
                                _delay = self._gemini_retry_delay(resp)
                                # v17 #R23: تبريد يتضاعف مع تكرار 429 (3 دقائق → 6 → 12 ... حتى ساعتين) بدل إعادة المحاولة كل حدث
                                _strikes = getattr(self, "_gemini_strikes", {})
                                _strikes[model_name] = min(6, _strikes.get(model_name, 0) + 1)
                                self._gemini_strikes = _strikes
                                self._gemini_cooldown[model_name] = time.time() + min(7200.0, max(60.0, _delay or 180.0) * (2 ** (_strikes[model_name] - 1)))
                                fallback_notes.append(f"{model_name}: نفدت الحصة (429)")
                                break
                            if resp.status_code == 404:
                                self._gemini_cooldown[model_name] = time.time() + 86400.0
                                self._gemini_dead.add(model_name)  # v17 #R28: ما نرجع له أبداً في هذه الجلسة
                                fallback_notes.append(f"{model_name}: غير متاح لهالمفتاح (404)")
                                break
                            if resp.status_code == 503 and attempt == 0:
                                time.sleep(1.0)
                                continue
                            fallback_notes.append(f"{model_name}: HTTP {resp.status_code}")
                            break
                        except Exception as ex:
                            last_err = redact_secrets(ex, self.cfg)
                            fallback_notes.append(f"{model_name}: خطأ اتصال")
                            break
                self.ai_fallback_note = "، ".join(fallback_notes)
                self.ai_connection_state = "last_failed"
                self.ai_last_error = last_err
                return {
                    "ok": False,
                    "reply": f"خطأ من Gemini API: {last_err}",
                    "model_used": model_used,
                    "ai_state": "last_failed",
                    "error_reason": last_err
                }

            else:
                base_url = self.cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
                model = self.cfg.get("ollama_model", "llava").strip() or "llava"
                model_used = model
                imgs_arr = [img_b64] + ([crop_b64] if crop_b64 else [])
                payload = {"model": model, "prompt": prompt_text, "images": imgs_arr, "stream": False}
                if request_json:
                    payload["format"] = "json"
                try:
                    resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=45)
                    if resp.status_code == 200:
                        reply_txt = (resp.json().get("response") or "").strip()
                        if reply_txt:
                            self.ai_connection_state = "last_succeeded"
                            self.ai_last_error = ""
                            return {
                                "ok": True,
                                "reply": reply_txt,
                                "model_used": model,
                                "ai_state": "last_succeeded",
                                "error_reason": ""
                            }
                    raw_err = redact_secrets(resp.text[:200], self.cfg)
                    if self._is_vision_unsupported_error(resp.status_code, raw_err):
                        self.ai_connection_state = "vision_unsupported"
                    else:
                        self.ai_connection_state = "last_failed"
                    self.ai_last_error = f"Ollama ({resp.status_code}): {raw_err}"
                    return {
                        "ok": False,
                        "reply": f"خطأ Ollama ({resp.status_code}): {raw_err}",
                        "model_used": model,
                        "ai_state": self.ai_connection_state,
                        "error_reason": self.ai_last_error
                    }
                except Exception as ex:
                    clean_ex = redact_secrets(ex, self.cfg)
                    self.ai_connection_state = "last_failed"
                    self.ai_last_error = clean_ex
                    return {
                        "ok": False,
                        "reply": f"تعذر الاتصال بـ Ollama المحلي: {clean_ex}",
                        "model_used": model,
                        "ai_state": "last_failed",
                        "error_reason": clean_ex
                    }
        finally:
            with self.lock:
                self.active_ai_requests = max(0, self.active_ai_requests - 1)

    def call_vision_ai_strict(self, frame, prompt_text):
        res = self.call_vision_ai_detailed(frame, prompt_text)
        return res.get("reply", "")

    def _send_telegram_alert(self, image_path, caption, cam_id=None, max_attempts=2, plate_path=None):
        from contextlib import ExitStack
        cfg = (self.cfg.get("notifiers") or {}).get("telegram") or {}
        token = str(cfg.get("bot_token") or self.cfg.get("telegram_bot_token") or "").strip()
        chat_id = str(cfg.get("chat_id") or self.cfg.get("telegram_chat_id") or "").strip()
        if getattr(self, "preview", False):
            return dict(ok=False, status="skipped", message="وضع المعاينة", attempts=0)
        if not token or not chat_id:
            return dict(ok=False, status="not_requested", message="بيانات تيليجرام غير مضبوطة", attempts=0)
        image_path = Path(image_path)
        if not image_path.is_file():
            return dict(ok=False, status="failed", message="صورة الحدث غير موجودة", attempts=0)
        album = bool(plate_path and Path(plate_path).is_file())
        method = "sendMediaGroup" if album else "sendPhoto"
        url = f"https://api.telegram.org/bot{token}/{method}"
        started = time.monotonic()
        for attempt in range(1, max(1, int(max_attempts)) + 1):
            try:
                with ExitStack() as stack:
                    files = {"photo": stack.enter_context(image_path.open("rb"))}
                    data = {"chat_id": chat_id}
                    if album:
                        files["plate"] = stack.enter_context(Path(plate_path).open("rb"))
                        data["media"] = json.dumps([
                            {"type": "photo", "media": "attach://photo", "caption": str(caption)[:1000]},
                            {"type": "photo", "media": "attach://plate", "caption": "قصّة اللوحة للمراجعة؛ حالة القراءة موضحة في الحدث"}], ensure_ascii=False)
                    else:
                        data["caption"] = str(caption)[:1000]
                    response = requests.post(url, data=data, files=files, timeout=(3, 10))
                payload = response.json()
                ms = int((time.monotonic()-started)*1000)
                if response.status_code == 200 and payload.get("ok") is True:
                    result = payload.get("result") or {}
                    messages = result if isinstance(result, list) else [result]
                    self.last_telegram_ms = ms
                    return dict(ok=True, status="accepted", message="قُبل من تيليجرام؛ وصول الهاتف غير مؤكد",
                        accept_ms=ms, message_id=messages[0].get("message_id") if messages else None,
                        message_ids=[m.get("message_id") for m in messages], attachments=len(messages), attempts=attempt)
                code = int(payload.get("error_code") or response.status_code)
                if code == 429:
                    retry = max(1, float((payload.get("parameters") or {}).get("retry_after", 1)))
                    if retry <= 3 and attempt < max_attempts:
                        time.sleep(retry)
                        continue
                    return dict(ok=False, status="rate_limited", message="تيليجرام طلب الانتظار؛ لم يتم الإرسال", retry_after=retry, attempts=attempt)
                return dict(ok=False, status="failed", message=redact_secrets(payload.get("description", f"HTTP {code}"), self.cfg), attempts=attempt)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                # Request may have reached Telegram. Automatic resend risks duplicates.
                return dict(ok=False, status="unknown", message="انقطع الرد؛ حالة القبول غير معروفة، لم تتم إعادة الإرسال", attempts=attempt)
            except Exception as ex:
                return dict(ok=False, status="failed", message=redact_secrets(ex, self.cfg), attempts=attempt)

    def answer_user_chat(self, question, cam_id=1):
        with self.events_lock:
            recent_events = [e for e in self.events if not e.get("is_error")][:20]
        history_text = "\n".join(
            [f"- [{e['timestamp']}] ({e.get('cam_name','كاميرا')}): {e['description']}" for e in recent_events]
        ) or "لا توجد أحداث سابقة مسجلة."

        worker = self.workers.get(int(cam_id)) or next(iter(self.workers.values()), None)
        frame = None
        if worker:
            with worker.frame_lock:
                if worker.raw_frame is not None:
                    frame = worker.raw_frame.copy()

        if frame is None:
            frame = np.zeros((320, 480, 3), dtype=np.uint8)
            cv2.putText(frame, "NO LIVE FRAME", (120, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        prompt = (
            "أنت الوكيل الأمني البصري لمركز الكاميرات. "
            f"سؤال المالك: «{question}»\n\n"
            f"سجل آخر الأحداث المرصودة:\n{history_text}\n\n"
            "شاهد الصورة الحية المرفقة من الكاميرا الآن وأجب بدقة ووضوح باللغة العربية."
        )
        t0 = time.time()
        ans = self.call_vision_ai_strict(frame, prompt)
        self.last_ai_latency_ms = int((time.time() - t0) * 1000)
        return ans

