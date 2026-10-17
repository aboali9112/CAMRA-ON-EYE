# -*- coding: utf-8 -*-
"""cctv.geometry — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import math
import cv2
import numpy as np


# ====================================================================================
# 2. متتبع الأجسام الحقيقي وفاحص عبور الخط المتجهي (Centroid Tracker + Track-ID Deduplication)
# ====================================================================================
def ccw(A, B, C):
    return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])

def segments_intersect(A, B, C, D):
    return ccw(A, C, D) != ccw(B, C, D) and ccw(A, B, C) != ccw(A, B, D)

# v15 #5: دالة فحص وقوع نقطة داخل مضلّع (Ray-Casting Polygon Point Inclusion)
def point_in_polygon(px, py, polygon_pts):
    '''يفحص ما إذا كانت النقطة (px, py) داخل المضلّع polygon_pts [(x1,y1), (x2,y2), ...]'''
    if not polygon_pts or len(polygon_pts) < 3:
        return False
    n = len(polygon_pts)
    inside = False
    x, y = float(px), float(py)
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon_pts[i][0]), float(polygon_pts[i][1])
        xj, yj = float(polygon_pts[j][0]), float(polygon_pts[j][1])
        intersect = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) if abs(yj - yi) > 1e-12 else 1e-12) + xi)
        if intersect:
            inside = not inside
        j = i
    return inside


def perpendicular_signed_distance(px, py, x1, y1, x2, y2):
    '''المسافة العمودية الموجهة للنقطة عن الخط الموجه (x1,y1)->(x2,y2)'''
    dx = float(x2 - x1)
    dy = float(y2 - y1)
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return 0.0
    cross = dx * (float(py) - float(y1)) - dy * (float(px) - float(x1))
    return cross / length


def check_zone_intrusion(bx, by, bw, bh, zx1, zy1, zx2, zy2, polygon_pts=None):
    foot_x = bx + bw * 0.5
    foot_y = by + bh
    if polygon_pts and len(polygon_pts) >= 3:
        return point_in_polygon(foot_x, foot_y, polygon_pts)
    rx1, rx2 = min(zx1, zx2), max(zx1, zx2)
    ry1, ry2 = min(zy1, zy2), max(zy1, zy2)
    return (rx1 <= foot_x <= rx2) and (ry1 <= foot_y <= ry2)


def classify_geometry_target(disp_boxes, disp_w, disp_h):
    if not disp_boxes:
        return "unknown"
    first = disp_boxes[0]
    if isinstance(first, dict):
        return f"{first.get('class_name', 'unknown')}_likely"
    largest = max(disp_boxes, key=lambda b: b[2] * b[3])
    bx, by, bw, bh = largest
    aspect = bw / float(max(1, bh))
    area_ratio = (bw * bh) / float(max(1, disp_w * disp_h))
    if aspect < 1.18 and area_ratio < 0.22:
        return "person_likely"
    if aspect >= 1.35 or area_ratio >= 0.18:
        return "vehicle_likely"
    return "unknown"

def extract_and_enhance_plate(raw_frame, disp_boxes, disp_w, disp_h):
    if raw_frame is None:
        return None
    if classify_geometry_target(disp_boxes, disp_w, disp_h) == "person_likely":
        return None

    rh, rw = raw_frame.shape[:2]
    scale_x = rw / float(max(1, disp_w))
    scale_y = rh / float(max(1, disp_h))

    if disp_boxes:
        largest = max(disp_boxes, key=lambda b: b[2] * b[3])
        bx, by, bw, bh = largest
        pad_x, pad_y = int(bw * 0.12), int(bh * 0.12)
        x1, y1 = max(0, int((bx - pad_x) * scale_x)), max(0, int((by - pad_y) * scale_y))
        x2, y2 = min(rw, int((bx + bw + pad_x) * scale_x)), min(rh, int((by + bh + pad_y) * scale_y))
        vehicle_roi = raw_frame[y1:y2, x1:x2]
    else:
        vehicle_roi = raw_frame[int(rh * 0.15):int(rh * 0.92), int(rw * 0.1):int(rw * 0.9)]

    if vehicle_roi.size == 0:
        return None

    vh, vw = vehicle_roi.shape[:2]
    plate_crop = None

    try:
        gray = cv2.cvtColor(vehicle_roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.bilateralFilter(gray, 9, 75, 75)
        grad_x = cv2.Sobel(gray, cv2.CV_8U, 1, 0, ksize=3)
        _, thresh = cv2.threshold(grad_x, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 5))
        morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(thresh if morph is None else morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_score = 0
        for c in contours:
            px, py, pw, ph = cv2.boundingRect(c)
            aspect = pw / float(max(1, ph))
            area = pw * ph
            if 1.8 <= aspect <= 5.8 and (vw * vh * 0.005) <= area <= (vw * vh * 0.28):
                roi_patch = gray[py:py+ph, px:px+pw]
                if roi_patch.size > 0 and float(np.std(roi_patch)) > 28.0:
                    pos_bonus = 1.3 if py > (vh * 0.25) else 0.9
                    score = area * pos_bonus * float(np.std(roi_patch))
                    if score > best_score:
                        best_score = score
                        mx, my = int(pw * 0.2), int(ph * 0.3)
                        plate_crop = vehicle_roi[max(0, py - my):min(vh, py + ph + my), max(0, px - mx):min(vw, px + pw + mx)]
    except Exception:
        pass

    if plate_crop is None or plate_crop.size == 0:
        return None

    ph, pw = plate_crop.shape[:2]
    target_w = 560
    scale_zoom = target_w / float(max(1, pw))
    target_h = max(140, min(360, int(ph * scale_zoom)))
    zoomed = cv2.resize(plate_crop, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

    lab = cv2.cvtColor(zoomed, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
    cl = clahe.apply(l_ch)
    enhanced = cv2.cvtColor(cv2.merge((cl, a_ch, b_ch)), cv2.COLOR_LAB2BGR)
    gaussian = cv2.GaussianBlur(enhanced, (0, 0), 2.0)
    return cv2.addWeighted(enhanced, 1.55, gaussian, -0.55, 0)


def build_ai_verification_crop(full_frame, target_box, pad_ratio=0.30):
    '''# v15 #6: يقصّ صورة مكبّرة للجسم المسبب للحدث (المربع + 30% هامش من جميع الجهات)'''
    if full_frame is None:
        return None
    fh, fw = full_frame.shape[:2]
    if not target_box or len(target_box) != 4:
        return full_frame.copy()
    bx, by, bw, bh = [int(v) for v in target_box]
    pad_x = int(bw * float(pad_ratio))
    pad_y = int(bh * float(pad_ratio))
    x1 = max(0, bx - pad_x)
    y1 = max(0, by - pad_y)
    x2 = min(fw, bx + bw + pad_x)
    y2 = min(fh, by + bh + pad_y)
    if x2 <= x1 + 8 or y2 <= y1 + 8:
        return full_frame.copy()
    return full_frame[y1:y2, x1:x2].copy()

