"""Checks the frame-timing pipeline against ground truth.

Runs probe/xr_probe.exe with a load of known shape - fixed work per frame plus a
hitch of known size every N frames - records it through the same calls the MCP
tools make (mcp-server/frame_timing.py), and asserts the analysis finds exactly
what was injected. A second run with more work per frame checks the A/B
comparison, and two more load the GPU instead (buffer copies, sized by a
calibration run) to check GPU timing and blame. Exits non-zero on any mismatch.

    python scripts/verify_frame_timing.py [--runtime-json PATH] [--seconds 8]

Needs a built runtime (--runtime-json, default bin/openxr_simulator.json), the
viewer (bin/openxr_sim_frametime.exe) and the probe (bin/xr_ui_probe.exe from the
CMake build, else probe/xr_probe.exe; --probe names another).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp-server"))
import frame_timing as ft  # noqa: E402

HITCH_EVERY = 90
HITCH_MS = 25.0
GPU_CALIBRATION_COPIES = 40
PROBE: Path = ROOT / "bin" / "xr_ui_probe.exe"
failures: list[str] = []


def check(condition: bool, what: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + what)
    if not condition:
        failures.append(what)


def run_probe(runtime_json: str, work_ms: float, frames: int, extra: dict | None = None) -> subprocess.Popen:
    env = dict(os.environ, XR_RUNTIME_JSON=runtime_json, XR_PROBE_FRAMES=str(frames), XR_PROBE_PROJECTION_ONLY="1",
               XR_PROBE_WORK_MS=str(work_ms), XR_PROBE_HITCH_EVERY=str(HITCH_EVERY), XR_PROBE_HITCH_MS=str(HITCH_MS))
    # The CMake probe loads the runtime DLL itself; the loader-based one reads the manifest.
    library = json.loads(Path(runtime_json).read_text(encoding="utf-8"))["runtime"]["library_path"]
    env["SIMXR_RUNTIME_DLL"] = str((Path(runtime_json).parent / library).resolve())
    env.update(extra or {})
    return subprocess.Popen([str(PROBE)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_session(pid: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(s["pid"] == pid and s["frames"] > 200 for s in ft.sessions().get("sessions", [])):
            return True
        time.sleep(0.5)
    return False


def record(runtime_json: str, work_ms: float, label: str, seconds: float, marker: str | None,
           extra: dict | None = None) -> dict:
    probe = run_probe(runtime_json, work_ms, frames=int((seconds + 15) * 90), extra=extra)
    try:
        if not wait_for_session(probe.pid):
            raise SystemExit(f"probe {probe.pid} never published a frame timeline")
        started = ft.start_capture({"label": label, "pid": probe.pid})
        if "error" in started:
            raise SystemExit(f"start_capture: {started}")
        time.sleep(seconds / 2)
        if marker:
            added = ft.add_marker({"text": marker})
            check(added.get("added") is True, f"marker '{marker}' acknowledged by the runtime")
        time.sleep(seconds / 2)
        summary = ft.stop_capture({"top": 40})
        if "error" in summary:
            raise SystemExit(f"stop_capture: {summary}")
        return summary
    finally:
        probe.kill()


def hitch_frames(summary: dict) -> list[dict]:
    return [f for f in summary["worst_frames"] if f["class"] == "hitch"]


def gpu_checks(args: argparse.Namespace, a: dict) -> None:
    """GPU-bound runs: 64 MB buffer copies queued every frame and never waited
    for, sized from a calibration run so the checks hold on any GPU."""
    budget = a["budget_ms"]
    base = a.get("gpu") or {"api": None, "frames": 0, "p50_ms": 0.0}
    check(base["frames"] >= 0.95 * a["frames"], f"run A has a GPU time for every frame ({base['api']}, {base['frames']} frames)")
    if not base["frames"]:
        return
    no_cpu_hitch = {"XR_PROBE_HITCH_MS": "0"}
    print(f"Run C: calibrating the GPU load with {GPU_CALIBRATION_COPIES} copies per frame")
    cal = record(args.runtime_json, 0.0, "verify-gpu-calibrate", args.seconds / 2, marker=None,
                 extra=dict(no_cpu_hitch, XR_PROBE_GPU_COPIES=str(GPU_CALIBRATION_COPIES)))
    per_copy = (cal["gpu"]["p50_ms"] - base["p50_ms"]) / GPU_CALIBRATION_COPIES
    check(per_copy > 0.0, f"the copies show up as GPU time ({per_copy * 1000:.1f} us each)")
    if per_copy <= 0.0:
        return
    copies = max(1, round((1.25 * budget - base["p50_ms"]) / per_copy))
    spike = max(1, round(2.0 * budget / per_copy))
    print(f"Run D: {copies} copies per frame (~{1.25 * budget:.1f} ms on the GPU), "
          f"{spike} more every {HITCH_EVERY} frames (~{spike * per_copy:.1f} ms)")
    d = record(args.runtime_json, 0.0, "verify-gpu", args.seconds, marker=None,
               extra=dict(no_cpu_hitch, XR_PROBE_GPU_COPIES=str(copies), XR_PROBE_GPU_HITCH_COPIES=str(spike)))
    frames = d["frames"]
    late = d["over_budget"]["frames"]
    print(f"  {frames} frames, GPU p50 {d['gpu']['p50_ms']:.2f} ms, {late} over budget, {d['hitches']} hitches")
    check(late >= 0.9 * frames and d["late_causes"]["gpu"] >= 0.9 * late,
          f"GPU-bound frames are late and blamed on the GPU ({d['late_causes']['gpu']} of {late})")
    expected = frames / HITCH_EVERY
    check(abs(d["hitches"] - expected) <= 1.0,
          f"each GPU spike counts once ({d['hitches']} hitches, expected about {expected:.1f})")
    hitches = hitch_frames(d)
    check(bool(hitches) and all(h["cause"] == "GPU" and h["cpu_ms"] < budget for h in hitches),
          "every hitch is the GPU's, with the CPU inside its budget")
    excess = [h["gpu_ms"] - d["gpu"]["p50_ms"] for h in hitches]
    injected = spike * per_copy
    check(bool(excess) and all(abs(e - injected) <= 0.2 * injected for e in excess),
          f"every GPU hitch costs the injected ~{injected:.1f} ms more GPU time "
          f"(measured {min(excess, default=0):.2f} .. {max(excess, default=0):.2f} ms)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-json", default=str(ROOT / "bin" / "openxr_simulator.json"))
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--probe", help="probe executable (default: bin/xr_ui_probe.exe, else probe/xr_probe.exe)")
    args = parser.parse_args()
    if not ft.frametime_exe():
        raise SystemExit("bin/openxr_sim_frametime.exe is missing; build the viewer first")
    global PROBE
    candidates = [Path(args.probe)] if args.probe else [ROOT / "bin" / "xr_ui_probe.exe", ROOT / "probe" / "xr_probe.exe"]
    PROBE = next((c for c in candidates if c.is_file()), None)
    if not PROBE:
        raise SystemExit("no probe: build xr_ui_probe (CMake) or pass --probe")

    print(f"Run A: {3.0} ms of work per frame, a {HITCH_MS:g} ms hitch every {HITCH_EVERY} frames")
    a = record(args.runtime_json, 3.0, "verify-a", args.seconds, marker="halfway")
    frames = a["frames"]
    path = a["capture"]["path"]
    csv_result = ft._run(["--export-csv", path])
    with open(csv_result["path"], newline="") as f:
        rows = list(csv.DictReader(f))
    Path(csv_result["path"]).unlink(missing_ok=True)
    # The probe spins in frame f when f % HITCH_EVERY == HITCH_EVERY - 1; frame ids
    # count from 1. The capture's first row has no interval, so it cannot be scored.
    injected = [r for r in rows[1:] if int(r["frame"]) % HITCH_EVERY == 0]
    others = [r for r in rows if r["class"] == "hitch" and r not in injected]
    print(f"  {frames} frames, {a['hitches']} hitches ({len(injected)} injected), "
          f"p50 {a['frame_time_ms']['p50']:.2f} ms")
    check(bool(injected) and all(r["class"] == "hitch" for r in injected), "every injected spike is a hitch")
    check(all(r["cause"] == "app busy" for r in injected), "and is blamed on the app being busy")
    # Another program's GPU work can stall even this light load; anything else
    # late enough to be a hitch is a fault in the runtime or the analysis.
    stray = [r for r in others if r["cause"] != "GPU"]
    check(not stray, f"no other hitches but GPU interference ({len(others)} other, "
                     f"{len(others) - len(stray)} from the GPU)")
    # The probe's own per-frame overhead varies by a few ms (2-6 ms measured), so
    # a hitch frame's excess over the median carries that noise on top of HITCH_MS.
    excess = [float(r["frame_time_ms"]) - a["frame_time_ms"]["p50"] for r in injected]
    check(bool(excess) and all(abs(e - HITCH_MS) <= 3.0 for e in excess),
          f"every injected spike costs {HITCH_MS:g} ms over a normal frame "
          f"(measured {min(excess, default=0):.2f} .. {max(excess, default=0):.2f} ms)")
    check(all(int(r["missed_refreshes"]) >= 2 for r in injected), "each misses at least two refreshes")
    check(any(m["kind"] == "user" and m["text"] == "halfway" for m in a["markers"]), "the marker is in the capture")
    session = a["capture"]["metadata"]["session"]
    check(bool(session.get("gpu", {}).get("name")), f"session info names the GPU ({session.get('gpu', {}).get('name')})")
    check(session.get("app_swapchain", {}).get("width", 0) > 0, "session info has the app's swapchain")
    check(a["capture"]["metadata"]["capture"]["lost_frames"] == 0, "the recorder lost no frames")

    faster = ft.analyze_capture({"path": path, "target_hz": 120})
    check(faster["target_hz"] == 120 and faster["over_budget"]["frames"] >= a["over_budget"]["frames"],
          f"re-scored at 120 Hz: {faster['over_budget']['frames']} late frames vs {a['over_budget']['frames']} at 90 Hz")
    # The summary leaves out the capture's first frame (it has no interval); the CSV keeps it.
    check(len(rows) == frames + 1, f"CSV has a row per recorded frame ({len(rows)} rows)")
    check(sum(1 for r in rows if r["class"] == "hitch") == a["hitches"], "CSV classes agree with the summary")

    print("Run B: 3 ms more work per frame")
    b = record(args.runtime_json, 6.0, "verify-b", args.seconds, marker=None)
    compared = ft.compare_captures({"baseline": path, "candidate": b["capture"]["path"]})
    p50 = compared["delta"]["frame_time_p50_ms"]
    print(f"  p50 {p50['baseline']:.2f} -> {p50['candidate']:.2f} ms")
    check(abs(p50["change"] - 3.0) <= 0.75, "the comparison sees the extra 3 ms per frame")
    check("frame_time_p50_ms" in compared["regressed"], "and reports it as a regression")

    gpu_checks(args, a)

    print()
    print("All checks passed." if not failures else f"{len(failures)} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
