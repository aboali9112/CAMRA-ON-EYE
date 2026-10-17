# -*- coding: utf-8 -*-
"""cctv.rtsp — جزء من مركز القيادة الأمني الذكي (v17، مقسّم من الملف الواحد v16)"""
import xml.etree.ElementTree as ET
from urllib.parse import quote
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from .config import redact_secrets


# v15 #4: دعم مسارين للبث لكل كاميرا: stream_live (الفرعي 02 افتراضياً) وstream_hq (الرئيسي 01 عند الحاجة)
def build_cam_rtsp(cam, role="live"):
    ctype = cam.get("conn_type", "hikvision_nvr")
    if ctype == "webcam":
        return 0
    if ctype == "custom_rtsp":
        if role == "third":
            return cam.get("custom_rtsp_clip", "").strip()  # v16 #R16
        if role == "hq" and cam.get("custom_rtsp_hq"):
            return cam.get("custom_rtsp_hq", "").strip()
        return cam.get("custom_rtsp", "").strip()
    ip = cam.get("ip", "").strip()
    port = cam.get("port", "554").strip() or "554"
    # v14 fix #6: تحويل اسم المستخدم وكلمة المرور بـ urllib.parse.quote(x, safe="")
    user = quote(str(cam.get("username", "admin")).strip(), safe="")
    pwd = quote(str(cam.get("password", "")), safe="")
    ch = str(cam.get("channel", "1")).strip() or "1"
    if role == "hq":
        stream_code = "01"
    elif role == "sub":
        stream_code = "02"
    elif role == "third":
        stream_code = "03"  # v16 #R16: البث الثالث (كثير من كاميرات Hikvision تعطي 1080p H.264)
    else:
        # v17 #R21: دور "live" = بث التحليل (YOLO). مستقل تماماً عن جودة العرض (go2rtc) والتسجيل (ffmpeg)
        pref = str(cam.get("_analysis_override") or cam.get("analysis_stream") or "sub").lower()
        stream_code = {"main": "01", "third": "03"}.get(pref, "02")
    return f"rtsp://{user}:{pwd}@{ip}:{port}/Streaming/Channels/{ch}{stream_code}"

# ====================================================================================
# 1. فاحص ISAPI الدقيق: يفصل بين موديل المسجل (NVR) وموديل الكاميرا الفرعية لكل قناة
# ====================================================================================
def xml_find_text(root, tag_name, default=""):
    for elem in root.iter():
        local_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local_name.lower() == tag_name.lower() and elem.text:
            return elem.text.strip()
    return default

def fetch_hikvision_isapi(ip, http_port, username, password, path):
    url = f"http://{ip}:{http_port}{path}"
    for auth_obj in (HTTPDigestAuth(username, password), HTTPBasicAuth(username, password)):
        try:
            resp = requests.get(url, auth=auth_obj, timeout=4.0)
            if resp.status_code == 200 and resp.text:
                return resp.text
        except Exception:
            pass
    return None

