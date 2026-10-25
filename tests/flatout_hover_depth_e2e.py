"""Verify real native-game button depth through submitted XR layers and ray input.

Start with a cancelable native dialog open. Diagnostics are read-only; the
hover and single Cancel click go through the simulator's real XR action path.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_frontend_e2e import Frontend, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import quaternion_matrix

ROOT = Path(__file__).resolve().parents[1]


class NativeDepth(Frontend):
    async def run(self):
        assert (await self.query("ui.get_message_box"))["open"], "Open a cancelable dialog first"
        before = await self.query("vr.targets")
        action = next(t for t in before["targets"] if t["name"] == "msgbox.cancel")
        capture = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "depth-source")
        self.last_capture = capture
        planes = [s for s in capture["geometry"]["surfaces"] if s["visibility"] == 0]
        assert planes, "The dialog must use a BOTH-eye HUD surface"
        base = max(planes, key=lambda s: s["size"][0]*s["size"][1])
        l, t, r, b = action["rect"]
        rotation = quaternion_matrix(base["pose"]["orientation"])
        center = rotation @ np.array([((l+r)/3840-.5)*base["size"][0],
                                      (.5-(t+b)/2160)*base["size"][1], 0]) + base["pose"]["position"]
        x, y = quad_pixel(capture, action["rect"])
        aim = {"capture_id": capture["capture_id"], "surface_id": base["id"],
               "x": float(x), "y": float(y), "hand": self.args.hand,
               "origin_mode": "hand", "ray_offset": RAY_OFFSET, "dwell_frames": 30}
        hovered = await self.tool("hover_ui_at", aim, "native-depth-hover")
        assert hovered["status"] == "input_delivered", hovered
        native = await self.query("vr.targets")
        assert native["hovered"] == action["name"], native
        assert native["clicks"] == before["clicks"], "Hover clicked the dialog"
        assert next(t["rect"] for t in native["targets"] if t["name"] == action["name"]) == action["rect"], "Hover moved the source hit rectangle"
        raised = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "native-depth-raised")
        self.last_capture = raised
        layers = raised["geometry"]["surfaces"]
        extras = [s for s in layers if s["id"] != base["id"] and s["visibility"] == 0]
        self.cases.append({"case": "native-button-has-separate-both-eye-surface", "passed": bool(extras),
                           "base": base, "submitted_surfaces": layers, "native_hover": native["hovered"]})
        assert extras, "Hovered native button is still flattened into the original HUD layer"
        current_base = next(s for s in layers if s["id"] == base["id"])
        assert np.allclose(current_base["pose"]["position"], base["pose"]["position"], atol=1e-5), "Hover moved the entire dialog"
        tile = min(extras, key=lambda s: np.linalg.norm((rotation.T @ (np.array(s["pose"]["position"])-center))[:2]))
        delta = rotation.T @ (np.array(tile["pose"]["position"])-center)
        expected_size = [(r-l)/1920*base["size"][0], (b-t)/1080*base["size"][1]]
        head = np.array(raised["geometry"]["head"]["position"])
        approach = float(np.linalg.norm(center-head)-np.linalg.norm(np.array(tile["pose"]["position"])-head))
        assert np.linalg.norm(delta[:2]) < .006, ("Button slid sideways", delta.tolist())
        assert .020 <= delta[2] <= .035 and approach >= .018, ("Button did not physically approach the camera", delta.tolist(), approach)
        assert np.allclose(tile["size"], expected_size, atol=.006), ("Hover changed the physical button size", tile["size"], expected_size)
        self.cases.append({"case": "native-button-physically-approaches-without-scaling", "passed": True,
                           "local_delta_m": delta.tolist(), "camera_approach_m": approach,
                           "tile": tile, "source_rect": action["rect"]})
        # The stable base surface remains a valid carrier while the button moves.
        click = {**aim, "command_id": uuid.uuid4().hex, "hold_frames": 45, "timeout": 30}
        receipt = await self.tool("click_ui_at", click, "native-depth-click-once")
        assert receipt["status"] == "input_delivered", receipt
        assert not (await self.query("ui.get_message_box"))["open"], "Cancel did not dismiss the dialog"
        after = await self.query("vr.targets")
        assert after["clicks"] == before["clicks"]+1, "Held trigger produced another click"
        retry = await self.tool("click_ui_at", click, "native-depth-same-id-retry")
        assert retry["status"] == "input_delivered", retry
        assert (await self.query("vr.targets"))["clicks"] == after["clicks"], "Retry clicked again"
        assert not any(t["name"].startswith("msgbox.") for t in after["targets"]), "Closed dialog retained ray targets"
        self.cases.append({"case": "moving-native-button-single-click-and-retry", "passed": True, "hand": self.args.hand})


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")], env=env)
    result = {"passed": False, "pid": args.pid, "hand": args.hand, "scope": "Native dialog physical depth, actual BOTH-eye XR layers and one ray click"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = NativeDepth(args, session)
            try:
                await test.run()
                result["passed"] = True
            except Exception as error:
                result["error"] = repr(error)
                raise
            finally:
                result["cases"] = test.cases
                (args.evidence/"result.json").write_text(json.dumps(result, indent=2))
                if test.last_capture:
                    for hand in ("left", "right"):
                        await session.call_tool("release_ui_pointer", {"capture_id": test.last_capture["capture_id"], "hand": hand})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--hand", choices=("left", "right"), default="right")
    asyncio.run(main(parser.parse_args()))
