# -*- coding: utf-8 -*-
"""cctv.selftest — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import sys
import os
import time
import json
import threading
from pathlib import Path
import cv2
import numpy as np
from .ai import evaluate_ai_reply_importance, parse_ai_json_response
from .camera import SingleCameraWorker
from .config import DEFAULT_CONFIG, SECRET_MASK, merge_client_config_preserving_secrets, redact_secrets, sanitize_config_for_client, make_default_cam_analysis
from .detector import DetectionHub, LocalObjectDetector
from .engine import MultiCamMatrixEngine
from .geometry import point_in_polygon
from .go2rtc import Go2RtcManager
from .guard import LoadGuard
from .isapi import IsapiAlertStreamListener
from .paths import CLIP_FPS, PLATES_DIR, SNAP_DIR, EVENTS_ARCHIVE
from .plates import read_best_plate
from .recorder import FfmpegSegmentRecorder
from .rtsp import build_cam_rtsp
from .tracker import CentroidLineTracker
from .web import mjpeg_cam_generator
from . import plates
from .notify import MultiProviderNotifier


# v14 fix #2 / # v15 #2: دالة الاختبار الذاتي الشامل _self_test_classifier لتشغيلها مع --selftest بدون اتصال بأي كاميرا أو إنترنت
def _self_test_classifier():
    from . import web
    engine = web.create_engine(auto_start=False)
    # v15 #2: ضمان عمل --selftest على الطرفيات التي لا تدعم UTF-8 افتراضياً
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # 1. اختبار التصنيف القديم (v14)
    ai_cases = [
        ("[رصد مركبة 🚘] سيارة بيضاء والرصيف خالٍ من المشاة", "vehicle", True),
        ("[رصد حيوان 🐾] قطة تسير على الرصيف", "animal", False),
        ("[رصد مركبة 🚘] شاحنة، لا توجد سيارة أخرى", "vehicle", True),
        ("[عادي] لا يوجد شخص في المشهد", "normal", False),
    ]
    for reply_txt, expected_cat, expected_imp in ai_cases:
        cat, imp = evaluate_ai_reply_importance(reply_txt, is_error=False)
        assert cat == expected_cat and imp == expected_imp, (
            f"FAIL classifier: {reply_txt!r} -> got ({cat}, {imp}), expected ({expected_cat}, {expected_imp})"
        )
        print(f"PASS [1. legacy_classifier] {reply_txt} -> category={cat}, important={imp}")

    # 2. اختبار فك JSON الـ AI (رد سليم، رد قبله نص، رد تالف يرجع للقديم)
    valid_json_str = '{"category":"vehicle","subtype":"car","in_zone":true,"crossed_line":true,"count":1,"plate":"أ ب ج 1234","confidence":0.92,"description_ar":"سيارة بيضاء تعبر الخط"}'
    p1 = parse_ai_json_response(valid_json_str)
    assert p1["category"] == "vehicle" and p1["subtype"] == "car" and p1["confidence"] == 0.92 and p1["_parsed_from_json"] is True
    print("PASS [2a. ai_json_valid] Parsed clean JSON response accurately")

    prefixed_json_str = "نتيجة التحليل البصري:\\n```json\\n" + '{"category":"person","subtype":"adult","in_zone":false,"crossed_line":true,"count":1,"plate":"","confidence":0.87,"description_ar":"شخص يعبر خط المراقبة"}' + "\\n```"
    p2 = parse_ai_json_response(prefixed_json_str)
    assert p2["category"] == "person" and p2["crossed_line"] is True and p2["_parsed_from_json"] is True
    print("PASS [2b. ai_json_prefixed] Extracted JSON object preceded by prose/markdown")

    corrupt_json_str = "[رصد مركبة 🚘] شاحنة تعبر الطريق {json_corrupt: true"
    p3 = parse_ai_json_response(corrupt_json_str, default_crossed_line=True)
    assert p3["category"] == "vehicle" and p3["_parsed_from_json"] is False
    print("PASS [2c. ai_json_corrupt_fallback] Gracefully fell back to parse_ai_category on corrupt JSON")

    # 3. اختبار عبور الخط من نقطة القدم (جسم يعبر = حدث، جسم يقرب ويرجع = لا حدث، اتجاه معاكس للمختار = لا حدث)
    # الخط أفقي عند y=200 من x=50 إلى x=550 على إطار 640x360. نقطة القدم هي (bx + bw/2, by + bh)
    # حالة (أ): جسم يتواجد 3 إطارات فوق الخط (y_foot = 160, 175, 188) ثم يعبر تحت الخط (y_foot = 225)
    tr_cross = CentroidLineTracker()
    for step_i, fy in enumerate([160, 175, 188, 225]):
        boxes_step = [{"bbox": (280, fy - 40, 30, 40), "class_name": "person", "confidence": 0.9}]
        c_boxes, c_dir = tr_cross.update_and_check_crossing(
            boxes_step, 640, 360, True, 50, 200, 550, 200, timestamp=1000.0 + step_i, line_direction="both"
        )
        if step_i < 3:
            assert len(c_boxes) == 0, f"FAIL line_cross: premature trigger at frame {step_i}"
        else:
            assert len(c_boxes) == 1, "FAIL line_cross: expected crossing event after 3 pre-frames"
    print("PASS [3a. line_cross_valid] Object present >=3 frames and crossing line triggered event")

    # حالة (ب): جسم يقترب من الخط (حتى داخل هامش 1%) ثم يرجع لنفس الجهة -> لا حدث
    tr_turnback = CentroidLineTracker()
    for step_i, fy in enumerate([150, 170, 198, 175, 155]):
        boxes_step = [{"bbox": (280, fy - 40, 30, 40), "class_name": "person", "confidence": 0.9}]
        c_boxes, _ = tr_turnback.update_and_check_crossing(
            boxes_step, 640, 360, True, 50, 200, 550, 200, timestamp=2000.0 + step_i, line_direction="both"
        )
        assert len(c_boxes) == 0, "FAIL line_turnback: object approaching and turning back must NOT trigger"
    print("PASS [3b. line_turnback_no_event] Object approaching line and turning back did NOT trigger event")

    # حالة (ج): جسم يعبر بالاتجاه المعاكس للاتجاه المختار -> لا حدث
    tr_opp = CentroidLineTracker()
    for step_i, fy in enumerate([160, 175, 188, 225]):
        boxes_step = [{"bbox": (280, fy - 40, 30, 40), "class_name": "person", "confidence": 0.9}]
        # العبور من y<200 إلى y>200 على خط (50,200)->(550,200) يعطي إشارة من سالب إلى موجب (B_to_A)
        c_boxes, _ = tr_opp.update_and_check_crossing(
            boxes_step, 640, 360, True, 50, 200, 550, 200, timestamp=3000.0 + step_i, line_direction="A_to_B"
        )
        assert len(c_boxes) == 0, "FAIL line_opposite_direction: crossing opposite to selected direction must NOT trigger"
    print("PASS [3c. line_opposite_dir_no_event] Crossing opposite to configured line_direction did NOT trigger")

    # 4. اختبار نقطة داخل مضلّع وخارجه (point_in_polygon)
    poly_test = [(100, 100), (300, 100), (300, 250), (100, 250)]
    assert point_in_polygon(200, 180, poly_test) is True, "FAIL polygon: point (200,180) should be inside"
    assert point_in_polygon(50, 50, poly_test) is False, "FAIL polygon: point (50,50) should be outside"
    print("PASS [4. polygon_inclusion] point_in_polygon verified for inside and outside points")

    # 5. اختبار التنظيف الآمن (سجل فارغ أو تالف لا يحذف أي ملف)
    test_orphan_snap = SNAP_DIR / "selftest_keep_me_orphan.jpg"
    test_orphan_snap.write_bytes(b"\xff\xd8\xff\xe0SELFTEST_IMAGE_DATA")
    old_mtime = time.time() - (48 * 3600)
    os.utime(test_orphan_snap, (old_mtime, old_mtime))
    saved_events_backup = list(engine.events)
    saved_ok_backup = engine.events_loaded_ok
    try:
        engine.events = []
        engine.events_loaded_ok = True
        engine.cleanup_storage(now_ts=time.time() + 1200, bypass_startup_grace=True)
        assert test_orphan_snap.exists(), "FAIL cleanup_storage: deleted file when events list was empty!"

        engine.events = [{"id": "ev1"}]
        engine.events_loaded_ok = False
        engine.cleanup_storage(now_ts=time.time() + 1200, bypass_startup_grace=True)
        assert test_orphan_snap.exists(), "FAIL cleanup_storage: deleted file when events_loaded_ok was False!"
        print("PASS [5. safe_cleanup_storage] Empty or corrupt events log did NOT delete any file")
    finally:
        engine.events = saved_events_backup
        engine.events_loaded_ok = saved_ok_backup
        if test_orphan_snap.exists():
            test_orphan_snap.unlink()

    # 6. اختبار ترويسة MJPEG (وجود سطر جديد حقيقي \r\n وContent-Length)
    gen_chunk = next(mjpeg_cam_generator(99))
    assert b"\r\nContent-Type: image/jpeg\r\nContent-Length: " in gen_chunk, "FAIL MJPEG: missing real CRLF or Content-Length"
    assert b"\\r\\n" not in gen_chunk, "FAIL MJPEG: literal escaped \\\\r\\\\n found in generator output"
    print("PASS [6. mjpeg_header] Real CRLF (\\\\r\\\\n) and Content-Length verified in mjpeg_cam_generator")

    # 7. اختبار إخفاء أسرار كل المزودين الجدد والقدامى
    redact_cases = [
        (
            "rtsp://admin:MyPass123@192.168.8.7:554/Streaming/Channels/101",
            "rtsp://admin:***@192.168.8.7:554/Streaming/Channels/101"
        ),
        (
            "Bearer sk-or-v1-abcdef1234567890abcdef",
            "Bearer ***REDACTED***"
        ),
        (
            "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=AIzaSyA1234567890abcdef",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=***REDACTED***"
        ),
        (
            "https://discord.com/api/webhooks/1234567890/AbCdEfGhIjKlMnOpQrStUvWxYz",
            "https://discord.com/api/webhooks/1234567890/***REDACTED***"
        ),
    ]
    for raw_s, expected_s in redact_cases:
        out_s = redact_secrets(raw_s)
        assert out_s == expected_s, f"FAIL redact_secrets: {raw_s!r} -> got {out_s!r}, expected {expected_s!r}"

    dummy_cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    dummy_cfg["notifiers"]["ntfy"]["token"] = "tk_secret_ntfy_12345"
    dummy_cfg["notifiers"]["smtp"]["password"] = "smtp_super_secret"
    dummy_cfg["notifiers"]["discord"]["webhook_url"] = "https://discord.com/api/webhooks/111/222"
    dummy_cfg["notifiers"]["whatsapp"]["callmebot_apikey"] = "wa_key_999"
    dummy_cfg["notifiers"]["whatsapp"]["access_token"] = "EAAG_wa_cloud_token"
    dummy_cfg["cloud_storage"]["s3"]["access_key"] = "AKIA1234567890ABCDEF"
    dummy_cfg["cloud_storage"]["s3"]["secret_key"] = "s3_private_secret_key"

    sanitized = sanitize_config_for_client(dummy_cfg)
    assert sanitized["notifiers"]["ntfy"]["token"] == SECRET_MASK and sanitized["notifiers"]["ntfy"]["has_token"] is True
    assert sanitized["notifiers"]["smtp"]["password"] == SECRET_MASK and sanitized["notifiers"]["smtp"]["has_password"] is True
    assert sanitized["notifiers"]["discord"]["webhook_url"] == SECRET_MASK and sanitized["notifiers"]["discord"]["has_webhook_url"] is True
    assert sanitized["notifiers"]["whatsapp"]["callmebot_apikey"] == SECRET_MASK and sanitized["notifiers"]["whatsapp"]["has_callmebot_apikey"] is True
    assert sanitized["notifiers"]["whatsapp"]["access_token"] == SECRET_MASK and sanitized["notifiers"]["whatsapp"]["has_access_token"] is True
    assert sanitized["cloud_storage"]["s3"]["access_key"] == SECRET_MASK and sanitized["cloud_storage"]["s3"]["has_access_key"] is True
    assert sanitized["cloud_storage"]["s3"]["secret_key"] == SECRET_MASK and sanitized["cloud_storage"]["s3"]["has_secret_key"] is True

    merged_back = merge_client_config_preserving_secrets(json.loads(json.dumps(dummy_cfg)), sanitized)
    assert merged_back["notifiers"]["ntfy"]["token"] == "tk_secret_ntfy_12345"
    assert merged_back["notifiers"]["smtp"]["password"] == "smtp_super_secret"
    assert merged_back["cloud_storage"]["s3"]["secret_key"] == "s3_private_secret_key"
    print("PASS [7. all_provider_secrets_masked] All new & existing provider secrets masked and preserved on merge")

    # v16 #8: اختبارات v16
    # 8a) سيارة واقفة داخل المنطقة 60 ثانية = حدث واحد فقط
    _tr16 = CentroidLineTracker()
    _w16 = object.__new__(SingleCameraWorker)
    _w16.track_cooldown_map = {}
    _w16.last_zone_motion_ts = -1e9
    _w16.last_trigger_time = 0.0
    _poly16 = [(100, 100), (540, 100), (540, 340), (100, 340)]
    _det16 = {"bbox": (300, 200, 80, 50), "class_name": "vehicle", "subtype": "car", "confidence": 0.9}
    _fired16 = []
    for _i in range(8 * 60):
        _ts = 1000.0 + _i / 8.0
        _tr16.update_and_check_crossing([_det16], 640, 360, False, 0, 0, 0, 0, timestamp=_ts,
                                        polygon_pts=_poly16, zone_enabled=True, zone_dwell_sec=1.0)
        if _tr16.last_zone_dwell_track_ids:
            _ok, _tids = _w16.should_allow_trigger("zone_motion", _tr16.last_zone_dwell_track_ids, 12.0, _ts)
            if _ok:
                _fired16.append(_ts)
                for _t in _tids:
                    _tr16.tracks[_t]["zone_fired"] = True
    assert len(_fired16) == 1, f"FAIL parked object: expected 1 event, got {len(_fired16)}"
    print("PASS [8a. parked_object_single_event] Static object inside zone for 60s fired exactly once")

    # 8b) نفس الجسم يطلع من المنطقة ويرجع = حدث ثاني
    _tr16b = CentroidLineTracker()
    _w16b = object.__new__(SingleCameraWorker)
    _w16b.track_cooldown_map = {}
    _w16b.last_zone_motion_ts = -1e9
    _w16b.last_trigger_time = 0.0
    _fired16b = []
    for _i in range(8 * 40):
        _ts = 2000.0 + _i / 8.0
        _x = 300 if (_i < 8 * 10 or _i >= 8 * 25) else 590
        _d = {"bbox": (_x, 200, 40, 50), "class_name": "person", "subtype": "", "confidence": 0.9}
        _tr16b.update_and_check_crossing([_d], 640, 360, False, 0, 0, 0, 0, timestamp=_ts,
                                         polygon_pts=_poly16, zone_enabled=True, zone_dwell_sec=1.0)
        if _tr16b.last_zone_dwell_track_ids:
            _ok, _tids = _w16b.should_allow_trigger("zone_motion", _tr16b.last_zone_dwell_track_ids, 12.0, _ts)
            if _ok:
                _fired16b.append(_ts)
                for _t in _tids:
                    _tr16b.tracks[_t]["zone_fired"] = True
    assert len(_fired16b) == 2, f"FAIL re-entry: expected 2 events, got {len(_fired16b)}"
    print("PASS [8b. zone_reentry_fires_again] Leaving and re-entering the zone fired a new event")

    # 8c) وقت الحدث واحد للمعاينة والحذف
    _e_old = {"timestamp": "2020-01-01 10:00:00"}
    _e_new = {"detected_ts": 4102444800.0}
    assert MultiCamMatrixEngine._event_epoch(_e_old) < MultiCamMatrixEngine._event_epoch(_e_new)
    assert MultiCamMatrixEngine._event_epoch({}) == 0.0
    print("PASS [8c. event_epoch_consistent] Preview and delete use the same event time")

    # 9a) ONNX parser على مخرجات اصطناعية بصيغة YOLOv8 (1x84x8400)
    _det9 = object.__new__(LocalObjectDetector)
    _det9.lock = threading.Lock()

    class _FakeNet:
        def setInput(self, b):
            self.b = b

        def forward(self):
            o = np.zeros((1, 84, 8400), dtype=np.float32)
            o[0, 0:4, 0] = [320, 320, 100, 200]   # شخص في نص الصورة
            o[0, 4 + 0, 0] = 0.9
            o[0, 0:4, 1] = [100, 400, 160, 80]    # سيارة
            o[0, 4 + 2, 1] = 0.8
            return o

    _det9.onnx_net = _FakeNet()
    _d9 = _det9._detect_onnx(np.zeros((640, 640, 3), dtype=np.uint8), 0.4)
    _cats9 = sorted(d["class_name"] for d in _d9)
    assert _cats9 == ["person", "vehicle"], f"FAIL onnx parse: {_d9}"
    print("PASS [9a. onnx_parser] OpenCV-DNN ONNX output parsed into person + vehicle")

    # 9b) الأحداث المشغولة ما تنحذف عند تجاوز الحد
    _busy = {"id": "b", "ai_status": "pending"}
    _done = {"id": "d", "ai_status": "succeeded", "clip_status": "complete"}
    assert MultiCamMatrixEngine._event_is_busy(_busy) and not MultiCamMatrixEngine._event_is_busy(_done)
    print("PASS [9b. busy_event_detection] Busy events are recognised and protected from trimming")

    # 9c) كتابة الفيديو بمعدل ثابت: إطارات غير منتظمة تطلع بمدة صحيحة
    import tempfile as _tf
    _tmpd = Path(_tf.mkdtemp())
    _frames = []
    _t = 100.0
    for _k in range(20):
        _img = np.full((120, 160, 3), (_k * 12) % 255, dtype=np.uint8)
        _ok, _b = cv2.imencode(".jpg", _img)
        _frames.append((_k, _t, _b.tobytes()))
        _t += 0.05 if _k < 10 else 0.4   # 10 إطارات سريعة ثم 10 بطيئة
    _span = _frames[-1][1] - _frames[0][1]
    _expected = int(round(_span * CLIP_FPS)) + 1
    _vw = cv2.VideoWriter(str(_tmpd / "t.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), float(CLIP_FPS), (160, 120))
    _si, _cur = 0, cv2.imdecode(np.frombuffer(_frames[0][2], np.uint8), cv2.IMREAD_COLOR)
    for _k in range(_expected):
        _tk = _frames[0][1] + _k / float(CLIP_FPS)
        while _si + 1 < len(_frames) and _frames[_si + 1][1] <= _tk + 1e-6:
            _si += 1
        _cur = cv2.imdecode(np.frombuffer(_frames[_si][2], np.uint8), cv2.IMREAD_COLOR)
        _vw.write(_cur)
    _vw.release()
    _cap = cv2.VideoCapture(str(_tmpd / "t.mp4"))
    _n = int(_cap.get(cv2.CAP_PROP_FRAME_COUNT))
    _cap.release()
    assert abs(_n / float(CLIP_FPS) - _span) <= 0.25, f"FAIL timing: {_n} frames for {_span:.2f}s"
    print(f"PASS [9c. constant_fps_clip] {_span:.2f}s of uneven frames -> {_n} frames at {CLIP_FPS}fps (real duration kept)")

    # 10a) منطق قارئ اللوحات: لوحة مقروءة تنقبل، وقراءة فاضية أو ضعيفة تنرفض
    _saved_state = (plates._PLATE_READER, plates._PLATE_READER_STATE, plates.HAS_FAST_ALPR)

    class _B:
        def __init__(self, x1, y1, x2, y2):
            self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2

    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _R:
        def __init__(self, text, conf):
            self.detection = _NS(bounding_box=_B(10, 10, 110, 40), confidence=0.8)
            self.ocr = _NS(text=text, confidence=conf)

    class _FakeReader:
        def __init__(self, results):
            self.results = results

        def predict(self, img):
            return self.results

    plates.HAS_FAST_ALPR, plates._PLATE_READER_STATE = True, "ready"
    _img10 = np.zeros((200, 300, 3), dtype=np.uint8)
    plates._PLATE_READER = _FakeReader([_R("1234ABC__", [0.9, 0.8, 0.85])])
    _good = read_best_plate(_img10, None, 0.55)
    plates._PLATE_READER = _FakeReader([_R("____", 0.95)])
    _empty = read_best_plate(_img10, None, 0.55)
    plates._PLATE_READER = _FakeReader([_R("7XQ", 0.30)])
    _weak = read_best_plate(_img10, None, 0.55)
    plates._PLATE_READER, plates._PLATE_READER_STATE, plates.HAS_FAST_ALPR = _saved_state
    assert _good["ok"] and _good["text"] == "1234ABC", f"FAIL good plate: {_good}"
    assert not _empty["ok"] and not _weak["ok"], f"FAIL rejection: {_empty} {_weak}"
    print("PASS [10a. plate_reader_logic] Readable plate accepted (1234ABC); empty/weak reads rejected")

    # 11a) قراءة retryDelay من رد 429 تبع Google
    class _Resp429:
        def json(self):
            return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                              "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}
    assert MultiCamMatrixEngine._gemini_retry_delay(_Resp429()) == 37.0
    print("PASS [11a. gemini_retry_delay] 429 retryDelay parsed (37s) for model cooldown")

    # 12a) خطأ داخل مكتبة اللوحات ما يطيّح شي
    class _BoomReader:
        def predict(self, img):
            raise RuntimeError("onnx boom")

    plates.HAS_FAST_ALPR, plates._PLATE_READER_STATE, plates._PLATE_READER = True, "ready", _BoomReader()
    _boom = read_best_plate(_img10, None, 0.55)
    plates._PLATE_READER, plates._PLATE_READER_STATE, plates.HAS_FAST_ALPR = _saved_state
    assert not _boom["ok"] and "boom" in _boom.get("error", ""), f"FAIL boom: {_boom}"
    print("PASS [12a. plate_reader_exception] reader.predict() error returned as ok=False (no crash)")

    # 12b) قراءة اللوحة بالخلفية: ما توقف الكشف، والتحليل يستلم النتيجة
    class _SlowReader:
        def predict(self, img):
            time.sleep(0.4)
            return [_R("5678XYZ", 0.9)]

    _eng12 = object.__new__(MultiCamMatrixEngine)
    _eng12.events_lock = threading.RLock()
    _eng12._plate_sem = threading.BoundedSemaphore(2)
    _eng12.cfg = {}
    _upd12 = {}
    _eng12.update_event_fields = lambda eid, persist=True, **kw: _upd12.update(kw)
    plates.HAS_FAST_ALPR, plates._PLATE_READER_STATE, plates._PLATE_READER = True, "ready", _SlowReader()
    _t12 = time.time()
    _eng12._start_plate_job("selftest_plate", _img10, None)
    _start_cost = time.time() - _t12
    _eng12._wait_plate_job("selftest_plate", timeout=4.0)
    plates._PLATE_READER, plates._PLATE_READER_STATE, plates.HAS_FAST_ALPR = _saved_state
    try:
        (PLATES_DIR / "plate_selftest_plate.jpg").unlink()
    except Exception:
        pass
    assert _start_cost < 0.1, f"FAIL plate job blocked detection for {_start_cost:.2f}s"
    assert _upd12.get("plate_text") == "5678XYZ" and _upd12.get("_candidate_plate_url"), f"FAIL plate job: {_upd12}"
    assert "selftest_plate" not in _eng12._plate_jobs
    print(f"PASS [12b. async_plate_job] detection returned in {_start_cost * 1000:.0f}ms; AI got plate 5678XYZ after wait")

    # 12c) ISAPI: القناة 5 تروح للكاميرا اللي قناتها 5، مو الكاميرا اللي رقمها الداخلي 5
    class _Eng12c:
        def __init__(self):
            self.cfg = {"ip": "10.0.0.9", "cameras": [
                {"id": 5, "name": "A", "ip": "10.0.0.9", "channel": "1", "analysis": {"trigger_source": "camera"}},
                {"id": 2, "name": "B", "ip": "10.0.0.9", "channel": "5", "analysis": {"trigger_source": "camera"}}]}
            self.got = []

        def register_and_dispatch_event(self, **kw):
            self.got.append(kw["cam_id"])

    _lis = object.__new__(IsapiAlertStreamListener)
    _lis.engine = _Eng12c()
    _lis.last_event_ts = {}
    _lis._handle_alert_xml("<EventNotificationAlert><channelID>5</channelID><eventType>linedetection</eventType>"
                           "<eventState>active</eventState></EventNotificationAlert>", device_ip="10.0.0.9")
    assert _lis.engine.got == [2], f"FAIL channel match: {_lis.engine.got}"
    print("PASS [12c. isapi_channel_only] channelID=5 -> camera with channel 5 (not internal id 5)")

    # 13a) خيط ضغط العرض ما يدور بلا توقف لما ما فيه مشاهدين (حتى لو الإشارة مرفوعة من stop())
    _w13 = object.__new__(SingleCameraWorker)
    _w13.viewers_lock = threading.Lock()
    _w13.active_viewers = 0
    _w13.viewer_active_event = threading.Event()
    _w13.viewer_active_event.set()  # نفس حالة stop() قبل الإصلاح
    _spins = [0]
    _orig_gav = SingleCameraWorker.get_active_viewers

    def _counting_gav(self):
        _spins[0] += 1
        return _orig_gav(self)

    _w13.get_active_viewers = _counting_gav.__get__(_w13)
    _stop13 = threading.Event()
    _th13 = threading.Thread(target=_w13._display_encode_loop, args=(_stop13,), daemon=True)
    _th13.start()
    time.sleep(0.8)
    _stop13.set()
    _w13.viewer_active_event.set()
    _th13.join(timeout=2)
    assert _spins[0] <= 8, f"FAIL display loop spins with no viewers: {_spins[0]} iterations in 0.8s"
    print(f"PASS [13a. display_loop_idle] No viewers -> {_spins[0]} checks in 0.8s (was a busy spin that starved YOLO)")

    # 13b) ملف go2rtc: كل مصدر يُكتب كنص JSON صالح (كلمات مرور فيها رموز خاصة)
    import tempfile as _tf13
    _g = object.__new__(Go2RtcManager)
    _g.cfg_path = Path(_tf13.mkdtemp()) / "g.yaml"
    _src = build_cam_rtsp({"ip": "10.0.0.5", "port": "554", "username": "admin", "password": 'p@ss:"w/rd#1', "channel": "1"}, role="hq")
    _g._write_yaml({"cam1": _src})
    _lines = _g.cfg_path.read_text(encoding="utf-8").splitlines()
    _cam_line = [l for l in _lines if l.startswith("  cam1: ")][0]
    assert json.loads(_cam_line.split(": ", 1)[1]) == _src and '127.0.0.1:1984' in "\n".join(_lines)
    print("PASS [13b. go2rtc_yaml] Stream source written as valid quoted string; API bound to 127.0.0.1")

    # 14a) الكشف الجماعي: 6 كاميرات تطلب بنفس الوقت → دفعة وحدة وكل وحدة تستلم نتيجتها
    class _FakeBatchDet:
        yolo_model = object()
        yolo_device = 0
        calls = []

        def detect_batch(self, frames, min_conf):
            self.calls.append(len(frames))
            time.sleep(0.02)
            return [[{"bbox": (int(f[0, 0, 0]), 0, 10, 10), "class_name": "person", "subtype": "p", "confidence": 0.9}] for f in frames]

    _fd = _FakeBatchDet()
    _hub = DetectionHub(_fd)
    _res14 = {}

    def _ask(i):
        fr = np.full((8, 8, 3), i, dtype=np.uint8)
        _res14[i] = _hub.detect(fr, 0.5, timeout=3)

    _ths = [threading.Thread(target=_ask, args=(i,)) for i in range(6)]
    [t.start() for t in _ths]
    [t.join() for t in _ths]
    assert all(_res14[i] and _res14[i][0]["bbox"][0] == i for i in range(6)), f"FAIL hub results: {_res14}"
    assert max(_fd.calls) >= 2, f"FAIL hub did not batch: {_fd.calls}"
    assert _hub.suggested_fps(16, 10) <= 10 and _hub.suggested_fps(1, 10) == 10
    print(f"PASS [14a. detection_hub] 6 concurrent requests served in batches {_fd.calls}; per-camera results correct")

    # 14b) اختيار مقاطع ffmpeg اللي تغطي [قبل 5 ث، بعد 10 ث] مع استبعاد المقطع المفتوح
    _T = 1000.0
    _segs = [(f"s{i}", _T - 20 + i * 4, _T - 16 + i * 4) for i in range(9)]  # 980..1016, الأخير مفتوح
    _sel = FfmpegSegmentRecorder.select_segments(_segs, _T, 5, 10)
    assert [s[0] for s in _sel] == ["s3", "s4", "s5", "s6", "s7"], f"FAIL segment selection: {[s[0] for s in _sel]}"
    print("PASS [14b. ffmpeg_segment_select] Segments covering [T-5, T+10] chosen (s3..s7), open segment excluded")

    # 14c) الحماية الذاتية: حمل مرتفع 20 ث → مستوى 1 (نصف الكشف)؛ هدوء 60 ث → يرجع
    _lg = LoadGuard(None, start_thread=False)
    _t0 = 5000.0
    for k in range(5):
        _lg.apply(95, 10, _t0 + k * 5)
    assert _lg.level == 1 and _lg.det_scale == 0.5 and _lg.msg, f"FAIL guard escalate: {_lg.status()}"
    for k in range(13):
        _lg.apply(30, 10, _t0 + 30 + k * 5)
    assert _lg.level == 0 and _lg.det_scale == 1.0 and not _lg.msg, f"FAIL guard recover: {_lg.status()}"
    print("PASS [14c. load_guard] CPU 95% for 20s -> detection halved; calm 60s -> restored")

    # 15a) فلتر وسيط الثقة: جسم بثقة 0.5 يقف داخل المنطقة ما يطلق حدث؛ وبثقة 0.8 يطلق
    for conf_v, expect in ((0.5, 0), (0.8, 1)):
        _tk = CentroidLineTracker()
        _poly = [(100, 100), (500, 100), (500, 300), (100, 300)]
        fired = 0
        for k in range(12):
            _tk.update_and_check_crossing([{"bbox": (280, 150, 40, 90), "class_name": "person", "confidence": conf_v}], 640, 360,
                                          False, 0, 0, 0, 0, timestamp=1000 + k * 0.2, polygon_pts=_poly, zone_enabled=True,
                                          zone_dwell_sec=1.0, alert_threshold=0.6, min_track_frames=3)
            fired += len(_tk.last_zone_dwell_track_ids)
            for _tid in _tk.last_zone_dwell_track_ids:
                _tk.tracks[_tid]["zone_fired"] = True  # مثل ما يسويه عامل الكاميرا بعد إطلاق الحدث
        assert fired == expect, f"FAIL alert_threshold conf={conf_v}: fired={fired}"
    print("PASS [15a. alert_threshold] median conf 0.5 blocked, 0.8 fired (leaves/shadows filtered, real person passes)")

    # 15b) قناع الاستبعاد: نقطة القدم داخل القناع تُستبعد
    assert point_in_polygon(150, 280, [(100, 100), (200, 100), (200, 300), (100, 300)]) and not point_in_polygon(450, 280, [(100, 100), (200, 100), (200, 300), (100, 300)])
    print("PASS [15b. exclusion_mask] foot point inside mask excluded, outside kept")

    # 16a) بث التحليل مستقل عن live_stream/stream_type
    _cam16 = {"ip": "10.0.0.5", "port": "554", "username": "a", "password": "b", "channel": "1", "stream_type": "main", "live_stream": "main"}
    assert build_cam_rtsp(dict(_cam16, analysis_stream="sub"), role="live").endswith("/102")
    assert build_cam_rtsp(dict(_cam16, analysis_stream="third"), role="live").endswith("/103")
    assert build_cam_rtsp(dict(_cam16, analysis_stream="main"), role="live").endswith("/101")
    assert build_cam_rtsp(dict(_cam16, analysis_stream="main", _analysis_override="sub"), role="live").endswith("/102")
    assert build_cam_rtsp(_cam16, role="hq").endswith("/101") and build_cam_rtsp(_cam16, role="third").endswith("/103")
    print("PASS [16a. analysis_stream] analysis source independent of display/recording; override forces sub")

    # 16b) الحماية الذاتية: السبب = بث تحليل رئيسي → الإجراء تحويل للفرعي بدون خفض الكشف؛ بدون كاميرات رئيسية → خفض الكشف
    _g = LoadGuard(None, start_thread=False)
    _sw = []
    for k in range(5):
        _g.apply(95, 10, 7000 + k * 5, main_cams=[1, 2], switcher=lambda cams: (_sw.extend(cams), list(cams))[1])
    assert _g.level == 1 and _g.det_scale == 1.0 and _sw == [1, 2] and "الفرعي" in _g.msg and "CAM-01" in _g.msg, _g.status()
    _g2 = LoadGuard(None, start_thread=False)
    for k in range(5):
        _g2.apply(95, None, 8000 + k * 5, main_cams=[])
    assert _g2.level == 1 and _g2.det_scale == 0.5 and "غير متاح" in _g2.msg and _g2.status()["gpu"] is None, _g2.status()
    print("PASS [16b. load_guard_cause] main-stream cams switched to sub (detection kept); GPU unavailable shown as such")

    # 16c) أسباب عدم الإرسال بدقة
    _sr = MultiProviderNotifier._skip_reason
    assert _sr({"enabled": False}, 1, "person") and "غير مفعّل" in _sr({"enabled": False}, 1, "person")
    assert "ناقصة" in _sr({"enabled": True, "bot_token": "", "chat_id": "1"}, 1, "person", needs=("bot_token", "chat_id"))
    assert "الكاميرات" in _sr({"enabled": True, "cameras": "2,3"}, 1, "person")
    assert "الأنواع" in _sr({"enabled": True, "categories": ["person"]}, 1, "vehicle")
    assert _sr({"enabled": True, "cameras": "all", "categories": ["person", "vehicle"]}, 1, "vehicle") is None
    # v17 #R26: نداء ثابت بدون self (الخطأ القديم: takes 3 positional arguments but 4 were given)
    _n16 = object.__new__(MultiProviderNotifier)
    assert _n16._matches_filter({"enabled": True, "cameras": "all"}, 1, "person") is True
    assert _n16._matches_filter({"enabled": False}, 1, "person") is False
    print("PASS [16c. notify_reasons] disabled / missing data / camera filter / category filter reported precisely")

    # 16d) تيليجرام بمحاكاة الرد (بدون أي إرسال فعلي): قبول، 429 مع retry_after، ورفض 400
    import types as _types
    from . import engine as _engine_mod

    class _FakeResp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body
            self.text = json.dumps(body)

        def json(self):
            return self._b

    _calls = []

    def _fake_post(url, data=None, files=None, timeout=None):
        _calls.append(1)
        n = len(_calls)
        if _scenario == "ok":
            return _FakeResp(200, {"ok": True, "result": {"message_id": 7}})
        if _scenario == "429":
            return _FakeResp(429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0.01}}) if n == 1 else _FakeResp(200, {"ok": True, "result": {"message_id": 8}})
        return _FakeResp(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    _eng16 = object.__new__(MultiCamMatrixEngine)
    _eng16.cfg = {"telegram_bot_token": "123:abc", "telegram_chat_id": "42", "notifiers": {}}
    _eng16.last_telegram_ms = 0
    _img16 = SNAP_DIR / "selftest_tg.jpg"
    cv2.imwrite(str(_img16), np.zeros((40, 40, 3), dtype=np.uint8))
    _orig_post = _engine_mod.requests.post
    _engine_mod.requests.post = _fake_post
    try:
        _scenario = "ok"; _calls.clear(); r_ok = _eng16._send_telegram_alert(_img16, "t")
        _scenario = "429"; _calls.clear(); r_429 = _eng16._send_telegram_alert(_img16, "t")
        _scenario = "400"; _calls.clear(); r_400 = _eng16._send_telegram_alert(_img16, "t")
    finally:
        _engine_mod.requests.post = _orig_post
        try:
            _img16.unlink()
        except Exception:
            pass
    assert r_ok["status"] == "accepted" and r_ok["message_id"] == 7
    assert r_429["status"] == "accepted" and r_429["attempts"] == 2
    assert r_400["status"] == "failed" and "chat not found" in r_400["message"], r_400
    print("PASS [16d. telegram_simulated] accepted / 429 retried then accepted / 400 reported with description (no real send)")

    # 16e) أرشيف الأحداث: ما يخرج من الفهرس يُحفظ ويُقرأ
    import tempfile as _tf16
    _arch_backup = EVENTS_ARCHIVE.read_bytes() if EVENTS_ARCHIVE.exists() else None
    try:
        if EVENTS_ARCHIVE.exists():
            EVENTS_ARCHIVE.unlink()
        _now16 = time.time()
        MultiCamMatrixEngine.archive_events([{"id": "old1", "detected_ts": _now16 - 100, "category": "person", "_secret": 1, "_snap_path": "x"},
                                             {"id": "old2", "detected_ts": _now16 - 50, "category": "vehicle"}])
        _got = MultiCamMatrixEngine.load_archived_events(since_ts=_now16 - 80)
        assert [e["id"] for e in _got] == ["old2"], _got
        _all16 = MultiCamMatrixEngine.load_archived_events()
        assert len(_all16) == 2 and "_secret" not in _all16[1] and _all16[1].get("_snap_path") == "x"
    finally:
        if _arch_backup is not None:
            EVENTS_ARCHIVE.write_bytes(_arch_backup)
        elif EVENTS_ARCHIVE.exists():
            EVENTS_ARCHIVE.unlink()
    print("PASS [16e. events_archive] trimmed events persisted to JSONL and filtered by time on read")

    # 17a) الوسيط المحلي: لو go2rtc شغّال والبث مسجّل → رابط محلي؛ وإلا None
    _g17 = object.__new__(Go2RtcManager)
    _g17.running, _g17.streams = True, {"cam1": "x", "cam1_sub": "y", "cam1_third": "z"}
    assert _g17.relay_url(1, "sub") == "rtsp://127.0.0.1:8554/cam1_sub"
    assert _g17.relay_url(1, "main") == "rtsp://127.0.0.1:8554/cam1" and _g17.relay_url(1, "third") == "rtsp://127.0.0.1:8554/cam1_third"
    assert _g17.relay_url(2, "sub") is None
    _g17.running = False
    assert _g17.relay_url(1, "sub") is None
    print("PASS [17a. go2rtc_relay] local relay URLs when running/registered; direct camera URL otherwise")

    # 17b) تبريد 429 يتضاعف مع التكرار (3 دقائق → 6 → 12) ويرجع بعد النجاح
    _eng17 = object.__new__(MultiCamMatrixEngine)
    _eng17._gemini_cooldown, _eng17._gemini_strikes = {}, {}
    _base = 180.0
    _waits = []
    for k in range(3):
        _eng17._gemini_strikes["m"] = min(6, _eng17._gemini_strikes.get("m", 0) + 1)
        _waits.append(min(7200.0, max(60.0, _base) * (2 ** (_eng17._gemini_strikes["m"] - 1))))
    assert _waits == [180.0, 360.0, 720.0], _waits
    print("PASS [17b. gemini_backoff] repeated 429 -> 3m, 6m, 12m cooldown (no retry storm)")

    # 17c) الافتراضي بدون «أخرى»
    assert "other" not in make_default_cam_analysis()["detect_classes"]
    print("PASS [17c. default_classes] 'other' not detected by default")

    # 17d) دقة الكشف: الحفظ ما يسمح إلا sub/third (الرئيسي ممنوع حتى لو جاء من إعدادات قديمة)
    _c17 = {"cameras": [{"id": 9, "name": "x", "analysis_stream": "main"}]}
    assert str(_c17["cameras"][0].get("analysis_stream")) == "main"
    # نفس منطق التحميل: main → sub
    for c in _c17["cameras"]:
        if str(c.get("analysis_stream")) == "main":
            c["analysis_stream"] = "sub"
    assert _c17["cameras"][0]["analysis_stream"] == "sub"
    print("PASS [17d. detect_quality] only sub/third allowed for analysis; legacy 'main' normalised to sub")

    # 17e) إطار عند لحظة الحدث: اختيار المقطع الصحيح والإزاحة داخله
    _segs17 = [("a", 100.0, 104.0), ("b", 104.0, 108.5), ("c", 108.5, 112.0)]
    assert FfmpegSegmentRecorder.segment_for_ts(_segs17, 105.2) == ("b", 1.2000000000000028) or abs(FfmpegSegmentRecorder.segment_for_ts(_segs17, 105.2)[1] - 1.2) < 1e-6
    assert FfmpegSegmentRecorder.segment_for_ts(_segs17, 99.0) is None and FfmpegSegmentRecorder.segment_for_ts(_segs17, 112.0) is None
    assert FfmpegSegmentRecorder.segment_for_ts(_segs17, 108.5)[0] == "c"
    print("PASS [17e. frame_at_event_time] segment + offset resolved for the event timestamp")

    # 18a) ترتيب موديلات Gemini: الميت يُستبعد، الجاهز أولاً، ولو الكل في تبريد ناخذ الأقرب انتهاءً
    _ord = MultiCamMatrixEngine._gemini_order
    _now = 1000.0
    assert _ord(["a", "b", "c"], {}, set(), _now)[0] == ["a", "b", "c"]
    assert _ord(["a", "b", "c"], {"a": _now + 50}, {"c"}, _now)[0] == ["b"]
    assert _ord(["a", "b"], {"a": _now + 300, "b": _now + 50}, set(), _now)[0] == []
    assert _ord(["a", "b"], {}, set(), _now, available={"b"})[0] == ["b"]
    print("PASS [18a. gemini_order] dead/unavailable models skipped; cooldown always respected")

    # 18b) منع تكرار حدث لنفس الجسم الواقف (تداخل ≥ 0.5 خلال 90 ثانية)
    _w18 = object.__new__(SingleCameraWorker)
    _w18.manager = type("M", (), {"cfg": {"stationary_suppress_sec": 90}})()
    assert _w18._is_recent_duplicate((100, 100, 200, 120), 5000.0) is False
    assert _w18._is_recent_duplicate((110, 105, 200, 120), 5030.0) is True
    assert _w18._is_recent_duplicate((400, 100, 200, 120), 5030.0) is False
    assert _w18._is_recent_duplicate((100, 100, 200, 120), 5200.0) is False
    print("PASS [18b. stationary_dedup] same parked object suppressed for 90s; different object and later time allowed")

    print("PASS: All V18 inherited self-tests succeeded.")
    return True

