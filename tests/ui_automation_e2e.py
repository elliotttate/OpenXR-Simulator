"""Real MCP stdio -> runtime -> application's raycast -> visible result.

Run with a Python environment containing mcp, Pillow and numpy:
  python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe.exe
Each run has an isolated IPC/settings folder and keeps its screenshot evidence.
"""
import argparse
import asyncio
import base64
import json
import math
import os
import subprocess
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def button_pixels(mask):
    # Select the largest connected component, including when both eyes are visible.
    remaining = mask.copy()
    regions = []
    for y, x in zip(*np.where(mask)):
        if not remaining[y, x]:
            continue
        remaining[y, x] = False
        todo, region = [(int(y), int(x))], []
        while todo:
            yy, xx = todo.pop()
            region.append((yy, xx))
            for ny, nx in ((yy-1, xx), (yy+1, xx), (yy, xx-1), (yy, xx+1)):
                if 0 <= ny < remaining.shape[0] and 0 <= nx < remaining.shape[1] and remaining[ny, nx]:
                    remaining[ny, nx] = False
                    todo.append((ny, nx))
        regions.append(region)
    return np.array(max(regions, key=len)).T


async def wait_fixture(directory, predicate, timeout=5):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        try:
            state = json.loads((directory/"ui_fixture.json").read_text())
            if predicate(state):
                return state
        except (OSError, ValueError):
            pass
        await asyncio.sleep(.02)
    raise AssertionError("Application did not report the expected UI state")


async def call(session, name, args, directory, label):
    response = await session.call_tool(name, args)
    assert not response.isError, response
    text = next(c.text for c in response.content if c.type == "text")
    info = json.loads(text)
    assert info.get("status") != "error", info
    pixels = base64.b64decode(next(c.data for c in response.content if c.type == "image"))
    (directory/f"{label}.json").write_text(json.dumps(info, indent=2))
    (directory/f"{label}.jpg").write_bytes(pixels)
    return info, np.array(Image.open(BytesIO(pixels)))


async def expect_error(session, name, arguments, contains):
    response = await session.call_tool(name, arguments)
    text = " ".join(c.text for c in response.content if c.type == "text")
    assert contains.lower() in text.lower(), text


