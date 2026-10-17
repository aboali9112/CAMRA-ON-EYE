# -*- coding: utf-8 -*-
"""cctv.tracker — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import math
import time
from collections import deque, Counter
from .geometry import perpendicular_signed_distance, point_in_polygon, segments_intersect


class CentroidLineTracker:
    '''
    # v15 #5: متتبع الأجسام مع حفظ class_name وconfidence، وفحص عبور الخط من نقطة القدم (منتصف أسفل المربع)
    مع اشتراط:
    1) تغير الإشارة من جهة لجهة مع هامش 1% من قطر الإطار لمنع اهتزاز المربع.
    2) وجود الجسم 3 إطارات على الأقل قبل لحظة العبور.
    3) مطابقة اتجاه العبور للاتجاه المختار (A_to_B أو B_to_A أو both).
    4) حساب بقاء نقطة القدم داخل مضلّع المنطقة لمدة zone_dwell_sec.
    '''
    def __init__(self, max_disappeared=10, max_distance_ratio=0.18):
        self.next_id = 1
        self.tracks = {}
        self.max_disappeared = max_disappeared
        self.max_distance_ratio = max_distance_ratio
        self.last_crossed_track_ids = []
        self.last_zone_dwell_track_ids = []

    @staticmethod
    def track_median_conf(tr):
        hist = sorted(tr.get("conf_hist") or [float(tr.get("confidence", 0.0))])
        n = len(hist)
        return hist[n // 2] if n % 2 else 0.5 * (hist[n // 2 - 1] + hist[n // 2])

    @classmethod
    def track_quality_ok(cls, tr, alert_threshold=0.0, min_track_frames=1):
        if int(tr.get("total_frames", 1)) < int(min_track_frames or 1):
            return False
        return cls.track_median_conf(tr) >= float(alert_threshold or 0.0)

    def update_and_check_crossing(
        self,
        boxes_or_dets,
        frame_w,
        frame_h,
        line_enabled,
        lx1,
        ly1,
        lx2,
        ly2,
        timestamp=None,
        line_direction="both",
        polygon_pts=None,
        zone_enabled=False,
        zone_dwell_sec=1.0,
        alert_threshold=0.0,
        min_track_frames=1
    ):
        '''
        v17 #R20: alert_threshold = وسيط ثقة الكاشف عبر آخر إطارات المسار (مو إطار واحد) اللازم لإطلاق حدث،
        وmin_track_frames = أقل عدد إطارات شوهد فيها الجسم. أوراق الشجر والظلال تطلع إطار أو اثنين بثقة واطية فتنفلتر.
        '''
        now = float(timestamp) if timestamp is not None else time.time()
        diag = float(math.hypot(frame_w, frame_h))
        max_dist = max(45.0, diag * self.max_distance_ratio)
        margin_px = max(1.5, 0.01 * diag)  # هامش 1% من القطر لمنع اهتزاز نقطة القدم

        detections = []
        for item in (boxes_or_dets or []):
            if isinstance(item, dict):
                bx, by, bw, bh = item["bbox"]
                cls_name = item.get("class_name", "other")
                subtype = item.get("subtype", "")
                conf = float(item.get("confidence", 0.5))
            else:
                bx, by, bw, bh = item
                cls_name = "other"
                subtype = ""
                conf = 0.6
            foot_x = float(bx) + float(bw) * 0.5
            foot_y = float(by) + float(bh)  # نقطة القدم: منتصف أسفل المربع تماماً
            cy = float(by) + float(bh) * 0.5
            detections.append((foot_x, cy, foot_y, (bx, by, bw, bh), cls_name, subtype, conf))

        used_det = set()
        for tid, tr in list(self.tracks.items()):
            last_fx, last_cy, last_fy, _ = tr["pts"][-1]
            best_idx = -1
            best_d = max_dist
            for i, (fx, cy, fy, box, cls_name, subtype, conf) in enumerate(detections):
                if i in used_det:
                    continue
                d = math.hypot(fx - last_fx, fy - last_fy)
                if d < best_d:
                    best_d = d
                    best_idx = i
            if best_idx >= 0:
                used_det.add(best_idx)
                fx, cy, fy, box, cls_name, subtype, conf = detections[best_idx]
                tr["pts"].append((fx, cy, fy, now))
                tr["box"] = box
                tr["missed"] = 0
                tr["total_frames"] = tr.get("total_frames", 1) + 1
                tr["class_name"] = cls_name
                tr["subtype"] = subtype
                tr["confidence"] = max(float(tr.get("confidence", 0.0)), conf)
                tr.setdefault("conf_hist", deque(maxlen=12)).append(float(conf))
            else:
                tr["missed"] += 1
                if tr["missed"] > self.max_disappeared:
                    del self.tracks[tid]

        for i, (fx, cy, fy, box, cls_name, subtype, conf) in enumerate(detections):
            if i not in used_det:
                init_side = 0
                if line_enabled:
                    d_init = perpendicular_signed_distance(fx, fy, lx1, ly1, lx2, ly2)
                    if abs(d_init) >= margin_px:
                        init_side = 1 if d_init > 0 else -1
                self.tracks[self.next_id] = {
                    "pts": deque([(fx, cy, fy, now)], maxlen=30),
                    "first_pt": (fx, fy),
                    "box": box,
                    "missed": 0,
                    "total_frames": 1,
                    "crossed_ts": 0.0,
                    "zone_frames": 0,
                    "zone_enter_ts": None,
                    "zone_triggered_ts": 0.0,
                    "class_name": cls_name,
                    "subtype": subtype,
                    "confidence": conf,
                    "conf_hist": deque([float(conf)], maxlen=12),
                    "initial_side": init_side,
                    "frames_on_initial_side": 1 if init_side != 0 else 0
                }
                self.next_id += 1

        crossed_boxes = []
        crossing_direction = ""
        crossing_dir_code = ""
        crossed_track_ids = []
        zone_dwell_track_ids = []

        L1 = (float(lx1), float(ly1))
        L2 = (float(lx2), float(ly2))

        for tid, tr in self.tracks.items():
            if tr.get("missed", 0) > 0:
                continue
            if not self.track_quality_ok(tr, alert_threshold, min_track_frames):
                continue  # v17 #R20: مسار ضعيف (ثقة واطية أو جديد) ما يطلق أحداث بعد
            cur_fx, cur_cy, cur_fy, cur_ts = tr["pts"][-1]

            # 1. فحص بقاء نقطة القدم داخل المضلّع لمدة zone_dwell_sec
            if zone_enabled and polygon_pts and len(polygon_pts) >= 3:
                if point_in_polygon(cur_fx, cur_fy, polygon_pts):
                    tr["zone_frames"] = tr.get("zone_frames", 0) + 1
                    if tr.get("zone_enter_ts") is None:
                        tr["zone_enter_ts"] = now
                    dwell_elapsed = now - float(tr["zone_enter_ts"])
                    tr["zone_exit_ts"] = None
                    # v16 #1: حدث المنطقة يطلق مرة واحدة لكل دخول (zone_fired) ولا يتكرر لجسم واقف داخل المنطقة
                    if dwell_elapsed >= float(zone_dwell_sec) and tr["zone_frames"] >= 2 and not tr.get("zone_fired"):
                        zone_dwell_track_ids.append(tid)
                else:
                    tr["zone_enter_ts"] = None
                    tr["zone_frames"] = 0
                    # v16 #1: يرجع يسمح بحدث جديد بس لما يطلع الجسم من المنطقة 1.5 ثانية متواصلة (يمنع اهتزاز الحدود)
                    if tr.get("zone_fired"):
                        if tr.get("zone_exit_ts") is None:
                            tr["zone_exit_ts"] = now
                        elif (now - float(tr["zone_exit_ts"])) >= 1.5:
                            tr["zone_fired"] = False
                            tr["zone_exit_ts"] = None

            # 2. فحص عبور الخط من نقطة القدم (cur_fx, cur_fy)
            if line_enabled:
                d_curr = perpendicular_signed_distance(cur_fx, cur_fy, lx1, ly1, lx2, ly2)
                cur_side = 0
                if abs(d_curr) >= margin_px:
                    cur_side = 1 if d_curr > 0 else -1

                init_side = tr.get("initial_side", 0)
                if init_side == 0 and cur_side != 0:
                    tr["initial_side"] = cur_side
                    tr["frames_on_initial_side"] = 1
                    continue

                if cur_side == init_side and cur_side != 0:
                    tr["frames_on_initial_side"] = tr.get("frames_on_initial_side", 0) + 1
                    continue

                # يشترط وجود الجسم 3 إطارات على الأقل قبل العبور، وتغير الإشارة خارج هامش 1% من القطر
                if init_side != 0 and cur_side != 0 and (cur_side * init_side) < 0:
                    if tr.get("frames_on_initial_side", 0) >= 3 and len(tr["pts"]) >= 4:
                        if (now - tr.get("crossed_ts", -1e9)) >= 3.0:
                            p_first_foot = (tr["pts"][0][0], tr["pts"][0][2])
                            p_prev_foot = (tr["pts"][-2][0], tr["pts"][-2][2])
                            p_curr_foot = (cur_fx, cur_fy)
                            seg_hit = (
                                segments_intersect(p_prev_foot, p_curr_foot, L1, L2)
                                or segments_intersect(p_first_foot, p_curr_foot, L1, L2)
                            )
                            if seg_hit:
                                dir_code = "A_to_B" if (init_side > 0 and cur_side < 0) else "B_to_A"
                                dir_pref = str(line_direction or "both").strip()
                                if dir_pref in ("both", "الاثنين", "") or dir_pref == dir_code:
                                    tr["crossed_ts"] = now
                                    tr["initial_side"] = cur_side
                                    tr["frames_on_initial_side"] = 1
                                    crossed_boxes.append(tr["box"])
                                    crossed_track_ids.append(tid)
                                    crossing_dir_code = dir_code
                                    crossing_direction = "من الجهة A إلى B" if dir_code == "A_to_B" else "من الجهة B إلى A"
                                else:
                                    # عبر بالاتجاه المعاكس للمختار: نحدث جهته دون إطلاق حدث
                                    tr["initial_side"] = cur_side
                                    tr["frames_on_initial_side"] = 1

        self.last_crossed_track_ids = crossed_track_ids
        self.last_zone_dwell_track_ids = zone_dwell_track_ids
        self.last_crossing_dir_code = crossing_dir_code
        return crossed_boxes, crossing_direction

