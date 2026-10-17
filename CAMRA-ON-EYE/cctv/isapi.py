# -*- coding: utf-8 -*-
"""cctv.isapi — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import time
import threading
import xml.etree.ElementTree as ET
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from .config import is_within_schedule
from .rtsp import xml_find_text


# ====================================================================================
# v15 #9: مستمع أحداث المسجل عبر ISAPI (/ISAPI/Event/notification/alertStream)
# ====================================================================================
class IsapiAlertStreamListener:
    '''
    يتصل بـ /ISAPI/Event/notification/alertStream (بروتوكول HTTP Digest، اتصال طويل multipart)
    لكل مسجل NVR، ويعيد الاتصال تلقائياً، ويقرأ أحداث:
    linedetection, fielddetection, regionEntrance, regionExiting, VMD
    ومعها رقم القناة ونوع الهدف (human/vehicle في كاميرات AcuSense) ويمررها لنفس مسار التسجيل والتحقق والإشعار.
    '''
    SUPPORTED_EVENTS = {"linedetection", "fielddetection", "regionentrance", "regionexiting", "vmd"}

    def __init__(self, engine):
        self.engine = engine
        self._stop_event = threading.Event()
        self.thread = None
        self.connected = False
        self.status_msg = "غير مفعل"
        self.last_event_ts = {}

    def start(self):
        self.stop()
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._listen_loop, args=(self._stop_event,), daemon=True)
        self.thread.start()

    def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.connected = False

    def _device_key_for_cam(self, c):
        ip = str(c.get("ip") or self.engine.cfg.get("ip") or "").strip()
        port = str(c.get("http_port") or self.engine.cfg.get("http_port") or "80").strip() or "80"
        user = str(c.get("username") or self.engine.cfg.get("username") or "admin").strip()
        pwd = str(c.get("password") or self.engine.cfg.get("password") or "")
        return (ip, port, user, pwd)

    def _listen_loop(self, stop_event):
        '''# v16 #6: مشرف يشغّل مستمع alertStream مستقل لكل جهاز مختلف (IP + منفذ + مستخدم)'''
        device_threads = {}
        if not hasattr(self, "device_states"):
            self.device_states = {}
        while not stop_event.is_set():
            wanted = {}
            for c in self.engine.cfg.get("cameras", []):
                if not c.get("enabled", True):
                    continue
                src = str((c.get("analysis") or {}).get("trigger_source") or c.get("trigger_source", "local"))
                if src in ("camera", "both"):
                    key = self._device_key_for_cam(c)
                    if key[0]:
                        wanted[key] = True
            for key in list(device_threads.keys()):
                if key not in wanted:
                    device_threads[key][0].set()
                    del device_threads[key]
            for key in wanted:
                th_info = device_threads.get(key)
                if th_info is None or not th_info[1].is_alive():
                    dev_stop = threading.Event()
                    th = threading.Thread(target=self._device_loop, args=(stop_event, dev_stop, key), daemon=True)
                    device_threads[key] = (dev_stop, th)
                    th.start()
            if not wanted:
                self.connected = False
                self.status_msg = "مكتفٍ بالكاشف المحلي"
            else:
                ok_n = sum(1 for k in wanted if self.device_states.get(k[0]) == "connected")
                self.connected = ok_n > 0
                self.status_msg = f"ISAPI alertStream: {ok_n}/{len(wanted)} أجهزة متصلة"
            if stop_event.wait(5.0):
                break
        for dev_stop, _th in device_threads.values():
            dev_stop.set()

    def _device_loop(self, stop_event, dev_stop, key):
        '''# v16 #6: اتصال alertStream طويل لجهاز واحد مع إعادة اتصال تدريجية'''
        ip, http_port, user, pwd = key
        url = f"http://{ip}:{http_port}/ISAPI/Event/notification/alertStream"
        attempt = 0
        while not stop_event.is_set() and not dev_stop.is_set():
            try:
                self.device_states[ip] = "connecting"
                with requests.get(url, auth=HTTPDigestAuth(user, pwd), stream=True, timeout=(6, 65)) as resp:
                    if resp.status_code != 200:
                        attempt += 1
                        self.device_states[ip] = f"http_{resp.status_code}"
                        if dev_stop.wait(min(30, 2 * (2 ** min(attempt - 1, 4)))) or stop_event.is_set():
                            break
                        continue
                    attempt = 0
                    self.device_states[ip] = "connected"
                    xml_buf = ""
                    for raw_line in resp.iter_lines(decode_unicode=True):
                        if stop_event.is_set() or dev_stop.is_set():
                            break
                        if not raw_line:
                            continue
                        line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="ignore")
                        if "<EventNotificationAlert" in line:
                            xml_buf = line
                        elif xml_buf:
                            xml_buf += "\n" + line
                        if "</EventNotificationAlert>" in xml_buf:
                            self._handle_alert_xml(xml_buf, device_ip=ip)
                            xml_buf = ""
            except Exception:
                attempt += 1
                self.device_states[ip] = "disconnected"
                if dev_stop.wait(min(30, 2 * (2 ** min(attempt - 1, 4)))) or stop_event.is_set():
                    break
        self.device_states[ip] = "stopped"

    def _handle_alert_xml(self, xml_str, device_ip=None):
        try:
            root = ET.fromstring(xml_str.strip())
        except Exception:
            return
        ev_type = xml_find_text(root, "eventType", "").strip()
        ev_state = xml_find_text(root, "eventState", "active").strip().lower()
        if ev_state == "inactive" or ev_type.lower() not in self.SUPPORTED_EVENTS:
            return
        ch_str = xml_find_text(root, "dynChannelID", "") or xml_find_text(root, "channelID", "1")
        target_type = xml_find_text(root, "targetType", "").strip().lower()  # human / vehicle في كاميرات AcuSense
        ch_num = int(ch_str) if str(ch_str).isdigit() else 1

        # إيجاد الكاميرا المطابقة للقناة
        target_cam = None
        for c in self.engine.cfg.get("cameras", []):
            # v16 #6: مطابقة القناة داخل نفس الجهاز اللي أرسل الحدث
            if device_ip and str(c.get("ip") or self.engine.cfg.get("ip") or "").strip() != device_ip:
                continue
            # v16 #R14: المطابقة برقم القناة فقط؛ رقم الكاميرا الداخلي يُستخدم بس لو القناة مو محددة أصلاً
            if str(c.get("channel") or c.get("id", "")).strip() == str(ch_num):
                target_cam = c
                break
        if not target_cam:
            return
        cid = int(target_cam["id"])
        an_cfg = target_cam.get("analysis") if isinstance(target_cam.get("analysis"), dict) else {}
        trig_src = str(an_cfg.get("trigger_source") or target_cam.get("trigger_source", "local"))
        if trig_src not in ("camera", "both") or not is_within_schedule(an_cfg.get("schedule", "always")):
            return

        now = time.time()
        cd = float(an_cfg.get("cooldown_seconds") or self.engine.cfg.get("cooldown_seconds", 12))
        last_t = self.last_event_ts.get((cid, ev_type.lower()), -1e9)
        if (now - last_t) < max(3.0, cd):
            return
        self.last_event_ts[(cid, ev_type.lower())] = now

        mapped_cls = "person" if target_type == "human" else ("vehicle" if target_type == "vehicle" else "other")
        ev_ar_map = {
            "linedetection": "عبور خط (ISAPI)",
            "fielddetection": "دخول منطقة (ISAPI)",
            "regionentrance": "دخول منطقة (ISAPI)",
            "regionexiting": "خروج من منطقة (ISAPI)",
            "vmd": "حركة فيديو (ISAPI VMD)"
        }
        ev_label = ev_ar_map.get(ev_type.lower(), f"حدث كاميرا ({ev_type})")
        if target_type:
            ev_label += f" [{target_type}]"

        detector_meta = {
            "event_kind": "line_cross" if ev_type.lower() == "linedetection" else "zone_motion",
            "crossing_direction": "ISAPI",
            "track_id": 1,
            "target_box": None,
            "detector_class": mapped_cls,
            "detector_subtype": target_type,
            "detector_confidence": 0.85 if target_type in ("human", "vehicle") else 0.65,
            "detector_backend": "isapi_alertStream"
        }
        self.engine.register_and_dispatch_event(
            cam_id=cid,
            trigger_reason=f"{ev_label} - {target_cam.get('name', f'كاميرا {cid}')}",
            detected_ts=now,
            track_ids=[9000 + cid],
            detector_meta=detector_meta
        )

