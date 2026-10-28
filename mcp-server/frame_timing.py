"""Frame-timing tools for the OpenXR Simulator MCP server.

Every number comes from openxr_sim_frametime.exe's headless modes, which run the
same analysis as the frame-timing viewer: an agent and a person looking at the
same capture see the same results.

Frame time here is the time a frame really took (the gap between submissions
minus time the app sat blocked by pacing), judged against the refresh budget:
"over budget" frames would have missed a refresh on a headset, "hitches" cost
at least `hitch_budgets` budgets.
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from atomic_json_file import atomic_json

# The runtime's command folder, overridable like the rest of the MCP server's
# (SIMXR_DATA_DIR); captures stay where the viewer keeps them.
SIMULATOR_DIR = Path(os.environ.get("SIMXR_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", "")) / "OpenXR-Simulator")
CAPTURES_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenXR-Simulator" / "captures"
PERF_MARKER_CMD_FILE = SIMULATOR_DIR / "perf_marker_command.json"
COMMAND_ACK_FILE = SIMULATOR_DIR / "command_ack.json"

# tools/frametime_viewer/capture_file.h: CaptureHeader, 112 bytes.
_CAPTURE_HEADER = struct.Struct("<8s4I3Q3Qd4Q")
assert _CAPTURE_HEADER.size == 112

_recording: dict[str, Any] = {}  # the one capture in progress, if any
_result_counter = 0


def frametime_exe() -> Optional[Path]:
    """The viewer executable: OPENXR_SIM_FRAMETIME_VIEWER, else the repo's bin/."""
    candidates = []
    if os.environ.get("OPENXR_SIM_FRAMETIME_VIEWER"):
        candidates.append(Path(os.environ["OPENXR_SIM_FRAMETIME_VIEWER"]))
    candidates.append(Path(__file__).resolve().parent.parent / "bin" / "openxr_sim_frametime.exe")
    return next((c for c in candidates if c.is_file()), None)


_MISSING_EXE = {"error": "openxr_sim_frametime.exe not found. Build the viewer (cmake --build build "
                         "--target openxr_sim_frametime) or set OPENXR_SIM_FRAMETIME_VIEWER."}


