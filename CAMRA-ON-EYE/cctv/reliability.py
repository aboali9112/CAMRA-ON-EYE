"""V18 policies shared by capture, events, diagnostics and tests."""
import re
from collections import Counter


def lpr_enabled(cam):
    """One source of truth; explicit False must override legacy nested True."""
    value = cam.get("lpr_enabled", (cam.get("analysis") or {}).get("lpr_enabled", False))
    return value is True or str(value).lower() in ("true", "1", "yes")


def normalize_plate(text):
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    return re.sub(r"[^A-Z0-9]", "", str(text or "").translate(digits).upper())


def assess_plate(samples, min_conf=0.80, min_votes=2):
    """Conservative Latin-line validation, not proof of a vehicle's identity.

    Confirmation needs a complete 1-4 digit + 3 letter pattern and agreement
    across distinct source frames. Other formats remain reviewable candidates.
    """
    usable = [s for s in samples if s.get("text")]
    base = {"plate_status": "unreadable", "plate_text": "", "plate_conf": 0.0,
            "plate_source": "anpr", "plate_votes": 0, "plate_samples": len(samples)}
    if not usable:
        return base
    best = max(usable, key=lambda s: float(s.get("conf", 0)))
    votes, frames = Counter(), set()
    for s in usable:
        text = normalize_plate(s["text"])
        key = s.get("frame_key")
        complete = bool(re.fullmatch(r"(?:[0-9]{1,4}[A-Z]{3}|[A-Z]{3}[0-9]{1,4})", text))
        if complete and s.get("quality_ok", False) and float(s.get("conf", 0)) >= min_conf and key is not None and key not in frames:
            votes[text] += 1
            frames.add(key)
    winner, count = votes.most_common(1)[0] if votes else (normalize_plate(best["text"]), 0)
    matching = [s for s in usable if normalize_plate(s["text"]) == winner]
    base.update(plate_text=winner, plate_conf=round(max(float(s.get("conf", 0)) for s in matching), 3), plate_votes=count)
    complete = bool(re.fullmatch(r"(?:[0-9]{1,4}[A-Z]{3}|[A-Z]{3}[0-9]{1,4})", winner))
    leaders = sum(v == count for v in votes.values()) if count else 0
    base["plate_status"] = "confirmed" if count >= min_votes and leaders == 1 else ("candidate" if complete else "partial")
    return base


def recording_fresh(paths, now, max_age=15):
    """Check actual bytes/mtime, never a fabricated segment end timestamp."""
    for path in paths:
        try:
            st = path.stat()
            if st.st_size > 0 and -1 <= now - st.st_mtime <= max_age:
                return True
        except OSError:
            continue
    return False


def ready_models(candidates, cooldown, dead, now, available=None):
    ready, notes = [], []
    for model in dict.fromkeys(candidates):
        if model in dead or (available is not None and model not in available):
            notes.append(f"{model}: غير متاح")
        elif float(cooldown.get(model, 0)) > now:
            notes.append(f"{model}: في فترة انتظار")
        else:
            ready.append(model)
    return ready, notes
