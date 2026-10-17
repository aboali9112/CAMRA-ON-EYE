# -*- coding: utf-8 -*-
"""cctv.go2rtc — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import subprocess
import os
import time
import json
import threading
import atexit
from urllib.parse import quote
import requests
from .config import redact_secrets
from .paths import BIN_DIR, DATA_DIR, MAX_CAMERAS
from .rtsp import build_cam_rtsp


class Go2RtcManager:
    '''
    # v16 #R16: يشغّل go2rtc المرفق (cctv_agent_data/bin/go2rtc.exe) ويسجّل فيه الكاميرات،
    عشان البث المباشر يوصل المتصفح WebRTC/MSE بدقته الكاملة بدون فك ترميز في بايثون.
    يسمع على 127.0.0.1 فقط. لو توقف أو ما اشتغل، الواجهة ترجع تلقائياً لبث MJPEG العادي.
    '''
    API_PORT = 1984
    WEBRTC_PORT = 8555
    RTSP_PORT = 8554  # v17 #R23: go2rtc يعيد بث الكاميرات محلياً؛ المسجّل يشوف جلسة وحدة لكل بث مهما كثر المستهلكون

    def __init__(self, engine):
        self.engine = engine
        self.API_PORT = 1985
        self.RTSP_PORT = 8556
        self.WEBRTC_PORT = 8557
        self.exe = BIN_DIR / ("go2rtc.exe" if os.name == "nt" else "go2rtc")
        self.cfg_path = DATA_DIR / "go2rtc.yaml"
        self.log_path = DATA_DIR / "go2rtc.log"
        self.pid_path = DATA_DIR / "go2rtc.pid"
        self.proc = None
        self.lock = threading.RLock()
        self.running = False
        self.external = False
        self.status_msg = "لم يبدأ"
        self.streams = {}
        self._watch_started = False
        self._last_start = 0.0

    def enabled(self):
        v = self.engine.cfg.get("go2rtc_enabled", True)
        return v is True or str(v).strip().lower() in ("true", "1", "yes", "on")

    def _api(self, method, path, timeout=4.0):
        return requests.request(method, f"http://127.0.0.1:{self.API_PORT}{path}", timeout=timeout)

    def _alive(self):
        try:
            return self._api("GET", "/api", timeout=1.5).status_code == 200
        except Exception:
            return False

    def desired_streams(self):
        out = {}
        for c in self.engine.cfg.get("cameras", [])[:MAX_CAMERAS]:
            if not c.get("enabled", True) or c.get("conn_type") == "webcam":
                continue
            cid = int(c["id"])
            hq = build_cam_rtsp(c, role="hq")
            sub = build_cam_rtsp(c, role="sub")
            if isinstance(hq, str) and hq.startswith("rtsp"):
                out[f"cam{cid}"] = hq
            if isinstance(sub, str) and sub.startswith("rtsp") and sub != hq:
                out[f"cam{cid}_sub"] = sub
            third = build_cam_rtsp(c, role="third")  # v17 #R23: للتسجيل 1080p عبر الوسيط
            if isinstance(third, str) and third.startswith("rtsp") and third not in (hq, sub):
                out[f"cam{cid}_third"] = third
        return out

    def relay_url(self, cam_id, kind):
        '''# v17 #R23: رابط البث عبر go2rtc المحلي (أو None لو go2rtc مو شغّال أو البث غير مسجّل)'''
        if not self.running:
            return None
        name = f"cam{int(cam_id)}" + {"main": "", "hq": "", "sub": "_sub", "third": "_third"}.get(kind, "_sub")
        if name not in self.streams:
            return None
        return f"rtsp://127.0.0.1:{self.RTSP_PORT}/{name}"

    def _write_yaml(self, streams):
        lines = [
            "# v16: يُنشأ تلقائياً من البرنامج عند كل تشغيل — لا تعدّله يدوياً",
            "api:", f'  listen: "127.0.0.1:{self.API_PORT}"', '  origin: "*"',
            "rtsp:", f'  listen: "127.0.0.1:{self.RTSP_PORT}"',
            "webrtc:", f'  listen: "127.0.0.1:{self.WEBRTC_PORT}"', "  candidates:", f"    - 127.0.0.1:{self.WEBRTC_PORT}",
            "log:", "  level: warn",
        ]
        if streams:
            lines.append("streams:")
            lines += [f"  {name}: {json.dumps(src)}" for name, src in streams.items()]
        else:
            lines.append("streams: {}")
        self.cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _kill_stale(self):
        try:
            pid = int(self.pid_path.read_text().strip())
        except Exception:
            return
        try:
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout
                if "go2rtc" in out.lower():
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=5,
                                   creationflags=0x08000000)
                    time.sleep(0.5)
        except Exception:
            pass
        try:
            self.pid_path.unlink()
        except Exception:
            pass

    def _pump_log(self, proc):
        try:
            with open(self.log_path, "w", encoding="utf-8", errors="replace") as log_f:
                for raw in iter(proc.stdout.readline, b""):
                    log_f.write(redact_secrets(raw.decode("utf-8", "replace"), self.engine.cfg))
                    log_f.flush()
        except Exception:
            pass

    def start(self):
        with self.lock:
            self._last_start = time.time()
            if not self.enabled():
                self.running = False
                self.status_msg = "معطّل من الإعدادات"
                return False
            self._kill_stale()
            streams = self.desired_streams()
            if self._alive():
                # go2rtc شغّال من قبل (نسخة ثانية للمستخدم) — نستخدمه ونسجّل الكاميرات عبر الـ API
                self.external, self.running, self.streams = True, True, {}
                self.status_msg = "يعمل (نسخة go2rtc شغّالة مسبقاً)"
                self._push_streams(streams)
                self._ensure_watch()
                return True
            if not self.exe.is_file():
                self.running = False
                self.status_msg = "go2rtc غير موجود في cctv_agent_data/bin"
                return False
            try:
                self._write_yaml(streams)
                self.proc = subprocess.Popen(
                    [str(self.exe), "-config", str(self.cfg_path)], cwd=str(DATA_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=(0x08000000 if os.name == "nt" else 0))
                self.pid_path.write_text(str(self.proc.pid))
                threading.Thread(target=self._pump_log, args=(self.proc,), daemon=True).start()
            except Exception as ex:
                self.running = False
                self.status_msg = f"تعذر تشغيل go2rtc: {redact_secrets(ex, self.engine.cfg)[:120]}"
                return False
            for _ in range(30):
                if self.proc.poll() is not None or self._alive():
                    break
                time.sleep(0.2)
            self.external = False
            self.running = self.proc.poll() is None and self._alive()
            self.streams = dict(streams) if self.running else {}
            if self.running:
                self._reconnect_consumers()  # v17 #R23
            self.status_msg = "يعمل" if self.running else "تعذر تشغيل go2rtc (راجع go2rtc.log)"
            self._ensure_watch()
            return self.running

    def _reconnect_consumers(self):
        '''# v17 #R23: بعد (إعادة) تشغيل go2rtc، الكاميرات اللي تقرأ مباشرة تنتقل للوسيط بإعادة اتصال'''
        try:
            for w in list(self.engine.workers.values()):
                if w.running and not getattr(w, "source_via_relay", False) and self.engine.cfg.get("go2rtc_relay", True):
                    w.request_analysis_reconnect()
        except Exception:
            pass

    def _push_streams(self, want):
        for name in [n for n in self.streams if n not in want]:
            try:
                self._api("DELETE", f"/api/streams?src={quote(name, safe='')}")
            except Exception:
                pass
        for name, src in want.items():
            if self.streams.get(name) == src:
                continue
            try:
                self._api("DELETE", f"/api/streams?src={quote(name, safe='')}")
                self._api("PUT", f"/api/streams?name={quote(name, safe='')}&src={quote(src, safe='')}")
            except Exception:
                pass
        self.streams = dict(want)

    def sync_streams(self):
        with self.lock:
            if not self.enabled():
                self.stop()
                self.status_msg = "معطّل من الإعدادات"
                return
            if not self.running or not self._alive():
                self.start()
                return
            want = self.desired_streams()
            self._push_streams(want)
            if not self.external:
                self._write_yaml(want)

    def request_sync(self):
        threading.Thread(target=self.sync_streams, daemon=True).start()

    def _ensure_watch(self):
        if self._watch_started:
            return
        self._watch_started = True
        atexit.register(self.stop)
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def _watch_loop(self):
        while True:
            time.sleep(15)
            try:
                if not self.enabled():
                    continue
                dead = (self.proc is not None and self.proc.poll() is not None) or (self.running and not self._alive())
                if (dead or not self.running) and time.time() - self._last_start > 30:
                    self.running = False
                    self.status_msg = "go2rtc توقف — إعادة تشغيل..."
                    self.start()
            except Exception:
                pass

    def stop(self):
        with self.lock:
            self.running = False
            proc, self.proc = self.proc, None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            try:
                self.pid_path.unlink()
            except Exception:
                pass

    def status(self):
        return {
            "enabled": self.enabled(),
            "running": bool(self.running),
            "available": self.exe.is_file() or self.external,
            "port": self.API_PORT,
            "status_msg": self.status_msg,
            "streams": sorted(self.streams.keys()),
        }

