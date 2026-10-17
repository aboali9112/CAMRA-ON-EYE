#!/usr/bin/env python3
"""Hikvision V18: isolated preview by default; explicit --live for camera operation."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import webbrowser
import importlib.util
import importlib.metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check_installation(data_dir):
    """Local checks only; do not load models, open cameras or disclose credentials."""
    versions = {}
    missing = []
    for module, distribution in (("flask", "Flask"), ("requests", "requests"), ("numpy", "numpy"), ("cv2", "opencv-python")):
        installed = importlib.util.find_spec(module) is not None
        if not installed:
            missing.append(module)
        try:
            versions[module] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[module] = "installed" if installed else "missing"
    config_file = Path(data_dir) / "config.json"
    count, config_ok = 0, False
    try:
        config = json.loads(config_file.read_text(encoding="utf-8-sig"))
        cameras = config.get("cameras", [])
        count = len(cameras)
        config_ok = isinstance(cameras, list) and 0 < count <= 32
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    binaries = {name: (Path(data_dir)/"bin"/name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe", "go2rtc.exe")}
    search_dirs = [ROOT, Path(data_dir), ROOT / "models", Path(data_dir) / "models"]
    models = [p.name for directory in search_dirs if directory.is_dir() for p in directory.glob("*.pt")]
    models += [p.name for directory in search_dirs if directory.is_dir() for p in directory.glob("*.onnx")]
    plate_reader = importlib.util.find_spec("fast_alpr") is not None
    lpr_active = False
    try:
        if isinstance(config, dict):
            for c in config.get("cameras", []):
                if isinstance(c, dict):
                    an = c.get("analysis") if isinstance(c.get("analysis"), dict) else {}
                    if c.get("lpr_enabled") is True or str(c.get("lpr_enabled")).lower() in ("true", "1") or an.get("lpr_enabled") is True or str(an.get("lpr_enabled")).lower() in ("true", "1"):
                        lpr_active = True
                        break
    except Exception:
        pass
    warnings = []
    if not all(binaries.values()):
        warnings.append("Some recording/stream tools are missing; check binaries below.")
    if not models:
        warnings.append("No detection model files found in root or models/ directories.")
    if lpr_active and not plate_reader:
        warnings.append("fast-alpr is missing but LPR is enabled in camera configuration; local plate recognition is unavailable.")
    binaries_ready = all(binaries.values())
    plate_ok = plate_reader if lpr_active else True
    local_dependencies_ready = (not missing) and config_ok and binaries_ready and bool(models) and plate_ok
    return dict(version="18.1", python=sys.executable, packages=versions, missing_packages=missing,
                config_found=config_file.is_file(), camera_count=count, config_ok=config_ok,
                binaries=binaries, binaries_ready=binaries_ready, models=sorted(set(models)),
                plate_reader_installed=plate_reader, lpr_enabled_in_config=lpr_active,
                core_dependencies_ready=not missing,
                local_dependencies_ready=local_dependencies_ready,
                validation_scope="local_files_only", warnings=warnings,
                camera_connectivity_tested=False, model_inference_tested=False)

def import_v17(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    app_source = source.parent if source.name == "cctv_agent_data" else source
    if (source / "cctv_agent_data").is_dir():
        source = source / "cctv_agent_data"
    if source == destination or source in destination.parents:
        raise ValueError("V18 data must be separate from V17")
    if (destination / "config.json").exists():
        raise ValueError("V18 config already exists; import will not overwrite it")
    config = json.loads((source / "config.json").read_text(encoding="utf-8-sig"))
    for camera in config.get("cameras", []):
        camera.setdefault("analysis_stream", "sub")
        analysis = camera.setdefault("analysis", {})
        enabled = camera.get("lpr_enabled", analysis.get("lpr_enabled", False))
        enabled = enabled is True or str(enabled).lower() in ("true", "1", "yes")
        camera["lpr_enabled"] = analysis["lpr_enabled"] = enabled
    config["config_version"] = 18
    config["go2rtc_url"] = "http://127.0.0.1:1985"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("bin", "static", "models"):
        if (source/name).is_dir():
            shutil.copytree(source/name, destination/name, dirs_exist_ok=True)
    for pattern in ("*.pt", "*.onnx", "*.names", "*.yaml"):
        for model in source.glob(pattern):
            if model.name != "go2rtc.yaml":
                shutil.copy2(model, destination/model.name)
    for pattern in ("*.pt", "*.onnx"):
        for model in app_source.glob(pattern):
            shutil.copy2(model, destination/model.name)
    (destination/"config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    parser = argparse.ArgumentParser(description="Hikvision V18")
    parser.add_argument("--live", action="store_true", help="Enable cameras and configured notifications")
    parser.add_argument("--port", type=int, default=5051)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--import-v17", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check this Python environment and live configuration without network access")
    args = parser.parse_args()
    args.data_dir = args.data_dir or ROOT / ("cctv_v18_data" if args.live or args.import_v17 or args.check else "preview_data")
    if args.check:
        report = check_installation(args.data_dir)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if (report["local_dependencies_ready"] and not report["warnings"]) else 1
    if args.selftest:
        with tempfile.TemporaryDirectory(prefix="cctv-v18-selftest-") as isolated:
            os.environ["CCTV_DATA_DIR"] = isolated
            os.environ["CCTV_PREVIEW"] = "1"
            from cctv.selftest import _self_test_classifier
            _self_test_classifier()
        return 0
    if args.data_dir.resolve().name == "cctv_agent_data":
        parser.error("Choose a separate V18 data folder, not cctv_agent_data")
    if args.import_v17:
        import_v17(args.import_v17, args.data_dir)
        print("✓ تم استيراد إعدادات V17 بنجاح إلى مجلد V18 المنفصل.")
        print("✓ للتشغيل المباشر الفعلي مع الكاميرات، شغّل START_LIVE.cmd")
        return 0
    if args.live and not (args.data_dir / "config.json").is_file():
        parser.error('Live config is missing. Run IMPORT_V17.cmd first, or --import-v17 "path-to-V17".')
    os.environ["CCTV_DATA_DIR"] = str(args.data_dir.resolve())
    os.environ["CCTV_PREVIEW"] = "0" if args.live else "1"
    from cctv import web
    from werkzeug.serving import make_server
    # Bind before starting any camera workers, preventing duplicate launches.
    server = make_server("127.0.0.1", args.port, web.app, threaded=True)
    try:
        engine = web.create_engine(auto_start=args.live)
        url = f"http://127.0.0.1:{args.port}"
        print(f"Hikvision V18.1 | {'LIVE' if args.live else 'PREVIEW'} | {url}", flush=True)
        if not args.no_browser:
            threading.Timer(1, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if web.engine is not None:
            web.engine.stop_all()
            web.engine.go2rtc.stop()
        server.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
