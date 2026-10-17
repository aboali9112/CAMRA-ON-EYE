# -*- coding: utf-8 -*-
"""cctv.ai — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import re
import json


# v14 fix #2: دالة parse_ai_category تعتمد على الوسم في بداية الرد فقط بعد lstrip
def parse_ai_category(reply):
    s = str(reply or "").lstrip()
    if s.startswith("[رصد شخص"):
        return "person"
    if s.startswith("[رصد مركبة") or s.startswith("[رصد لوحة"):
        return "vehicle"
    if s.startswith("[رصد حيوان"):
        return "animal"
    if s.startswith("[عادي]"):
        return "normal"
    return "unknown"

def evaluate_ai_reply_importance(reply, is_error=False):
    cat = parse_ai_category(reply)
    if is_error or cat in ("normal", "animal"):
        return cat, False
    s = str(reply or "").lstrip()
    important = (cat in ("person", "vehicle", "unknown")) or s.startswith("[مهم]") or s.startswith("[تنبيه أمني]")
    return cat, bool(important)


# v15 #6: فك رد JSON من الـ AI بشكل متسامح (استخراج أول {...}) والتراجع إلى parse_ai_category عند الفشل
def extract_first_json_object(raw_text):
    '''يستخرج أول كائن JSON متوازن {...} من النص حتى لو سبقه أو تلاه نص أو علامات Markdown'''
    s = str(raw_text or "")
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(s)):
        ch = s[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start:idx + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        pass
                    break
    return None


def parse_ai_json_response(reply_text, default_in_zone=False, default_crossed_line=False):
    '''
    # v15 #6: يفك رد JSON المهيكل من الـ AI:
    {"category":"person|vehicle|animal|other|none","subtype":"...","in_zone":true,"crossed_line":true,"count":1,"plate":"","confidence":0.0,"description_ar":"..."}
    ولو فشل الفك يرجع تلقائياً إلى parse_ai_category القديمة دون رمي استثناء.
    '''
    parsed = extract_first_json_object(reply_text)
    if isinstance(parsed, dict) and ("category" in parsed or "description_ar" in parsed):
        raw_cat = str(parsed.get("category") or "other").strip().lower()
        if raw_cat == "normal":
            raw_cat = "none"
        if raw_cat not in ("person", "vehicle", "animal", "other", "none"):
            raw_cat = "other"
        try:
            conf_val = float(parsed.get("confidence", 0.75))
        except Exception:
            conf_val = 0.75
        try:
            cnt_val = max(0, int(parsed.get("count", 1 if raw_cat != "none" else 0)))
        except Exception:
            cnt_val = 1 if raw_cat != "none" else 0
        return {
            "category": raw_cat,
            "subtype": str(parsed.get("subtype") or "").strip().lower(),
            "in_zone": bool(parsed.get("in_zone", default_in_zone)),
            "crossed_line": bool(parsed.get("crossed_line", default_crossed_line)),
            "count": cnt_val,
            "plate": str(parsed.get("plate") or "").strip(),
            "confidence": round(max(0.0, min(1.0, conf_val)), 3),
            "description_ar": str(parsed.get("description_ar") or parsed.get("description") or "").strip() or f"تم رصد {raw_cat}",
            "_parsed_from_json": True
        }

    # التراجع الآمن إلى parse_ai_category القديمة
    legacy_cat = parse_ai_category(reply_text)
    mapped_cat = "none" if legacy_cat == "normal" else ("other" if legacy_cat == "unknown" else legacy_cat)
    default_conf = 0.25 if mapped_cat == "none" else (0.82 if mapped_cat in ("person", "vehicle") else 0.65)
    plate_match = re.search(r"\[اللوحة:\s*([^\]]+)\]", str(reply_text or ""))
    plate_str = plate_match.group(1).strip() if plate_match else ""
    return {
        "category": mapped_cat,
        "subtype": "",
        "in_zone": bool(default_in_zone and mapped_cat != "none"),
        "crossed_line": bool(default_crossed_line and mapped_cat != "none"),
        "count": 0 if mapped_cat == "none" else 1,
        "plate": plate_str,
        "confidence": default_conf,
        "description_ar": str(reply_text or "").strip(),
        "_parsed_from_json": False
    }

