# -*- coding: utf-8 -*-
"""cctv.web — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import time
import json
import threading
from collections import deque, Counter
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
import requests
from flask import Flask, Response, request, jsonify, render_template, send_from_directory
from .config import SECRET_MASK, make_cam_entry, merge_client_config_preserving_secrets, redact_secrets, sanitize_config_for_client, save_config, save_events
from .engine import MultiCamMatrixEngine
from .paths import CLIPS_DIR, DISK_IO_LOCK, MAX_CAMERAS, PLATES_DIR, SNAP_DIR, STATIC_DIR, EVENTS_ARCHIVE
from .recorder import FfmpegSegmentRecorder
from .tracker import CentroidLineTracker


app = Flask("cctv")  # القوالب والملفات الثابتة داخل حزمة cctv (templates/ و static/)
engine = None  # يُنشأ عبر create_engine() من ملف التشغيل


def create_engine(auto_start=True):
    global engine
    engine = MultiCamMatrixEngine(auto_start=auto_start)
    return engine
@app.route("/")
def index():
    # v17 #R20: الصفحة ما تنحفظ في كاش المتصفح، عشان أي تحديث يظهر بدون Ctrl+F5 (رقم الإصدار يجدد app.js/app.css)
    resp = app.make_response(render_template("index.html", v=asset_version()))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


def asset_version():
    '''رقم إصدار للملفات الثابتة (آخر تعديل) عشان المتصفح ما يستخدم نسخة قديمة'''
    try:
        return int(max(p.stat().st_mtime for p in (Path(app.root_path) / "static").glob("*")))
    except Exception:
        return int(time.time())

@app.route("/snapshots/<path:filename>")
def serve_snapshot(filename):
    return send_from_directory(str(SNAP_DIR), filename)

@app.route("/plates/<path:filename>")
def serve_plate(filename):
    return send_from_directory(str(PLATES_DIR), filename)

@app.route("/clips/<path:filename>")
def serve_clip_file(filename):
    return send_from_directory(str(CLIPS_DIR), filename)

_ASSET_EXTS = {".js", ".css", ".woff2", ".svg", ".png"}

@app.route("/assets/<path:filename>")
def serve_asset(filename):
    '''# v16 #R16: ملفات الواجهة المحلية (الخط وChart.js) — تشتغل بدون إنترنت'''
    if Path(filename).suffix.lower() not in _ASSET_EXTS:
        return ("", 404)
    resp = send_from_directory(str(STATIC_DIR), filename)
    resp.cache_control.max_age = 7 * 86400
    return resp


@app.route("/api/overlay")
def api_overlay():
    '''# v16 #R17: مربعات الكشف الحية (منسوبة 0..1) لرسمها فوق البث عالي الدقة في المتصفح'''
    out = {}
    for cid, w in list(engine.workers.items()):
        dw, dh = w.det_size if w.det_size and w.det_size[0] > 0 else (640, 360)
        boxes = []
        for d in list(w.latest_detections)[:12]:
            try:
                bx, by, bw, bh = d["bbox"]
                boxes.append({"x": round(bx / dw, 4), "y": round(by / dh, 4), "w": round(bw / dw, 4), "h": round(bh / dh, 4),
                              "c": d.get("class_name", "other"), "p": round(float(d.get("confidence", 0)), 2)})
            except Exception:
                continue
        out[cid] = {"boxes": boxes, "flash": w.line_triggered_flash > 0, "running": w.running}
    return jsonify(out)


@app.route("/api/events")
def api_events():
    '''# v17 #R21: الأحداث من الفهرس السريع + الأرشيف (للبحث والجدول)، الأحدث أولاً'''
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
        limit = max(10, min(2000, int(request.args.get("limit", 400))))
    except Exception:
        days, limit = 30, 400
    since = time.time() - days * 86400
    with engine.events_lock:
        mem = [{k: v for k, v in e.items() if not str(k).startswith("_")} for e in engine.events]
    ids = {e.get("id") for e in mem}
    arch = [{k: v for k, v in e.items() if not str(k).startswith("_")} for e in MultiCamMatrixEngine.load_archived_events(since_ts=since, limit=limit)
            if e.get("id") not in ids]
    allev = [e for e in mem if MultiCamMatrixEngine._event_epoch(e) >= since] + arch
    allev.sort(key=MultiCamMatrixEngine._event_epoch, reverse=True)
    return jsonify({"events": allev[:limit], "total_index": len(mem), "archived": len(arch), "days": days})


@app.route("/api/stats")
def api_stats():
    '''# v16 #R16: إحصائيات الأحداث المحفوظة (آخر 24 ساعة بالساعة + توزيع الفئات والكاميرات وأوقات النشاط)'''
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except Exception:
        days = 7
    now = time.time()
    since = now - days * 86400
    cats = ("person", "vehicle", "animal", "other")
    hourly = [dict.fromkeys(cats, 0) for _ in range(24)]
    hour_of_day = [0] * 24
    by_cat, by_cam, by_day = Counter(), Counter(), Counter()
    lat, delay = [], []
    total = important = confirmed = plates = 0
    with engine.events_lock:
        evs = [dict(e) for e in engine.events]
    _ids = {e.get("id") for e in evs}
    evs += [e for e in MultiCamMatrixEngine.load_archived_events(since_ts=since) if e.get("id") not in _ids]  # v17 #R21
    for ev in evs:
        ts = MultiCamMatrixEngine._event_epoch(ev)
        if ts <= 0 or ts < since or ts > now + 60:
            continue
        total += 1
        cat = ev.get("category") if ev.get("category") in cats else "other"
        by_cat[cat] += 1
        cam_label = f"CAM-{int(ev.get('cam_id') or 0):02d}"
        by_cam[cam_label] += 1
        lt = time.localtime(ts)
        by_day[time.strftime("%Y-%m-%d", lt)] += 1
        hour_of_day[lt.tm_hour] += 1
        if now - ts < 86400:
            hourly[min(23, max(0, 23 - int((now - ts) // 3600)))][cat] += 1
        important += 1 if ev.get("important") else 0
        confirmed += 1 if ev.get("confirmed") else 0
        plates += 1 if ev.get("plate_status") == "confirmed" else 0
        if ev.get("ai_latency_ms"):
            lat.append(float(ev["ai_latency_ms"]))
        if ev.get("alert_delay_ms") and ev.get("telegram_status") == "accepted":
            delay.append(float(ev["alert_delay_ms"]))
    hour_labels = [time.strftime("%H:00", time.localtime(now - (23 - i) * 3600)) for i in range(24)]
    day_keys = [time.strftime("%Y-%m-%d", time.localtime(now - (days - 1 - i) * 86400)) for i in range(days)]
    return jsonify({
        "days": days,
        "total": total,
        "important": important,
        "confirmed": confirmed,
        "plates": plates,
        "avg_ai_ms": int(sum(lat) / len(lat)) if lat else 0,
        "avg_alert_ms": int(sum(delay) / len(delay)) if delay else 0,
        "hourly": {"labels": hour_labels, "series": {c: [h[c] for h in hourly] for c in cats}},
        "hour_of_day": hour_of_day,
        "by_category": {c: by_cat.get(c, 0) for c in cats},
        "by_camera": dict(sorted(by_cam.items())),
        "by_day": {"labels": day_keys, "values": [by_day.get(d, 0) for d in day_keys]},
        "stored_events": len(evs),
    })

@app.route("/api/probe_device", methods=["POST"])
def api_probe_device():
    data = request.json or {}
    cid = int(data.get("cam_id", 1))
    http_port = str(data.get("http_port", "80")).strip() or "80"
    cam = next((c for c in engine.cfg.get("cameras", []) if int(c["id"]) == cid), None)
    if not cam:
        return jsonify({"ok": False, "error": "الكاميرا المحددة غير موجودة."})
    cam["http_port"] = http_port
    res = engine.probe_camera_hw(cid)

    if not res.get("isapi_ok"):
        return jsonify({"ok": False, "error": redact_secrets(res.get("error_reason", "فشل الاتصال بـ ISAPI"), engine.cfg)})

    ch_map = res.get("channels", {})
    with engine.lock:
        existing_ids = {int(c["id"]) for c in engine.cfg.get("cameras", [])}
        for ch_str, ch_info in ch_map.items():
            if ch_str.isdigit():
                ch_num = int(ch_str)
                if ch_num not in existing_ids and len(engine.cfg["cameras"]) < MAX_CAMERAS:
                    engine.cfg["cameras"].append(
                        make_cam_entry(
                            ch_num,
                            ch_info.get("name") or f"كاميرا قناة {ch_num}",
                            "hikvision_nvr",
                            cam.get("ip", "192.168.8.7"),
                            cam.get("port", "554"),
                            cam.get("username", "admin"),
                            cam.get("password", ""),
                            ch_str,
                            cam.get("stream_type", "main"),
                            enabled=True,
                            ai_watch=(ch_num <= 4),
                            cam_hw_info={
                                "cam_model": ch_info["cam_model"],
                                "cam_firmware": ch_info["cam_firmware"],
                                "cam_serial": ch_info["cam_serial"],
                                "nvr_model": ch_info["nvr_model"],
                                "nvr_firmware": ch_info["nvr_firmware"],
                                "rtsp_configured_kbps": ch_info.get("configured_kbps", 0),
                                "source_note": ch_info["source_note"]
                            }
                        )
                    )
        save_config(engine.cfg)
    engine.sync_workers(auto_start=False)
    return jsonify({"ok": True, "info": res, "config": sanitize_config_for_client(engine.cfg)})

@app.route("/api/hikconnect_sync", methods=["POST"])
def api_hikconnect_sync():
    data = request.json or {}
    api_url = (data.get("api_url") or "https://open.ezvizlife.com").strip().rstrip("/")
    app_key = (data.get("app_key") or "").strip()
    app_secret = (data.get("app_secret") or "").strip()
    if app_secret == SECRET_MASK:
        app_secret = (engine.cfg.get("hikconnect") or {}).get("app_secret", "").strip()

    if not app_key or not app_secret:
        engine.cfg["hikconnect"]["connected"] = False
        save_config(engine.cfg)
        return jsonify({
            "ok": False,
            "error": "يجب إدخال AppKey و AppSecret الصحيحين من بوابة مطوري Hikvision/EZVIZ OpenAPI (أو استخدم فحص ISAPI المحلي أعلاه)."
        })

    try:
        token_resp = requests.post(
            f"{api_url}/api/lapp/token/get",
            data={"appKey": app_key, "appSecret": app_secret},
            timeout=10
        )
        if token_resp.status_code != 200:
            engine.cfg["hikconnect"]["connected"] = False
            save_config(engine.cfg)
            return jsonify({"ok": False, "error": f"رفض سيرفر السحابة الاتصال (HTTP {token_resp.status_code})."})

        t_json = token_resp.json()
        if str(t_json.get("code")) != "200" or not t_json.get("data", {}).get("accessToken"):
            engine.cfg["hikconnect"]["connected"] = False
            save_config(engine.cfg)
            return jsonify({"ok": False, "error": f"فشل التحقق من مفاتيح السحابة: {redact_secrets(t_json.get('msg', 'مفاتيح غير صالحة'), engine.cfg)}"})

        access_token = t_json["data"]["accessToken"]
        cam_resp = requests.post(
            f"{api_url}/api/lapp/camera/list",
            data={"accessToken": access_token, "pageStart": 0, "pageSize": 32},
            timeout=10
        )
        c_json = cam_resp.json()
        cloud_list = c_json.get("data") or []
        if not cloud_list:
            return jsonify({"ok": False, "error": "تم التحقق من الحساب السحابي ولكن لم يتم العثور على كاميرات مرتبطة به."})

        engine.cfg["hikconnect"] = {
            "api_url": api_url,
            "app_key": app_key,
            "app_secret": app_secret,
            "connected": True,
            "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_config(engine.cfg)
        return jsonify({
            "ok": True,
            "message": f"✓ تم التحقق الفعلي من السحابة والعثور على {len(cloud_list)} كاميرا!",
            "config": sanitize_config_for_client(engine.cfg)
        })
    except Exception as ex:
        engine.cfg["hikconnect"]["connected"] = False
        save_config(engine.cfg)
        return jsonify({"ok": False, "error": f"خطأ أثناء الاتصال بسحابة OpenAPI: {redact_secrets(ex, engine.cfg)}"})

@app.route("/api/toggle_lpr", methods=["POST"])
def api_toggle_lpr():
    data = request.json or {}
    cid = int(data.get("cam_id", 1))
    enabled = bool(data.get("lpr_enabled", False))
    with engine.lock:
        for c in engine.cfg.get("cameras", []):
            if int(c["id"]) == cid:
                c["lpr_enabled"] = enabled
                c.setdefault("analysis", {})["lpr_enabled"] = enabled
                break
        if cid in engine.workers:
            engine.workers[cid].cam["lpr_enabled"] = enabled
            engine.workers[cid].cam.setdefault("analysis", {})["lpr_enabled"] = enabled
        save_config(engine.cfg)
    return jsonify({"ok": True, "cam_id": cid, "lpr_enabled": enabled})

@app.route("/api/clip/<clip_id>")
def get_clip_data(clip_id):
    safe_id = "".join(c for c in clip_id if c.isalnum() or c in ("_", "-"))
    cpath = CLIPS_DIR / f"{safe_id}.json"
    if not cpath.exists():
        return jsonify({"ok": False, "frames": []})
    try:
        with DISK_IO_LOCK:
            data = json.loads(cpath.read_text(encoding="utf-8"))
        return jsonify({
            "ok": bool(data.get("ok", True) and (data.get("mp4_verified") or data.get("frames"))),
            "fps": data.get("fps", 8),
            "effective_fps": data.get("effective_fps", data.get("fps", 8)),
            "duration_sec": data.get("duration_sec", 0),
            "pre_duration_sec": data.get("pre_duration_sec", 0),
            "post_duration_sec": data.get("post_duration_sec", 0),
            "trigger_offset_sec": data.get("trigger_offset_sec", 0),
            "clip_status": data.get("clip_status", "complete"),
            "clip_status_msg": data.get("clip_status_msg", ""),
            "mp4_verified": data.get("mp4_verified", False),
            "mp4_url": data.get("mp4_url", ""),
            "browser_playable": data.get("browser_playable", False),
            "needs_hevc": bool(data.get("needs_hevc", False)),
            "codec": data.get("codec", ""),
            "resolution": data.get("resolution", ""),
            "frame_timestamps_rel": data.get("frame_timestamps_rel", []),
            "frames": data.get("frames", [])
        })
    except Exception:
        return jsonify({"ok": False, "frames": []})

@app.route("/api/save_roi", methods=["POST"])
def api_save_roi():
    data = request.json or {}
    cid = int(data.get("cam_id", 1))
    roi = data.get("roi") or {}
    engine.update_camera_roi(cid, roi)
    return jsonify({"ok": True})


@app.route("/api/reset_roi", methods=["POST"])
def api_reset_roi():
    '''# v16 #9: مسح خط العبور و/أو منطقة المراقبة لكاميرا وحدة أو لكل الكاميرات، مع تصفير متتبع الأجسام'''
    data = request.json or {}
    which = str(data.get("which") or "all").strip()
    if which not in ("line", "zone", "mask", "all"):
        return jsonify({"ok": False, "error": "which لازم تكون line أو zone أو mask أو all"}), 400
    target = str(data.get("cam_id", "all")).strip()
    target_id = None
    if target != "all":
        try:
            target_id = int(target)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "رقم الكاميرا غير صالح"}), 400

    updated = []
    with engine.lock:
        for c in engine.cfg.get("cameras", []):
            cid = int(c.get("id", 0))
            if target_id is not None and cid != target_id:
                continue
            roi = dict(c.get("roi") or {})
            if which in ("line", "all"):
                roi["line_enabled"] = False
            if which in ("zone", "all"):
                roi["zone_enabled"] = False
                roi["polygon"] = []
            if which in ("mask", "all"):
                roi["masks"] = []  # v17 #R20
            c["roi"] = roi
            w = engine.workers.get(cid)
            if w is not None:
                w.cam["roi"] = roi
                w.tracker = CentroidLineTracker()
                w.track_cooldown_map = {}
            updated.append({"id": cid, "roi": roi})
        save_config(engine.cfg)
    if target_id is not None and not updated:
        return jsonify({"ok": False, "error": "الكاميرا غير موجودة"}), 404
    return jsonify({"ok": True, "count": len(updated), "cameras": updated})
_STANDBY_JPEG_CACHE = {}
_STANDBY_CACHE_LOCK = threading.Lock()


def get_cached_standby_jpeg(cam_id, max_width=None):
    '''# v15 #4: ينشئ صورة STANDBY مرة واحدة فقط لكل كاميرا/عرض ويحفظها في الذاكرة دون إعادة تكوينها'''
    cid = int(cam_id)
    w_key = 480 if (max_width and int(max_width) <= 480) else 640
    cache_key = (cid, w_key)
    with _STANDBY_CACHE_LOCK:
        if cache_key in _STANDBY_JPEG_CACHE:
            return _STANDBY_JPEG_CACHE[cache_key]
        h_val = 270 if w_key == 480 else 360
        blank = np.zeros((h_val, w_key, 3), dtype=np.uint8)
        scale = 0.62 if w_key == 480 else 0.8
        cv2.putText(blank, f"CAM {cid:02d} - STANDBY", (int(w_key * 0.26), int(h_val * 0.48)), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 212, 255), 2)
        cv2.putText(blank, "Click Play to start stream", (int(w_key * 0.28), int(h_val * 0.60)), cv2.FONT_HERSHEY_SIMPLEX, scale * 0.7, (148, 163, 184), 1)
        ret, buf = cv2.imencode(".jpg", blank, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        jpg_bytes = buf.tobytes() if ret else b""
        _STANDBY_JPEG_CACHE[cache_key] = jpg_bytes
        return jpg_bytes


def format_mjpeg_part(jpg_bytes):
    '''# v15 #4: يبني جزء MJPEG مع Content-Length وأسطر \\r\\n حقيقية'''
    return (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpg_bytes)).encode("ascii")
        + b"\r\n\r\n"
        + jpg_bytes
        + b"\r\n"
    )


def mjpeg_cam_generator(cam_id, max_width=None):
    '''
    # v15 #3 / # v15 #4: مولد البث المباشر عالي الأداء:
    - يعدل active_viewers تحت قفل viewers_lock.
    - يرسل كل إطار جديد مرة واحدة فقط مع ترويسة Content-Length.
    - لا ينتظر sleep ثابت بعد الإرسال، بل ينتظر إشعار الإطار الجديد عبر display_cond.
    - يتوقف فوراً عند إغلاق المتصفح ويستخدم صورة STANDBY المخبأة مسبقاً.
    '''
    cid = int(cam_id)
    last_seq = -1
    worker = engine.workers.get(cid)
    if worker:
        worker.increment_viewers()
    try:
        while True:
            worker = engine.workers.get(cid)
            if worker and worker.connected:
                with worker.display_cond:
                    if worker.display_seq == last_seq:
                        worker.display_cond.wait(timeout=0.4)
                    cur_seq = worker.display_seq
                    jpg = worker.get_display_jpeg_for_width(max_width)
                if jpg and cur_seq != last_seq:
                    last_seq = cur_seq
                    yield format_mjpeg_part(jpg)
            else:
                standby_jpg = get_cached_standby_jpeg(cid, max_width=max_width)
                if standby_jpg:
                    yield format_mjpeg_part(standby_jpg)
                if worker and worker._stop_event:
                    time.sleep(0.45)  # v16 #R14: لو الكاميرا موقوفة كان wait يرجع فوراً ويصير تكرار سريع
                else:
                    break
    except GeneratorExit:
        pass
    finally:
        w = engine.workers.get(cid)
        if w:
            w.decrement_viewers()


@app.route("/video_feed/<int:cam_id>")
def video_feed_cam(cam_id):
    req_w = None
    if request is not None and hasattr(request, "args"):
        w_arg = request.args.get("w", "")
        if str(w_arg).isdigit():
            req_w = int(w_arg)
    return Response(mjpeg_cam_generator(cam_id, max_width=req_w), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/video_feed")
def video_feed_default():
    req_w = None
    if request is not None and hasattr(request, "args"):
        w_arg = request.args.get("w", "")
        if str(w_arg).isdigit():
            req_w = int(w_arg)
    return Response(mjpeg_cam_generator(1, max_width=req_w), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        new_data = request.json or {}
        with engine.lock:
            merge_client_config_preserving_secrets(engine.cfg, new_data)
            save_config(engine.cfg)
        engine.sync_workers(auto_start=False)
        return jsonify({"ok": True, "config": sanitize_config_for_client(engine.cfg)})
    return jsonify(sanitize_config_for_client(engine.cfg))


# v15 #10: مسارات إدارة التخزين (/api/storage/stats و POST /api/storage/clear)
@app.route("/api/storage/stats")
def api_storage_stats():
    return jsonify(engine.get_storage_stats())


@app.route("/api/storage/clear", methods=["POST"])
def api_storage_clear():
    data = request.json or {}
    mode = str(data.get("mode") or "events_only").strip()
    days = int(data.get("days") or 7)
    preview = bool(data.get("preview", False))
    if preview:
        return jsonify(engine.clear_storage_by_mode(mode=mode, days=days, preview=True))
    confirm_text = str(data.get("confirm_text") or "").strip()
    if confirm_text != "حذف":
        return jsonify({"ok": False, "error": "يجب كتابة كلمة «حذف» لتأكيد عملية المسح."}), 400
    return jsonify(engine.clear_storage_by_mode(mode=mode, days=days, preview=False))


# v15 #8: مسار اختبار أي مزود إشعارات (/api/notifiers/test)
@app.route("/api/notifiers/test", methods=["POST"])
def api_notifiers_test():
    data = request.json or {}
    prov = str(data.get("provider") or "telegram").strip().lower()
    tmp_img_path = SNAP_DIR / "notifier_test_probe.jpg"
    test_img = np.zeros((120, 320, 3), dtype=np.uint8)
    cv2.putText(test_img, f"TEST {prov.upper()} OK", (22, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (16, 185, 129), 2)
    ok_enc, buf = cv2.imencode(".jpg", test_img)
    if ok_enc:
        tmp_img_path.write_bytes(buf.tobytes())

    caption = f"✅ إشعار تجريبي من مركز القيادة الأمني الذكي (v15) عبر مزود {prov}."
    notifiers_cfg = engine.cfg.get("notifiers") if isinstance(engine.cfg.get("notifiers"), dict) else {}
    p_cfg = notifiers_cfg.get(prov) or {}

    if prov == "telegram":
        res = engine._send_telegram_alert(tmp_img_path, caption, max_attempts=2)
    elif prov == "ntfy":
        res = engine.notifier.send_ntfy(p_cfg, tmp_img_path, caption, max_attempts=2)
    elif prov == "smtp":
        res = engine.notifier.send_smtp_email(p_cfg, tmp_img_path, caption, max_attempts=2)
    elif prov == "discord":
        res = engine.notifier.send_discord_webhook(p_cfg, tmp_img_path, caption, max_attempts=2)
    elif prov == "whatsapp":
        res = engine.notifier.send_whatsapp(p_cfg, tmp_img_path, caption, max_attempts=2)
    else:
        return jsonify({"ok": False, "error": f"مزود غير معروف: {prov}"})
    return jsonify(res)


@app.route("/api/cam_action", methods=["POST"])
def api_cam_action():
    data = request.json or {}
    act = data.get("action")
    cid = data.get("cam_id", 1)
    if act == "start":
        engine.start_camera(cid)
    elif act == "stop":
        engine.stop_camera(cid)
    elif act == "start_all":
        engine.start_all_enabled()
    elif act == "stop_all":
        engine.stop_all()
    return jsonify({"ok": True})


@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    data = request.json or {}
    with engine.lock:
        if "telegram_bot_token" in data and str(data["telegram_bot_token"]).strip() != SECRET_MASK:
            engine.cfg["telegram_bot_token"] = str(data["telegram_bot_token"]).strip()
            engine.cfg.setdefault("notifiers", {}).setdefault("telegram", {})["bot_token"] = engine.cfg["telegram_bot_token"]
        if "telegram_chat_id" in data:
            engine.cfg["telegram_chat_id"] = str(data["telegram_chat_id"]).strip()
            engine.cfg.setdefault("notifiers", {}).setdefault("telegram", {})["chat_id"] = engine.cfg["telegram_chat_id"]
        save_config(engine.cfg)

    token = engine.cfg.get("telegram_bot_token", "").strip()
    chat_id = engine.cfg.get("telegram_chat_id", "").strip()
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "يرجى إدخال Bot Token و Chat ID أولاً."})

    tmp_img_path = SNAP_DIR / "tg_test_probe.jpg"
    test_img = np.zeros((120, 320, 3), dtype=np.uint8)
    cv2.putText(test_img, "TELEGRAM ALERT OK", (24, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (16, 185, 129), 2)
    ok_enc, buf = cv2.imencode(".jpg", test_img)
    if ok_enc:
        tmp_img_path.write_bytes(buf.tobytes())

    res = engine._send_telegram_alert(
        tmp_img_path,
        "✅ إشعار تجريبي من مركز المراقبة: تم التحقق من قبول تيليجرام لصور الأحداث.",
        max_attempts=2
    )
    if res.get("ok"):
        return jsonify({
            "ok": True,
            "telegram_accept_ms": res.get("accept_ms"),
            "message": f"✓ قُبل الطلب من تيليجرام بنجاح (زمن قبول تيليجرام للطلب: {res.get('accept_ms')} ms)"
        })
    return jsonify({
        "ok": False,
        "error": redact_secrets(res.get("message", "فشل إرسال تيليجرام"), engine.cfg)
    })


@app.route("/api/ai/models", methods=["POST"])
def api_ai_models():
    '''# v16 #R13: يجلب قائمة الموديلات المتاحة فعلاً لمفتاحك من المزود نفسه'''
    data = request.json or {}
    prov = str(data.get("provider") or engine.cfg.get("ai_provider") or "").strip()

    def pick(field):
        v = str(data.get(field) or "").strip()
        return str(engine.cfg.get(field, "") or "").strip() if (not v or v == SECRET_MASK) else v

    try:
        if prov == "gemini":
            key = pick("gemini_api_key")
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": key}, timeout=15)
            skip = ("tts", "image", "embedding", "transcribe", "robotics", "computer-use", "aqa", "imagen", "veo", "banana")
            names = []
            if r.status_code == 200:
                for mdl in r.json().get("models", []):
                    nm = str(mdl.get("name", "")).replace("models/", "")
                    if "generateContent" in (mdl.get("supportedGenerationMethods") or []) and nm.startswith("gemini") and not any(s in nm for s in skip):
                        names.append(nm)
        elif prov == "ollama":
            base = pick("ollama_url").rstrip("/") or "http://127.0.0.1:11434"
            r = requests.get(f"{base}/api/tags", timeout=8)
            names = [m.get("name") for m in (r.json().get("models") or [])] if r.status_code == 200 else []
        else:
            base = pick("universal_base_url").rstrip("/")
            if base.endswith("/chat/completions"):
                base = base[:-len("/chat/completions")]
            key = pick("universal_api_key")
            r = requests.get(f"{base}/models", headers=({"Authorization": f"Bearer {key}"} if key else {}), timeout=15)
            names = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict) and m.get("id")] if r.status_code == 200 else []
        if r.status_code != 200:
            return jsonify({"ok": False, "error": redact_secrets(f"HTTP {r.status_code}: {r.text[:200]}", engine.cfg)})
        return jsonify({"ok": True, "provider": prov, "models": sorted({n for n in names if n})[:500]})
    except Exception as ex:
        return jsonify({"ok": False, "error": redact_secrets(ex, engine.cfg)})


@app.route("/api/test_ai", methods=["POST"])
def api_test_ai():
    t0 = time.time()
    test_img = np.zeros((120, 240, 3), dtype=np.uint8)
    cv2.rectangle(test_img, (20, 20), (220, 100), (0, 212, 255), -1)
    cv2.putText(test_img, "VISION OK", (42, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 0, 0), 2)
    res = engine.call_vision_ai_detailed(test_img, "أنت في اختبار رؤية بصرية. اقرأ الكلمة المكتوبة داخل المستطيل الأزرق وقل: تم التحقق من الرؤية البصرية بنجاح!")
    engine.last_ai_latency_ms = int((time.time() - t0) * 1000)
    ok = bool(res.get("ok"))
    return jsonify({
        "ok": ok,
        "ai_state": engine.get_effective_ai_state(),
        "model_used": res.get("model_used"),
        "reply": redact_secrets(res.get("reply", ""), engine.cfg)
    })


@app.route("/api/analyze_now", methods=["POST"])
def api_analyze_now():
    data = request.json or {}
    cid = int(data.get("cam_id", 1))
    wait_flag = bool(data.get("wait", False))
    ev = engine.analyze_and_record_15s_event(cid, trigger_reason="تحليل يدوي فوري", wait_for_ai=wait_flag)
    return jsonify({"ok": ev is not None, "event": ev})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.json or {}
    q = data.get("question", "")
    cid = int(data.get("cam_id", 1))
    ans = engine.answer_user_chat(q, cam_id=cid)
    return jsonify({"answer": redact_secrets(ans, engine.cfg)})


@app.route("/api/clear_events", methods=["POST"])
def api_clear_events():
    with engine.events_lock:
        engine.events = []
        save_events(engine.events)
        try:
            if EVENTS_ARCHIVE.exists():
                EVENTS_ARCHIVE.unlink()  # v17 #R21: مسح السجل يشمل الأرشيف (الملفات على القرص تبقى حسب سياسة التخزين)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    cams_info = {}
    for cid, w in list(engine.workers.items()):
        with w.ai_lock:
            q_len = len(w.pending_ai_queue)
            is_an = w.analyzing
        cams_info[cid] = {
            "running": w.running,
            "connected": w.connected,
            "analyzing": is_an,
            "pending_ai_count": q_len,
            "status_msg": w.status_msg,
            "stream_fps": w.stream_fps,
            "display_fps": w.display_fps,
            "detect_fps": w.detect_fps,
            "encode_ms": w.encode_ms,
            "active_viewers": w.get_active_viewers(),
            "mjpeg_kbps": w.mjpeg_kbps,
            "resolution": w.resolution_str,
            "motion_level": w.motion_level,
            "clip_source": (w.recorder.label() if (w.recorder is not None and w.recorder.healthy) else w.clip_src_label),  # v16 #R16/#R18
            "clip_fps": (w.recorder.fps if (w.recorder is not None and w.recorder.healthy) else w.clip_src_fps),
            "recorder_status": (w.recorder.status_msg if w.recorder is not None else ""),
            "analysis_stream": w.analysis_stream_effective(),  # v17 #R21
            "via_relay": bool(getattr(w, "source_via_relay", False)),  # v17 #R23
            "analysis_note": getattr(w, "analysis_note", ""),  # v17 #R24
            "analysis_override": w.analysis_override or ""
        }
    with engine.events_lock:
        clean_events = [
            {k: v for k, v in ev.items() if not str(k).startswith("_")}
            for ev in engine.events[:60]
        ]
    return jsonify({
        "version": "18.1",
        "preview": engine.preview,
        "cameras": cams_info,
        "active_model": engine.get_active_model_label(),
        "ai_state": engine.get_effective_ai_state(),
        "ai_health": engine.get_ai_health(),
        "telegram_health": engine.get_telegram_health(),
        "ai_last_error": engine.ai_last_error,
        "ai_last_model_used": getattr(engine, "ai_last_model_used", ""),  # v16 #R13
        "ai_fallback_note": getattr(engine, "ai_fallback_note", ""),
        "ai_latency_ms": engine.last_ai_latency_ms,
        "alert_delay_ms": engine.last_alert_delay_ms,
        "telegram_accept_ms": engine.last_telegram_ms,
        "telegram_ms": engine.last_telegram_ms,
        "detector_backend": engine.local_detector.backend,
        "detector_status": engine.local_detector.status_label,
        "low_accuracy_warning": engine.local_detector.low_accuracy_warning,
        "isapi_alert_status": engine.isapi_listener.status_msg,
        "go2rtc": engine.go2rtc.status(),  # v16 #R16
        "load": engine.load_guard.status(),  # v16 #R18
        "detect_hub": engine.detect_hub.status(),
        "ffmpeg_available": FfmpegSegmentRecorder.available(),
        "events": clean_events
    })


@app.before_request
def protect_preview():
    if engine is not None and engine.preview and request.method not in ("GET", "HEAD", "OPTIONS"):
        return jsonify(ok=False, error="وضع المعاينة لا يتصل بالكاميرات ولا يرسل إشعارات"), 403

@app.route("/api/diagnostics")
def api_diagnostics():
    from . import plates
    cameras = []
    for cid, w in list(engine.workers.items()):
        age = time.time() - getattr(w, "raw_frame_ts", 0)
        rec = w.recorder
        cameras.append(dict(id=cid, analysis_connected=bool(w.connected and age < 5),
            last_frame_age_sec=round(age, 1) if age < 86400 else None,
            analysis_stream=w.analysis_stream_effective(), analysis_note=w.analysis_note,
            recording_healthy=bool(rec and rec.healthy), recording_status=rec.status_msg if rec else "غير متاح",
            pending_ai=len(w.pending_ai_queue)))
    return jsonify(
        version="18.1",
        preview=engine.preview,
        cameras=cameras,
        plate_reader=plates._PLATE_READER_STATE if plates.HAS_FAST_ALPR else "not_installed",
        ai_state=engine.get_effective_ai_state(),
        ai_health=engine.get_ai_health(),
        telegram_health=engine.get_telegram_health(),
        system_libraries=engine.get_system_libraries_status(),
        system_tools=engine.get_system_tools_status(),
        ffmpeg_available=FfmpegSegmentRecorder.available(),
        notification_note="accepted يعني قبول المزود للطلب، وليس تأكيد وصوله للهاتف"
    )
