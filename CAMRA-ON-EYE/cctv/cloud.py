# -*- coding: utf-8 -*-
"""cctv.cloud — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import subprocess
import time
import threading
from collections import deque, Counter
from datetime import datetime
from .config import atomic_write_json, redact_secrets
from .paths import CLIPS_DIR, PLATES_DIR, SNAP_DIR


# v15 #11: استيراد مكتبة boto3 للتخزين السحابي S3 بشكل اختياري داخل try
try:
    import boto3
    from botocore.config import Config as BotoConfig
    HAS_BOTO3 = True
except Exception as _boto_err:
    boto3 = None
    BotoConfig = None
    HAS_BOTO3 = False
    BOTO3_IMPORT_MSG = f"مكتبة boto3 غير مثبتة ({_boto_err}) — ميزة التخزين السحابي S3 معطلة حتى تثبيت boto3."


# ====================================================================================
# v15 #11: مدير التخزين السحابي الاختياري (S3 المتوافق عبر boto3 وrclone عبر subprocess)
# ====================================================================================
class CloudStorageManager:
    def __init__(self, engine):
        self.engine = engine
        self.queue = deque(maxlen=200)
        self.lock = threading.Lock()
        self.worker_active = False

    def enqueue_event_upload(self, event_id):
        cloud_cfg = self.engine.cfg.get("cloud_storage") if isinstance(self.engine.cfg.get("cloud_storage"), dict) else {}
        if not cloud_cfg.get("enabled", False):
            return False
        ev = self.engine.get_event_by_id(event_id)
        if not ev:
            return False
        if cloud_cfg.get("confirmed_only", True) and not ev.get("confirmed", False):
            self.engine.update_event_fields(event_id, cloud_status="skipped_unconfirmed")
            return False
        self.engine.update_event_fields(event_id, cloud_status="queued")
        with self.lock:
            self.queue.append(event_id)
            if not self.worker_active:
                self.worker_active = True
                threading.Thread(target=self._upload_worker, daemon=True).start()
        return True

    def _upload_worker(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.worker_active = False
                    return
                event_id = self.queue.popleft()
            try:
                self.upload_event_now(event_id)
            except Exception as ex:
                self.engine.update_event_fields(
                    event_id,
                    cloud_status="failed",
                    cloud_error=redact_secrets(ex, self.engine.cfg)
                )

    def upload_event_now(self, event_id, max_attempts=2):
        ev = self.engine.get_event_by_id(event_id)
        if not ev:
            return {"ok": False, "error": "الحدث غير موجود"}
        cloud_cfg = self.engine.cfg.get("cloud_storage") if isinstance(self.engine.cfg.get("cloud_storage"), dict) else {}
        if not cloud_cfg.get("enabled", False):
            return {"ok": False, "error": "التخزين السحابي غير مفعّل"}

        provider = str(cloud_cfg.get("provider") or "s3").strip().lower()
        dt_obj = datetime.fromtimestamp(float(ev.get("detected_ts") or time.time()))
        yyyy, mm, dd = dt_obj.strftime("%Y"), dt_obj.strftime("%m"), dt_obj.strftime("%d")
        cid = int(ev.get("cam_id", 1))

        snap_file = SNAP_DIR / f"{event_id}.jpg"
        plate_file = PLATES_DIR / f"plate_{event_id}.jpg"
        clip_file = CLIPS_DIR / f"{event_id}.mp4"
        tmp_event_json = CLIPS_DIR / f".event_meta_{event_id}.json"
        atomic_write_json(tmp_event_json, {k: v for k, v in ev.items() if not str(k).startswith("_")}, indent=2)

        files_map = []
        if snap_file.exists():
            files_map.append(("snapshot.jpg", snap_file, "image/jpeg"))
        if plate_file.exists():
            files_map.append(("plate.jpg", plate_file, "image/jpeg"))
        if clip_file.exists():
            files_map.append(("clip.mp4", clip_file, "video/mp4"))
        if tmp_event_json.exists():
            files_map.append(("event.json", tmp_event_json, "application/json"))

        self.engine.update_event_fields(event_id, cloud_status="uploading")
        last_err = ""

        try:
            if provider == "s3":
                if not HAS_BOTO3 or boto3 is None:
                    msg = "مكتبة boto3 غير مثبتة — يرجى تثبيت boto3 لتفعيل رفع S3"
                    self.engine.update_event_fields(event_id, cloud_status="failed", cloud_error=msg)
                    return {"ok": False, "error": msg}
                s3_cfg = cloud_cfg.get("s3") or {}
                endpoint_url = str(s3_cfg.get("endpoint_url") or "").strip() or None
                region = str(s3_cfg.get("region") or "auto").strip()
                bucket = str(s3_cfg.get("bucket") or "").strip()
                access_key = str(s3_cfg.get("access_key") or "").strip()
                secret_key = str(s3_cfg.get("secret_key") or "").strip()
                prefix = str(s3_cfg.get("prefix") or "cctv_events").strip().strip("/")
                if not bucket or not access_key or not secret_key:
                    msg = "إعدادات S3 (bucket / access_key / secret_key) غير مكتملة"
                    self.engine.update_event_fields(event_id, cloud_status="failed", cloud_error=msg)
                    return {"ok": False, "error": msg}

                base_key = f"{prefix}/{yyyy}/{mm}/{dd}/cam{cid}/{event_id}"
                for attempt in range(1, max_attempts + 1):
                    try:
                        s3_client = boto3.client(
                            "s3",
                            endpoint_url=endpoint_url,
                            region_name=region,
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            config=BotoConfig(signature_version="s3v4") if BotoConfig else None
                        )
                        presigned_snap_url = ""
                        for remote_name, local_path, mime_t in files_map:
                            obj_key = f"{base_key}/{remote_name}"
                            s3_client.upload_file(
                                str(local_path),
                                bucket,
                                obj_key,
                                ExtraArgs={"ContentType": mime_t}
                            )
                            if remote_name == "snapshot.jpg":
                                presigned_snap_url = s3_client.generate_presigned_url(
                                    "get_object",
                                    Params={"Bucket": bucket, "Key": obj_key},
                                    ExpiresIn=7 * 86400  # رابط مؤقت صالح لمدة 7 أيام
                                )
                        self.engine.update_event_fields(
                            event_id,
                            cloud_status="uploaded",
                            cloud_uploaded_ts=time.time(),
                            cloud_url=presigned_snap_url or f"s3://{bucket}/{base_key}/"
                        )
                        return {"ok": True, "cloud_url": presigned_snap_url}
                    except Exception as ex:
                        last_err = redact_secrets(ex, self.engine.cfg)
                        if attempt < max_attempts:
                            time.sleep(1.0 * attempt)

            elif provider == "rclone":
                rclone_cfg = cloud_cfg.get("rclone") or {}
                remote_name = str(rclone_cfg.get("remote_name") or "").strip().rstrip(":")
                prefix = str(rclone_cfg.get("prefix") or "cctv_events").strip().strip("/")
                if not remote_name:
                    msg = "لم يتم تحديد اسم remote لـ rclone"
                    self.engine.update_event_fields(event_id, cloud_status="failed", cloud_error=msg)
                    return {"ok": False, "error": msg}
                remote_dir = f"{remote_name}:{prefix}/{yyyy}/{mm}/{dd}/cam{cid}/{event_id}"
                for attempt in range(1, max_attempts + 1):
                    try:
                        for dst_name, local_path, _ in files_map:
                            subprocess.check_call(
                                ["rclone", "copyto", str(local_path), f"{remote_dir}/{dst_name}"],
                                timeout=40
                            )
                        self.engine.update_event_fields(
                            event_id,
                            cloud_status="uploaded",
                            cloud_uploaded_ts=time.time(),
                            cloud_url=remote_dir
                        )
                        return {"ok": True, "cloud_url": remote_dir}
                    except Exception as ex:
                        last_err = redact_secrets(ex, self.engine.cfg)
                        if attempt < max_attempts:
                            time.sleep(1.0 * attempt)
        finally:
            if tmp_event_json.exists():
                try:
                    tmp_event_json.unlink()
                except Exception:
                    pass

        self.engine.update_event_fields(event_id, cloud_status="failed", cloud_error=last_err)
        return {"ok": False, "error": last_err}

