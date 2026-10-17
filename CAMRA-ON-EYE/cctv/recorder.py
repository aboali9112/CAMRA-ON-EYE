# -*- coding: utf-8 -*-
"""cctv.recorder — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import subprocess
import os
import time
from .reliability import recording_fresh, lpr_enabled
import threading
import atexit
import cv2
import numpy as np
from .config import redact_secrets
from .paths import FFMPEG_EXE, FFPROBE_EXE, SEG_DIR
from .rtsp import build_cam_rtsp


class FfmpegSegmentRecorder:
    '''
    # v16 #R18: تسجيل دوّار بنسخ البث (stream copy) عبر ffmpeg بدون فك ترميز ولا إعادة ضغط:
    صفر معالج تقريباً لكل كاميرا، والمقطع بجودة الكاميرا الأصلية (حتى 4K). مقاطع قصيرة (~4 ثواني) آخر 70 ثانية،
    وعند الحدث نلصق المقاطع اللي تغطي [قبل 5 ث، بعد 10 ث] في MP4 واحد خلال أجزاء من الثانية.
    نفس أسلوب Frigate. لو ffmpeg غير موجود أو فشل، يرجع البرنامج للطريقة القديمة تلقائياً.
    '''
    SEG_SEC = 4
    KEEP_SEC = 70

    def __init__(self, worker):
        self.worker = worker
        self.cam_id = int(worker.cam_id)
        self.dir = SEG_DIR / f"cam{self.cam_id}"
        self.proc = None
        self.url = None
        self.kind = ""
        self.codec = ""
        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.healthy = False
        self.status_msg = "لم يبدأ"
        self.last_err = ""
        self.started_ts = 0.0
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = None

    @staticmethod
    def available():
        return FFMPEG_EXE.is_file() and FFPROBE_EXE.is_file()

    def label(self):
        if not self.healthy:
            return ""
        kind_lbl = {"third": "البث الثالث", "main": "البث الرئيسي", "sub": "البث الفرعي"}.get(self.kind, self.kind)
        return f"ffmpeg {kind_lbl} {self.width}x{self.height} {self.codec}"

    # ---------- اختيار المصدر
    def _candidates(self):
        cam = self.worker.cam
        mode = str(cam.get("clip_source") or self.worker.manager.cfg.get("clip_source", "auto") or "auto").lower()
        if cam.get("conn_type") == "webcam":
            return []
        # آخر حل: البث الفرعي (نفس جودة الطريقة القديمة لكن بدون فك ترميز)
        # v17 #R26: كاميرا عليها قراءة لوحات (ANPR) تسجّل من البث الرئيسي عشان قصّة اللوحة تجي بأعلى دقة (12MP بدل 1080p)
        an = cam.get("analysis") if isinstance(cam.get("analysis"), dict) else {}
        lpr_on = lpr_enabled(cam)
        order = ["main", "sub"] if (mode == "main" or lpr_on) else ["third", "main", "sub"]
        out = []
        g2 = getattr(self.worker.manager, "go2rtc", None)
        use_relay = bool(self.worker.manager.cfg.get("go2rtc_relay", True)) and g2 is not None
        for kind in order:
            urls = []
            if use_relay:
                urls.append(g2.relay_url(self.cam_id, kind))  # v17 #R23: عبر الوسيط المحلي أولاً، ثم الكاميرا مباشرة
            urls.append(build_cam_rtsp(cam, role={"main": "hq", "third": "third", "sub": "sub"}[kind]))
            for url in urls:
                if isinstance(url, str) and url.startswith("rtsp://") and url not in [u for _, u in out]:
                    out.append((kind, url))
        return out

    @staticmethod
    def kill_stale():
        '''يقتل عمليات ffmpeg يتيمة من تشغيل سابق (لو البرنامج انقفل فجأة) عشان ما تحجز جلسات على المسجّل'''
        try:
            import psutil
            me = os.getpid()
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if (p.info["name"] or "").lower().startswith("ffmpeg") and any(str(SEG_DIR) in a for a in (p.info["cmdline"] or [])):
                        if p.ppid() != me:
                            p.kill()
                except Exception:
                    continue
        except Exception:
            pass

    def _probe(self, url):
        try:
            r = subprocess.run([str(FFPROBE_EXE), "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                                "-show_entries", "stream=codec_name,width,height,avg_frame_rate", "-of", "csv=p=0", url],
                               capture_output=True, text=True, timeout=(8 if "127.0.0.1" in url else 15),  # v17 #R26: الوسيط المحلي ما يحتاج أكثر من 8 ثواني
                               creationflags=(0x08000000 if os.name == "nt" else 0))
            if "453" in (r.stderr or "") and "Bandwidth" in (r.stderr or ""):
                self.last_err = "المسجّل رفض الجلسة (453 Not Enough Bandwidth): عدد جلسات هذه القناة ممتلئ — أغلق البرامج/التطبيقات الأخرى المتصلة بالكاميرا"
            parts = (r.stdout.strip().splitlines() or [""])[0].split(",")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                fps = 0.0
                try:
                    num, den = parts[3].split("/")
                    fps = float(num) / float(den) if float(den) else 0.0
                except Exception:
                    pass
                return {"codec": parts[0], "w": int(parts[1]), "h": int(parts[2]), "fps": round(fps, 1)}
        except Exception:
            pass
        return None

    # ---------- التشغيل
    def start(self):
        self.stop_event.clear()
        self.dir.mkdir(parents=True, exist_ok=True)
        atexit.register(self._kill)  # ما نترك ffmpeg شغّال بعد إغلاق البرنامج
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"ffrec-cam{self.cam_id}")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._kill()
        self.healthy = False
        try:
            for f in self.dir.glob("seg_*.ts"):
                f.unlink()
        except Exception:
            pass

    def _kill(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _pump_err(self, p):
        try:
            for raw in iter(p.stderr.readline, b""):
                line = redact_secrets(raw.decode("utf-8", "replace").strip(), self.worker.manager.cfg)
                if line:
                    self.last_err = line[-200:]
        except Exception:
            pass

    def _run(self):
        fails = 0
        while not self.stop_event.is_set():
            chosen = None
            for kind, url in self._candidates():
                info = self._probe(url)
                if not info or (kind == "third" and info["h"] < 600):
                    continue
                chosen = (kind, url, info)
                break
            if chosen is None:
                self.healthy = False
                self.status_msg = self.last_err if "453" in self.last_err else "تعذر فتح بث للتسجيل (ffprobe)"
                fails += 1
                if self.stop_event.wait(min(60.0, 5.0 * fails)):
                    break
                continue
            self.kind, self.url, info = chosen
            self.codec, self.width, self.height, self.fps = info["codec"], info["w"], info["h"], info["fps"]
            cmd = [str(FFMPEG_EXE), "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", self.url,
                   "-map", "0:v:0", "-c", "copy", "-f", "segment", "-segment_time", str(self.SEG_SEC),
                   "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mpegts",
                   str(self.dir / "seg_%Y%m%d_%H%M%S.ts")]
            try:
                with self.lock:
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                 creationflags=(0x08000000 if os.name == "nt" else 0))
                    p = self.proc
                threading.Thread(target=self._pump_err, args=(p,), daemon=True).start()
            except Exception as ex:
                self.healthy = False
                self.status_msg = f"تعذر تشغيل ffmpeg: {redact_secrets(ex, self.worker.manager.cfg)[:100]}"
                fails += 1
                if self.stop_event.wait(min(60.0, 5.0 * fails)):
                    break
                continue
            self.started_ts = time.time()
            self.status_msg = "يسجل"
            # نعتبره سليم بعد ما يطلع أول مقطع
            while not self.stop_event.is_set() and p.poll() is None:
                segs = self.segments()
                self.healthy = recording_fresh([s[0] for s in segs], time.time(), 17)
                if not self.healthy and time.time() - self.started_ts > 25:
                    self._kill()
                    break
                if self.healthy:
                    fails = 0
                self._cleanup()
                if self.stop_event.wait(2.0):
                    break
            self.healthy = False
            if self.stop_event.is_set():
                break
            fails += 1
            self.status_msg = f"ffmpeg توقف — إعادة المحاولة ({self.last_err[:80] or 'بدون رسالة'})"
            if self.stop_event.wait(min(60.0, 3.0 * fails)):
                break
        self._kill()

    # ---------- المقاطع
    @staticmethod
    def _parse_start(path):
        try:
            stem = path.name[4:-3]  # seg_YYYYmmdd_HHMMSS
            return time.mktime(time.strptime(stem, "%Y%m%d_%H%M%S"))
        except Exception:
            return None

    def segments(self):
        '''[(path, start_ts, end_ts)] مرتبة؛ نهاية كل مقطع = بداية اللي بعده، وآخر مقطع (المفتوح) نهايته = الآن'''
        items = []
        try:
            for f in self.dir.glob("seg_*.ts"):
                st = self._parse_start(f)
                if st is not None:
                    items.append([f, st, 0.0])
        except Exception:
            return []
        items.sort(key=lambda x: x[1])
        for i, it in enumerate(items):
            it[2] = items[i + 1][1] if i + 1 < len(items) else it[0].stat().st_mtime
        return [tuple(it) for it in items]

    def _cleanup(self):
        cutoff = time.time() - self.KEEP_SEC
        for f, st, en in self.segments()[:-1]:
            if en < cutoff:
                try:
                    f.unlink()
                except Exception:
                    pass

    @staticmethod
    def select_segments(segments, detected_ts, pre_sec, post_sec):
        '''المقاطع المغلقة اللي تغطي [detected-pre, detected+post] (دالة نقية للاختبار)'''
        lo, hi = float(detected_ts) - float(pre_sec), float(detected_ts) + float(post_sec)
        closed = segments[:-1]  # آخر مقطع لسا يُكتب
        return [s for s in closed if s[2] > lo and s[1] < hi]

    def make_clip(self, detected_ts, pre_sec, post_sec, out_mp4):
        '''ينتظر لين تنقفل المقاطع المطلوبة ثم يلصقها في MP4 بدون إعادة ترميز'''
        need_until = float(detected_ts) + float(post_sec)
        deadline = time.time() + float(post_sec) + 2 * self.SEG_SEC + 6
        while time.time() < deadline and not self.stop_event.is_set():
            segs = self.segments()
            if segs and segs[-1][1] >= need_until:
                break
            if not self.healthy and time.time() - need_until > self.SEG_SEC:
                break
            time.sleep(0.5)
        segs = self.segments()
        use = self.select_segments(segs, detected_ts, pre_sec, post_sec)
        if not use:
            return {"ok": False, "error": "لا توجد مقاطع مسجلة تغطي وقت الحدث"}
        lst = self.dir / f"concat_{int(detected_ts)}_{threading.get_ident()}.txt"
        tmp = out_mp4.with_name(f".{out_mp4.name}.tmp.{threading.get_ident()}.mp4")
        try:
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p, _, _ in use), encoding="utf-8")
            cmd = [str(FFMPEG_EXE), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy"]
            if self.codec == "hevc":
                cmd += ["-tag:v", "hvc1"]  # عشان المتصفح يتعرف على H.265 داخل MP4
            cmd += ["-movflags", "+faststart", str(tmp)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, creationflags=(0x08000000 if os.name == "nt" else 0))
            if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
                return {"ok": False, "error": (r.stderr or "ffmpeg concat failed").strip()[-200:]}
            pr = subprocess.run([str(FFPROBE_EXE), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tmp)],
                                capture_output=True, text=True, timeout=20, creationflags=(0x08000000 if os.name == "nt" else 0))
            try:
                duration = float(pr.stdout.strip().splitlines()[0])
            except Exception:
                duration = sum(en - st for _, st, en in use)
            os.replace(tmp, out_mp4)
            return {"ok": True, "duration": round(duration, 2), "first_start": use[0][1], "last_end": use[-1][2],
                    "codec": self.codec, "width": self.width, "height": self.height, "segments": len(use)}
        except Exception as ex:
            return {"ok": False, "error": str(ex)[:200]}
        finally:
            for f in (lst, tmp):
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    @staticmethod
    def segment_for_ts(segments, ts):
        '''المقطع اللي يحتوي اللحظة ts: (path, offset_sec) أو None (دالة نقية للاختبار)'''
        for p, st, en in segments:
            if st <= ts < en:
                return p, max(0.0, ts - st)
        return None

    def grab_frame_at(self, ts, back_sec=0.0, timeout=3.0):
        '''# v17 #R27: إطار بدقة التسجيل الكاملة عند لحظة الحدث نفسها (من المقاطع المسجلة)، بدل "آخر إطار"'''
        try:
            hit = self.segment_for_ts(self.segments(), float(ts) - back_sec)
            if hit is None:
                return None
            src, off = hit
            r = subprocess.run([str(FFMPEG_EXE), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{off:.2f}", "-i", str(src),
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"],
                               capture_output=True, timeout=timeout, creationflags=(0x08000000 if os.name == "nt" else 0))
            if r.stdout:
                img = cv2.imdecode(np.frombuffer(r.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is not None and img.shape[0] >= 200:
                    return img
        except Exception:
            pass
        return None

    def grab_latest_frame(self):
        '''آخر إطار من آخر مقطع مغلق (بدل فتح جلسة RTSP جديدة على المسجّل)'''
        segs = self.segments()
        if not segs:
            return None
        # أول إطار (مفتاحي) من المقطع المفتوح عمره ≤ 4 ثواني؛ ولو فشل ناخذ أول إطار من آخر مقطع مغلق
        for src in [segs[-1][0]] + ([segs[-2][0]] if len(segs) >= 2 else []):
            try:
                r = subprocess.run([str(FFMPEG_EXE), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
                                    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"],
                                   capture_output=True, timeout=20, creationflags=(0x08000000 if os.name == "nt" else 0))
                if r.stdout:
                    img = cv2.imdecode(np.frombuffer(r.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if img is not None and img.shape[0] >= 200:
                        return img
            except Exception:
                pass
        return None

