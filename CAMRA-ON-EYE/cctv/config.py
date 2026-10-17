# -*- coding: utf-8 -*-
"""cctv.config — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import os
import re
import json
import uuid
import threading
from datetime import datetime
from pathlib import Path
from .paths import CONFIG_FILE, DISK_IO_LOCK, EVENTS_FILE, MAX_STORED_EVENTS


def redact_secrets(text, cfg=None):
    """
# v14 fix #3: إخفاء الأسرار باستخدام أنماط regex صحيحة بشرطة مائلة واحدة داخل r"..."
    يخفي مفاتيح البوت ومفاتيح الـ API وكلمات مرور RTSP/ISAPI من أي رسائل خطأ أو سجلات.
    """
    if not text:
        return ""
    s = str(text)
    # إخفاء توكن بوت تيليجرام في الروابط أو النصوص
    s = re.sub(r"bot\d{6,15}:[A-Za-z0-9_-]{20,}", "bot***REDACTED***", s)
    s = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{25,}\b", "***REDACTED_BOT_TOKEN***", s)
    # إخفاء كلمات المرور في روابط RTSP أو HTTP
    s = re.sub(r"(rtsp://[^:\s/]+:)([^@\s]+)(@)", r"\1***\3", s, flags=re.IGNORECASE)
    s = re.sub(r"(https?://[^:\s/]+:)([^@\s]+)(@)", r"\1***\3", s, flags=re.IGNORECASE)
    # إخفاء مفاتيح API في العناوين أو الهيدرز
    s = re.sub(r"(key=)([A-Za-z0-9_-]{12,})", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    s = re.sub(r"(Bearer\s+)([A-Za-z0-9._-]{10,})", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    s = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-***REDACTED***", s)

    # v15 #8: إخفاء أسرار المزودات الجديدة (Discord Webhook، ntfy، WhatsApp، SMTP، وS3)
    s = re.sub(r"(https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/)([A-Za-z0-9_-]+)", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    s = re.sub(r"(apikey=)([A-Za-z0-9_-]{4,})", r"\1***REDACTED***", s, flags=re.IGNORECASE)
    s = re.sub(r"(tk_[A-Za-z0-9_-]{10,})", "tk_***REDACTED***", s)
    s = re.sub(r"(EAAG[A-Za-z0-9]{12,})", "EAAG***REDACTED***", s)
    s = re.sub(r"(AKIA[0-9A-Z]{16})", "AKIA***REDACTED***", s)

    if isinstance(cfg, dict):
        notifiers_cfg = cfg.get("notifiers") if isinstance(cfg.get("notifiers"), dict) else {}
        cloud_cfg = cfg.get("cloud_storage") if isinstance(cfg.get("cloud_storage"), dict) else {}
        s3_cfg = cloud_cfg.get("s3") if isinstance(cloud_cfg.get("s3"), dict) else {}
        secret_vals = [
            cfg.get("telegram_bot_token", ""),
            cfg.get("universal_api_key", ""),
            cfg.get("gemini_api_key", ""),
            cfg.get("password", ""),
            (cfg.get("hikconnect") or {}).get("app_secret", ""),
            (notifiers_cfg.get("telegram") or {}).get("bot_token", ""),
            (notifiers_cfg.get("ntfy") or {}).get("token", ""),
            (notifiers_cfg.get("smtp") or {}).get("password", ""),
            (notifiers_cfg.get("discord") or {}).get("webhook_url", ""),
            (notifiers_cfg.get("whatsapp") or {}).get("callmebot_apikey", ""),
            (notifiers_cfg.get("whatsapp") or {}).get("access_token", ""),
            s3_cfg.get("access_key", ""),
            s3_cfg.get("secret_key", ""),
        ]
        for cam in cfg.get("cameras", []):
            if isinstance(cam, dict) and cam.get("password"):
                secret_vals.append(cam["password"])
        for val in secret_vals:
            val_str = str(val or "").strip()
            if len(val_str) >= 4 and val_str != "••••" and val_str in s:
                s = s.replace(val_str, "***REDACTED***")
    return s

def atomic_write_json(target_path: Path, payload, indent=2):
    """
    كتابة ذرية وآمنة خيطياً عبر ملف مؤقت ثم استبدال ذري (os.replace) لمنع تلف ملفات JSON.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=indent)
    with DISK_IO_LOCK:
        tmp_path = target_path.with_name(f".{target_path.name}.tmp.{threading.get_ident()}.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, target_path)
        except OSError:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

DEFAULT_CONFIG = {
    "conn_type": "hikvision_nvr",
    "ip": "192.168.8.7",
    "port": "554",
    "http_port": "80",
    "username": "admin",
    "password": "",
    "channel": "1",
    "stream_type": "main",
    "power_mode": "balanced",
    "jpeg_quality": 85,
    "custom_rtsp": "",
    "cameras": [],
    "nvr_device_info": {},
    "hikconnect": {
        "api_url": "https://open.ezvizlife.com",
        "app_key": "",
        "app_secret": "",
        "connected": False,
        "last_sync": ""
    },
    "grid_layout": "1",
    "viewport_height": 580,
    "object_fit": "contain",
    "ai_provider": "openrouter",
    "universal_base_url": "https://openrouter.ai/api/v1",
    "universal_api_key": "",
    "universal_model": "qwen/qwen2.5-vl-72b-instruct:free",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.8-flash",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "llava",
    "motion_enabled": True,
    "draw_motion_boxes": True,
    "show_osd_fps": True,
    "motion_sensitivity": 40,
    "cooldown_seconds": 12,
    "custom_rules": "تجاهل حركة الأشجار والشارع الفارغ واكتب [عادي] إذا لم يوجد أحد، واكتب [مهم] أو [تنبيه أمني] فور عبور أي شخص أو سيارة أو حيوان لخط العبور أو منطقة الكشف مع الوصف الدقيق.",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "alert_only_important": True,
    # v14 fix #10: إضافة إعدادَي retention_days (الافتراضي 7) وmax_storage_mb (الافتراضي 2000)
    "retention_days": 7,
    "max_storage_mb": 2000,
    # v15 #1: إعداد تنظيف الملفات اليتيمة (الافتراضي False لحماية التسجيلات)
    "orphan_cleanup_enabled": False,
    # v15 #4: إعدادات البث المباشر عالي الإطارات وgo2rtc الاختياري
    "live_fps": 15,
    "detect_fps": 8,
    "live_max_width": 1280,
    "live_jpeg_quality": 75,
    "go2rtc_enabled": True,   # v16 #R16: البث المباشر عالي الدقة عبر go2rtc المرفق
    "go2rtc_url": "http://127.0.0.1:1984",
    "go2rtc_relay": True,  # v17 #R23: التحليل والتسجيل ياخذون البث من go2rtc المحلي (جلسة وحدة على المسجّل لكل بث)
    "clip_max_width": 1920,   # v16 #R16: دقة مقاطع الأحداث (1920 = 1080p)
    "clip_source": "auto",    # auto: البث الثالث 1080p لو متوفر وإلا الرئيسي مصغّر
    "yolo_device": "auto",    # auto: كرت الشاشة (CUDA) لو متوفر
    "clip_recorder": "ffmpeg",  # v16 #R18: ffmpeg = نسخ البث بدون فك ترميز (صفر معالج) | legacy = الطريقة القديمة
    "stationary_suppress_sec": 120,  # v17 #R28: كبت الإنذارات المكررة للأجسام المتوقفة
    # v15 #8: إعدادات نظام الإشعارات متعدد المزودات (notifiers)
    "notifiers": {
        "telegram": {
            "enabled": True,
            "bot_token": "",
            "chat_id": "",
            "categories": ["person", "vehicle"],
            "cameras": "all"
        },
        "ntfy": {
            "enabled": False,
            "server_url": "https://ntfy.sh",
            "topic": "",
            "token": "",
            "categories": ["person", "vehicle"],
            "cameras": "all"
        },
        "smtp": {
            "enabled": False,
            "host": "smtp.gmail.com",
            "port": 587,
            "use_tls": True,
            "username": "",
            "password": "",
            "from_addr": "",
            "to_addrs": "",
            "categories": ["person", "vehicle"],
            "cameras": "all"
        },
        "discord": {
            "enabled": False,
            "webhook_url": "",
            "categories": ["person", "vehicle"],
            "cameras": "all"
        },
        "whatsapp": {
            "enabled": False,
            "mode": "callmebot",
            "phone": "",
            "callmebot_apikey": "",
            "phone_number_id": "",
            "access_token": "",
            "categories": ["person", "vehicle"],
            "cameras": "all"
        }
    },
    # v15 #11: إعدادات التخزين السحابي الاختياري (S3 المتوافق وrclone)
    "cloud_storage": {
        "enabled": False,
        "provider": "s3",
        "confirmed_only": True,
        "delete_local_after_days": 0,
        "s3": {
            "endpoint_url": "",
            "region": "auto",
            "bucket": "",
            "access_key": "",
            "secret_key": "",
            "prefix": "cctv_events"
        },
        "rclone": {
            "remote_name": "",
            "prefix": "cctv_events"
        }
    }
}

SECRET_MASK = "••••"
TOP_SECRET_KEYS = ("password", "universal_api_key", "gemini_api_key", "telegram_bot_token", "app_secret")
NOTIFIER_SECRET_MAP = {
    "telegram": ("bot_token",),
    "ntfy": ("token",),
    "smtp": ("password",),
    "discord": ("webhook_url",),
    "whatsapp": ("callmebot_apikey", "access_token"),
}
CLOUD_S3_SECRET_KEYS = ("access_key", "secret_key")

# v15 #7: الإعدادات الافتراضية لقسم التحليل لكل كاميرا (analysis)
def make_default_cam_analysis(lpr_enabled=False, cooldown_seconds=12):
    return {
        "detect_classes": ["person", "vehicle", "animal"],  # v17 #R23: «أخرى» كانت 69% من الأحداث بدون فائدة
        "alert_classes": ["person", "vehicle"],
        "vehicle_subtypes": ["car", "truck", "motorcycle", "bicycle", "bus"],
        "animal_subtypes": ["cat", "dog", "bird"],
        "min_confidence": 0.45,
        # v17 #R20: فلاتر الإنذارات الكاذبة (أسلوب Frigate): وسيط ثقة المسار، وحجم الجسم، وثقة تأكيد الـAI
        "alert_threshold": 0.60,
        "min_box_height_pct": 8,
        "max_box_area_pct": 60,
        "ai_confirm_min_conf": 0.60,
        "ai_verify": True,
        "lpr_enabled": bool(lpr_enabled),
        "line_direction": "both",
        "zone_dwell_sec": 1.0,
        "cooldown_seconds": int(cooldown_seconds or 12),
        "schedule": "always",
        "trigger_source": "local"
    }

def sanitize_config_for_client(cfg):
    '''إرجاع نسخة آمنة من الإعدادات للواجهة مع إخفاء كلمات المرور والمفاتيح لكل المزودات كـ •••• وإضافة has_<name>: true'''
    safe = json.loads(json.dumps(cfg or {}))
    for k in ("password", "universal_api_key", "gemini_api_key", "telegram_bot_token"):
        has_val = bool(str(safe.get(k) or "").strip())
        safe[f"has_{k}"] = has_val
        safe[k] = SECRET_MASK if has_val else ""
    hc = safe.get("hikconnect")
    if not isinstance(hc, dict):
        hc = {}
    has_app_sec = bool(str(hc.get("app_secret") or safe.get("app_secret") or "").strip())
    hc["has_app_secret"] = has_app_sec
    hc["app_secret"] = SECRET_MASK if has_app_sec else ""
    safe["hikconnect"] = hc
    safe["has_app_secret"] = has_app_sec
    if "app_secret" in safe:
        safe["app_secret"] = SECRET_MASK if has_app_sec else ""

    # v15 #8: إخفاء مفاتيح المزودات في قسم notifiers
    notifiers = safe.get("notifiers")
    if not isinstance(notifiers, dict):
        notifiers = json.loads(json.dumps(DEFAULT_CONFIG["notifiers"]))
    for prov, sec_keys in NOTIFIER_SECRET_MAP.items():
        p_dict = notifiers.get(prov)
        if not isinstance(p_dict, dict):
            p_dict = json.loads(json.dumps(DEFAULT_CONFIG["notifiers"].get(prov, {})))
        for sk in sec_keys:
            raw_v = str(p_dict.get(sk) or "").strip()
            if prov == "telegram" and sk == "bot_token" and not raw_v:
                raw_v = str(cfg.get("telegram_bot_token") or "").strip()
            has_sk = bool(raw_v)
            p_dict[f"has_{sk}"] = has_sk
            p_dict[sk] = SECRET_MASK if has_sk else ""
        notifiers[prov] = p_dict
    safe["notifiers"] = notifiers

    # v15 #11: إخفاء مفاتيح S3 في قسم cloud_storage
    cloud = safe.get("cloud_storage")
    if not isinstance(cloud, dict):
        cloud = json.loads(json.dumps(DEFAULT_CONFIG["cloud_storage"]))
    s3_d = cloud.get("s3")
    if not isinstance(s3_d, dict):
        s3_d = json.loads(json.dumps(DEFAULT_CONFIG["cloud_storage"]["s3"]))
    for sk in CLOUD_S3_SECRET_KEYS:
        has_sk = bool(str(s3_d.get(sk) or "").strip())
        s3_d[f"has_{sk}"] = has_sk
        s3_d[sk] = SECRET_MASK if has_sk else ""
    cloud["s3"] = s3_d
    safe["cloud_storage"] = cloud

    for cam in safe.get("cameras", []):
        if isinstance(cam, dict):
            has_cam_pwd = bool(str(cam.get("password") or "").strip())
            cam["has_password"] = has_cam_pwd
            cam["password"] = SECRET_MASK if has_cam_pwd else ""
    return safe

def merge_client_config_preserving_secrets(target_cfg, incoming):
    '''دمج الإعدادات الواردة من الواجهة مع الحفاظ على الأسرار المحفوظة لجميع المزودات إذا وصلت القيمة ••••'''
    if not isinstance(incoming, dict):
        return target_cfg
    clean_in = json.loads(json.dumps(incoming))
    for k in list(clean_in.keys()):
        if k.startswith("has_"):
            clean_in.pop(k, None)
    for secret_k in TOP_SECRET_KEYS:
        if secret_k in clean_in:
            if str(clean_in[secret_k]).strip() == SECRET_MASK:
                clean_in[secret_k] = target_cfg.get(secret_k, "")
    if isinstance(clean_in.get("hikconnect"), dict):
        inc_hc = clean_in["hikconnect"]
        inc_hc.pop("has_app_secret", None)
        old_hc = target_cfg.get("hikconnect") if isinstance(target_cfg.get("hikconnect"), dict) else {}
        if str(inc_hc.get("app_secret", "")).strip() == SECRET_MASK:
            inc_hc["app_secret"] = old_hc.get("app_secret", "")
        merged_hc = old_hc.copy()
        merged_hc.update(inc_hc)
        clean_in["hikconnect"] = merged_hc

    # v15 #8: الحفاظ على أسرار notifiers عند وصول ••••
    if isinstance(clean_in.get("notifiers"), dict):
        old_notifiers = target_cfg.get("notifiers") if isinstance(target_cfg.get("notifiers"), dict) else {}
        merged_notifiers = json.loads(json.dumps(DEFAULT_CONFIG["notifiers"]))
        merged_notifiers.update(json.loads(json.dumps(old_notifiers)))
        for prov, inc_p in clean_in["notifiers"].items():
            if not isinstance(inc_p, dict):
                continue
            old_p = merged_notifiers.get(prov, {})
            for k in list(inc_p.keys()):
                if k.startswith("has_"):
                    inc_p.pop(k, None)
            for sk in NOTIFIER_SECRET_MAP.get(prov, ()):
                if sk in inc_p and str(inc_p[sk]).strip() == SECRET_MASK:
                    inc_p[sk] = old_p.get(sk, "")
            upd_p = dict(old_p)
            upd_p.update(inc_p)
            merged_notifiers[prov] = upd_p
        clean_in["notifiers"] = merged_notifiers
        # مزامنة توكن تيليجرام مع الحقول العليا للتوافق مع الإصدارات السابقة
        tg_p = merged_notifiers.get("telegram", {})
        if tg_p.get("bot_token") and tg_p.get("bot_token") != SECRET_MASK:
            clean_in["telegram_bot_token"] = tg_p["bot_token"]
        if tg_p.get("chat_id"):
            clean_in["telegram_chat_id"] = tg_p["chat_id"]

    # v15 #11: الحفاظ على أسرار cloud_storage.s3 عند وصول ••••
    if isinstance(clean_in.get("cloud_storage"), dict):
        old_cloud = target_cfg.get("cloud_storage") if isinstance(target_cfg.get("cloud_storage"), dict) else {}
        merged_cloud = json.loads(json.dumps(DEFAULT_CONFIG["cloud_storage"]))
        merged_cloud.update(json.loads(json.dumps(old_cloud)))
        inc_cloud = clean_in["cloud_storage"]
        if isinstance(inc_cloud.get("s3"), dict):
            old_s3 = merged_cloud.get("s3", {})
            inc_s3 = inc_cloud["s3"]
            for k in list(inc_s3.keys()):
                if k.startswith("has_"):
                    inc_s3.pop(k, None)
            for sk in CLOUD_S3_SECRET_KEYS:
                if sk in inc_s3 and str(inc_s3[sk]).strip() == SECRET_MASK:
                    inc_s3[sk] = old_s3.get(sk, "")
            upd_s3 = dict(old_s3)
            upd_s3.update(inc_s3)
            inc_cloud["s3"] = upd_s3
        merged_cloud.update(inc_cloud)
        clean_in["cloud_storage"] = merged_cloud

    if isinstance(clean_in.get("cameras"), list):
        old_cams_by_id = {
            int(c["id"]): c for c in target_cfg.get("cameras", [])
            if isinstance(c, dict) and "id" in c
        }
        for cam in clean_in["cameras"]:
            if isinstance(cam, dict):
                cam.pop("has_password", None)
                cid = int(cam.get("id", 0))
                old_cam = old_cams_by_id.get(cid, {})
                if str(cam.get("password", "")).strip() == SECRET_MASK:
                    cam["password"] = old_cam.get("password", target_cfg.get("password", ""))
                # ضمان وجود قسم analysis وإعدادات البث لكل كاميرا
                cam.setdefault("live_stream", old_cam.get("live_stream", "sub"))
                cam.setdefault("analysis_stream", old_cam.get("analysis_stream", "sub"))
                cam.setdefault("line_direction", old_cam.get("line_direction", "both"))
                cam.setdefault("zone_dwell_sec", float(old_cam.get("zone_dwell_sec", 1.0)))
                cam.setdefault("trigger_source", old_cam.get("trigger_source", "local"))
                def_an = make_default_cam_analysis(cam.get("lpr_enabled", False), target_cfg.get("cooldown_seconds", 12))
                if isinstance(old_cam.get("analysis"), dict):
                    def_an.update(old_cam["analysis"])
                if isinstance(cam.get("analysis"), dict):
                    def_an.update(cam["analysis"])
                cam["analysis"] = def_an
    target_cfg.update(clean_in)
    return target_cfg

def make_cam_entry(cam_id, name, conn_type, ip, port, username, password, channel, stream_type="main", custom_rtsp="", enabled=True, ai_watch=True, roi_data=None, cam_hw_info=None, live_stream="sub"):
    default_poly = [[0.15, 0.15], [0.85, 0.15], [0.85, 0.85], [0.15, 0.85]]
    return {
        "id": int(cam_id),
        "name": name or f"كاميرا {cam_id}",
        "conn_type": conn_type or "hikvision_nvr",
        "ip": ip or "192.168.8.7",
        "port": str(port or "554"),
        "http_port": "80",
        "username": username or "admin",
        "password": password or "",
        "channel": str(channel or cam_id),
        "stream_type": stream_type or "main",
        "live_stream": live_stream or "sub",
        "analysis_stream": "sub",  # v17 #R21: بث التحليل (YOLO) مستقل عن العرض والتسجيل: sub (مستحسن) | third | main
        "custom_rtsp": custom_rtsp or "",
        "enabled": bool(enabled),
        "ai_watch": bool(ai_watch),
        "lpr_enabled": False,
        "line_direction": "both",
        "zone_dwell_sec": 1.0,
        "trigger_source": "local",
        "analysis": make_default_cam_analysis(False, 12),
        "cam_hw_info": cam_hw_info or {
            "cam_model": "غير مفحوص",
            "cam_firmware": "--",
            "cam_serial": "--",
            "nvr_model": "غير مفحوص",
            "nvr_firmware": "--",
            "rtsp_configured_kbps": 0,
            "source_note": "لم يتم فحص ISAPI بعد"
        },
        "roi": roi_data or {
            "line_enabled": False, "lx1": 0.2, "ly1": 0.55, "lx2": 0.8, "ly2": 0.55,
            "line_direction": "both",
            "zone_enabled": False, "zx1": 0.15, "zy1": 0.15, "zx2": 0.85, "zy2": 0.85,
            "polygon": default_poly,
            "zone_dwell_sec": 1.0,
            "combo_mode": "both"
        }
    }

# v14 fix #11: قراءة الإعدادات من CONFIG_FILE فقط وحذف البحث في Music وDownloads وDesktop
def load_config():
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with DISK_IO_LOCK:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
            for k, v in saved.items():
                if v not in ("", None, []):
                    cfg[k] = v
        except Exception:
            pass

    if not cfg.get("gemini_model") or cfg.get("gemini_model") == "gemini-2.5-flash":
        cfg["gemini_model"] = "gemini-3.8-flash"
    cfg.setdefault("orphan_cleanup_enabled", False)
    cfg.setdefault("live_fps", 15)
    cfg.setdefault("detect_fps", 8)
    cfg.setdefault("live_max_width", 1280)
    cfg.setdefault("live_jpeg_quality", 75)
    cfg.setdefault("go2rtc_enabled", True)
    cfg.setdefault("go2rtc_relay", True)
    cfg.setdefault("clip_max_width", 1920)
    cfg.setdefault("clip_source", "auto")
    cfg.setdefault("yolo_device", "auto")
    cfg.setdefault("clip_recorder", "ffmpeg")
    cfg.setdefault("stationary_suppress_sec", 120)
    cfg.setdefault("go2rtc_url", "http://127.0.0.1:1984")

    if not isinstance(cfg.get("notifiers"), dict):
        cfg["notifiers"] = copy.deepcopy(DEFAULT_CONFIG["notifiers"])
    else:
        for prov, def_p in DEFAULT_CONFIG["notifiers"].items():
            if not isinstance(cfg["notifiers"].get(prov), dict):
                cfg["notifiers"][prov] = copy.deepcopy(def_p)
            else:
                for pk, pv in def_p.items():
                    cfg["notifiers"][prov].setdefault(pk, pv)
    if cfg.get("telegram_bot_token") and not cfg["notifiers"]["telegram"].get("bot_token"):
        cfg["notifiers"]["telegram"]["bot_token"] = cfg["telegram_bot_token"]
    if cfg.get("telegram_chat_id") and not cfg["notifiers"]["telegram"].get("chat_id"):
        cfg["notifiers"]["telegram"]["chat_id"] = cfg["telegram_chat_id"]

    if not isinstance(cfg.get("cloud_storage"), dict):
        cfg["cloud_storage"] = copy.deepcopy(DEFAULT_CONFIG["cloud_storage"])
    else:
        for ck, cv in DEFAULT_CONFIG["cloud_storage"].items():
            if isinstance(cv, dict):
                if not isinstance(cfg["cloud_storage"].get(ck), dict):
                    cfg["cloud_storage"][ck] = copy.deepcopy(cv)
                else:
                    for sck, scv in cv.items():
                        cfg["cloud_storage"][ck].setdefault(sck, scv)
            else:
                cfg["cloud_storage"].setdefault(ck, cv)

    if not cfg.get("cameras"):
        cfg["cameras"] = [
            make_cam_entry(
                1,
                "الكاميرا الرئيسية (قناة 1)",
                cfg.get("conn_type", "hikvision_nvr"),
                cfg.get("ip", "192.168.8.7"),
                cfg.get("port", "554"),
                cfg.get("username", "admin"),
                cfg.get("password", ""),
                cfg.get("channel", "1"),
                cfg.get("stream_type", "main"),
                cfg.get("custom_rtsp", ""),
                enabled=True,
                ai_watch=True
            )
        ]
    else:
        for c in cfg["cameras"]:
            if "lpr_enabled" not in c:
                c["lpr_enabled"] = False
            if "http_port" not in c:
                c["http_port"] = "80"
            # v15 #4: لكل كاميرا live_stream (الافتراضي sub للبث المباشر السريع)
            c.setdefault("live_stream", "sub")
            # بث التحليل (YOLO): sub (افتراضي مستحسن للأداء) | third (عالي 1080p) | main (الرئيسي 4K)
            c.setdefault("analysis_stream", "sub")
            c.setdefault("line_direction", "both")
            c.setdefault("zone_dwell_sec", 1.0)
            c.setdefault("trigger_source", "local")
            def_an = make_default_cam_analysis(c.get("lpr_enabled", False), cfg.get("cooldown_seconds", 12))
            if isinstance(c.get("analysis"), dict):
                def_an.update(c["analysis"])
            def_an["lpr_enabled"] = bool(c.get("lpr_enabled", def_an.get("lpr_enabled", False)))
            c["analysis"] = def_an

            if "cam_hw_info" not in c or not isinstance(c["cam_hw_info"], dict):
                c["cam_hw_info"] = {
                    "cam_model": "غير مفحوص",
                    "cam_firmware": "--",
                    "cam_serial": "--",
                    "nvr_model": "غير مفحوص",
                    "nvr_firmware": "--",
                    "rtsp_configured_kbps": 0,
                    "source_note": "لم يتم فحص ISAPI بعد"
                }
            r = c.get("roi")
            if not isinstance(r, dict) or "line_enabled" not in r:
                old_mode = (r or {}).get("mode", "none") if isinstance(r, dict) else "none"
                zx1 = float((r or {}).get("x1", 0.15)) if old_mode == "zone" else 0.15
                zy1 = float((r or {}).get("y1", 0.15)) if old_mode == "zone" else 0.15
                zx2 = float((r or {}).get("x2", 0.85)) if old_mode == "zone" else 0.85
                zy2 = float((r or {}).get("y2", 0.85)) if old_mode == "zone" else 0.85
                c["roi"] = {
                    "line_enabled": old_mode == "line",
                    "lx1": float((r or {}).get("x1", 0.2)) if old_mode == "line" else 0.2,
                    "ly1": float((r or {}).get("y1", 0.55)) if old_mode == "line" else 0.55,
                    "lx2": float((r or {}).get("x2", 0.8)) if old_mode == "line" else 0.8,
                    "ly2": float((r or {}).get("y2", 0.55)) if old_mode == "line" else 0.55,
                    "line_direction": c.get("line_direction", "both"),
                    "zone_enabled": old_mode == "zone",
                    "zx1": zx1, "zy1": zy1, "zx2": zx2, "zy2": zy2,
                    "polygon": [[zx1, zy1], [zx2, zy1], [zx2, zy2], [zx1, zy2]],
                    "zone_dwell_sec": float(c.get("zone_dwell_sec", 1.0)),
                    "combo_mode": "both"
                }
            else:
                if "polygon" not in r or not isinstance(r.get("polygon"), list) or len(r.get("polygon", [])) < 3:
                    zx1, zy1 = float(r.get("zx1", 0.15)), float(r.get("zy1", 0.15))
                    zx2, zy2 = float(r.get("zx2", 0.85)), float(r.get("zy2", 0.85))
                    r["polygon"] = [[zx1, zy1], [zx2, zy1], [zx2, zy2], [zx1, zy2]]
                r.setdefault("line_direction", c.get("line_direction", "both"))
                r.setdefault("zone_dwell_sec", float(c.get("zone_dwell_sec", 1.0)))

    save_config(cfg)
    return cfg

def save_config(cfg):
    atomic_write_json(CONFIG_FILE, cfg, indent=2)

# v15 #1: load_events يرجع (events, ok) ولو ok=False يتوقف التنظيف التلقائي بالكامل ويكتب تحذيراً في السجل
def load_events(events_file=EVENTS_FILE, return_tuple=True):
    ef = Path(events_file)
    if not ef.exists():
        return ([], True) if return_tuple else []
    try:
        with DISK_IO_LOCK:
            raw_txt = ef.read_text(encoding="utf-8").strip()
            if not raw_txt:
                print("[WARNING] ملف سجل الأحداث events_log.json فارغ نصياً — لن يُحذف أي ملف يتيم.")
                return ([], False) if return_tuple else []
            evs = json.loads(raw_txt)
        if isinstance(evs, list):
            valid_evs = [e for e in evs if isinstance(e, dict)][:MAX_STORED_EVENTS + 64]  # v16 #R14: نفس حد الحفظ
            return (valid_evs, True) if return_tuple else valid_evs
        print("[WARNING] صيغة events_log.json ليست قائمة صالحة — تم تعطيل التنظيف التلقائي.")
        return ([], False) if return_tuple else []
    except Exception as ex:
        print(f"[WARNING] فشل قراءة events_log.json ({ex}) — تم إيقاف التنظيف التلقائي لحماية التسجيلات.")
        return ([], False) if return_tuple else []

def save_events(events):
    with DISK_IO_LOCK:
        trimmed = list(events)[:MAX_STORED_EVENTS + 64]  # v16 #R8: الذاكرة هي اللي تقص، والملف يحفظ الأحداث المشغولة
    atomic_write_json(EVENTS_FILE, trimmed, indent=2)


def is_within_schedule(schedule_str, now_dt=None):
    '''يفحص ما إذا كان الوقت الحالي ضمن جدول التفعيل مثل 22:00-06:00 أو always'''
    s = str(schedule_str or "always").strip()
    if not s or s.lower() in ("always", "دائماً", "24/7", "*"):
        return True
    m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", s)
    if not m:
        return True
    dt = now_dt or datetime.now()
    cur_mins = dt.hour * 60 + dt.minute
    start_mins = int(m.group(1)) * 60 + int(m.group(2))
    end_mins = int(m.group(3)) * 60 + int(m.group(4))
    if start_mins <= end_mins:
        return start_mins <= cur_mins <= end_mins
    return cur_mins >= start_mins or cur_mins <= end_mins