def _run(args: list[str], timeout: float = 120.0) -> dict[str, Any]:
    """Runs a headless mode and returns its JSON result."""
    global _result_counter
    exe = frametime_exe()
    if not exe:
        return dict(_MISSING_EXE)
    SIMULATOR_DIR.mkdir(parents=True, exist_ok=True)
    _result_counter += 1
    result_file = SIMULATOR_DIR / f"perf_result_{os.getpid()}_{_result_counter}.json"
    try:
        proc = subprocess.run([str(exe), *args, "--json", str(result_file)], capture_output=True, timeout=timeout)
        if not result_file.exists():
            return {"error": f"{exe.name} exited with {proc.returncode} and no result"}
        return json.loads(result_file.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        return {"error": f"{exe.name} {args[0]} timed out after {timeout:.0f} s"}
    except json.JSONDecodeError as e:
        return {"error": f"unreadable result from {exe.name}: {e}"}
    finally:
        result_file.unlink(missing_ok=True)


def _scoring(arguments: dict[str, Any]) -> list[str]:
    args = []
    if arguments.get("target_hz"):
        args += ["--hz", str(float(arguments["target_hz"]))]
    args += ["--hitch-budgets", str(float(arguments.get("hitch_budgets", 2.0)))]
    return args


def sessions() -> dict[str, Any]:
    return _run(["--list-sessions"], timeout=30)


def _resolve_pid(pid: Optional[int]) -> tuple[Optional[int], Optional[dict[str, Any]]]:
    """The pid to use, or an error naming the choices when it is ambiguous."""
    found = sessions()
    if "error" in found:
        return None, found
    running = found.get("sessions", [])
    if pid:
        if any(s["pid"] == int(pid) for s in running):
            return int(pid), None
        return None, {"error": f"process {pid} is not publishing a frame timeline", "sessions": running}
    if len(running) == 1:
        return running[0]["pid"], None
    if not running:
        return None, {"error": "no process is running the OpenXR Simulator (or its frame timeline is off)"}
    return None, {"error": "several apps are running the simulator; pass pid", "sessions": running}


def _capture_path(arguments_label: str, process: str) -> Path:
    def clean(text: str) -> str:
        text = re.sub(r"[ .]", "-", text)
        return re.sub(r"[^A-Za-z0-9_-]", "", text)[:48]
    stem = clean(process)
    if stem.endswith("-exe"):
        stem = stem[:-4]
    name = f"{stem or 'capture'}_{datetime.now():%Y-%m-%d_%H-%M-%S}"
    if clean(arguments_label):
        name += "_" + clean(arguments_label)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    # One-second resolution: never reuse a name, or the second capture replaces the first.
    path, n = CAPTURES_DIR / (name + ".xrft"), 2
    while path.exists():
        path, n = CAPTURES_DIR / f"{name}-{n}.xrft", n + 1
    return path


def _resolve_capture(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = CAPTURES_DIR / p
    if p.suffix != ".xrft" and not p.exists():
        p = p.with_suffix(".xrft")
    return p


def live_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    pid, error = _resolve_pid(arguments.get("pid"))
    if error:
        return error
    return _run(["--live-summary", "--pid", str(pid), "--seconds", str(float(arguments.get("seconds", 10.0))),
                 "--top", str(int(arguments.get("top", 10))), *_scoring(arguments)], timeout=60)


def start_capture(arguments: dict[str, Any]) -> dict[str, Any]:
    if _recording and _recording["process"].poll() is None:
        return {"error": "a capture is already recording; stop it first", "path": str(_recording["path"])}
    exe = frametime_exe()
    if not exe:
        return dict(_MISSING_EXE)
    pid, error = _resolve_pid(arguments.get("pid"))
    if error:
        return error
    process = next((s["process"] for s in sessions().get("sessions", []) if s["pid"] == pid), "capture")
    label = str(arguments.get("label", ""))
    path = _capture_path(label, process)
    stop_file = SIMULATOR_DIR / f"perf_stop_{os.getpid()}_{int(time.time())}.flag"
    result_file = SIMULATOR_DIR / f"perf_record_{os.getpid()}_{int(time.time())}.json"
    stop_file.unlink(missing_ok=True)
    max_seconds = float(arguments.get("max_seconds", 600))
    proc = subprocess.Popen([str(exe), "--record", "--pid", str(pid), "--out", str(path), "--label", label,
                             "--stop-file", str(stop_file), "--seconds", str(max_seconds), "--json", str(result_file)])
    _recording.clear()
    _recording.update(process=proc, path=path, stop_file=stop_file, result_file=result_file, pid=pid,
                      label=label, started=time.time())
    return {"recording": True, "pid": pid, "process": process, "path": str(path), "max_seconds": max_seconds,
            "note": "Call perf_stop_capture to finish and get the analysis."}


def stop_capture(arguments: dict[str, Any]) -> dict[str, Any]:
    if not _recording:
        return {"error": "no capture is recording"}
    proc: subprocess.Popen = _recording["process"]
    _recording["stop_file"].touch()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"error": "the recorder did not stop; killed it"}
    result_file: Path = _recording["result_file"]
    try:
        recorded = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
    except json.JSONDecodeError:
        recorded = {}
    finally:
        result_file.unlink(missing_ok=True)
        _recording["stop_file"].unlink(missing_ok=True)
    path = _recording["path"]
    _recording.clear()
    if "error" in recorded or not Path(path).exists():
        return {"error": recorded.get("error", "the recorder wrote no capture"), "path": str(path)}
    summary = analyze_capture({**arguments, "path": str(path)})
    summary["recording"] = recorded
    return summary


def add_marker(arguments: dict[str, Any]) -> dict[str, Any]:
    text = str(arguments.get("text", "")).strip()
    if not text:
        return {"error": "text is required"}
    SIMULATOR_DIR.mkdir(parents=True, exist_ok=True)
    before = COMMAND_ACK_FILE.stat().st_mtime_ns if COMMAND_ACK_FILE.exists() else 0
    atomic_json(PERF_MARKER_CMD_FILE, {"text": text[:79]})
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if COMMAND_ACK_FILE.exists() and COMMAND_ACK_FILE.stat().st_mtime_ns != before:
            try:
                ack = json.loads(COMMAND_ACK_FILE.read_text(encoding="utf-8"))
                if ack.get("command") == "perf_marker":
                    return {"marker": text[:79], "added": bool(ack.get("success"))}
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.05)
    return {"marker": text[:79], "added": False,
            "note": "the runtime did not acknowledge within 3 s (is an app rendering frames?)"}


