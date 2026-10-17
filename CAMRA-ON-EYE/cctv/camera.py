# -*- coding: utf-8 -*-
"""cctv.camera — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import os
import math
import time
import threading
from collections import deque, Counter
import cv2
import numpy as np
from .config import is_within_schedule, make_default_cam_analysis, redact_secrets
from .geometry import check_zone_intrusion, point_in_polygon
from .paths import CLIP_FPS, MAX_PENDING_AI_PER_CAM, PRE_BUFFER_SEC
from .recorder import FfmpegSegmentRecorder
from .rtsp import build_cam_rtsp
from .tracker import CentroidLineTracker


class SingleCameraWorker:
    '''
    # v15 #3 / # v15 #4: عامل الكاميرا المحدّث بثلاثة مسارات (Threads) مفصولة:
    1) مسار القراءة (_capture_loop): يقرأ من stream_live (البث الفرعي 02 افتراضياً) ويحفظ مرجع آخر إطار بدون copy.
    2) مسار الكشف (_detect_loop): يعمل على صورة مصغرة بعرض 640 وبمعدل detect_fps قابل للضبط (الافتراضي 8) باستخدام Event.wait.
    3) مسار ضغط العرض (_display_encode_loop): يعمل فقط إذا active_viewers > 0 (محمي بقفل viewers_lock)، بمعدل live_fps (الافتراضي 15)،
       وأقصى عرض live_max_width (الافتراضي 1280)، وجودة JPEG 75، مع حساب وقت الضغط encode_ms وfps العرض display_fps.
    كما يوفر capture_hq_frame لفتح stream_hq (البث الرئيسي 01) عند الحاجة فقط (لقطة الحدث، قراءة اللوحات، أو التسجيل عالي الدقة).
    '''
    def __init__(self, cam_cfg, manager):
        self.cam = cam_cfg
        self.manager = manager
        self.cam_id = int(cam_cfg["id"])
        self.running = False
        self._stop_event = threading.Event()
        self._stop_event.set()
        self.connected = False
        self.analyzing = False
        self.status_msg = "متوقف"

        self.frame_lock = threading.Lock()
        self.raw_frame = None
        self.raw_frame_ts = 0.0
        self.frame_id = 0

        # v15 #3: حماية active_viewers بقفل مستقل وإشعار خيط الضغط فوراً عند دخول مشاهد
        self.viewers_lock = threading.Lock()
        self.active_viewers = 0
        self.viewer_active_event = threading.Event()

        # v15 #4: حالة إطار العرض المضغوط وإشعار مولد MJPEG بدون sleep ثابت
        self.display_cond = threading.Condition()
        self.display_jpeg = None
        self.display_jpeg_by_width = {}
        self.display_seq = 0

        # v14 fix #7: تقليص clip_buffer إلى CLIP_FPS * (PRE_BUFFER_SEC + 3) وحفظ (frame_id, frame_ts, jpeg_bytes) فقط
        self.clip_lock = threading.Lock()
        self.clip_buffer = deque(maxlen=CLIP_FPS * (PRE_BUFFER_SEC + 3))
        self.last_clip_frame_id = -1
        self.last_clip_sample_time = 0.0

        self.ai_lock = threading.Lock()
        self.pending_ai_queue = deque()
        self.max_pending_ai = MAX_PENDING_AI_PER_CAM

        self.tracker = CentroidLineTracker()
        self.track_cooldown_map = {}
        self.last_zone_motion_ts = -1e9

        self.stream_fps = 0.0
        self.display_fps = 0.0
        self.detect_fps = 0.0
        self.encode_ms = 0.0
        self.mjpeg_kbps = 0
        self.resolution_str = "0x0"
        self.motion_level = 0.0
        self.motion_boxes = []
        self.latest_detections = []
        self.det_size = (640, 360)  # v16 #R17: أبعاد إطار الكشف (لتطبيع المربعات للواجهة)
        self.line_triggered_flash = 0
        self.last_trigger_time = 0.0

        self.cap_thread = None
        self.proc_thread = None
        self.enc_thread = None
        self.clip_thread = None  # v16 #R15
        # v16 #R16: مصدر إطارات المقطع (third / main / live / fallback)
        self.clip_src_kind = "live"
        self.clip_src_label = ""
        self.clip_src_fps = 0.0
        self.clip_hq_frame = None
        self.clip_hq_ts = 0.0
        self.clip_seq = 0
        self.recorder = None  # v16 #R18: FfmpegSegmentRecorder
        self.analysis_override = None  # v17 #R21
        self.source_via_relay = False  # v17 #R23
        self.relay_block_until = 0.0   # v17 #R23: لو الوسيط ما فتح، نتصل مباشرة بالكاميرا لدقيقتين ثم نعيد المحاولة
        self.analysis_note = ""        # v17 #R24: سبب الرجوع للفرعي لو «عالي» غير ممكن
        self.hq_lock = threading.Lock()

    # v15 #3: دوال آمنة خيطياً لتعديل وقراءة عدد المشاهدين النشطين
    def increment_viewers(self):
        with self.viewers_lock:
            self.active_viewers += 1
            self.viewer_active_event.set()
            return self.active_viewers

    def decrement_viewers(self):
        with self.viewers_lock:
            self.active_viewers = max(0, self.active_viewers - 1)
            if self.active_viewers == 0:
                self.viewer_active_event.clear()
            return self.active_viewers

    def get_active_viewers(self):
        with self.viewers_lock:
            return self.active_viewers

    @property
    def stream_live_url(self):
        cam = dict(self.cam)
        eff = self.analysis_stream_effective()
        if self.analysis_override:
            cam["_analysis_override"] = self.analysis_override
        else:
            cam["analysis_stream"] = eff
        # v17 #R23: عبر وسيط go2rtc المحلي لو شغّال (جلسة وحدة على المسجّل لكل بث)، وإلا مباشرة من الكاميرا
        g2 = getattr(self.manager, "go2rtc", None)
        if (g2 is not None and self.cam.get("conn_type") != "webcam" and self.manager.cfg.get("go2rtc_relay", True)
                and time.time() >= self.relay_block_until):
            relay = g2.relay_url(self.cam_id, eff)
            if relay:
                self.source_via_relay = True
                return relay
        self.source_via_relay = False
        return build_cam_rtsp(cam, role="live")

    def analysis_stream_effective(self):
        if self.analysis_override:
            return str(self.analysis_override).lower()
        astream = self.cam.get("analysis_stream")
        if astream and str(astream).lower() in ("main", "third"):
            return str(astream).lower()
        # إذا اختار المستخدم البث الرئيسي (stream_type أو live_stream == 'main')
        if str(self.cam.get("live_stream") or self.cam.get("stream_type") or "").lower() == "main":
            # إذا لم يُحدد analysis_stream صراحة أو كان موروثاً sub
            if not self.cam.get("_explicit_sub"):
                return "main"
        return str(astream or "sub").lower()

    def request_analysis_reconnect(self):
        signal = getattr(self, "_analysis_reconnect", None)
        if signal is not None:
            signal.set()

    def set_analysis_override(self, mode):
        '''# v17 #R21: الحماية الذاتية تحوّل بث التحليل مؤقتاً (بدون تغيير الإعدادات المحفوظة) وتعيد الاتصال'''
        mode = (mode or None)
        if mode == self.analysis_override:
            return False
        self.analysis_override = mode
        if mode is None:
            self._third_checked = False
            self.analysis_note = ""
        if self.running:
            self.request_analysis_reconnect()
        return True

    @property
    def stream_hq_url(self):
        return build_cam_rtsp(self.cam, role="hq")

    # v15 #4: جلب لقطة عالية الدقة من البث الرئيسي (stream_hq) عند الحاجة فقط (وقت الحدث أو قراءة اللوحة)
    def capture_hq_frame(self, fallback_frame=None, timeout_sec=2.2):
        # v16 #R18: من آخر مقطع ffmpeg مسجّل (بدون جلسة RTSP جديدة على المسجّل)
        rec = self.recorder
        if rec is not None and rec.healthy:
            frm = rec.grab_latest_frame()
            if frm is not None:
                return frm
        # v16 #R16: لو خيط المقطع يقرأ البث الرئيسي أصلاً، ناخذ آخر إطار منه فوراً بدل فتح اتصال جديد (~1.5 ثانية)
        hq_now = self.clip_hq_frame
        if self.clip_src_kind == "main" and hq_now is not None and (time.time() - self.clip_hq_ts) < 1.5:
            return hq_now.copy()
        live_pref = self.analysis_stream_effective()
        if live_pref == "main":
            with self.frame_lock:
                if self.raw_frame is not None:
                    return self.raw_frame.copy()
            return fallback_frame

        hq_url = self.stream_hq_url
        if not hq_url or not isinstance(hq_url, str):
            return fallback_frame

        result_holder = [None]

        def _grab_hq():
            cap_hq = None
            try:
                with self.hq_lock:
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                        "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;400000|stimeout;2000000"
                    )
                    cap_hq = cv2.VideoCapture(hq_url, cv2.CAP_FFMPEG)
                    cap_hq.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if cap_hq.isOpened():
                        ok, frm = cap_hq.read()
                        if ok and frm is not None:
                            result_holder[0] = frm
            except Exception:
                pass
            finally:
                if cap_hq is not None:
                    try:
                        cap_hq.release()
                    except Exception:
                        pass

        t = threading.Thread(target=_grab_hq, daemon=True)
        t.start()
        t.join(timeout=float(timeout_sec))
        if result_holder[0] is not None:
            return result_holder[0]
        return fallback_frame

    # v14 fix #4: تمرير threading.Event خاص بكل تشغيل (stop_event) للمسارات الثلاثة
    def start(self):
        self.stop()
        with self.viewers_lock:  # v16 #R16: stop() يرفع الإشارة عشان يصحّي الخيوط؛ ننزّلها لو ما فيه مشاهدين
            if self.active_viewers <= 0:
                self.viewer_active_event.clear()
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._analysis_reconnect = threading.Event()
        self.running = True
        self.cap_thread = threading.Thread(target=self._capture_loop, args=(stop_event,), daemon=True)
        self.proc_thread = threading.Thread(target=self._process_loop, args=(stop_event,), daemon=True)
        self.enc_thread = threading.Thread(target=self._display_encode_loop, args=(stop_event,), daemon=True)
        self.clip_thread = threading.Thread(target=self._clip_sample_loop, args=(stop_event,), daemon=True)
        # v16 #R18 / v17 #R21: مسجّل ffmpeg (نسخ البث) يُنشأ قبل خيط المقطع، وإلا يسبقه الخيط ويفتح فك ترميز 4K ثقيل ويظل عليه
        if (FfmpegSegmentRecorder.available() and str(self.manager.cfg.get("clip_recorder", "ffmpeg")) == "ffmpeg"
                and self.cam.get("conn_type") != "webcam"):
            self.recorder = FfmpegSegmentRecorder(self)
            self.recorder.start()
        self.cap_thread.start()
        self.proc_thread.start()
        self.enc_thread.start()
        self.clip_thread.start()

    def stop(self):
        self.running = False
        if self._stop_event:
            self._stop_event.set()
        rec, self.recorder = self.recorder, None  # v16 #R18
        if rec is not None:
            threading.Thread(target=rec.stop, daemon=True).start()
        self.viewer_active_event.set()
        with self.display_cond:
            self.display_cond.notify_all()
        for th in (self.cap_thread, self.proc_thread, self.enc_thread, self.clip_thread):
            if th and th.is_alive():
                th.join(timeout=1.0)
        self.connected = False
        self.stream_fps = 0.0
        self.display_fps = 0.0
        self.detect_fps = 0.0
        self.encode_ms = 0.0
        self.mjpeg_kbps = 0
        self.status_msg = "متوقف"

    def _capture_loop(self, stop_event):
        fps_ts = deque(maxlen=45)
        failures = 0
        while not stop_event.is_set():
            cap = None
            self._analysis_reconnect.clear()
            self.status_msg = "جارٍ اتصال التحليل"
            try:
                source = self.stream_live_url
                if isinstance(source, str) and source.startswith("rtsp://"):
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
                    cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4500,
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4500])
                else:
                    cap = cv2.VideoCapture(source)
                while cap.isOpened() and not stop_event.is_set() and not self._analysis_reconnect.is_set():
                    ok, frame = cap.read()
                    if stop_event.is_set() or self._analysis_reconnect.is_set():
                        break
                    if not ok or frame is None:
                        break
                    h, w = frame.shape[:2]
                    if self.analysis_stream_effective() == "third" and h < 600:
                        fallback = "main" if str(self.cam.get("live_stream") or self.cam.get("stream_type") or "").lower() == "main" else "sub"
                        self.analysis_override = fallback
                        self.analysis_note = f"البث الثالث منخفض الدقة؛ تم تحويل التحليل إلى {'الرئيسي' if fallback == 'main' else 'الفرعي'}"
                        self._analysis_reconnect.set()
                        break
                    now = time.time()
                    failures = 0
                    fps_ts.append(now)
                    if len(fps_ts) > 1 and fps_ts[-1] > fps_ts[0]:
                        self.stream_fps = round((len(fps_ts)-1)/(fps_ts[-1]-fps_ts[0]), 1)
                    self.resolution_str = f"{w}x{h}"
                    self.connected = True
                    self.status_msg = "التحليل متصل"
                    with self.frame_lock:
                        self.raw_frame = frame
                        self.raw_frame_ts = now
                        self.frame_id += 1
            except Exception:
                self.status_msg = "تعذر فتح بث التحليل؛ ستتم إعادة المحاولة"
            finally:
                if cap is not None:
                    cap.release()
                self.connected = False
                self.stream_fps = 0.0
                fps_ts.clear()
            if stop_event.is_set():
                break
            if self._analysis_reconnect.is_set():
                continue
            failures += 1
            if self.analysis_stream_effective() == "third":
                fallback = "main" if str(self.cam.get("live_stream") or self.cam.get("stream_type") or "").lower() == "main" else "sub"
                self.analysis_override = fallback
                self.analysis_note = f"البث الثالث لا يرسل إطارات؛ تم تحويل التحليل إلى {'الرئيسي' if fallback == 'main' else 'الفرعي'}"
            elif self.source_via_relay:
                self.relay_block_until = time.time() + 120
            self.status_msg = "إعادة اتصال التحليل"
            stop_event.wait(min(15, failures * 2))

    def _prepare_clip_item(self, fid, frame_ts, vis_frame):
        ch, cw = vis_frame.shape[:2]
        # v16 #R16: المقطع بدقة 1080p (كان 640 بجودة 74)
        try:
            target_w = max(320, min(3840, int(self.manager.cfg.get("clip_max_width", 1920) or 1920)))
        except Exception:
            target_w = 1920
        if cw > target_w:
            sc = target_w / float(cw)
            small_bgr = cv2.resize(vis_frame, (target_w, int(ch * sc) // 2 * 2), interpolation=cv2.INTER_AREA)
        else:
            small_bgr = vis_frame
        ok, buf = cv2.imencode(".jpg", small_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        jpeg_bytes = buf.tobytes() if ok else b""
        return (int(fid), float(frame_ts), jpeg_bytes)

    def _clip_wanted_sources(self):
        '''# v16 #R16: مصادر المقطع عالي الدقة بالترتيب (البث الثالث 1080p ثم الرئيسي مصغّر)'''
        mode = str(self.cam.get("clip_source") or self.manager.cfg.get("clip_source", "auto") or "auto").lower()
        if self.cam.get("conn_type") == "webcam" or mode == "live":
            return []
        if self.recorder is not None:
            return []  # v16 #R18: ffmpeg يسجل بدون فك ترميز؛ ما نفتح مفكك ثاني (البث الخفيف يظل احتياط)
        if mode == "auto" and self.analysis_stream_effective() == "main":
            return []  # البث المباشر نفسه رئيسي، فناخذ منه بدون اتصال ثاني
        order = ["main"] if mode == "main" else ["third", "main"]
        out = []
        for kind in order:
            url = build_cam_rtsp(self.cam, role=("hq" if kind == "main" else "third"))
            if isinstance(url, str) and url.startswith("rtsp://") and url not in [u for _, u in out]:
                out.append((kind, url))
        return out

    def _open_clip_capture(self, kind, url, stop_event):
        with self.hq_lock:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
            if kind == "main":
                # فك ترميز البث الرئيسي (4K/H.265) بكرت الشاشة لو أمكن
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
            else:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return None, None
        t0 = time.time()
        while not stop_event.is_set() and time.time() - t0 < 8.0:
            ok, frm = cap.read()
            if ok and frm is not None:
                if kind == "third" and frm.shape[0] < 600:
                    break  # البث الثالث معطّل أو صغير (مثل 128x96)
                return cap, frm
        cap.release()
        return None, None

    def _run_clip_capture(self, cap, frame, kind, stop_event, interval):
        h, w = frame.shape[:2]
        self.clip_src_kind = kind
        # v17 #R21: الطريقة القديمة = فك ترميز في بايثون (ثقيل) — نقولها صراحة في الحالة
        self.clip_src_label = f"فك ترميز بايثون (ثقيل): {'البث الثالث' if kind == 'third' else 'البث الرئيسي'} {w}x{h}"
        last_push = 0.0
        fps_ts = deque(maxlen=30)
        while not stop_event.is_set():
            if self.recorder is not None:
                return  # v17 #R21: مسجّل ffmpeg موجود — ما نفك ترميز بث ثقيل بالتوازي
            now = time.time()
            if kind == "main":
                self.clip_hq_frame = frame
                self.clip_hq_ts = now
            if now - last_push >= interval * 0.92:
                last_push = now
                self.clip_seq += 1
                try:
                    self.push_clip_frame(self.clip_seq, now, frame)
                except Exception:
                    pass
                fps_ts.append(now)
                if len(fps_ts) >= 2 and fps_ts[-1] > fps_ts[0]:
                    self.clip_src_fps = round((len(fps_ts) - 1) / (fps_ts[-1] - fps_ts[0]), 1)
            ok, frame = cap.read()
            if not ok or frame is None:
                return

    def _clip_sample_loop(self, stop_event):
        '''
        # v16 #R15 + #R16: إطارات المقطع بخيط مستقل عن YOLO وبدقة 1080p:
        يفتح البث الثالث (1080p) أو الرئيسي ويصغّره لـ clip_max_width. لو تعذر، يكمل من البث المباشر مؤقتاً
        ويعيد المحاولة كل شوي، عشان ما يضيع أي مقطع.
        '''
        interval = 1.0 / float(CLIP_FPS)
        sources = self._clip_wanted_sources()
        retry_at = 0.0
        fail_count = 0
        next_t = time.time()
        while not stop_event.is_set():
            if time.time() >= retry_at:
                sources = self._clip_wanted_sources()
                retry_at = time.time() + 10.0
                if sources:
                    ran = False
                    for kind, url in sources:
                        cap, first = self._open_clip_capture(kind, url, stop_event)
                        if cap is None:
                            continue
                        ran = True
                        try:
                            self._run_clip_capture(cap, first, kind, stop_event, interval)
                        finally:
                            cap.release()
                        break
                    if stop_event.is_set():
                        break
                    fail_count = 0 if ran else fail_count + 1
                    retry_at = time.time() + (3.0 if ran else min(60.0, 5.0 * fail_count))
                    self.clip_hq_frame = None
                    next_t = time.time()
            # البث المباشر نفسه (أو احتياط مؤقت لين يرجع البث عالي الدقة)
            self.clip_src_kind = "fallback" if sources else "live"
            self.clip_src_label = ("البث الخفيف مؤقتاً (تعذر فتح البث عالي الدقة)" if sources
                                   else f"البث المباشر {self.resolution_str}")
            with self.frame_lock:
                frame_ref = self.raw_frame
                cur_fid = self.frame_id
                frame_ts = self.raw_frame_ts
            if frame_ref is not None and cur_fid != self.last_clip_frame_id:
                self.last_clip_frame_id = cur_fid
                self.last_clip_sample_time = time.time()
                self.clip_seq += 1
                try:
                    self.push_clip_frame(self.clip_seq, frame_ts or self.last_clip_sample_time, frame_ref)
                except Exception:
                    pass
            next_t += interval
            delay = next_t - time.time()
            if delay < 0:
                next_t = time.time()
                delay = interval * 0.5
            if stop_event.wait(delay):
                break

    def push_clip_frame(self, fid, frame_ts, vis_frame):
        item = self._prepare_clip_item(fid, frame_ts, vis_frame)
        with self.clip_lock:
            self.clip_buffer.append(item)
        return item

    def get_pre_buffer_snapshot(self, detected_ts):
        with self.clip_lock:
            items = list(self.clip_buffer)
        cutoff = float(detected_ts) - float(PRE_BUFFER_SEC) - 0.25
        filtered = [it for it in items if cutoff <= it[1] <= float(detected_ts) + 0.05]
        return filtered if filtered else items[-int(CLIP_FPS * PRE_BUFFER_SEC):]

    @staticmethod
    def _box_iou(a, b):
        ax1, ay1, aw, ah = a
        bx1, by1, bw, bh = b
        ix = max(0.0, min(ax1 + aw, bx1 + bw) - max(ax1, bx1))
        iy = max(0.0, min(ay1 + ah, by1 + bh) - max(ay1, by1))
        inter = ix * iy
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

    def _is_recent_duplicate(self, box_640, now_ts):
        '''# v17 #R28: نفس الجسم الواقف (سيارة متوقفة) ما يطلق حدثاً ثانياً خلال stationary_suppress_sec لو تداخل مربعه ≥ 0.5'''
        window = float(self.manager.cfg.get("stationary_suppress_sec", 90) or 0)
        hist = getattr(self, "_recent_event_boxes", None)
        if hist is None:
            hist = self._recent_event_boxes = []
        hist[:] = [(t, b) for t, b in hist if now_ts - t <= max(window, 1.0)]
        if box_640 and window > 0:
            for t, b in hist:
                if self._box_iou(box_640, b) >= 0.5:
                    return True
            hist.append((now_ts, tuple(box_640)))
        return False

    def should_allow_trigger(self, event_kind, track_ids, cooldown_sec, now_ts):
        stale_cutoff = now_ts - max(120.0, cooldown_sec * 4.0)
        for k, ts_val in list(self.track_cooldown_map.items()):
            if ts_val < stale_cutoff:
                del self.track_cooldown_map[k]

        zone_min_cooldown = max(5.0, float(cooldown_sec) / 2.0)
        if event_kind == "zone_motion" and (now_ts - self.last_zone_motion_ts) < zone_min_cooldown:
            return False, []

        if track_ids:
            allowed_tids = []
            for tid in track_ids:
                key = (event_kind, int(tid))
                last_t = self.track_cooldown_map.get(key, -1e9)
                if (now_ts - last_t) >= max(2.5, float(cooldown_sec)):
                    allowed_tids.append(int(tid))
            if not allowed_tids:
                return False, []
            for tid in allowed_tids:
                self.track_cooldown_map[(event_kind, int(tid))] = now_ts
            if event_kind == "zone_motion":
                self.last_zone_motion_ts = now_ts
            self.last_trigger_time = now_ts
            return True, allowed_tids

        fallback_key = (event_kind, 0)
        last_t = self.track_cooldown_map.get(fallback_key, -1e9)
        if (now_ts - last_t) >= float(cooldown_sec):
            self.track_cooldown_map[fallback_key] = now_ts
            if event_kind == "zone_motion":
                self.last_zone_motion_ts = now_ts
            self.last_trigger_time = now_ts
            return True, [0]
        return False, []

    def enqueue_event_for_ai(self, event_id):
        should_spawn = False
        overloaded = False
        with self.ai_lock:
            if len(self.pending_ai_queue) >= self.max_pending_ai:
                overloaded = True
            else:
                self.pending_ai_queue.append(event_id)
                try:
                    self.manager.update_event_fields(event_id, persist=False, _ai_queued_ts=time.time())  # v17 #R21
                except Exception:
                    pass
                if not self.analyzing:
                    self.analyzing = True
                    should_spawn = True

        if overloaded:
            self.manager.mark_event_ai_overloaded(event_id)
            return False

        if should_spawn:
            threading.Thread(target=self._drain_pending_events, daemon=True).start()
        return True

    def _drain_pending_events(self):
        try:
            while True:
                with self.ai_lock:
                    if not self.pending_ai_queue:
                        self.analyzing = False
                        return
                    event_id = self.pending_ai_queue.popleft()
                try:
                    self.manager.process_queued_event_ai(event_id)
                except Exception as ex:
                    self.manager.update_event_fields(
                        event_id,
                        ai_status="failed",
                        is_error=True,
                        ai_error_reason=redact_secrets(ex, self.manager.cfg),
                        description=f"فشل تحليل الصورة: {redact_secrets(ex, self.manager.cfg)}"
                    )
        finally:
            with self.ai_lock:
                if not self.pending_ai_queue:
                    self.analyzing = False

    def _render_overlay_on_frame(self, base_frame):
        '''يرسم الخط الافتراضي ومضلّع المنطقة ومربعات الأجسام المتتبعة على نسخة العرض'''
        out_vis = base_frame.copy()
        dh, dw = out_vis.shape[:2]
        roi = self.cam.get("roi") or {}
        line_on = bool(roi.get("line_enabled", False))
        zone_on = bool(roi.get("zone_enabled", False))
        line_thickness = max(2, int(3 * (dh / 720.0)))

        # v17 #R20: مناطق الاستبعاد (رمادي متقطع)
        for mp in (roi.get("masks") or []):
            if mp and len(mp) >= 3:
                mpts = np.array([[int(float(pt[0]) * dw), int(float(pt[1]) * dh)] for pt in mp], dtype=np.int32)
                overlay = out_vis.copy()
                cv2.fillPoly(overlay, [mpts], (90, 90, 90))
                cv2.addWeighted(overlay, 0.35, out_vis, 0.65, 0, out_vis)
                cv2.polylines(out_vis, [mpts], isClosed=True, color=(170, 170, 170), thickness=max(1, line_thickness - 1))
        if zone_on:
            zone_color = (115, 200, 231)  # v16 #7: ذهبي فاتح على البث (BGR)
            poly_norm = roi.get("polygon")
            if poly_norm and len(poly_norm) >= 3:
                pts_px = np.array([[int(float(pt[0]) * dw), int(float(pt[1]) * dh)] for pt in poly_norm], dtype=np.int32)
                cv2.polylines(out_vis, [pts_px], isClosed=True, color=zone_color, thickness=line_thickness)
                p0x, p0y = int(pts_px[0][0]), int(pts_px[0][1])
                cv2.putText(out_vis, "POLYGON ZONE", (max(8, p0x), max(20, p0y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, zone_color, 1, cv2.LINE_AA)
            else:
                zx1, zy1 = int(float(roi.get("zx1", 0.15)) * dw), int(float(roi.get("zy1", 0.15)) * dh)
                zx2, zy2 = int(float(roi.get("zx2", 0.85)) * dw), int(float(roi.get("zy2", 0.85)) * dh)
                cv2.rectangle(out_vis, (zx1, zy1), (zx2, zy2), zone_color, line_thickness)

        if line_on:
            lx1, ly1 = int(float(roi.get("lx1", 0.2)) * dw), int(float(roi.get("ly1", 0.55)) * dh)
            lx2, ly2 = int(float(roi.get("lx2", 0.8)) * dw), int(float(roi.get("ly2", 0.55)) * dh)
            line_color = (60, 40, 255) if self.line_triggered_flash > 0 else (0, 210, 255)
            cv2.line(out_vis, (lx1, ly1), (lx2, ly2), line_color, line_thickness, cv2.LINE_AA)
            cv2.circle(out_vis, (lx1, ly1), line_thickness * 2, (0, 255, 180), -1)
            cv2.circle(out_vis, (lx2, ly2), line_thickness * 2, (0, 255, 180), -1)
            mid_x, mid_y = int((lx1 + lx2) / 2), int((ly1 + ly2) / 2) - 8
            dir_lbl = roi.get("line_direction", self.cam.get("line_direction", "both"))
            cv2.putText(out_vis, f"LINE ({dir_lbl})", (max(8, mid_x - 50), max(18, mid_y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 210, 255), 1, cv2.LINE_AA)

        if self.manager.cfg.get("draw_motion_boxes", True) and self.motion_boxes:
            for (bx, by, bw, bh) in self.motion_boxes[:8]:
                cv2.rectangle(out_vis, (bx, by), (bx + bw, by + bh), (0, 230, 153), max(2, int(2 * (dh / 720.0))))

        if self.manager.cfg.get("show_osd_fps", True):
            osd = (
                f"CAM {self.cam_id:02d} | {self.resolution_str} | "
                f"Src:{self.stream_fps:.1f} Disp:{self.display_fps:.1f} Det:{self.detect_fps:.1f}fps | "
                f"Enc:{self.encode_ms:.1f}ms"
            )
            cv2.rectangle(out_vis, (8, dh - 32), (min(dw - 8, 620), dh - 6), (0, 0, 0), -1)
            cv2.putText(out_vis, osd, (14, dh - 13), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 210, 255), 1, cv2.LINE_AA)
        return out_vis

    def _process_loop(self, stop_event):
        '''
        # v15 #4 / # v15 #5: مسار الكشف المستقل — يعمل على صورة مصغرة بعرض 640 وبمعدل detect_fps قابل للضبط (الافتراضي 8)
        باستخدام stop_event.wait المحسوب على الـ fps المطلوب بدون أي time.sleep ثابت.
        '''
        prev_gray = None
        last_id = -1
        det_ts = deque(maxlen=35)

        while not stop_event.is_set():
            t_loop_start = time.time()
            target_det_fps = max(1.0, min(30.0, float(self.cam.get("detect_fps") or self.manager.cfg.get("detect_fps", 8) or 8)))
            # v16 #R18: ميزانية كرت الشاشة تتقسم على الكاميرات الشغّالة + تخفيض الحماية الذاتية
            _hub = getattr(self.manager, "detect_hub", None)
            if _hub is not None and _hub.enabled:
                _n_run = sum(1 for _w in list(self.manager.workers.values()) if _w.running)
                target_det_fps = _hub.suggested_fps(_n_run, target_det_fps)
            _guard = getattr(self.manager, "load_guard", None)
            if _guard is not None and _guard.det_scale < 1.0:
                target_det_fps = max(1.0, target_det_fps * _guard.det_scale)
            frame_interval = 1.0 / target_det_fps

            with self.frame_lock:
                frame_ref = self.raw_frame
                cur_fid = self.frame_id
                frame_capture_ts = self.raw_frame_ts or t_loop_start

            if frame_ref is None or cur_fid == last_id or time.time() - frame_capture_ts > 3:
                if stop_event.wait(min(0.02, frame_interval * 0.5)):
                    break
                continue

            last_id = cur_fid
            now_proc = time.time()
            det_ts.append(now_proc)
            if len(det_ts) >= 2:
                el_d = det_ts[-1] - det_ts[0]
                if el_d > 0:
                    self.detect_fps = round((len(det_ts) - 1) / el_d, 1)

            fh, fw = frame_ref.shape[:2]
            use_hi_res = (self.analysis_stream_effective() in ("main", "third") and fw >= 900)
            det_w = 1280 if use_hi_res else 640
            det_scale = (det_w / float(fw)) if fw > det_w else 1.0
            det_h = max(1, int(fh * det_scale))
            frame_640 = cv2.resize(frame_ref, (det_w, det_h), interpolation=cv2.INTER_LINEAR) if det_scale != 1.0 else frame_ref
            det_h, det_w = frame_640.shape[:2]  # الأبعاد الفعلية للكشف

            if self.line_triggered_flash > 0:
                self.line_triggered_flash -= 1

            # v16 #R15: إطارات المقطع صارت تُسحب في _clip_sample_loop (كانت هنا وتتأخر مع بطء YOLO → مقطع متقطع ~1 إطار/ث)

            cfg = self.manager.cfg
            cam_analysis = self.cam.get("analysis") if isinstance(self.cam.get("analysis"), dict) else make_default_cam_analysis()
            trigger_source = str(cam_analysis.get("trigger_source") or self.cam.get("trigger_source", "local"))
            schedule_str = str(cam_analysis.get("schedule") or "always")

            if (
                cfg.get("motion_enabled", True)
                and self.cam.get("ai_watch", True)
                and trigger_source in ("local", "both")
                and is_within_schedule(schedule_str)
            ):
                roi = self.cam.get("roi") or {}
                line_on = bool(roi.get("line_enabled", False))
                zone_on = bool(roi.get("zone_enabled", False))
                combo_mode = roi.get("combo_mode", "both")
                line_dir = str(cam_analysis.get("line_direction") or roi.get("line_direction") or self.cam.get("line_direction", "both"))
                zone_dwell_sec = float(cam_analysis.get("zone_dwell_sec") or roi.get("zone_dwell_sec") or self.cam.get("zone_dwell_sec", 1.0))
                allowed_classes = set(cam_analysis.get("detect_classes") or ["person", "vehicle", "animal", "other"])
                min_conf = float(cam_analysis.get("min_confidence", 0.45))
                # v17 #R20: فلاتر الإنذارات الكاذبة
                alert_threshold = float(cam_analysis.get("alert_threshold", 0.60) or 0.0)
                min_box_h = float(cam_analysis.get("min_box_height_pct", 8) or 0.0) / 100.0 * det_h
                max_box_area = float(cam_analysis.get("max_box_area_pct", 60) or 100.0) / 100.0 * det_w * det_h
                masks_640 = []
                for mp in (roi.get("masks") or []):
                    if mp and len(mp) >= 3:
                        masks_640.append([(float(pt[0]) * det_w, float(pt[1]) * det_h) for pt in mp])

                lx1, ly1 = float(roi.get("lx1", 0.2)) * det_w, float(roi.get("ly1", 0.55)) * det_h
                lx2, ly2 = float(roi.get("lx2", 0.8)) * det_w, float(roi.get("ly2", 0.55)) * det_h
                zx1, zy1 = float(roi.get("zx1", 0.15)) * det_w, float(roi.get("zy1", 0.15)) * det_h
                zx2, zy2 = float(roi.get("zx2", 0.85)) * det_w, float(roi.get("zy2", 0.85)) * det_h

                poly_norm = roi.get("polygon")
                if poly_norm and len(poly_norm) >= 3:
                    poly_640 = [(float(pt[0]) * det_w, float(pt[1]) * det_h) for pt in poly_norm]
                else:
                    poly_640 = [(zx1, zy1), (zx2, zy1), (zx2, zy2), (zx1, zy2)]

                # v16 #R18: الكشف الجماعي على كرت الشاشة؛ لو ما رجع (مهلة/CPU) نستخدم المسار العادي
                _hub_dets = _hub.detect(frame_640, min_conf * 0.75) if (_hub is not None and _hub.enabled) else None
                if _hub_dets is not None:
                    dets_640, mot_lvl = _hub_dets, round(min(100.0, len(_hub_dets) * 18.0), 1)
                else:
                    dets_640, prev_gray, mot_lvl = self.manager.local_detector.detect_on_640(
                        frame_640,
                        prev_gray=prev_gray,
                        motion_sens=cfg.get("motion_sensitivity", 40),
                        min_conf=min_conf * 0.75
                    )
                self.motion_level = mot_lvl

                # تصفية الكشوفات حسب الفئات المطلوبة للكاميرا وتحويل الإحداثيات إلى أبعاد الإطار الأصلي
                inv_scale = 1.0 / det_scale
                filtered_dets_640 = []
                scaled_boxes = []
                for d in dets_640:
                    if d["class_name"] not in allowed_classes and "other" not in allowed_classes:
                        continue
                    bx, by, bw, bh = d["bbox"]
                    # v17 #R20: أجسام أصغر من الحد (أوراق/ظلال بعيدة) أو أكبر من الحد (تغير إضاءة) تنتجاهل
                    if bh < min_box_h or (bw * bh) > max_box_area:
                        continue
                    # v17 #R20: مناطق الاستبعاد (نقطة قدم الجسم داخل القناع)
                    if masks_640 and any(point_in_polygon(bx + bw * 0.5, by + bh, mp) for mp in masks_640):
                        continue
                    if zone_on and not line_on:
                        if not check_zone_intrusion(bx, by, bw, bh, zx1, zy1, zx2, zy2, polygon_pts=poly_640):
                            continue
                    filtered_dets_640.append(d)
                    scaled_boxes.append((int(bx * inv_scale), int(by * inv_scale), int(bw * inv_scale), int(bh * inv_scale)))

                crossed_boxes_640, cross_dir = self.tracker.update_and_check_crossing(
                    filtered_dets_640,
                    det_w,
                    det_h,
                    line_on,
                    lx1,
                    ly1,
                    lx2,
                    ly2,
                    timestamp=frame_capture_ts,
                    line_direction=line_dir,
                    polygon_pts=poly_640,
                    zone_enabled=zone_on,
                    zone_dwell_sec=zone_dwell_sec,
                    alert_threshold=alert_threshold,
                    min_track_frames=3
                )
                crossed_tids = list(self.tracker.last_crossed_track_ids)
                dwell_tids = list(self.tracker.last_zone_dwell_track_ids)
                self.motion_boxes = scaled_boxes
                self.latest_detections = filtered_dets_640
                self.det_size = (det_w, det_h)

                triggered_reason = None
                event_kind = None
                trigger_tids = []
                cross_dir_code = getattr(self.tracker, "last_crossing_dir_code", "")

                if crossed_boxes_640 and crossed_tids:
                    self.line_triggered_flash = 10
                    triggered_reason = f"عبور الخط الافتراضي ({cross_dir}) - {self.cam.get('name')}"
                    event_kind = "line_cross"
                    trigger_tids = crossed_tids
                elif zone_on and dwell_tids and (not line_on or combo_mode == "both"):
                    triggered_reason = f"بقاء داخل مضلّع الكشف ({zone_dwell_sec:.1f}s) - {self.cam.get('name')}"
                    event_kind = "zone_motion"
                    trigger_tids = dwell_tids
                elif (not line_on and not zone_on) and scaled_boxes:
                    # v14 fix #8: اشتراط zone_frames >= 3 وتحرك مسافة لا تقل عن 2% من قطر الإطار وفوقها تهدئة عامة للكاميرا
                    min_move_dist = 0.02 * float(math.hypot(det_w, det_h))
                    ready_tids = []
                    for tid, tr in self.tracker.tracks.items():
                        if tr.get("missed", 0) == 0 and tr.get("total_frames", 0) >= 3 and not tr.get("motion_fired"):  # v16 #1
                            pts = tr.get("pts")
                            first_pt = tr.get("first_pt") or (pts[0][:2] if pts else (0, 0))
                            cur_pt = (pts[-1][0], pts[-1][2]) if pts else first_pt
                            if float(math.hypot(cur_pt[0] - first_pt[0], cur_pt[1] - first_pt[1])) >= min_move_dist:
                                ready_tids.append(tid)
                    if ready_tids:
                        triggered_reason = f"رصد حركة - {self.cam.get('name')}"
                        event_kind = "zone_motion"
                        trigger_tids = ready_tids

                cooldown = float(cam_analysis.get("cooldown_seconds") or cfg.get("cooldown_seconds", 12))
                if triggered_reason and event_kind:
                    allowed, active_tids = self.should_allow_trigger(event_kind, trigger_tids, cooldown, frame_capture_ts)
                    if allowed:
                        # v16 #1: تعليم الأجسام اللي أطلقت حدث منطقة/حركة حتى ما تتكرر وهي واقفة
                        if event_kind == "zone_motion":
                            for _tid in active_tids:
                                _tr = self.tracker.tracks.get(_tid)
                                if _tr is not None:
                                    _tr["zone_fired"] = True
                                    _tr["motion_fired"] = True
                        primary_tid = active_tids[0] if active_tids else None
                        tr_info = self.tracker.tracks.get(primary_tid, {}) if primary_tid else {}
                        tr_box_640 = tr_info.get("box")
                        if tr_box_640:
                            tbx, tby, tbw, tbh = tr_box_640
                            target_box_full = (int(tbx * inv_scale), int(tby * inv_scale), int(tbw * inv_scale), int(tbh * inv_scale))
                        else:
                            target_box_full = scaled_boxes[0] if scaled_boxes else None

                        detector_meta = {
                            "event_kind": event_kind,
                            "crossing_direction": cross_dir,
                            "crossing_dir_code": cross_dir_code,
                            "track_id": primary_tid or 1,
                            "target_box": target_box_full,
                            "detector_class": tr_info.get("class_name", "other"),
                            "detector_subtype": tr_info.get("subtype", ""),
                            "detector_confidence": round(float(tr_info.get("confidence", 0.55)), 2),
                            "detector_backend": self.manager.local_detector.backend
                        }
                        _dup_evt = self._is_recent_duplicate(tr_box_640, frame_capture_ts)  # v17 #R28
                        if _dup_evt:
                            self.status_msg = "متصل • تجاهل حدث مكرر لنفس الجسم الواقف"
                        try:  # v16 #R14: أي خطأ في تسجيل الحدث ما يوقف الكشف لهالكاميرا
                            _dup_evt or self.manager.register_and_dispatch_event(
                                cam_id=self.cam_id,
                                disp_frame=frame_ref.copy(),
                                raw_frame=frame_ref.copy(),
                                trigger_reason=triggered_reason,
                                boxes=scaled_boxes,
                                detected_ts=frame_capture_ts,
                                track_ids=active_tids,
                                detector_meta=detector_meta
                            )
                        except Exception as ex:
                            self.status_msg = f"متصل • خطأ أثناء تسجيل حدث: {redact_secrets(ex, self.manager.cfg)[:80]}"

            elapsed_loop = time.time() - t_loop_start
            wait_sec = max(0.001, frame_interval - elapsed_loop)
            if stop_event.wait(wait_sec):
                break

    def _display_encode_loop(self, stop_event):
        '''
        # v15 #3 / # v15 #4: مسار ضغط العرض المستقل — يعمل فقط عندما يكون active_viewers > 0،
        وبمعدل live_fps قابل للضبط (الافتراضي 15)، وأقصى عرض live_max_width (الافتراضي 1280)، وجودة JPEG 75،
        ويوقظ جميع عملاء MJPEG المنتظرين عبر display_cond بدون أي time.sleep ثابت.
        '''
        last_encoded_fid = -1
        disp_ts = deque(maxlen=45)
        bytes_window = deque(maxlen=30)

        while not stop_event.is_set():
            # v15 #3: لا يضغط أي إطار JPEG إذا لم يكن هناك مشاهد فعلي مفتوح
            if self.get_active_viewers() <= 0:
                self.display_fps = 0.0
                self.mjpeg_kbps = 0
                # v16 #R16: stop()/start() كانت تخلي الإشارة مرفوعة بدون مشاهدين، فيصير wait يرجع فوراً
                # والخيط يدور بلا توقف (~50% معالج لكل كاميرا) ويخنق YOLO. ننزّلها لو ما فيه مشاهد فعلي.
                with self.viewers_lock:
                    if self.active_viewers <= 0:
                        self.viewer_active_event.clear()
                self.viewer_active_event.wait(timeout=0.25)
                if stop_event.is_set():
                    break
                continue

            t_enc_loop = time.time()
            cfg = self.manager.cfg
            target_live_fps = max(1.0, min(30.0, float(self.cam.get("live_fps") or cfg.get("live_fps", 15) or 15)))
            live_interval = 1.0 / target_live_fps

            with self.frame_lock:
                frame_ref = self.raw_frame
                cur_fid = self.frame_id

            if frame_ref is None or cur_fid == last_encoded_fid:
                if stop_event.wait(min(0.015, live_interval * 0.4)):
                    break
                continue

            last_encoded_fid = cur_fid
            t0 = time.perf_counter()

            h, w = frame_ref.shape[:2]
            max_w = int(self.cam.get("live_max_width") or cfg.get("live_max_width", 1280) or 1280)
            if w > max_w:
                sc = max_w / float(w)
                disp_base = cv2.resize(frame_ref, (max_w, max(1, int(h * sc))), interpolation=cv2.INTER_LINEAR)
            else:
                disp_base = frame_ref

            vis_frame = self._render_overlay_on_frame(disp_base)
            q_val = int(self.cam.get("live_jpeg_quality") or cfg.get("live_jpeg_quality", 75) or 75)
            ok, buf = cv2.imencode(".jpg", vis_frame, [int(cv2.IMWRITE_JPEG_QUALITY), max(50, min(92, q_val))])
            t1 = time.perf_counter()
            self.encode_ms = round((t1 - t0) * 1000.0, 2)

            if ok:
                raw_bytes = buf.tobytes()
                now_e = time.time()
                disp_ts.append(now_e)
                if len(disp_ts) >= 2:
                    el = disp_ts[-1] - disp_ts[0]
                    if el > 0:
                        self.display_fps = round((len(disp_ts) - 1) / el, 1)

                bytes_window.append((now_e, len(raw_bytes)))
                if len(bytes_window) >= 2:
                    dt_b = bytes_window[-1][0] - bytes_window[0][0]
                    if dt_b > 0:
                        total_bits = sum(b[1] for b in bytes_window) * 8.0
                        self.mjpeg_kbps = int((total_bits / dt_b) / 1000.0)

                # تجهيز نسخة مصغرة بعرض 480 عند الحاجة لشبكة 9 أو 16 كاميرا
                small_map = {0: raw_bytes}
                vh, vw = vis_frame.shape[:2]
                if vw > 480:
                    sc480 = 480.0 / float(vw)
                    vis_480 = cv2.resize(vis_frame, (480, max(1, int(vh * sc480))), interpolation=cv2.INTER_AREA)
                    ok480, buf480 = cv2.imencode(".jpg", vis_480, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    if ok480:
                        small_map[480] = buf480.tobytes()

                with self.display_cond:
                    self.display_jpeg = raw_bytes
                    self.display_jpeg_by_width = small_map
                    self.display_seq += 1
                    self.display_cond.notify_all()

            elapsed_enc = time.time() - t_enc_loop
            wait_sec = max(0.001, live_interval - elapsed_enc)
            if stop_event.wait(wait_sec):
                break

    def get_display_jpeg_for_width(self, req_width=None):
        if req_width and int(req_width) <= 480:
            return self.display_jpeg_by_width.get(480) or self.display_jpeg
        return self.display_jpeg