def query_hikvision_isapi_full(ip, http_port, username, password):
    ip = (ip or "").strip()
    http_port = (http_port or "80").strip() or "80"
    username = (username or "admin").strip()

    result = {
        "isapi_ok": False,
        "error_reason": "",
        "device_name": "",
        "device_type": "",
        "base_model": "",
        "base_firmware": "",
        "base_build_date": "",
        "base_serial": "",
        "base_mac": "",
        "channels": {}
    }

    xml_dev = fetch_hikvision_isapi(ip, http_port, username, password, "/ISAPI/System/deviceInfo")
    if not xml_dev:
        result["error_reason"] = f"تعذر الوصول إلى بروتوكول ISAPI على http://{ip}:{http_port} (تأكد من تفعيل Web/HTTP أو صحة منفذ HTTP وكلمة المرور)."
        return result

    try:
        root = ET.fromstring(xml_dev)
        result["isapi_ok"] = True
        result["device_name"] = xml_find_text(root, "deviceName", "Hikvision")
        result["device_type"] = xml_find_text(root, "deviceType", "Device")
        result["base_model"] = xml_find_text(root, "model", "غير معروف")
        result["base_firmware"] = xml_find_text(root, "firmwareVersion", "--")
        result["base_build_date"] = xml_find_text(root, "firmwareReleasedDate", "--")
        result["base_serial"] = xml_find_text(root, "serialNumber", "--")
        result["base_mac"] = xml_find_text(root, "macAddress", "--")
    except Exception as ex:
        result["error_reason"] = f"فشل تحليل XML من الجهاز: {redact_secrets(ex)}"
        return result

    xml_proxy = fetch_hikvision_isapi(ip, http_port, username, password, "/ISAPI/ContentMgmt/InputProxy/channels")
    if xml_proxy:
        try:
            proot = ET.fromstring(xml_proxy)
            for ch_elem in proot.iter():
                tname = ch_elem.tag.split("}")[-1] if "}" in ch_elem.tag else ch_elem.tag
                if tname == "InputProxyChannel":
                    cid = xml_find_text(ch_elem, "id", "")
                    cname = xml_find_text(ch_elem, "name", f"كاميرا {cid}")
                    cam_model = ""
                    cam_fw = ""
                    cam_sn = ""
                    cam_ip = ""
                    for sub in ch_elem.iter():
                        sname = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                        if sname == "sourceInputPortDescriptor":
                            cam_model = xml_find_text(sub, "model", "")
                            cam_fw = xml_find_text(sub, "firmwareVersion", "")
                            cam_sn = xml_find_text(sub, "serialNumber", "")
                            cam_ip = xml_find_text(sub, "ipAddress", "")
                    if cid.isdigit():
                        has_own_model = bool(cam_model)
                        result["channels"][cid] = {
                            "channel": cid,
                            "name": cname,
                            "cam_ip": cam_ip or ip,
                            "cam_model": cam_model if has_own_model else f"غير معلن من الكاميرا (المسجل: {result['base_model']})",
                            "cam_firmware": cam_fw if cam_fw else f"غير معلن (تحديث المسجل: {result['base_firmware']})",
                            "cam_serial": cam_sn or result["base_serial"],
                            "nvr_model": result["base_model"],
                            "nvr_firmware": f"{result['base_firmware']} ({result['base_build_date']})",
                            "configured_kbps": 0,
                            "source_note": "موديل الكاميرا الفعلي من InputProxy" if has_own_model else "المسجل لا يمرر موديل الكاميرا الفرعية (يعرض موديل المسجل NVR)"
                        }
        except Exception:
            pass

    if not result["channels"]:
        xml_vin = fetch_hikvision_isapi(ip, http_port, username, password, "/ISAPI/System/Video/inputs/channels")
        if xml_vin:
            try:
                vroot = ET.fromstring(xml_vin)
                v_chans = []
                for ch_elem in vroot.iter():
                    tname = ch_elem.tag.split("}")[-1] if "}" in ch_elem.tag else ch_elem.tag
                    if tname == "VideoInputChannel":
                        cid = xml_find_text(ch_elem, "id", "1")
                        cname = xml_find_text(ch_elem, "name", f"كاميرا {cid}")
                        v_chans.append((cid, cname))
                is_single_ipc = len(v_chans) == 1 and ("ipcamera" in result["device_type"].lower() or "ipc" in result["device_type"].lower())
                for cid, cname in v_chans:
                    result["channels"][cid] = {
                        "channel": cid,
                        "name": cname,
                        "cam_ip": ip,
                        "cam_model": result["base_model"] if is_single_ipc else f"قناة أنالوج/مباشرة على المسجل ({result['base_model']})",
                        "cam_firmware": result["base_firmware"],
                        "cam_serial": result["base_serial"],
                        "nvr_model": result["base_model"],
                        "nvr_firmware": f"{result['base_firmware']} ({result['base_build_date']})",
                        "configured_kbps": 0,
                        "source_note": "كاميرا IP مباشرة" if is_single_ipc else "قراءة من مسجل الفيديو (DVR/NVR)"
                    }
            except Exception:
                pass

    xml_stream = fetch_hikvision_isapi(ip, http_port, username, password, "/ISAPI/Streaming/channels")
    if xml_stream:
        try:
            sroot = ET.fromstring(xml_stream)
            for sc in sroot.iter():
                tname = sc.tag.split("}")[-1] if "}" in sc.tag else sc.tag
                if tname == "StreamingChannel":
                    sid = xml_find_text(sc, "id", "")
                    if len(sid) >= 3 and sid.endswith("01"):
                        ch_num = sid[:-2]
                        kbps_str = xml_find_text(sc, "constantBitRate", "") or xml_find_text(sc, "maxBitRate", "")
                        if kbps_str.isdigit() and ch_num in result["channels"]:
                            result["channels"][ch_num]["configured_kbps"] = int(kbps_str)
        except Exception:
            pass

    return result

