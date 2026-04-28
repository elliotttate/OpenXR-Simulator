#!/usr/bin/env python3
"""
OpenXR Simulator MCP Server

This MCP server provides tools for diagnosing OpenXR issues and capturing
screenshots from the OpenXR Simulator runtime.

Features:
- Capture screenshots of current XR frames
- Read and analyze simulator logs
- Get frame timing and diagnostic information
- Monitor session state and head tracking
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        ImageContent,
        EmbeddedResource,
    )
except ImportError:
    print("Error: MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)


# Configuration
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
SIMULATOR_DIR = Path(LOCALAPPDATA) / "OpenXR-Simulator"
LOG_FILE = SIMULATOR_DIR / "openxr_simulator.log"
SCREENSHOT_REQUEST_FILE = SIMULATOR_DIR / "screenshot_request.json"
# Runtime may write any of these formats — we'll pick whichever shows up most recently after the request.
SCREENSHOT_OUTPUT_CANDIDATES = (
    SIMULATOR_DIR / "screenshot.png",
    SIMULATOR_DIR / "screenshot.bmp",
    SIMULATOR_DIR / "screenshot.jpg",
    SIMULATOR_DIR / "screenshot.jpeg",
)
# Max width in pixels for the returned JPEG. The runtime renders at full stereo resolution
# (often 2560+ wide), which makes the base64 payload too big for the API. 1280 keeps the
# image readable while staying small.
SCREENSHOT_MAX_WIDTH = 1280
SCREENSHOT_JPEG_QUALITY = 70
FRAME_INFO_FILE = SIMULATOR_DIR / "frame_info.json"
STATUS_FILE = SIMULATOR_DIR / "runtime_status.json"

# Diagnostic command files (the simulator polls for and consumes these).
HEAD_POSE_CMD_FILE       = SIMULATOR_DIR / "head_pose_command.json"
FOV_CMD_FILE             = SIMULATOR_DIR / "fov_command.json"
IPD_CMD_FILE             = SIMULATOR_DIR / "ipd_command.json"
HEADSET_PROFILE_CMD_FILE = SIMULATOR_DIR / "headset_profile_command.json"
ANAGLYPH_CMD_FILE        = SIMULATOR_DIR / "anaglyph_command.json"
PROJ_LOG_DUMP_REQUEST    = SIMULATOR_DIR / "projection_log_dump_request"
PROJ_LOG_FILE            = SIMULATOR_DIR / "projection_log.json"

import math as _math

# Create MCP server
server = Server("openxr-simulator")


def ensure_simulator_dir():
    """Ensure the simulator directory exists."""
    SIMULATOR_DIR.mkdir(parents=True, exist_ok=True)


def read_log_file(lines: int = 100, filter_pattern: Optional[str] = None) -> str:
    """Read the last N lines from the simulator log file."""
    if not LOG_FILE.exists():
        return "Log file not found. The OpenXR Simulator may not have been run yet."

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        # Get last N lines
        recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        # Apply filter if specified
        if filter_pattern:
            pattern = re.compile(filter_pattern, re.IGNORECASE)
            recent_lines = [line for line in recent_lines if pattern.search(line)]

        return "".join(recent_lines)
    except Exception as e:
        return f"Error reading log file: {e}"


def parse_log_for_diagnostics() -> dict[str, Any]:
    """Parse the log file to extract diagnostic information."""
    diagnostics = {
        "session_state": "Unknown",
        "frame_count": 0,
        "swapchain_info": [],
        "errors": [],
        "warnings": [],
        "last_activity": None,
        "graphics_api": "Unknown",
        "head_tracking": {"position": None, "orientation": None}
    }

    if not LOG_FILE.exists():
        return diagnostics

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Extract session state
        state_matches = re.findall(r"session state -> (\w+)", content)
        if state_matches:
            diagnostics["session_state"] = state_matches[-1]

        # Extract frame count
        frame_matches = re.findall(r"xrEndFrame called \(frame #(\d+)\)", content)
        if frame_matches:
            diagnostics["frame_count"] = int(frame_matches[-1])

        # Extract swapchain info
        swapchain_matches = re.findall(
            r"xrCreateSwapchain.*?format=(\w+).*?(\d+)x(\d+)",
            content
        )
        for match in swapchain_matches:
            diagnostics["swapchain_info"].append({
                "format": match[0],
                "width": int(match[1]),
                "height": int(match[2])
            })

        # Extract errors
        error_matches = re.findall(r"\[SimXR\].*?ERROR.*", content, re.IGNORECASE)
        diagnostics["errors"] = error_matches[-10:]  # Last 10 errors

        # Extract warnings
        warning_matches = re.findall(r"\[SimXR\].*?WARNING.*", content, re.IGNORECASE)
        diagnostics["warnings"] = warning_matches[-10:]  # Last 10 warnings

        # Extract graphics API
        if "D3D12" in content:
            diagnostics["graphics_api"] = "D3D12"
        elif "D3D11" in content:
            diagnostics["graphics_api"] = "D3D11"

        # Get file modification time as last activity
        diagnostics["last_activity"] = datetime.fromtimestamp(
            LOG_FILE.stat().st_mtime
        ).isoformat()

    except Exception as e:
        diagnostics["parse_error"] = str(e)

    return diagnostics


def request_screenshot(eye: str = "both", include_ui: bool = True) -> dict[str, Any]:
    """
    Request a screenshot from the runtime.

    The runtime watches for the screenshot_request.json file and captures
    the frame when it sees it.
    """
    ensure_simulator_dir()

    request = {
        "timestamp": time.time(),
        "eye": eye,  # "left", "right", or "both"
        "include_ui": include_ui,
        "requested_by": "mcp"
    }

    try:
        # Write the request
        with open(SCREENSHOT_REQUEST_FILE, "w") as f:
            json.dump(request, f)

        return {"status": "requested", "request": request}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def wait_for_screenshot(timeout: float = 5.0, request_time: float = 0.0) -> Optional[bytes]:
    """Wait for a screenshot file to be created and return its raw bytes.

    Accepts any of the candidate formats. Skips files written before request_time
    so we don't return a stale capture from a previous run.
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        for path in SCREENSHOT_OUTPUT_CANDIDATES:
            if not path.exists():
                continue
            try:
                # Skip stale files (written before this request)
                if request_time and path.stat().st_mtime < request_time - 0.5:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                # Clean up — remove all candidate files plus the request marker
                for p in SCREENSHOT_OUTPUT_CANDIDATES:
                    p.unlink(missing_ok=True)
                SCREENSHOT_REQUEST_FILE.unlink(missing_ok=True)
                return data
            except Exception:
                pass
        time.sleep(0.1)

    return None