def hit_pixel_error(info, target, state):
    # Reproject the application's independently computed ray/quad intersection.
    hx, hy = state["hit"]
    point = np.array([.32+math.cos(.22)*hx, 1.52+hy, -2-math.sin(.22)*hx])
    eye = next(e for e in info["geometry"]["eyes"] if e["eye"] == target["eye"])
    x, y, z, w = eye["pose"]["orientation"]
    rotation = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                         [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                         [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
    local = rotation.T@(point-np.array(eye["pose"]["position"]))
    left, right, up, down = map(math.tan, eye["fov"])
    u, v = (local[0]/-local[2]-left)/(right-left), (local[1]/-local[2]-up)/(down-up)
    ex, ey, ew, eh = eye["viewport"]
    return max(abs(ex+u*ew-target["raw_pixel"][0]), abs(ey+v*eh-target["raw_pixel"][1]))


async def run(args):
    directory = (args.evidence/uuid.uuid4().hex).resolve()
    directory.mkdir(parents=True)
    # The unusual aspect, asymmetric FOV, and sub-image rect expose coordinate guessing.
    (directory/"settings.json").write_text(json.dumps({
        "headset_profile": "quest3", "layout": args.layout, "view_mode": "both", "asymmetric_fov": True,
        "zoom_mode": "fill", "window_width": 1231, "window_height": 703, "preview_fps": -1}))
    env = {**os.environ, "SIMXR_DATA_DIR": str(directory), "SIMXR_RUNTIME_DLL": str(args.probe.with_name("openxr_simulator.dll")),
           "XR_PROBE_UI_TEST": "1", "XR_PROBE_UI_LEFT": "1" if args.hand == "left" else "0",
           "XR_PROBE_FRAMES": "12000", "XR_PROBE_SLEEP_MS": "3", "XR_PROBE_SUBRECT": "1", "XR_UI_BACKEND": args.backend,
           "XR_PROBE_UI_WORLD": "1" if args.ui_mode == "world" else "0", "SIMXR_AUTOMATION_ONLY": "1"}
    if args.app_ray_offset:
        env["XR_PROBE_APP_RAY_OFFSET"] = "1"
    log = (directory/"probe.log").open("w")
    proc = subprocess.Popen([str(args.probe)], env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        await wait_fixture(directory, lambda s: s["frame"] >= 4)
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"capture_screenshot", "hover_ui_at", "click_ui_at", "release_ui_pointer"} <= names
                before, image = await call(session, "capture_screenshot", {"eye": args.eye, "max_width": 960 if args.layout == "over_under" else 480}, directory, "01-before")
                assert before["crop"][2] > image.shape[1], "Test must exercise screenshot resizing"
                if args.ui_mode == "world":
                    assert not before["geometry"]["surfaces"], "World UI case must not submit a quad layer"
                    assert len(before["geometry"]["projection_eyes"]) == 2
                else:
                    assert len(before["geometry"]["surfaces"]) == 1
                blue = (image[:, :, 2] > 195) & (image[:, :, 0] < 120) & (image[:, :, 1] > 95) & (image[:, :, 1] < 190)
                ys, xs = np.where(blue)
                assert len(xs) > 50, "No visible menu button in screenshot"
                # A both-eye screenshot contains two buttons. Pick one connected region.
                ys, xs = button_pixels(blue)
                x, y = float(np.median(xs)), float(np.median(ys))
                target = {"capture_id": before["capture_id"], "x": x, "y": y, "hand": args.hand}
                if args.app_ray_offset:
                    target["ray_offset"] = {"position": [.03, 0, 0],
                        "orientation": [0, math.sin(math.radians(-2.5)), 0, math.cos(math.radians(-2.5))]}
                if args.ui_mode == "world":
                    target["origin_mode"] = args.origin_mode
                    if args.origin_mode == "hand":
                        target["surface"] = {"pose": {"position": [.32, 1.52, -2],
                            "orientation": [0, math.sin(.11), 0, math.cos(.11)]}, "size": [1.6, .9]}
                hovered, image = await call(session, "hover_ui_at", {**target, "dwell_frames": 10}, directory, "02-hover")
                state = await wait_fixture(directory, lambda s: s["hovered"] and s["hover_frames"] >= 8)
                assert state["clicks"] == 0 and not state["button_down"], state
                patch = image[max(0, int(y)-2):int(y)+3, max(0, int(x)-2):int(x)+3].mean(axis=(0, 1))
                assert patch[0] > 180 and patch[1] > 140 and patch[2] < 120, f"Hover highlight missing: {patch}"
                assert np.count_nonzero(np.all(image > 180, axis=2)) > 10, "Dwell tooltip missing"
                error = hit_pixel_error(before, hovered["target"], state)
                assert error < 1, f"Aim missed screenshot point by {error} source pixels"
                # Exact point on the angled quad, reconstructed from the application's ray.
                point = hovered["target"]["target_point"]
                assert point is not None or args.ui_mode == "world" and args.origin_mode == "eye"
                key = uuid.uuid4().hex
                clicked, image = await call(session, "click_ui_at", {**target, "button": "primary", "hold_frames": 5, "command_id": key}, directory, "03-click")
                state = await wait_fixture(directory, lambda s: s["clicks"] == 1 and s["release_edges"] == 1)
                assert state["press_edges"] == 1 and state["sync_errors"] == 0 and not state["button_down"], state
                patch = image[max(0, int(y)-2):int(y)+3, max(0, int(x)-2):int(x)+3].mean(axis=(0, 1))
                assert patch[1] > 180 and patch[0] < 140 and patch[2] < 160, f"Activation result missing: {patch}"
                await call(session, "click_ui_at", {**target, "button": "primary", "command_id": key}, directory, "04-retry")
                state = await wait_fixture(directory, lambda s: s["frame"] > state["frame"]+8)
                assert state["clicks"] == state["press_edges"] == state["release_edges"] == 1, state
                await call(session, "release_ui_pointer", {"capture_id": before["capture_id"], "hand": args.hand}, directory, "05-release")
                if args.negative_tests:
                    await expect_error(session, "click_ui_at", {**target, "x": before["image_size"][0]+1}, "outside")
                    # This fixture binds activation to A/X, so trigger delivery cannot
                    # complete through its active action set. Timeout must release it.
                    await expect_error(session, "click_ui_at", {**target, "button": "trigger", "timeout": .2}, "did not complete")
                    await expect_error(session, "click_ui_at", {**target, "button": "trigger", "command_id": key}, "different interaction")
                    old_frame = state["frame"]
                    state = await wait_fixture(directory, lambda s: s["frame"] > old_frame+8)
                    assert state["clicks"] == state["press_edges"] == state["release_edges"] == 1 and not state["button_down"], state
                    await session.call_tool("set_head_pose", {"x": .15, "y": 1.7, "z": 0})
                    # The existing head tool queues its command; wait for a frame
                    # that actually used that pose before testing stale targeting.
                    until = time.monotonic()+5
                    while True:
                        moved, _ = await call(session, "capture_screenshot", {"eye": args.eye}, directory, "06-moved")
                        if abs(moved["geometry"]["head"]["position"][0]-.15) < 1e-6:
                            break
                        assert time.monotonic() < until, "Queued head movement was never applied"
                        await asyncio.sleep(.02)
                    await expect_error(session, "click_ui_at", {**target, "button": "primary"}, "changed")
                (directory/"result.json").write_text(json.dumps({"passed": True, "pixel": [x, y], "eye": args.eye,
                    "hand": args.hand, "layout": args.layout, "backend": args.backend, "source_pixel_error": error,
                    "ui_mode": args.ui_mode, "origin_mode": target.get("origin_mode", "hand"),
                    "app_ray_offset": args.app_ray_offset,
                    "negative_tests": args.negative_tests, "state": state, "target": hovered["target"]}, indent=2))
                print(f"PASS: screenshot -> hover -> one click -> visible result; {args.ui_mode}/{args.hand}/{args.eye}/{args.layout}\nEvidence: {directory}")
    finally:
        (directory/"ui_probe_stop").touch()
        try:
            code = await asyncio.to_thread(proc.wait, 5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            code = await asyncio.to_thread(proc.wait, 5)
        log.close()
    assert code == 0, f"Probe failed ({code}); see {directory/'probe.log'}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=ROOT/"build-ui-automation/ui-evidence")
    parser.add_argument("--eye", choices=["left", "right", "both"], default="left")
    parser.add_argument("--hand", choices=["left", "right"], default="right")
    parser.add_argument("--layout", choices=["side_by_side", "over_under"], default="side_by_side")
    parser.add_argument("--backend", choices=["d3d12", "d3d11", "opengl", "vulkan"], default="d3d12")
    parser.add_argument("--negative-tests", action="store_true")
    parser.add_argument("--ui-mode", choices=["quad", "world"], default="quad")
    parser.add_argument("--origin-mode", choices=["hand", "eye"], default="eye")
    parser.add_argument("--app-ray-offset", action="store_true")
    args = parser.parse_args()
    args.probe = args.probe.resolve()
    asyncio.run(run(args))
