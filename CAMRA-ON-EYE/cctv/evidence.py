"""Bounded waiting for recorded event evidence, independent of the AI queue."""
import time


def wait_recorded_frame(recorder, timestamp, timeout=12.0, clock=None, sleep=None):
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    if recorder is None or not timestamp:
        return None, "no_recorder", 0
    deadline = clock() + max(0, timeout)
    attempts = 0
    while clock() < deadline:
        stopped = getattr(recorder, "stop_event", None)
        if stopped is not None and stopped.is_set():
            return None, "recorder_stopped", attempts
        if recorder.healthy:
            attempts += 1
            remaining = deadline - clock()
            if remaining <= 0:
                break
            frame = recorder.grab_frame_at(timestamp, timeout=min(2.0, remaining))
            if frame is not None:
                return frame, "recording", attempts
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(0.25, remaining))
    return None, "recording_not_ready", attempts


def plate_label(event):
    status = event.get("plate_status", "not_requested")
    labels = {
        "confirmed": "مؤكدة آلياً", "candidate": "مرشحة غير مؤكدة",
        "partial": "قراءة جزئية غير مؤكدة", "unreadable": "غير مقروءة",
        "waiting_hq": "بانتظار إطار التسجيل", "reading": "القراءة قيد التنفيذ",
        "reader_unavailable": "قارئ اللوحات غير جاهز", "busy": "قارئ اللوحات مشغول",
        "failed": "فشلت قراءة اللوحة", "not_requested": "غير مطلوبة",
    }
    text = str(event.get("plate_text") or "")
    result = labels.get(status, "غير مؤكدة") + (f": {text}" if text else "")
    proposal = str(event.get("ai_plate_proposal") or "")
    if proposal and status != "confirmed":
        result += f" — اقتراح نموذج الرؤية للمراجعة فقط: {proposal}"
    return result


def evidence_caption(event, description):
    # The caller passes a clean description. Plate state is sampled at send time.
    parts = [str(description or "")]
    if event.get("plate_status", "not_requested") != "not_requested" or event.get("ai_plate_proposal"):
        parts.append("حالة اللوحة: " + plate_label(event))
    source = event.get("evidence_source")
    if source:
        label = "التسجيل عند الحدث" if source == "recording" else "صورة التحليل الاحتياطية"
        dims = event.get("evidence_resolution") or "غير معروفة"
        parts.append(f"مصدر الصورة: {label} ({dims})")
    return "\n".join(parts)