def to_jpeg(image_bytes: bytes, max_width: int = SCREENSHOT_MAX_WIDTH,
            quality: int = SCREENSHOT_JPEG_QUALITY) -> bytes:
    """Decode an image (PNG/BMP/JPEG) and re-encode as a downscaled JPEG.

    Forces JPEG output because the Anthropic API rejects some PNG/BMP payloads
    from this MCP, and JPEG keeps the base64 payload small enough to ship reliably.
    """
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    # JPEG can't store alpha — convert RGBA/P/etc. to RGB
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_width:
        new_h = int(img.height * (max_width / img.width))
        img = img.resize((max_width, new_h), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def get_frame_info() -> dict[str, Any]:
    """Get current frame information from the runtime status file."""
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            return {"error": f"Failed to read status: {e}"}

    # Fall back to parsing from log
    return parse_log_for_diagnostics()


def analyze_openxr_issue(symptoms: str) -> dict[str, Any]:
    """Analyze potential OpenXR issues based on symptoms and log data."""
    diagnostics = parse_log_for_diagnostics()
    logs = read_log_file(lines=200)

    analysis = {
        "symptoms": symptoms,
        "diagnostics": diagnostics,
        "potential_issues": [],
        "recommendations": []
    }

    symptoms_lower = symptoms.lower()

    # Check for common issues
    if "black" in symptoms_lower or "blank" in symptoms_lower:
        analysis["potential_issues"].append("Blank/black screen in VR view")
        if diagnostics["frame_count"] == 0:
            analysis["recommendations"].append(
                "No frames have been submitted. Check if xrEndFrame is being called."
            )
        if not diagnostics["swapchain_info"]:
            analysis["recommendations"].append(
                "No swapchains created. Verify xrCreateSwapchain is successful."
            )
        analysis["recommendations"].append(
            "Check that projection layers are being submitted in xrEndFrame."
        )

    if "crash" in symptoms_lower or "error" in symptoms_lower:
        analysis["potential_issues"].append("Runtime errors or crashes")
        if diagnostics["errors"]:
            analysis["recommendations"].append(
                f"Found {len(diagnostics['errors'])} errors in log. Review them below."
            )
            analysis["recent_errors"] = diagnostics["errors"]

    if "tracking" in symptoms_lower or "position" in symptoms_lower:
        analysis["potential_issues"].append("Head tracking issues")
        analysis["recommendations"].append(
            "The simulator uses mouse for head rotation and WASD for movement. "
            "Click the preview window to capture mouse."
        )

    if "performance" in symptoms_lower or "lag" in symptoms_lower or "slow" in symptoms_lower:
        analysis["potential_issues"].append("Performance issues")
        analysis["recommendations"].append(
            "Check frame timing - the simulator targets 90Hz (11.1ms per frame)."
        )
        if diagnostics["graphics_api"] == "D3D12":
            analysis["recommendations"].append(
                "D3D12 path is in use. Complex command list management may cause overhead."
            )

    if "format" in symptoms_lower or "color" in symptoms_lower:
        analysis["potential_issues"].append("Texture format issues")
        if diagnostics["swapchain_info"]:
            analysis["recommendations"].append(
                f"Swapchain formats in use: {[s['format'] for s in diagnostics['swapchain_info']]}"
            )
        analysis["recommendations"].append(
            "Verify sRGB vs linear color space handling is correct."
        )

    # Add log excerpt for context
    if "error" in logs.lower() or "warning" in logs.lower():
        analysis["log_excerpt"] = read_log_file(lines=50, filter_pattern=r"(error|warning|fail)")

    return analysis


def _write_json_command(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a JSON command file the simulator polls for."""
    ensure_simulator_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    tmp.replace(path)


def _deg(rad: float) -> float:
    return rad * 180.0 / _math.pi


def _rad(deg: float) -> float:
    return deg * _math.pi / 180.0


def _validate_stereo_from_screenshot(image_bytes: bytes) -> dict[str, Any]:
    """Crude horizontal-disparity check on a side-by-side stereo screenshot.

    We don't depend on OpenCV; just split the image down the middle and run
    a horizontal-shift template-match on a center-cropped window. Returns
    diagnostics (pixel disparity, recommended pass/fail thresholds, plus
    an IPD sanity estimate).
    """
    from io import BytesIO
    from PIL import Image, ImageOps
    img = Image.open(BytesIO(image_bytes)).convert("L")  # grayscale for matching
    w, h = img.size
    # Heuristic: simulator preview is side-by-side at the top, but on
    # FORCE_STEREO games the simulator outputs the eye halves stacked.
    # Try both layouts and pick the one with the better correlation.
    half_w, half_h = w // 2, h // 2
    layouts = [
        ("side_by_side", img.crop((0, 0, half_w, h)),  img.crop((half_w, 0, w, h))),
        ("over_under",   img.crop((0, 0, w,      half_h)), img.crop((0, half_h, w, h))),
    ]

    def _shift_search(left, right, max_shift):
        """Return (best_dx, score) where dx<0 means right shifted left."""
        import numpy as np
        L = np.asarray(left,  dtype=np.float32)
        R = np.asarray(right, dtype=np.float32)
        # Crop a center window for matching to avoid edge artifacts.
        ch, cw = L.shape
        win_h, win_w = ch // 2, cw // 2
        y0, y1 = (ch - win_h) // 2, (ch - win_h) // 2 + win_h
        x0, x1 = (cw - win_w) // 2, (cw - win_w) // 2 + win_w
        L_win = L[y0:y1, x0:x1]
        best_dx, best_score = 0, float("inf")
        for dx in range(-max_shift, max_shift + 1):
            xa, xb = x0 + dx, x1 + dx
            if xa < 0 or xb > cw: continue
            R_win = R[y0:y1, xa:xb]
            if R_win.shape != L_win.shape: continue
            diff = np.mean((L_win - R_win) ** 2)
            if diff < best_score:
                best_score, best_dx = diff, dx
        return best_dx, best_score

    try:
        import numpy as np  # noqa: F401
    except ImportError:
        return {"error": "numpy required for validate_stereo (pip install numpy)"}

    results = {}
    for name, L, R in layouts:
        if L.size != R.size:  # uneven crop — skip
            continue
        dx, score = _shift_search(L, R, max_shift=min(60, L.size[0] // 6))
        results[name] = {"disparity_px": dx, "score": float(score)}

    # Pick the layout with the lowest score (most-confident match).
    if not results:
        return {"error": "Could not split image into eye halves"}
    best_layout = min(results, key=lambda k: results[k]["score"])
    dx = results[best_layout]["disparity_px"]
    layout_w = (w // 2) if best_layout == "side_by_side" else w

    # Heuristic verdict:
    #  - |dx| within [2, 30] px on a 1280-wide eye -> typical IPD parallax (PASS)
    #  - |dx| < 2  -> eyes nearly identical (FAIL: no IPD or aliased eyes)
    #  - |dx| > 30 -> excessive parallax (FAIL: IPD too large or wrong)
    expected_min = max(2,  layout_w // 640)   # ~2 px on 1280-wide
    expected_max = max(30, layout_w // 40)    # ~30 px on 1280-wide
    if abs(dx) < expected_min:
        verdict = "FAIL_NO_PARALLAX"
        diagnosis = ("Both eye images look identical (disparity below noise floor). "
                     "Likely cause: aliased per-eye matrix, IPD=0, or projection "
                     "matrix not differentiating eyes.")
    elif abs(dx) > expected_max:
        verdict = "FAIL_EXCESSIVE_PARALLAX"
        diagnosis = ("Disparity is much larger than expected for typical IPD. "
                     "Likely cause: IPD applied as raw OpenXR LOCAL-space position, "
                     "or per-eye view matrix mis-translated.")
    else:
        verdict = "PASS"
        diagnosis = "Stereo disparity is within expected range for human IPD."

    return {
        "verdict": verdict,
        "diagnosis": diagnosis,
        "best_layout": best_layout,
        "horizontal_disparity_px": dx,
        "expected_range_px": [expected_min, expected_max],
        "all_layouts": results,
    }


# Define MCP tools
@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="capture_screenshot",
            description="Capture a screenshot of the current OpenXR frame being rendered. "
                       "Returns the stereo view (left and right eye) as displayed in the preview window.",
            inputSchema={
                "type": "object",
                "properties": {
                    "eye": {
                        "type": "string",
                        "enum": ["both", "left", "right"],
                        "default": "both",
                        "description": "Which eye view to capture"
                    },
                    "timeout": {
                        "type": "number",
                        "default": 5.0,
                        "description": "Timeout in seconds to wait for screenshot"
                    }
                }
            }
        ),
        Tool(
            name="get_frame_info",
            description="Get detailed information about the current frame including timing, "
                       "resolution, format, head tracking state, and session state.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="read_logs",
            description="Read recent entries from the OpenXR Simulator log file. "
                       "Useful for debugging issues and understanding runtime behavior.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "default": 100,
                        "description": "Number of recent log lines to read"
                    },
                    "filter": {
                        "type": "string",
                        "description": "Optional regex pattern to filter log lines"
                    }
                }
            }
        ),
        Tool(
            name="get_diagnostics",
            description="Get comprehensive diagnostic information about the OpenXR Simulator "
                       "including session state, frame count, swapchain info, and any errors.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="diagnose_issue",
            description="Analyze potential OpenXR issues based on described symptoms. "
                       "Provides potential causes and recommendations based on log analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symptoms": {
                        "type": "string",
                        "description": "Description of the issue or symptoms you're experiencing"
                    }
                },
                "required": ["symptoms"]
            }
        ),
        Tool(
            name="get_session_state",
            description="Get the current OpenXR session state (IDLE, READY, SYNCHRONIZED, "
                       "VISIBLE, FOCUSED, STOPPING, EXITING).",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_head_tracking",
            description="Get the current head tracking state including position and orientation.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="clear_logs",
            description="Clear the OpenXR Simulator log file to start fresh.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="set_head_pose",
            description=("Set the simulator's head pose. Use a non-identity pose "
                         "(yaw/pitch/roll != 0) to surface coordinate-system "
                         "/ quaternion-handedness bugs that an identity head "
                         "pose would hide. Roll is especially valuable for "
                         "catching axis-flip bugs."),
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "number", "default": 0.0, "description": "Head X position (meters)"},
                    "y": {"type": "number", "default": 1.7, "description": "Head Y position (meters)"},
                    "z": {"type": "number", "default": 0.0, "description": "Head Z position (meters)"},
                    "yaw_deg":   {"type": "number", "default": 0.0, "description": "Yaw in degrees (left/right)"},
                    "pitch_deg": {"type": "number", "default": 0.0, "description": "Pitch in degrees (up/down)"},
                    "roll_deg":  {"type": "number", "description": "Roll in degrees (head tilt). Omit to leave unchanged."},
                }
            }
        ),
        Tool(
            name="set_fov",
            description=("Override the per-eye OpenXR FOV with asymmetric values "
                         "(degrees). The simulator's default is symmetric, which "
                         "hides projection-matrix bugs that show up against a real "
                         "headset's asymmetric lens FOV. Pass {\"clear\": true} to "
                         "revert to the symmetric default."),
            inputSchema={
                "type": "object",
                "properties": {
                    "clear": {"type": "boolean", "default": False},
                    "left_eye": {
                        "type": "object",
                        "description": "Left eye FOV (degrees, +/- around forward)",
                        "properties": {
                            "left_deg":  {"type": "number"},
                            "right_deg": {"type": "number"},
                            "up_deg":    {"type": "number"},
                            "down_deg":  {"type": "number"},
                        }
                    },
                    "right_eye": {
                        "type": "object",
                        "description": "Right eye FOV (degrees, +/- around forward)",
                        "properties": {
                            "left_deg":  {"type": "number"},
                            "right_deg": {"type": "number"},
                            "up_deg":    {"type": "number"},
                            "down_deg":  {"type": "number"},
                        }
                    },
                }
            }
        ),
        Tool(
            name="set_ipd",
            description=("Override the per-eye separation (interpupillary distance). "
                         "Set to 0 to verify the app responds to no-parallax (both eyes "
                         "should render identical images). Set to 80 mm to check large-IPD "
                         "behavior. Pass {\"clear\": true} to revert to 64 mm."),
            inputSchema={
                "type": "object",
                "properties": {
                    "ipd_mm": {"type": "number", "description": "IPD in millimeters (0-100)"},
                    "clear":  {"type": "boolean", "default": False},
                }
            }
        ),
        Tool(
            name="set_headset_profile",
            description=("Apply a named headset preset (FOV + IPD together). "
                         "Useful for quickly testing whether the app handles "
                         "asymmetric Quest/Index lens profiles correctly. "
                         "Available: quest2, quest3, index, default (revert)."),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["quest2", "quest3", "index", "default", "clear"],
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="enable_anaglyph_preview",
            description=("Toggle the simulator's anaglyph (red/cyan) stereo preview "
                         "overlay. When enabled, the preview window composites left "
                         "and right eyes into a single red/cyan image. Properly "
                         "converged stereo will appear (mostly) gray; broken IPD or "
                         "swapped eyes will show pronounced red/cyan ghosting."),
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean", "default": True}
                }
            }
        ),
        Tool(
            name="get_projection_log",
            description=("Returns the recent projection-layer FOV / pose / image-rect "
                         "the app submitted via xrEndFrame. Lets you compare what the "
                         "app told the compositor against what the simulator was "
                         "configured to publish — a quick way to find FOV mismatch."),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_entries": {"type": "integer", "default": 20}
                }
            }
        ),
        Tool(
            name="enable_pose_sweep",
            description=("Auto-oscillate the simulator's head yaw / pitch / roll "
                         "on a sine wave. Forces the app through a continuous "
                         "range of orientations — any quaternion-handedness or "
                         "axis-flip bug produces a visible 'world wobbles wrong "
                         "direction' symptom within seconds. Each axis runs at "
                         "a different phase so the motion isn't degenerate."),
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled":         {"type": "boolean", "default": True},
                    "yaw_amp_deg":     {"type": "number",  "default": 30.0},
                    "pitch_amp_deg":   {"type": "number",  "default": 15.0},
                    "roll_amp_deg":    {"type": "number",  "default": 15.0},
                    "freq_hz":         {"type": "number",  "default": 0.25},
                }
            }
        ),
        Tool(
            name="validate_stereo",
            description=("Capture the current stereo preview and run a disparity "
                         "analysis. Returns: verdict (PASS / FAIL_NO_PARALLAX / "
                         "FAIL_EXCESSIVE_PARALLAX), measured horizontal pixel "
                         "disparity, expected range, and a diagnostic hint."),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {"type": "number", "default": 5.0}
                }
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Handle tool calls."""

    if name == "capture_screenshot":
        eye = arguments.get("eye", "both")
        timeout = arguments.get("timeout", 5.0)

        # Pre-clean any stale captures so wait_for_screenshot doesn't latch onto an old file
        for p in SCREENSHOT_OUTPUT_CANDIDATES:
            p.unlink(missing_ok=True)

        request_time = time.time()
        result = request_screenshot(eye=eye)
        if result["status"] == "error":
            return [TextContent(
                type="text",
                text=f"Failed to request screenshot: {result['error']}"
            )]

        screenshot_data = wait_for_screenshot(timeout=timeout, request_time=request_time)

        if screenshot_data:
            try:
                jpeg_data = to_jpeg(screenshot_data)
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=(f"Captured {len(screenshot_data)} bytes but failed to convert to JPEG: {e}. "
                          "Ensure Pillow is installed in the MCP environment.")
                )]
            b64_data = base64.standard_b64encode(jpeg_data).decode("utf-8")
            return [
                TextContent(
                    type="text",
                    text=(f"Screenshot captured ({len(screenshot_data)} bytes raw -> "
                          f"{len(jpeg_data)} bytes JPEG @ q{SCREENSHOT_JPEG_QUALITY}, "
                          f"max width {SCREENSHOT_MAX_WIDTH}px)")
                ),
                ImageContent(
                    type="image",
                    data=b64_data,
                    mimeType="image/jpeg"
                )
            ]
        else:
            return [TextContent(
                type="text",
                text="Screenshot timeout. The OpenXR Simulator runtime may not be running, "
                     "or no application is actively rendering frames. Make sure:\n"
                     "1. An OpenXR application is running\n"
                     "2. The OpenXR Simulator is set as the active runtime\n"
                     "3. The application is submitting frames"
            )]

    elif name == "get_frame_info":
        info = get_frame_info()
        return [TextContent(
            type="text",
            text=json.dumps(info, indent=2)
        )]

    elif name == "read_logs":
        lines = arguments.get("lines", 100)
        filter_pattern = arguments.get("filter")
        logs = read_log_file(lines=lines, filter_pattern=filter_pattern)
        return [TextContent(
            type="text",
            text=f"=== OpenXR Simulator Logs (last {lines} lines) ===\n\n{logs}"
        )]

    elif name == "get_diagnostics":
        diagnostics = parse_log_for_diagnostics()
        return [TextContent(
            type="text",
            text=json.dumps(diagnostics, indent=2)
        )]

    elif name == "diagnose_issue":
        symptoms = arguments.get("symptoms", "")
        analysis = analyze_openxr_issue(symptoms)

        output = ["=== OpenXR Issue Analysis ===\n"]
        output.append(f"Symptoms: {analysis['symptoms']}\n")

        if analysis["potential_issues"]:
            output.append("\nPotential Issues:")
            for issue in analysis["potential_issues"]:
                output.append(f"  - {issue}")

        if analysis["recommendations"]:
            output.append("\nRecommendations:")
            for rec in analysis["recommendations"]:
                output.append(f"  - {rec}")

        if "recent_errors" in analysis:
            output.append("\nRecent Errors from Log:")
            for err in analysis["recent_errors"]:
                output.append(f"  {err.strip()}")

        if "log_excerpt" in analysis:
            output.append(f"\nRelevant Log Entries:\n{analysis['log_excerpt']}")

        output.append("\n\nDiagnostics Summary:")
        output.append(json.dumps(analysis["diagnostics"], indent=2))

        return [TextContent(
            type="text",
            text="\n".join(output)
        )]

    elif name == "get_session_state":
        diagnostics = parse_log_for_diagnostics()
        state = diagnostics.get("session_state", "Unknown")

        state_descriptions = {
            "IDLE": "Session created but not yet started",
            "READY": "Session ready to begin rendering",
            "SYNCHRONIZED": "Session synchronized with display",
            "VISIBLE": "Application is visible but not focused",
            "FOCUSED": "Application has focus and full input",
            "STOPPING": "Session is stopping",
            "EXITING": "Session is exiting",
            "Unknown": "State unknown - check if runtime is active"
        }

        return [TextContent(
            type="text",
            text=f"Session State: {state}\n\n{state_descriptions.get(state, '')}"
        )]

    elif name == "get_head_tracking":
        # Read from status file or log
        info = get_frame_info()
        tracking = info.get("head_tracking", {})

        if not tracking or tracking.get("position") is None:
            # Parse from recent logs
            logs = read_log_file(lines=50)
            pos_match = re.search(
                r"head pos.*?(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*)",
                logs, re.IGNORECASE
            )
            if pos_match:
                tracking["position"] = {
                    "x": float(pos_match.group(1)),
                    "y": float(pos_match.group(2)),
                    "z": float(pos_match.group(3))
                }

        output = "=== Head Tracking State ===\n\n"
        if tracking.get("position"):
            p = tracking["position"]
            output += f"Position: ({p.get('x', 0):.3f}, {p.get('y', 0):.3f}, {p.get('z', 0):.3f})\n"
        else:
            output += "Position: Default (0.0, 1.7, 0.0)\n"

        if tracking.get("orientation"):
            o = tracking["orientation"]
            output += f"Orientation (quaternion): ({o.get('x', 0):.3f}, {o.get('y', 0):.3f}, {o.get('z', 0):.3f}, {o.get('w', 1):.3f})\n"
        else:
            output += "Orientation: Default (identity)\n"

        output += "\nControls:\n"
        output += "  - Mouse: Look around (click preview window to capture)\n"
        output += "  - WASD: Move forward/left/backward/right\n"
        output += "  - ESC: Release mouse capture\n"

        return [TextContent(
            type="text",
            text=output
        )]

    elif name == "clear_logs":
        try:
            if LOG_FILE.exists():
                LOG_FILE.unlink()
            return [TextContent(
                type="text",
                text="Log file cleared successfully."
            )]
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Failed to clear log file: {e}"
            )]

    elif name == "set_head_pose":
        payload = {
            "x": float(arguments.get("x", 0.0)),
            "y": float(arguments.get("y", 1.7)),
            "z": float(arguments.get("z", 0.0)),
            "yaw":   _rad(float(arguments.get("yaw_deg",   0.0))),
            "pitch": _rad(float(arguments.get("pitch_deg", 0.0))),
        }
        if "roll_deg" in arguments and arguments["roll_deg"] is not None:
            payload["roll"] = _rad(float(arguments["roll_deg"]))
        _write_json_command(HEAD_POSE_CMD_FILE, payload)
        return [TextContent(type="text", text=f"Head pose command queued: {payload}")]

    elif name == "set_fov":
        if arguments.get("clear"):
            _write_json_command(FOV_CMD_FILE, {"clear": True})
            return [TextContent(type="text", text="FOV reverted to symmetric default.")]
        def _eye(d):
            if not d: return None
            return {
                "aL": _rad(float(d.get("left_deg",  -45.0))),
                "aR": _rad(float(d.get("right_deg", +45.0))),
                "aU": _rad(float(d.get("up_deg",    +45.0))),
                "aD": _rad(float(d.get("down_deg",  -45.0))),
            }
        L, R = _eye(arguments.get("left_eye")), _eye(arguments.get("right_eye"))
        if L is None or R is None:
            return [TextContent(type="text",
                text="set_fov requires both left_eye and right_eye sub-objects (or clear=true)")]
        _write_json_command(FOV_CMD_FILE, {"left": L, "right": R})
        return [TextContent(type="text",
            text=f"Per-eye asymmetric FOV applied. left={L} right={R}")]

    elif name == "set_ipd":
        if arguments.get("clear"):
            _write_json_command(IPD_CMD_FILE, {"clear": True})
            return [TextContent(type="text", text="IPD reverted to 64 mm.")]
        ipd_mm = float(arguments.get("ipd_mm", 64.0))
        if ipd_mm < 0 or ipd_mm > 200:
            return [TextContent(type="text", text="ipd_mm must be in [0, 200]")]
        _write_json_command(IPD_CMD_FILE, {"ipd_mm": ipd_mm})
        return [TextContent(type="text", text=f"IPD set to {ipd_mm:.1f} mm")]

    elif name == "set_headset_profile":
        prof_name = arguments.get("name", "default")
        _write_json_command(HEADSET_PROFILE_CMD_FILE, {"name": prof_name})
        return [TextContent(type="text", text=f"Headset profile applied: {prof_name}")]

    elif name == "enable_anaglyph_preview":
        enabled = bool(arguments.get("enabled", True))
        _write_json_command(ANAGLYPH_CMD_FILE, {"enabled": enabled})
        return [TextContent(type="text",
            text=f"Anaglyph preview {'enabled' if enabled else 'disabled'}.")]

    elif name == "get_projection_log":
        # Touch the dump-request flag file; the simulator writes the log
        # at the end of the next xrEndFrame.
        ensure_simulator_dir()
        PROJ_LOG_FILE.unlink(missing_ok=True)
        PROJ_LOG_DUMP_REQUEST.touch()
        # Wait briefly for the simulator to dump.
        for _ in range(50):
            if PROJ_LOG_FILE.exists():
                break
            time.sleep(0.05)
        if not PROJ_LOG_FILE.exists():
            return [TextContent(type="text",
                text="Projection log was not produced — is the simulator currently rendering frames?")]
        try:
            data = json.loads(PROJ_LOG_FILE.read_text())
            entries = data.get("entries", [])
            limit = int(arguments.get("max_entries", 20))
            tail = entries[-limit:] if len(entries) > limit else entries
            return [TextContent(type="text",
                text=json.dumps({"count": len(tail), "entries": tail}, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to parse projection log: {e}")]

    elif name == "enable_pose_sweep":
        payload = {
            "enabled":       bool(arguments.get("enabled", True)),
            "yaw_amp_deg":   float(arguments.get("yaw_amp_deg",   30.0)),
            "pitch_amp_deg": float(arguments.get("pitch_amp_deg", 15.0)),
            "roll_amp_deg":  float(arguments.get("roll_amp_deg",  15.0)),
            "freq_hz":       float(arguments.get("freq_hz",        0.25)),
        }
        _write_json_command(SIMULATOR_DIR / "pose_sweep_command.json", payload)
        return [TextContent(type="text",
            text=("Pose sweep " + ("enabled" if payload["enabled"] else "disabled") +
                  f" — yaw=±{payload['yaw_amp_deg']}° pitch=±{payload['pitch_amp_deg']}°"
                  f" roll=±{payload['roll_amp_deg']}° freq={payload['freq_hz']}Hz"))]

    elif name == "validate_stereo":
        timeout = float(arguments.get("timeout", 5.0))
        # Pre-clean stale screenshots so we don't validate an old frame.
        for p in SCREENSHOT_OUTPUT_CANDIDATES:
            p.unlink(missing_ok=True)
        request_time = time.time()
        request_screenshot(eye="both")
        screenshot_data = wait_for_screenshot(timeout=timeout, request_time=request_time)
        if not screenshot_data:
            return [TextContent(type="text",
                text="Screenshot timeout — can't validate stereo without a current frame.")]
        result = _validate_stereo_from_screenshot(screenshot_data)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    else:
        return [TextContent(
            type="text",
            text=f"Unknown tool: {name}"
        )]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
