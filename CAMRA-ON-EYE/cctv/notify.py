# -*- coding: utf-8 -*-
"""cctv.notify — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import time
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
import requests
from .config import redact_secrets
from .paths import PLATES_DIR


# ====================================================================================
# v15 #8: نظام الإشعارات متعدد المزودات (notifiers.py المدمج: Telegram, ntfy, SMTP, Discord, WhatsApp)
# ====================================================================================
class MultiProviderNotifier:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def _skip_reason(prov_cfg, cam_id, category, needs=()):
        '''# v17 #R21: سبب عدم الإرسال بدقة (أو None لو الإرسال مطلوب)'''
        if not prov_cfg or not prov_cfg.get("enabled", False):
            return "المزود غير مفعّل في الإعدادات"
        missing = [k for k in needs if not str(prov_cfg.get(k) or "").strip()]
        if missing:
            return "بيانات ناقصة: " + "، ".join(missing)
        cams_flt = prov_cfg.get("cameras", "all")
        allowed_ids = None
        if isinstance(cams_flt, list) and cams_flt:
            allowed_ids = [int(x) for x in cams_flt if str(x).isdigit()]
        elif isinstance(cams_flt, str) and cams_flt.strip() not in ("", "all", "*"):
            allowed_ids = [int(x.strip()) for x in cams_flt.split(",") if x.strip().isdigit()]
        if allowed_ids and int(cam_id) not in allowed_ids:
            return f"مستبعد بفلتر الكاميرات (المسموح: {allowed_ids})"
        cats_flt = prov_cfg.get("categories") or ["person", "vehicle"]
        if isinstance(cats_flt, list) and cats_flt and category not in cats_flt:
            return f"مستبعد بفلتر الأنواع (الحدث: {category}، المسموح: {cats_flt})"
        return None

    @staticmethod  # v17 #R26: كان الديكوريتر انحذف بالغلط في R21 → كل الإشعارات كانت تفشل بـ"takes 3 positional arguments"
    def _matches_filter(prov_cfg, cam_id, category):
        if not prov_cfg or not prov_cfg.get("enabled", False):
            return False
        cams_flt = prov_cfg.get("cameras", "all")
        if isinstance(cams_flt, list) and cams_flt and int(cam_id) not in [int(x) for x in cams_flt if str(x).isdigit()]:
            return False
        elif isinstance(cams_flt, str) and cams_flt.strip() not in ("", "all", "*"):
            allowed_ids = [int(x.strip()) for x in cams_flt.split(",") if x.strip().isdigit()]
            if allowed_ids and int(cam_id) not in allowed_ids:
                return False
        cats_flt = prov_cfg.get("categories") or ["person", "vehicle"]
        if isinstance(cats_flt, list) and cats_flt and category not in cats_flt:
            return False
        return True

    def send_ntfy(self, prov_cfg, image_path, caption, cloud_url="", max_attempts=2):
        server_url = str(prov_cfg.get("server_url") or "https://ntfy.sh").strip().rstrip("/")
        topic = str(prov_cfg.get("topic") or "").strip().lstrip("/")
        token = str(prov_cfg.get("token") or "").strip()
        if not topic:
            return {"ok": False, "status": "not_configured", "message": "لم يتم تحديد topic لـ ntfy"}
        url = f"{server_url}/{topic}"
        msg_body = f"{caption}\n{cloud_url}".strip() if cloud_url else str(caption or "")
        headers = {"Title": "Hikvision AI Alert", "Message": msg_body[:500].encode("utf-8", errors="ignore").decode("latin-1", errors="ignore")}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        t0 = time.time()
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                p = Path(image_path) if image_path else None
                if p and p.exists():
                    headers["Filename"] = p.name
                    with open(p, "rb") as f:
                        resp = requests.put(url, data=f, headers=headers, timeout=12)
                else:
                    resp = requests.post(url, data=msg_body.encode("utf-8"), headers=headers, timeout=12)
                ms = int((time.time() - t0) * 1000)
                if resp.status_code in (200, 201, 202):
                    return {"ok": True, "status": "accepted", "message": f"قُبل من ntfy ({ms} ms)", "accept_ms": ms}
                last_err = redact_secrets(f"HTTP {resp.status_code}: {resp.text[:140]}", self.engine.cfg)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    time.sleep(0.6 * attempt)
                    continue
                break
            except Exception as ex:
                last_err = redact_secrets(ex, self.engine.cfg)
                if attempt < max_attempts:
                    time.sleep(0.5 * attempt)
        return {"ok": False, "status": "failed", "message": f"فشل ntfy: {last_err}"}

    def send_smtp_email(self, prov_cfg, image_path, caption, cloud_url="", max_attempts=2):
        host = str(prov_cfg.get("host") or "").strip()
        port = int(prov_cfg.get("port") or 587)
        user = str(prov_cfg.get("username") or "").strip()
        pwd = str(prov_cfg.get("password") or "").strip()
        from_addr = str(prov_cfg.get("from_addr") or user).strip()
        to_addrs = [x.strip() for x in str(prov_cfg.get("to_addrs") or "").split(",") if x.strip()]
        use_tls = bool(prov_cfg.get("use_tls", True))
        if not host or not to_addrs:
            return {"ok": False, "status": "not_configured", "message": "بيانات SMTP أو المستلم غير مكتملة"}
        t0 = time.time()
        last_err = ""
        msg = EmailMessage()
        msg["Subject"] = "تنبيه أمني — مركز المراقبة الذكي Hikvision AI"
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        body_txt = f"{caption}\n\nالرابط السحابي المؤقت: {cloud_url}" if cloud_url else str(caption or "")
        msg.set_content(body_txt)
        p = Path(image_path) if image_path else None
        if p and p.exists():
            try:
                img_bytes = p.read_bytes()
                msg.add_attachment(img_bytes, maintype="image", subtype="jpeg", filename=p.name)
            except Exception:
                pass
        for attempt in range(1, max_attempts + 1):
            try:
                context = ssl.create_default_context()
                with smtplib.SMTP(host, port, timeout=12) as server:
                    if use_tls:
                        server.starttls(context=context)
                    if user and pwd:
                        server.login(user, pwd)
                    server.send_message(msg)
                ms = int((time.time() - t0) * 1000)
                return {"ok": True, "status": "accepted", "message": f"تم إرسال البريد عبر SMTP TLS ({ms} ms)", "accept_ms": ms}
            except Exception as ex:
                last_err = redact_secrets(ex, self.engine.cfg)
                if attempt < max_attempts:
                    time.sleep(0.6 * attempt)
        return {"ok": False, "status": "failed", "message": f"فشل SMTP: {last_err}"}

    def send_discord_webhook(self, prov_cfg, image_path, caption, cloud_url="", max_attempts=2):
        webhook_url = str(prov_cfg.get("webhook_url") or "").strip()
        if not webhook_url:
            return {"ok": False, "status": "not_configured", "message": "رابط Discord Webhook غير مضبوط"}
        t0 = time.time()
        last_err = ""
        content_txt = f"{caption}\n{cloud_url}".strip() if cloud_url else str(caption or "")
        p = Path(image_path) if image_path else None
        for attempt in range(1, max_attempts + 1):
            try:
                if p and p.exists():
                    with open(p, "rb") as f:
                        resp = requests.post(
                            webhook_url,
                            data={"content": content_txt[:1900]},
                            files={"file": (p.name, f, "image/jpeg")},
                            timeout=12
                        )
                else:
                    resp = requests.post(webhook_url, json={"content": content_txt[:1900]}, timeout=12)
                ms = int((time.time() - t0) * 1000)
                if resp.status_code in (200, 204):
                    return {"ok": True, "status": "accepted", "message": f"قُبل من Discord ({ms} ms)", "accept_ms": ms}
                last_err = redact_secrets(f"HTTP {resp.status_code}: {resp.text[:140]}", self.engine.cfg)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue
                break
            except Exception as ex:
                last_err = redact_secrets(ex, self.engine.cfg)
                if attempt < max_attempts:
                    time.sleep(0.5 * attempt)
        return {"ok": False, "status": "failed", "message": f"فشل Discord: {last_err}"}

    def send_whatsapp(self, prov_cfg, image_path, caption, cloud_url="", max_attempts=2):
        mode = str(prov_cfg.get("mode") or "callmebot").strip().lower()
        phone = str(prov_cfg.get("phone") or "").strip()
        text_msg = f"{caption}\n{cloud_url}".strip() if cloud_url else str(caption or "")
        t0 = time.time()
        last_err = ""

        if mode == "cloud_api":
            phone_id = str(prov_cfg.get("phone_number_id") or "").strip()
            token = str(prov_cfg.get("access_token") or "").strip()
            if not phone_id or not token or not phone:
                return {"ok": False, "status": "not_configured", "message": "بيانات WhatsApp Cloud API غير مكتملة"}
            url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            if cloud_url:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "image",
                    "image": {"link": cloud_url, "caption": str(caption or "")[:900]}
                }
            else:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": text_msg[:1500]}
                }
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = requests.post(url, headers=headers, json=payload, timeout=12)
                    ms = int((time.time() - t0) * 1000)
                    if resp.status_code in (200, 201):
                        return {"ok": True, "status": "accepted", "message": f"قُبل من WhatsApp Cloud API ({ms} ms)", "accept_ms": ms}
                    last_err = redact_secrets(f"HTTP {resp.status_code}: {resp.text[:140]}", self.engine.cfg)
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        time.sleep(0.8 * attempt)
                        continue
                    break
                except Exception as ex:
                    last_err = redact_secrets(ex, self.engine.cfg)
            return {"ok": False, "status": "failed", "message": f"فشل WhatsApp Cloud API: {last_err}"}

        # CallMeBot للاستخدام الشخصي
        apikey = str(prov_cfg.get("callmebot_apikey") or "").strip()
        if not phone or not apikey:
            return {"ok": False, "status": "not_configured", "message": "رقم الهاتف أو مفتاح CallMeBot غير مضبوط"}
        url = f"https://api.callmebot.com/whatsapp.php?phone={quote(phone, safe='')}&text={quote(text_msg[:900], safe='')}&apikey={quote(apikey, safe='')}"
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.get(url, timeout=12)
                ms = int((time.time() - t0) * 1000)
                if resp.status_code == 200:
                    return {"ok": True, "status": "accepted", "message": f"قُبل من WhatsApp CallMeBot ({ms} ms)", "accept_ms": ms}
                last_err = redact_secrets(f"HTTP {resp.status_code}: {resp.text[:140]}", self.engine.cfg)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue
                break
            except Exception as ex:
                last_err = redact_secrets(ex, self.engine.cfg)
        return {"ok": False, "status": "failed", "message": f"فشل WhatsApp CallMeBot: {last_err}"}

    def dispatch_all_configured(self, event_dict, snap_path, caption, cloud_url=""):
        '''يرسل الإشعار لجميع المزودات المفعلة التي تطابق فلتر الكاميرا والنوع ويحفظ النتيجة في notify_results'''
        cfg = self.engine.cfg
        notifiers_cfg = cfg.get("notifiers") if isinstance(cfg.get("notifiers"), dict) else {}
        cid = int(event_dict.get("cam_id", 1))
        cat = str(event_dict.get("category") or "other")
        results = {}

        # 1. Telegram
        tg_cfg = dict(notifiers_cfg.get("telegram") or {})
        if not tg_cfg.get("bot_token") and cfg.get("telegram_bot_token"):
            tg_cfg["bot_token"] = cfg.get("telegram_bot_token")
            tg_cfg["chat_id"] = cfg.get("telegram_chat_id")
            tg_cfg.setdefault("enabled", True)
        _why = self._skip_reason(tg_cfg, cid, cat, needs=("bot_token", "chat_id"))
        if _why is None:
            plate_url = event_dict.get("plate_url") or event_dict.get("_candidate_plate_url")
            plate_path = PLATES_DIR / Path(plate_url).name if plate_url and cat == "vehicle" else None
            results["telegram"] = self.engine._send_telegram_alert(snap_path, caption, cam_id=cid, plate_path=plate_path)
        else:
            results["telegram"] = {"ok": False, "status": "skipped", "message": f"لم يُرسل: {_why}", "accept_ms": None, "attempts": 0}

        # 2. ntfy
        ntfy_cfg = notifiers_cfg.get("ntfy") or {}
        if self._matches_filter(ntfy_cfg, cid, cat):
            results["ntfy"] = self.send_ntfy(ntfy_cfg, snap_path, caption, cloud_url=cloud_url)

        # 3. SMTP Email
        smtp_cfg = notifiers_cfg.get("smtp") or {}
        if self._matches_filter(smtp_cfg, cid, cat):
            results["smtp"] = self.send_smtp_email(smtp_cfg, snap_path, caption, cloud_url=cloud_url)

        # 4. Discord
        disc_cfg = notifiers_cfg.get("discord") or {}
        if self._matches_filter(disc_cfg, cid, cat):
            results["discord"] = self.send_discord_webhook(disc_cfg, snap_path, caption, cloud_url=cloud_url)

        # 5. WhatsApp
        wa_cfg = notifiers_cfg.get("whatsapp") or {}
        if self._matches_filter(wa_cfg, cid, cat):
            results["whatsapp"] = self.send_whatsapp(wa_cfg, snap_path, caption, cloud_url=cloud_url)

        return results