def list_captures(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = int(arguments.get("limit", 30))
    entries = []
    for path in sorted(CAPTURES_DIR.glob("*.xrft"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        # One damaged or foreign file must not fail the listing.
        try:
            with open(path, "rb") as f:
                head = _CAPTURE_HEADER.unpack(f.read(_CAPTURE_HEADER.size))
                if not head[0].startswith(b"XRFTCAP") or head[7] > (16 << 20):
                    continue
                metadata = json.loads(f.read(head[7]).decode("utf-8")) if head[7] else {}
        except (OSError, struct.error, ValueError, OverflowError, MemoryError):
            continue  # ValueError covers JSON and UTF-8 errors
        if not isinstance(metadata, dict):
            continue
        capture = metadata.get("capture", {})
        entries.append({"path": str(path), "file": path.name, "label": capture.get("label", ""),
                        "process": metadata.get("process", {}).get("name", ""),
                        "started_utc": capture.get("started_utc", ""), "seconds": capture.get("seconds"),
                        "frames": head[5], "recorded_at_hz": head[11]})
    return {"directory": str(CAPTURES_DIR), "captures": entries}


def analyze_capture(arguments: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_capture(str(arguments.get("path", "")))
    if not path.exists():
        return {"error": f"no capture at {path}"}
    return _run(["--analyze", str(path), "--top", str(int(arguments.get("top", 20))), *_scoring(arguments)])


# (key, how to read it from a summary, whether higher is better, and for
# counts what one frame more or less changes it by: smaller changes are noise)
_COMPARED = [
    ("delivered_fps", lambda s: s["delivered_fps"], True, None),
    ("frame_time_p50_ms", lambda s: s["frame_time_ms"]["p50"], False, None),
    ("frame_time_p99_ms", lambda s: s["frame_time_ms"]["p99"], False, None),
    ("frame_time_p999_ms", lambda s: s["frame_time_ms"]["p999"], False, None),
    ("worst_frame_ms", lambda s: s["frame_time_ms"]["max"], False, None),
    ("one_percent_low_fps", lambda s: s["one_percent_low_fps"], True, None),
    ("over_budget_percent", lambda s: s["over_budget"]["percent"], False,
     lambda s: 100.0 / s["frames"] if s["frames"] else 0.0),
    ("hitches_per_minute", lambda s: s["hitches"] * 60.0 / s["seconds"] if s["seconds"] else 0.0, False,
     lambda s: 60.0 / s["seconds"] if s["seconds"] else 0.0),
    ("headroom_p50_ms", lambda s: s["headroom_p50_ms"], True, None),
    ("simulator_p50_ms", lambda s: s["simulator_ms"]["p50"], False, None),
    # Null, so left out, unless both captures timed the GPU.
    ("gpu_p50_ms", lambda s: (s.get("gpu") or {}).get("p50_ms"), False, None),
    ("gpu_p99_ms", lambda s: (s.get("gpu") or {}).get("p99_ms"), False, None),
]


def compare_captures(arguments: dict[str, Any]) -> dict[str, Any]:
    baseline = analyze_capture({**arguments, "path": arguments.get("baseline", ""), "top": 5})
    if "error" in baseline:
        return {"error": "baseline: " + baseline["error"]}
    # Both scored against the same refresh rate: the one asked for, else the baseline's.
    scored = {**arguments, "target_hz": arguments.get("target_hz") or baseline["target_hz"]}
    candidate = analyze_capture({**scored, "path": arguments.get("candidate", ""), "top": 5})
    if "error" in candidate:
        return {"error": "candidate: " + candidate["error"]}
    delta: dict[str, Any] = {}
    better, worse = [], []
    for key, read, higher_is_better, one_event in _COMPARED:
        b, c = read(baseline), read(candidate)
        if b is None or c is None:
            continue
        change = c - b
        noise = max(one_event(candidate), one_event(baseline)) if one_event else 0.0
        significant = abs(change) > max(1e-9, abs(b) * 0.02, noise * 1.0001)
        verdict = None if not significant else ((change > 0) == higher_is_better)
        delta[key] = {"baseline": round(b, 4), "candidate": round(c, 4), "change": round(change, 4),
                      "change_percent": round(100.0 * change / b, 2) if b else None, "better": verdict}
        if verdict is True:
            better.append(key)
        elif verdict is False:
            worse.append(key)
    return {
        "target_hz": candidate["target_hz"],
        "baseline": {"path": baseline["capture"]["path"], "frames": baseline["frames"], "seconds": baseline["seconds"]},
        "candidate": {"path": candidate["capture"]["path"], "frames": candidate["frames"], "seconds": candidate["seconds"]},
        "delta": delta,
        "improved": better,
        "regressed": worse,
        "candidate_worst_frames": candidate.get("worst_frames", [])[:5],
    }


_SCORING_SCHEMA = {
    "target_hz": {"type": "number", "description": "Judge frames against this refresh rate (72, 90, 120, 144...). "
                                                   "Default: the rate the simulator paced at."},
    "hitch_budgets": {"type": "number", "default": 2.0,
                      "description": "A frame costing at least this many refresh budgets is a hitch."},
}

# (name, description, input schema properties, required)
TOOL_SPECS = [
    ("perf_sessions",
     "List the processes running the OpenXR Simulator that publish a frame timeline (pid, process, refresh rate, "
     "frames so far).", {}, []),
    ("perf_live_summary",
     "Frame-timing summary of the last few seconds of a running app: delivered fps, frame-time percentiles, "
     "1% low, frames over the refresh budget, hitches with their likely cause (app busy, app waiting, "
     "simulator, GPU), GPU time per frame when the runtime measures it, the worst frames and recent events.",
     {"pid": {"type": "integer", "description": "Process to look at; optional when only one app is running."},
      "seconds": {"type": "number", "default": 10.0}, "top": {"type": "integer", "default": 10}, **_SCORING_SCHEMA},
     []),
    ("perf_start_capture",
     "Start recording every frame of a running app into a capture file (.xrft) for careful measurement. "
     "Stop with perf_stop_capture, which returns the analysis.",
     {"label": {"type": "string", "description": "Short name for the capture, e.g. 'forest-walk-before-fix'."},
      "pid": {"type": "integer", "description": "Optional when only one app is running."},
      "max_seconds": {"type": "number", "default": 600}}, []),
    ("perf_stop_capture",
     "Stop the capture started by perf_start_capture, save it, and return its full frame-timing analysis.",
     {"top": {"type": "integer", "default": 10}, **_SCORING_SCHEMA}, []),
    ("perf_add_marker",
     "Drop a named marker into the running app's frame timeline (e.g. 'enter combat') so captures and the "
     "viewer show where it happened.",
     {"text": {"type": "string", "description": "Up to 79 characters."}}, ["text"]),
    ("perf_list_captures",
     "List saved frame-timing captures, newest first.",
     {"limit": {"type": "integer", "default": 30}}, []),
    ("perf_analyze_capture",
     "Full frame-timing analysis of a saved capture, optionally re-scored against another refresh rate.",
     {"path": {"type": "string", "description": "Capture path, or a file name in the captures folder."},
      "top": {"type": "integer", "default": 20}, **_SCORING_SCHEMA}, ["path"]),
    ("perf_compare_captures",
     "Compare two captures scored against the same refresh rate (A/B a change): each key metric's baseline, "
     "candidate and change, and which improved or regressed.",
     {"baseline": {"type": "string"}, "candidate": {"type": "string"}, **_SCORING_SCHEMA},
     ["baseline", "candidate"]),
]

_HANDLERS = {
    "perf_sessions": lambda a: sessions(),
    "perf_live_summary": live_summary,
    "perf_start_capture": start_capture,
    "perf_stop_capture": stop_capture,
    "perf_add_marker": add_marker,
    "perf_list_captures": list_captures,
    "perf_analyze_capture": analyze_capture,
    "perf_compare_captures": compare_captures,
}


def dispatch(name: str, arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The tool's result, or None if `name` is not a frame-timing tool."""
    handler = _HANDLERS.get(name)
    return handler(arguments or {}) if handler else None
