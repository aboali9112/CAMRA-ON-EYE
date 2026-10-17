# -*- coding: utf-8 -*-
"""cctv.plates — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import threading
import cv2


# ====================================================================================
# v16 #R12: قراءة اللوحات بـ fast-alpr (كاشف لوحات YOLOv9 + قارئ أحرف) بدل التخمين بالتباين
# ====================================================================================
import importlib.util as _imp_util
import statistics as _stats

HAS_FAST_ALPR = _imp_util.find_spec("fast_alpr") is not None
_PLATE_READER = None
_PLATE_READER_STATE = "idle"          # idle | loading | ready | failed
_PLATE_READER_ERROR = ""
_PLATE_READER_LOCK = threading.Lock()
_PLATE_PREDICT_LOCK = threading.Lock()


def _load_plate_reader():
    global _PLATE_READER, _PLATE_READER_STATE, _PLATE_READER_ERROR
    try:
        from fast_alpr import ALPR
        reader = ALPR(
            detector_model="yolo-v9-t-384-license-plate-end2end",
            detector_conf_thresh=0.35,
            ocr_model="cct-xs-v2-global-model",
            ocr_device="cpu",
        )
        _PLATE_READER = reader
        _PLATE_READER_STATE = "ready"
    except Exception as ex:
        _PLATE_READER_STATE = "failed"
        _PLATE_READER_ERROR = str(ex)[:200]


def get_plate_reader(wait=False):
    '''يرجّع قارئ اللوحات لو جاهز. التحميل الأول يصير بالخلفية عشان ما يعطّل الكشف'''
    global _PLATE_READER_STATE
    if not HAS_FAST_ALPR:
        return None
    if _PLATE_READER_STATE == "ready":
        return _PLATE_READER
    if _PLATE_READER_STATE == "failed":
        return None
    with _PLATE_READER_LOCK:
        if _PLATE_READER_STATE == "idle":
            _PLATE_READER_STATE = "loading"
            if wait:
                _load_plate_reader()
            else:
                threading.Thread(target=_load_plate_reader, daemon=True).start()
    return _PLATE_READER if _PLATE_READER_STATE == "ready" else None


def enhance_plate_crop(crop_bgr, target_w=480):
    '''تكبير وتحسين قصّة اللوحة للعرض فقط'''
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    ph, pw = crop_bgr.shape[:2]
    scale = target_w / float(max(1, pw))
    zoomed = cv2.resize(crop_bgr, (target_w, max(60, int(ph * scale))), interpolation=cv2.INTER_LANCZOS4)
    lab = cv2.cvtColor(zoomed, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(l_ch)
    return cv2.cvtColor(cv2.merge((l_ch, a_ch, b_ch)), cv2.COLOR_LAB2BGR)


def read_best_plate(frame_bgr, vehicle_box=None, min_ocr_conf=0.55):
    '''
    يدوّر على لوحة داخل منطقة المركبة (أو الإطار كامل) ويقرأ نصها.
    يرجّع {"ok", "text", "conf", "det_conf", "box", "crop"}؛ ok=False لو ما فيه لوحة مقروءة بثقة كافية.
    '''
    out = {"ok": False, "text": "", "conf": 0.0, "det_conf": 0.0, "box": None, "crop": None}
    reader = get_plate_reader()
    if reader is None or frame_bgr is None or frame_bgr.size == 0:
        return out
    h, w = frame_bgr.shape[:2]
    region, ox, oy = frame_bgr, 0, 0
    if vehicle_box and len(vehicle_box) == 4:
        bx, by, bw, bh = [int(v) for v in vehicle_box]
        px, py = int(bw * 0.30), int(bh * 0.30)
        x1, y1 = max(0, bx - px), max(0, by - py)
        x2, y2 = min(w, bx + bw + px), min(h, by + bh + py)
        if (x2 - x1) > 48 and (y2 - y1) > 48:
            region, ox, oy = frame_bgr[y1:y2, x1:x2], x1, y1
    try:  # v16 #R14: خطأ داخل المكتبة ما يطيّح شي
        with _PLATE_PREDICT_LOCK:
            results = reader.predict(region)
    except Exception as ex:
        out["error"] = str(ex)[:160]
        return out
    best = None
    for r in results:
        b = r.detection.bounding_box
        text = str(getattr(r.ocr, "text", "") or "").replace("_", "").strip() if r.ocr else ""
        conf = getattr(r.ocr, "confidence", 0.0) if r.ocr else 0.0
        if isinstance(conf, (list, tuple)):
            conf = _stats.mean(conf) if conf else 0.0
        conf = float(conf or 0.0)
        cand = {
            "text": text, "conf": round(conf, 3), "det_conf": round(float(r.detection.confidence), 3),
            "box": (int(b.x1 + ox), int(b.y1 + oy), int(b.x2 - b.x1), int(b.y2 - b.y1)),
            "crop": region[max(0, b.y1):max(0, b.y2), max(0, b.x1):max(0, b.x2)].copy()
        }
        if best is None or (len(cand["text"]) >= 3, cand["conf"]) > (len(best["text"]) >= 3, best["conf"]):
            best = cand
    if best is None:
        return out
    out.update({k: best[k] for k in ("text", "conf", "det_conf", "box", "crop")})
    out["ok"] = bool(len(best["text"]) >= 3 and best["conf"] >= float(min_ocr_conf) and best["crop"] is not None and best["crop"].size > 0)
    return out

