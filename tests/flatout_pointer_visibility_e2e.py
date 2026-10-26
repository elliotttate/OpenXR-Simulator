"""Require a real hand beam and its surface cursor over an opaque quad menu.

Start on FlatOut Mode's first page. Input uses screenshot-derived XR actions;
game diagnostics are read-only. Capture while the delivered aim lease is live.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_frontend_e2e import Frontend, ROOT, ui_surface, RAY_OFFSET
from flatout_livery_e2e import quaternion_matrix, world_pixel


def stage_pixel(capture, point, eye_name, projection=False):
    eyes = capture["geometry"]["projection_eyes" if projection else "eyes"]
    eye = next(e for e in eyes if e["eye"] == eye_name)
    local = quaternion_matrix(eye["pose"]["orientation"]).T @ (np.array(point) - eye["pose"]["position"])
    left, right, up, down = map(math.tan, eye["fov"])
    u, v = (local[0] / -local[2] - left) / (right - left), (local[1] / -local[2] - up) / (down - up)
    ex, ey, ew, eh = eye["viewport"]
    cx, cy, cw, ch = capture["crop"]
    width, height = capture["image_size"]
    return np.array([(ex + u * ew - cx) / cw * width - .5, (ey + v * eh - cy) / ch * height - .5])


def color_count(pixels, point, hand):
    x, y = np.rint(point).astype(int)
    assert 4 <= x < pixels.shape[1] - 4 and 4 <= y < pixels.shape[0] - 4, (x, y)
    patch = pixels[y - 4:y + 5, x - 4:x + 5].astype(float)
    r, g, b = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    mask = (b > r + 30) & (g > r + 30) & (b > 130) if hand == "left" else (
        (r > b + 60) & (g > b + 30) & (r > 150))
    return int(np.sum(mask))


class PointerVisibility(Frontend):
    async def run(self):
        assert (await self.query("menu.get_state"))["current_menu"] == ("MAIN MENU" if self.args.world_menu else "INSANITY")
        failures = []
        for hand in ("left", "right"):
            if self.args.world_menu:
                menu = await self.query("menu.get_state")
                before = await self.query("vr.targets")
                source = await self.capture("world-label-source-" + hand)
                assert not source["geometry"]["surfaces"], "World-menu test must not accidentally use a HUD carrier"
                item = menu["items"][1]
                # Native spatial hit testing uses the first item's common plane.
                # The hovered label's animated face can move off that plane.
                frame = {**before["vr_frame"], "panel": {**menu["spatial_layout"], "origin": menu["items"][0]["rest_position"]},
                         "panel_width": 1.0, "panel_height": 1.0}
                state = {"vr": {"frame": frame}}
                x, y = world_pixel(state, source, item["rendered_position"], "left")
                aim = {"capture_id": source["capture_id"], "x": float(x), "y": float(y), "hand": hand,
                       "surface": ui_surface(state), "origin_mode": "hand", "ray_offset": RAY_OFFSET, "dwell_frames": 30}
            else:
                before, aim = await self.point_args(self.args.target, (.83, .43), hand)
                assert self.last_capture["geometry"]["surfaces"], "This regression requires a compositor quad"
            for other in ("left", "right"):
                await self.tool("release_ui_pointer", {"capture_id": aim["capture_id"], "hand": other}, "release-" + other)
            receipt = await self.tool("hover_ui_at", aim, "hover-" + hand)
            assert receipt["status"] == "input_delivered", receipt
            # No settle sleep or expensive diagnostic call before this image:
            # otherwise the aim lease could expire and manufacture a false red.
            cap = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "pointer-both-eyes-" + hand)
            native_capture = None
            if self.args.gpu_capture:
                native_capture = await self.query("renderdoc.capture", {"path": str(self.args.gpu_capture) + "-" + hand})
            pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
            current = await self.query("vr.targets")
            if self.args.world_menu:
                assert (await self.query("menu.get_state"))["selection_index"] == 1
            else:
                assert current["hovered"] == self.args.target, current
            assert current["clicks"] == before["clicks"], current
            target = receipt["target"]
            origin = np.array(target["application_ray"]["position"])
            hit = np.array(target["target_point"])
            rays = cap["geometry"]["decorations"]
            assert len(rays) == 2, "Both hand rays must be compositor quads"
            assert all(r["visibility"] == 0 and abs(r["size"][0] - .002) < 1e-6 for r in rays), rays
            assert all(r["id"] > max((s["id"] for s in cap["geometry"]["surfaces"]), default=0) for r in rays), rays
            direction = (hit - origin) / np.linalg.norm(hit - origin)
            expected_start = origin + direction * .05
            endpoint_errors = []
            for ray in rays:
                matrix = quaternion_matrix(ray["pose"]["orientation"])
                center = np.array(ray["pose"]["position"])
                start = center - matrix[:, 1] * ray["size"][1] * .5
                end = center + matrix[:, 1] * ray["size"][1] * .5
                endpoint_errors.append(float(np.linalg.norm(start - expected_start) + np.linalg.norm(end - hit)))
                assert abs(np.linalg.det(matrix) - 1) < 1e-4, "Ribbon pose must use a unit right-handed basis"
            assert min(endpoint_errors) < .002, ("Ribbon must physically span the controller-to-hit segment", endpoint_errors)
            observations = []
            for eye in ("left", "right"):
                cursor = stage_pixel(cap, hit, eye)
                cursor_pixels = color_count(pixels, cursor, hand)
                samples = []
                for fraction in (.90, .95):
                    # These points are genuinely between the hand and the UI,
                    # rather than a 2D extension painted on the menu face.
                    point = origin + (hit - origin) * fraction
                    screen = stage_pixel(cap, point, eye)
                    samples.append({"fraction": fraction, "world": point.tolist(),
                                    "pixel": screen.tolist(), "colored_pixels": color_count(pixels, screen, hand)})
                observations.append({"eye": eye, "cursor_pixel": cursor.tolist(),
                                     "cursor_colored_pixels": cursor_pixels, "beam_samples": samples})
                if cursor_pixels == 0:
                    failures.append(hand + "/" + eye + " surface cursor missing")
                if not all(sample["colored_pixels"] > 0 for sample in samples):
                    failures.append(hand + "/" + eye + " foreground hand beam covered by UI")
            self.cases.append({"case": "quad-foreground-beam-and-cursor-" + hand,
                               "passed": all(o["cursor_colored_pixels"] > 0 and all(s["colored_pixels"] > 0 for s in o["beam_samples"]) for o in observations),
                               "observations": observations, "aim": target, "capture": cap,
                               "ribbon_endpoint_error_m": min(endpoint_errors), "native_capture": native_capture})
        assert not failures, failures
        if self.args.world_menu:
            return
        for hand in ("left", "right"):
            self.args.hand = hand
            await self.click("challenge.page.next")
            indices = [int(t["name"].split(".")[-1]) for t in (await self.query("vr.targets"))["targets"]
                       if t["name"].startswith("challenge.event.")]
            assert sorted(indices) == list(range(6, 12)), "One held click must advance exactly one page"
            await self.click("challenge.page.prev")


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())})
    result = {"passed": False, "pid": args.pid, "scope": "Both real hand beams in front of an opaque compositor UI, with surface cursors in both eyes"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = PointerVisibility(args, session)
            try:
                await test.run()
                result["passed"] = True
            except Exception as error:
                result["error"] = repr(error)
                raise
            finally:
                result["cases"] = test.cases
                (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
                if test.last_capture:
                    for hand in ("left", "right"):
                        await session.call_tool("release_ui_pointer", {"capture_id": test.last_capture["capture_id"], "hand": hand})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--target", default="challenge.event.1")
    parser.add_argument("--hand", default="left")
    parser.add_argument("--world-menu", action="store_true", help="Check both compositor rays over the main world-space menu instead")
    parser.add_argument("--gpu-capture", type=Path, help="Also queue a native RenderDoc frame for each live hand aim")
    asyncio.run(main(parser.parse_args()))
