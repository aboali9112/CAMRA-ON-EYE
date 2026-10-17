# -*- coding: utf-8 -*-
"""cctv.detector — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import sys
import os
import time
import threading
import cv2
import numpy as np
from .paths import BASE_DIR, DATA_DIR


# v15 #5: استيراد مكتبة ultralytics YOLO بشكل اختياري داخل try
# تنبيه هام: مكتبة ultralytics مرخصة تحت رخصة AGPL-3.0 (راجع شروط الترخيص عند الاستخدام التجاري)
try:
    from ultralytics import YOLO as _UltralyticsYOLO
    HAS_ULTRALYTICS = True
except Exception as _ul_err:
    _UltralyticsYOLO = None
    HAS_ULTRALYTICS = False
    ULTRALYTICS_IMPORT_MSG = f"مكتبة ultralytics غير مثبتة ({_ul_err}) — سيتم استخدام OpenCV DNN أو كشف الحركة البديل."


# v15 #5: كاشف أجسام محلي (ultralytics YOLO أو OpenCV DNN ONNX أو التراجع لكشف الحركة مع تحذير دقة منخفضة)
# تنبيه الترخيص: مكتبة ultralytics مرخصة تحت رخصة AGPL-3.0. لاستخدام رخصة متساهلة يمكن تشغيل نموذج ONNX عبر OpenCV DNN.
COCO_ID_TO_CATEGORY = {
    0: ("person", "person"),
    1: ("vehicle", "bicycle"),
    2: ("vehicle", "car"),
    3: ("vehicle", "motorcycle"),
    5: ("vehicle", "bus"),
    7: ("vehicle", "truck"),
    14: ("animal", "bird"),
    15: ("animal", "cat"),
    16: ("animal", "dog"),
    17: ("animal", "horse"),
    18: ("animal", "sheep"),
    19: ("animal", "cow"),
}


class LocalObjectDetector:
    def __init__(self, device_pref="auto"):
        self.device_pref = str(device_pref or "auto").lower()
        self.yolo_device = "cpu"
        self.backend = "motion_fallback"
        self.low_accuracy_warning = True
        self.status_label = "كشف حركة بديل (دقة منخفضة — ثبّت ultralytics أو نموذج ONNX لدقة أعلى)"
        self.yolo_model = None
        self.onnx_net = None
        self.lock = threading.Lock()
        self._init_detector()

    def _init_detector(self):
        if "--selftest" in sys.argv or os.environ.get("CCTV_PREVIEW") == "1":
            return
        # 1. محاولة تحميل ultralytics YOLO (yolo11n.pt أو yolov8n.pt)
        if HAS_ULTRALYTICS and _UltralyticsYOLO is not None:
            for model_candidate in ( BASE_DIR / "yolo11n.pt", BASE_DIR / "yolov8n.pt", DATA_DIR / "yolo11n.pt", DATA_DIR / "yolov8n.pt" ):
                if model_candidate.exists():
                    try:
                        self.yolo_model = _UltralyticsYOLO(str(model_candidate))
                        self.backend = f"ultralytics:{model_candidate.name}"
                        self.low_accuracy_warning = False
                        dev_txt = self._pick_yolo_device()
                        self.status_label = f"كاشف YOLO محلي نشط ({model_candidate.name} • {dev_txt})"
                        return
                    except Exception:
                        pass
        # 2. محاولة تحميل OpenCV DNN ONNX في حال عدم توفر ultralytics
        for onnx_candidate in ( BASE_DIR / "yolov8n.onnx", BASE_DIR / "yolo11n.onnx", DATA_DIR / "yolov8n.onnx", DATA_DIR / "yolo11n.onnx" ):
            if onnx_candidate.exists():
                try:
                    self.onnx_net = cv2.dnn.readNetFromONNX(str(onnx_candidate))
                    # v16 #R1: نتأكد إن النموذج يشتغل فعلاً قبل ما نعلن إنه نشط
                    self._detect_onnx(np.zeros((360, 640, 3), dtype=np.uint8), 0.5)
                    self.backend = f"opencv_dnn:{onnx_candidate.name}"
                    self.low_accuracy_warning = False
                    self.status_label = f"كاشف OpenCV DNN ONNX نشط ({onnx_candidate.name})"
                    return
                except Exception:
                    pass

    def _pick_yolo_device(self):
        '''# v16 #R16: يشغّل YOLO على كرت الشاشة (CUDA) لو متوفر — أسرع ~15 مرة من المعالج'''
        if self.device_pref in ("auto", "cuda", "gpu"):
            try:
                import torch
                if torch.cuda.is_available():
                    self.yolo_model.predict(np.zeros((360, 640, 3), dtype=np.uint8), imgsz=640, device=0, verbose=False)
                    self.yolo_device = 0
                    self.backend += ":cuda"
                    return f"GPU {torch.cuda.get_device_name(0)}"
            except Exception:
                self.yolo_device = "cpu"
        return "CPU"

    def _detect_onnx(self, img, min_conf=0.35):
        '''# v16 #R1: استدلال YOLOv8/YOLO11 بصيغة ONNX عبر OpenCV DNN (letterbox 640 + NMS)'''
        h, w = img.shape[:2]
        size = 640
        scale = min(size / float(w), size / float(h))
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        top, left = (size - nh) // 2, (size - nw) // 2
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0, (size, size), swapRB=True, crop=False)
        with self.lock:
            self.onnx_net.setInput(blob)
            out = self.onnx_net.forward()
        out = np.squeeze(np.asarray(out))
        if out.ndim != 2:
            raise ValueError("unexpected ONNX output shape")
        if out.shape[0] < out.shape[1]:
            out = out.T  # (N, 4 + classes)
        if out.shape[1] < 5:
            raise ValueError("unexpected ONNX output columns")
        conf_th = max(0.2, float(min_conf))
        cls_scores = out[:, 4:]
        cls_idx = np.argmax(cls_scores, axis=1)
        cls_conf = cls_scores[np.arange(len(cls_idx)), cls_idx]
        keep = cls_conf >= conf_th
        boxes, scores, cls_ids = [], [], []
        for (cx, cy, bw, bh), cid, sc in zip(out[keep, :4], cls_idx[keep], cls_conf[keep]):
            x = (float(cx) - float(bw) / 2.0 - left) / scale
            y = (float(cy) - float(bh) / 2.0 - top) / scale
            boxes.append([int(x), int(y), int(float(bw) / scale), int(float(bh) / scale)])
            scores.append(float(sc))
            cls_ids.append(int(cid))
        if not boxes:
            return []
        idxs = cv2.dnn.NMSBoxes(boxes, scores, conf_th, 0.45)
        dets = []
        for i in np.array(idxs).flatten().tolist():
            x, y, bw, bh = boxes[i]
            x, y = max(0, x), max(0, y)
            cat, sub = COCO_ID_TO_CATEGORY.get(cls_ids[i], ("other", "object"))
            dets.append({
                "bbox": (x, y, max(2, min(bw, w - x)), max(2, min(bh, h - y))),
                "class_name": cat,
                "subtype": sub,
                "confidence": round(scores[i], 3)
            })
        return dets

    @staticmethod
    def _parse_result(r):
        dets = []
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            return dets
        for b in boxes:
            cls_id = int(b.cls[0].item()) if hasattr(b.cls[0], "item") else int(b.cls[0])
            conf_v = float(b.conf[0].item()) if hasattr(b.conf[0], "item") else float(b.conf[0])
            x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]
            bw, bh = max(2, x2 - x1), max(2, y2 - y1)
            cat, sub = COCO_ID_TO_CATEGORY.get(cls_id, ("other", "object"))
            dets.append({
                "bbox": (max(0, x1), max(0, y1), bw, bh),
                "class_name": cat,
                "subtype": sub,
                "confidence": round(conf_v, 3)
            })
        return dets

    def detect_batch(self, frames, min_conf=0.35):
        '''# v16 #R18: كشف دفعة إطارات (من عدة كاميرات) بنداء واحد لكرت الشاشة'''
        if self.yolo_model is None:
            raise RuntimeError("no yolo model")
        with self.lock:
            results = self.yolo_model.predict(list(frames), imgsz=640, conf=max(0.2, float(min_conf)),
                                              device=self.yolo_device, verbose=False)
        return [self._parse_result(r) for r in results]

    def detect_on_640(self, frame_640, prev_gray=None, motion_sens=40, min_conf=0.35):
        '''
        يكتشف الأجسام على صورة مصغرة بعرض 640 ويعيد:
        (detections_list, updated_prev_gray, motion_level)
        حيث كل عنصر في detections_list هو:
        {"bbox": (x, y, w, h), "class_name": "person|vehicle|animal|other", "subtype": str, "confidence": float}
        '''
        h, w = frame_640.shape[:2]
        # v16 #R1: مسار ONNX عبر OpenCV DNN صار يُستخدم فعلاً في الكشف
        if self.yolo_model is None and self.onnx_net is not None:
            try:
                dets = self._detect_onnx(frame_640, min_conf)
                return dets, prev_gray, round(min(100.0, len(dets) * 18.0), 1)
            except Exception:
                pass
        if self.yolo_model is not None:
            try:
                with self.lock:
                    results = self.yolo_model.predict(frame_640, imgsz=640, conf=max(0.2, float(min_conf)),
                                                      device=self.yolo_device, verbose=False)
                dets = []
                for r in results:
                    dets.extend(self._parse_result(r))
                return dets, prev_gray, round(min(100.0, len(dets) * 18.0), 1)
            except Exception:
                pass

        # التراجع الآمن لكشف الحركة عند عدم توفر YOLO/ONNX
        gray = cv2.cvtColor(frame_640, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (15, 15), 0)
        if prev_gray is None or prev_gray.shape != gray.shape:
            return [], gray, 0.0

        delta = cv2.absdiff(prev_gray, gray)
        thresh = cv2.threshold(delta, 22, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        next_prev_gray = cv2.addWeighted(prev_gray, 0.82, gray, 0.18, 0)

        sens = int(motion_sens or 40)
        min_area = max(90, int((sens ** 1.6) * 1.1))
        tot_px = float(max(1, w * h))
        max_ratio = 0.0
        dets = []

        for c in contours:
            area = cv2.contourArea(c)
            if area > min_area:
                bx, by, bw, bh = cv2.boundingRect(c)
                ratio = (area / tot_px) * 100.0
                max_ratio = max(max_ratio, ratio)
                aspect = bw / float(max(1, bh))
                if aspect < 1.15 and ratio < 22.0:
                    cat, sub = "person", "pedestrian"
                elif aspect >= 1.32 or ratio >= 16.0:
                    cat, sub = "vehicle", "car"
                else:
                    cat, sub = "other", "motion"
                conf_est = min(0.85, max(0.46, 0.45 + (ratio / 35.0)))
                dets.append({
                    "bbox": (bx, by, bw, bh),
                    "class_name": cat,
                    "subtype": sub,
                    "confidence": round(conf_est, 2)
                })
        return dets, next_prev_gray, round(min(100.0, max_ratio * 8.0), 1)

class DetectionHub:
    '''
    # v16 #R18: الكشف الجماعي — إطارات كل الكاميرات تتجمع وتتحلل دفعة وحدة على كرت الشاشة.
    قياس على RTX 3070: إطار واحد = 18ms، دفعة 16 = 52ms (3.2ms للإطار) → 16 كاميرا × 10 إطارات/ث صارت ممكنة.
    كل كاميرا تنادي detect() وتنتظر نتيجتها؛ ولو الـhub مو متاح (CPU بس) ترجع للمسار العادي.
    '''
    MAX_BATCH = 16
    GATHER_SEC = 0.012

    def __init__(self, detector):
        self.det = detector
        self.cond = threading.Condition()
        self.pending = []
        self.enabled = bool(getattr(detector, "yolo_model", None) is not None and str(getattr(detector, "yolo_device", "cpu")) != "cpu")
        self.ms_per_frame = 18.0
        self.last_batch = 0
        self.batches = 0
        self.frames = 0
        if self.enabled:
            threading.Thread(target=self._loop, daemon=True, name="detect-hub").start()

    def detect(self, frame, min_conf, timeout=2.5):
        if not self.enabled:
            return None
        done = threading.Event()
        holder = [None]
        with self.cond:
            self.pending.append((frame, float(min_conf), holder, done))
            self.cond.notify()
        if not done.wait(timeout):
            return None
        return holder[0]

    def _loop(self):
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                deadline = time.time() + self.GATHER_SEC
                while len(self.pending) < self.MAX_BATCH:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    self.cond.wait(left)
                batch = self.pending[:self.MAX_BATCH]
                del self.pending[:self.MAX_BATCH]
            frames = [b[0] for b in batch]
            conf = min(b[1] for b in batch)
            t0 = time.time()
            try:
                results = self.det.detect_batch(frames, conf)
                if len(results) != len(batch):
                    results = [None] * len(batch)
            except Exception:
                results = [None] * len(batch)
            dt_ms = (time.time() - t0) * 1000.0
            self.ms_per_frame = 0.8 * self.ms_per_frame + 0.2 * (dt_ms / max(1, len(batch)))
            self.last_batch = len(batch)
            self.batches += 1
            self.frames += len(batch)
            for (frame, mc, holder, done), dets in zip(batch, results):
                holder[0] = None if dets is None else [d for d in dets if float(d.get("confidence", 0)) >= mc]
                done.set()

    def capacity_fps(self):
        return 0.75 * 1000.0 / max(1.0, self.ms_per_frame)

    def suggested_fps(self, n_cams, requested):
        '''يقسم طاقة كرت الشاشة على الكاميرات الشغّالة بدل ما تتزاحم'''
        if not self.enabled or n_cams <= 0:
            return requested
        return max(1.0, min(float(requested), self.capacity_fps() / float(n_cams)))

    def status(self):
        return {"enabled": self.enabled, "ms_per_frame": round(self.ms_per_frame, 1), "last_batch": self.last_batch,
                "capacity_fps": round(self.capacity_fps(), 1), "batches": self.batches}

