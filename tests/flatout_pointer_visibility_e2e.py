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

from flatout_frontend_e2e import Frontend, ROOT
from flatout_livery_e2e import quaternion_matrix


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
        assert (await self.query("menu.get_state"))["current_menu"] == "INSANITY"
        failures = []
        for hand in ("left", "right"):
            before, aim = await self.point_args(self.args.target, (.83, .43), hand)
            assert self.last_capture["geometry"]["surfaces"], "This regression requires a compositor quad"
            for other in ("left", "right"):
                await self.tool("release_ui_pointer", {"capture_id": aim["capture_id"], "hand": other}, "release-" + other)
            receipt = await self.tool("hover_ui_at", aim, "hover-" + hand)
            assert receipt["status"] == "input_delivered", receipt
            # No settle sleep or expensive diagnostic call before this image:
            # otherwise the aim lease could expire and manufacture a false red.
            cap = await self.tool("capture_screenshot", {"eye": "both", "max_width": 2048}, "pointer-both-eyes-" + hand)
            pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
            current = await self.query("vr.targets")
            assert current["hovered"] == self.args.target and current["clicks"] == before["clicks"], current
            target = receipt["target"]
            origin = np.array(target["application_ray"]["position"])
            hit = np.array(target["target_point"])
            observations = []
            for eye in ("left", "right"):
                cursor = stage_pixel(cap, hit, eye)
                cursor_pixels = color_count(pixels, cursor, hand)
                samples = []
                for fraction in (.90, .95):
                    # These points are genuinely between the hand and the UI,
                    # rather than a 2D extension painted on the menu face.
                    point = origin + (hit - origin) * fraction
                    screen = stage_pixel(cap, point, eye, projection=True)
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
                               "observations": observations, "aim": target, "capture": cap})
        assert not failures, failures


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
    asyncio.run(main(parser.parse_args()))
