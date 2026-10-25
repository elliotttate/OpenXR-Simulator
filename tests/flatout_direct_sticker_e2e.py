"""Direct artwork selection and manipulation through screenshot-derived XR rays.

Start in the isolated livery editor after the two-hand regression, with its
visible white basic circle on the door. Screenshot coordinates target actual
artwork; native hit samples supply depth and independent manipulation oracles.
No game command selects or transforms artwork. Changes remain unsaved.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_livery_e2e import LiveryTest, ROOT, world_pixel


class DirectSticker(LiveryTest):
    async def point(self, hand, pixel, hold=True, expect_grab=True):
        capture = await self.capture("direct-gesture-source")
        aim = {"capture_id": capture["capture_id"], "x": float(pixel[0]), "y": float(pixel[1]),
               "hand": hand, "origin_mode": "hand", "surface": self.car_surface, "dwell_frames": 2}
        delivered = await self.tool("hold_ui_at" if hold else "hover_ui_at", aim, "direct-gesture-"+hand)
        assert delivered["status"] == "input_delivered", delivered
        state = await self.state()
        (self.args.evidence/f"{self.sequence:03}-gesture-state.json").write_text(json.dumps(state, indent=2))
        hit = state["vr"]["hands"][hand == "right"]
        assert hit["car_hit"] and not hit["ui_hit"], (pixel, hit)
        if hold:
            assert hit["grabbed"] == expect_grab, (pixel, hit)
        return state

    def visible_circle_pixel(self):
        # Locate the large light circle in the actual screenshot, rather than
        # assume a viewport height: the simulator window can change aspect.
        pixels = self.last_pixels.astype(float)
        height, width = pixels.shape[:2]
        x0, x1, y0, y1 = int(width*.59), int(width*.71), int(height*.31), int(height*.43)
        roi = pixels[y0:y1, x0:x1]
        mask = (roi.mean(2)>180) & (roi.max(2)-roi.min(2)<22)
        seen = np.zeros(mask.shape, bool)
        components = []
        for iy, ix in zip(*np.where(mask)):
            if seen[iy, ix]:
                continue
            queue = [(iy, ix)]; seen[iy, ix] = True; points = []
            while queue:
                cy, cx = queue.pop(); points.append((cx+x0, cy+y0))
                for ny, nx in ((cy-1,cx),(cy+1,cx),(cy,cx-1),(cy,cx+1)):
                    if 0<=ny<mask.shape[0] and 0<=nx<mask.shape[1] and mask[ny,nx] and not seen[ny,nx]:
                        seen[ny,nx] = True; queue.append((ny,nx))
            if len(points)>500:
                components.append(np.array(points))
        assert components, "The screenshot fixture must show the white circle on the door"
        region = max(components, key=len)
        center = region.mean(0)
        assert np.all(region.max(0)-region.min(0)>30), "Artwork must have a large visible opaque interior"
        (self.args.evidence/"visible-artwork-target.json").write_text(json.dumps({
            "image_size": [width,height], "pixel": center.tolist(), "opaque_pixels":len(region),
            "bounds": [region.min(0).tolist(),region.max(0).tolist()]},indent=2))
        return center

    async def run_direct(self):
        await self.click("STICKERS")  # Leaves explicit placement mode through the actual UI.
        before = await self.state()
        assert before["document"]["layers"][before["selected"]]["asset"] == "FO_BASIC_CIRCLE"
        await self.click("DUPLICATE")
        expected = len((await self.state())["document"]["layers"])-1
        await self.click("Layer 0")  # Deliberately select the covered, lower layer.
        await self.click("STUDIO")
        before = await self.state()
        assert before["selected"] == 0 and expected != 0
        assert not any(c["label"] == "DONE PLACING" for c in before["vr"]["controls"])
        before = await self.state()
        layer = before["document"]["layers"][expected]
        u, n = np.array(layer["projectionTangent"]), np.array(layer["projectionNormal"])
        v = np.cross(n, u)
        center = u*layer["position"][0]+v*layer["position"][1]+n*layer["plane"]
        capture = await self.capture("direct-sticker-source")
        # The visible white circle in this screenshot fixture is on the door.
        # First obtain its actual surface depth with an unpressed eye ray;
        # a projector plane can lie inside the curved body and is not a target.
        x, y = self.visible_circle_pixel()
        await self.tool("hover_ui_at", {"capture_id": capture["capture_id"], "x": x, "y": y,
            "hand": "left", "origin_mode": "eye", "dwell_frames": 12}, "visible-artwork-depth")
        depth_state = await self.state()
        (self.args.evidence/"depth-state.json").write_text(json.dumps(depth_state, indent=2))
        depth_hit = depth_state["vr"]["hands"][0]
        assert depth_hit["car_hit"] and not depth_hit["ui_hit"]
        world = np.array(depth_hit["end"])
        await self.release("left")
        capture = await self.capture("direct-hand-source")
        x, y = world_pixel(before, capture, world, "left")
        camera = before["vr"]["frame"]["camera"]
        inverse = np.column_stack([camera[k] for k in ("right", "up", "back")]).T
        depth = float((inverse @ (world-camera["origin"]))[2])
        self.car_surface = {"pose": {"position": [0, 0, depth], "orientation": [0, 0, 0, 1]}, "size": [20, 20]}
        aim = {"capture_id": capture["capture_id"], "x": float(x), "y": float(y), "hand": "left",
               "origin_mode": "hand", "surface": self.car_surface, "dwell_frames": 2}
        receipt = await self.tool("hover_ui_at", aim, "direct-sticker-hover")
        assert receipt["status"] == "input_delivered", receipt
        hovered = await self.state()
        hit = hovered["vr"]["hands"][0]
        (self.args.evidence/"hover-state.json").write_text(json.dumps(hovered, indent=2))
        assert hit["car_hit"] and not hit["ui_hit"], "Target must be the visible artwork on the actual car"
        delta = np.array(hit["surface_point"])-center
        assert np.linalg.norm([delta@u/layer["scale"][0], delta@v/layer["scale"][1]]) < .6, "Ray must hit the opaque middle of the circle"
        receipt = await self.tool("hold_ui_at", aim, "direct-sticker-grab")
        assert receipt["status"] == "input_delivered", receipt
        grabbed = await self.state()
        passed = grabbed["selected"] == expected and grabbed["vr"]["grab_mask"] == 1
        self.cases.append({"case": "point-at-unselected-visible-sticker-and-grab", "passed": passed,
                           "pixel": [float(x), float(y)], "expected_layer": expected,
                           "before": before, "after": grabbed})
        assert passed, "Pointing at the visible top sticker must select and grab it without Place on Car"
        assert grabbed["document"] == before["document"], "Initial grab must preserve the sticker's placement"
        assert not any(c["window"] in ("Sticker tools", "Livery camera rail", "Livery layers")
                       for c in grabbed["vr"]["controls"]), "Near controls must tuck away so the other ray can reach the car"
        assert grabbed["vr"]["frame"]["camera"] == before["vr"]["frame"]["camera"], "Tucking controls must preserve the camera"
        await asyncio.sleep(.6)
        still = await self.state()
        assert still["vr"]["rendered_frames"] > grabbed["vr"]["rendered_frames"]+2
        assert still["generation"] == grabbed["generation"], "Stationary held rays must not rebake the preview"
        assert still["document"] == grabbed["document"] and still["vr"]["grab_mask"] == 1
        self.cases.append({"case": "stationary-held-ray-does-not-rebake", "passed": True,
                           "before": grabbed, "after": still})
        # Moving the ray moves only the newly targeted sticker, preserving its
        # grab offset. Native surface points independently predict translation.
        moved = await self.point("left", (x+20, y-10))
        initial_layer = grabbed["document"]["layers"][expected]
        moved_layer = moved["document"]["layers"][expected]
        delta = np.array(moved["vr"]["hands"][0]["surface_point"])-grabbed["vr"]["hands"][0]["surface_point"]
        assert np.linalg.norm(delta) > .005
        assert np.allclose(moved_layer["position"], np.array(initial_layer["position"])+[delta@u, delta@v], atol=.0001)
        assert moved_layer["scale"] == initial_layer["scale"]
        assert moved["document"]["layers"][:expected] == before["document"]["layers"][:expected]
        # Join the other ray on this same sticker, then spread and twist.
        joined = await self.point("right", (x+40, y+30))
        assert joined["vr"]["grab_mask"] == 3
        assert joined["document"] == moved["document"], "A second hand must join without a jump"
        await self.point("left", (x-10, y-25))
        changed = await self.point("right", (x+60, y+25))
        def span(state):
            vector = np.array(state["vr"]["hands"][1]["surface_point"])-state["vr"]["hands"][0]["surface_point"]
            return np.array([vector@u, vector@v])
        a, b = span(joined), span(changed)
        raw_ratio = np.linalg.norm(b)/np.linalg.norm(a)
        scale = np.array(joined["document"]["layers"][expected]["scale"])
        ratio = np.clip(raw_ratio, np.max(.01/scale), np.min(4/scale))
        angle = np.degrees(np.arctan2(a[0]*b[1]-a[1]*b[0], a@b))
        actual = changed["document"]["layers"][expected]
        assert abs(ratio-1) > .05 and abs(angle) > 1, (ratio, angle)
        assert np.allclose(actual["scale"], scale*ratio, atol=.0001)
        assert abs(actual["rotation"]-joined["document"]["layers"][expected]["rotation"]-angle) < .001
        assert changed["document"]["layers"][:expected] == before["document"]["layers"][:expected]
        released = await self.release("left")
        assert released["vr"]["grab_mask"] == 2 and released["document"] == changed["document"], "One hand releasing must not jump"
        final = await self.point("right", (x+65, y+20))
        released_all = await self.release("right")
        assert any(c["window"] == "Sticker tools" for c in released_all["vr"]["controls"]), "Tools must return after the last hand releases"
        assert released_all["document"] == final["document"]
        undone = await self.click("UNDO")
        assert undone["document"] == before["document"], "One Undo must restore the entire direct grab"
        redone = await self.click("REDO")
        assert redone["document"] == final["document"]
        self.cases.append({"case": "direct-sticker-move-two-hand-resize-turn-release-undo-redo", "passed": True,
                           "before": before, "grabbed": grabbed, "moved": moved, "joined": joined,
                           "changed": changed, "released": released, "final": final,
                           "undone": undone, "redone": redone})
        print("Direct selection, offset-preserving movement, two-hand resize/turn and one Undo PASS", flush=True)
        # A visible locked top sticker must not edit the covered lower layer.
        locked = await self.click("LOCK")
        denied = await self.point("left", (x+25, y), expect_grab=False)
        assert denied["vr"]["hands"][0]["hovered_layer"] == expected
        assert denied["vr"]["grab_mask"] == 0 and denied["document"] == locked["document"]
        await self.release("left")
        unlocked = await self.click("UNLOCK")
        # A click without movement must not add a ghost Undo operation.
        clicked = await self.point("left", (x+25, y))
        assert clicked["document"] == unlocked["document"]
        await self.release("left")
        undo_unlock = await self.click("UNDO")
        assert undo_unlock["document"] == locked["document"], "A stationary click must not create an Undo entry"
        restored = await self.click("REDO")
        assert restored["document"] == unlocked["document"]
        bare = await self.point("left", (600, y-20), expect_grab=False)
        assert bare["vr"]["hands"][0]["hovered_layer"] == -1
        assert bare["vr"]["grab_mask"] == 0 and bare["document"] == restored["document"]
        await self.release("left")
        self.cases.append({"case": "locked-artwork-bare-body-and-no-op-click", "passed": True,
                           "locked": locked, "denied": denied, "bare": bare,
                           "undo_unlock": undo_unlock, "restored": restored})
        print("Locked artwork protection, bare-body safety and no-op click history PASS", flush=True)



async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    result = {"passed": False, "pid": args.pid, "scope": "Point at actual visible artwork, directly select/move/resize/turn it without placement mode; release either hand, Undo/Redo"}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
                                   env={**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = DirectSticker(args, session)
            try:
                await test.run_direct()
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--evidence", type=Path, required=True)
    asyncio.run(main(p.parse_args()))
