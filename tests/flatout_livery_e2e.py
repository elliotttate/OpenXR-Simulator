"""Live FlatOut RC livery test: screenshots -> real XR rays -> native UI feedback.

Launch a freshly built Fox RC with this simulator and an isolated save. Open
Garage -> Livery editor with no unsaved edits. Use an isolated settings profile
(the latest RC derives it from FOX_WRECK_RUN_TEST_SLOT >= 1000), or back up its
Liveries folder. A progression save slot alone may not isolate settings on older
branches. This test applies a local livery; it never publishes to Workshop or
injects game-side input.

python tests/flatout_livery_e2e.py --pid GAME_PID --data-dir IPC_DIR --evidence OUT
"""
import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import uuid
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def game_read(pid, operation="state"):
    assert operation in ("state", "catalog"), "The game bridge is a read-only oracle"
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "livery.command",
                          "params": {"op": operation}}).encode()
    with open(r"\\.\pipe\FlatoutMCP_" + str(pid), "r+b", buffering=0) as pipe:
        pipe.write(struct.pack("<I", len(payload)) + payload)

        def read(count):
            data = b""
            while len(data) < count:
                chunk = pipe.read(count-len(data))
                if not chunk:
                    raise RuntimeError("Game pipe closed")
                data += chunk
            return data

        response = json.loads(read(struct.unpack("<I", read(4))[0]))
    assert "error" not in response, response
    return response["result"]


def quaternion_matrix(q):
    x, y, z, w = q
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def ui_surface(state):
    """Native panel in XR local space, allowing natural hand-origin targeting."""
    frame = state["vr"]["frame"]
    camera, panel = frame["camera"], frame["panel"]
    inverse = np.column_stack([camera[k] for k in ("right", "up", "back")]).T
    basis = inverse @ np.column_stack([panel["right"], panel["up"], -np.array(panel["back"])])
    # A symmetric eigenproblem converts a rotation matrix without a fragile
    # trace branch near 180 degrees. Quaternion order is OpenXR x,y,z,w.
    m = basis
    k = np.array([[m[0,0]-m[1,1]-m[2,2], m[0,1]+m[1,0], m[0,2]+m[2,0], m[2,1]-m[1,2]],
                  [m[0,1]+m[1,0], m[1,1]-m[0,0]-m[2,2], m[1,2]+m[2,1], m[0,2]-m[2,0]],
                  [m[0,2]+m[2,0], m[1,2]+m[2,1], m[2,2]-m[0,0]-m[1,1], m[1,0]-m[0,1]],
                  [m[2,1]-m[1,2], m[0,2]-m[2,0], m[1,0]-m[0,1], np.trace(m)]]) / 3
    _, vectors = np.linalg.eigh(k)
    orientation = vectors[:, -1]
    if orientation[3] < 0:
        orientation = -orientation
    return {"pose": {"position": (inverse @ (np.array(panel["origin"])-camera["origin"])).tolist(),
                     "orientation": orientation.tolist()},
            "size": [frame["panel_width"], frame["panel_height"]]}


def screenshot_pixel(state, capture, x, y):
    """Project the game's independently reported rendered control onto the image."""
    frame = state["vr"]["frame"]
    panel, camera = frame["panel"], frame["camera"]
    world = (np.array(panel["origin"]) + np.array(panel["right"])*(x/1920-.5)*frame["panel_width"]
             + np.array(panel["up"])*(.5-y/1080)*frame["panel_height"])
    camera_point = np.column_stack([camera[k] for k in ("right", "up", "back")]).T @ (world-camera["origin"])
    # This panel is rendered in the scene; use its fractional projection
    # viewport, as resolve_target does for an explicitly supplied surface.
    eye = next(e for e in capture["geometry"]["projection_eyes"] if e["eye"] == "left")
    local = quaternion_matrix(eye["pose"]["orientation"]).T @ (camera_point-eye["pose"]["position"])
    left, right, up, down = map(math.tan, eye["fov"])
    u, v = (local[0]/-local[2]-left)/(right-left), (local[1]/-local[2]-up)/(down-up)
    ex, ey, ew, eh = eye["viewport"]
    cx, cy, cw, ch = capture["crop"]
    width, height = capture["image_size"]
    return (ex+u*ew-cx)/cw*width-.5, (ey+v*eh-cy)/ch*height-.5


def world_pixel(state, capture, point, eye_name):
    camera = state["vr"]["frame"]["camera"]
    camera_point = np.column_stack([camera[k] for k in ("right", "up", "back")]).T @ (np.array(point)-camera["origin"])
    eye = next(e for e in capture["geometry"]["projection_eyes"] if e["eye"] == eye_name)
    local = quaternion_matrix(eye["pose"]["orientation"]).T @ (camera_point-eye["pose"]["position"])
    left, right, up, down = map(math.tan, eye["fov"])
    u, v = (local[0]/-local[2]-left)/(right-left), (local[1]/-local[2]-up)/(down-up)
    ex, ey, ew, eh = eye["viewport"]
    cx, cy, cw, ch = capture["crop"]
    width, height = capture["image_size"]
    return np.array([(ex+u*ew-cx)/cw*width-.5, (ey+v*eh-cy)/ch*height-.5])


class LiveryTest:
    def __init__(self, args, session):
        self.args, self.session, self.sequence, self.cases = args, session, 0, []
        self.last_capture = None

    async def state(self):
        state = await asyncio.wait_for(asyncio.to_thread(game_read, self.args.pid), 15)
        assert state["ready"] and state["vr"]["active"] and not state["vr"]["test_pointer"], state
        return state

    async def tool(self, name, arguments, label):
        if name in ('hover_ui_at','click_ui_at','hold_ui_at','scroll_ui_at'):
            from flatout_frontend_e2e import read_game
            from flatout_ray_offset import native_pointer_offset
            native=await asyncio.to_thread(read_game,self.args.pid,'vr.get_state')
            calibration=native_pointer_offset(native,arguments.get('hand','right'))
            if calibration is not None:
                arguments={**arguments,'ray_offset':calibration}
        result = await self.session.call_tool(name, arguments)
        self.sequence += 1
        stem = self.args.evidence/f"{self.sequence:03}-{label}"
        values = [json.loads(c.text) for c in result.content if c.type == "text" and c.text.startswith("{")]
        stem.with_suffix(".json").write_text(json.dumps(values, indent=2))
        for index, block in enumerate(c for c in result.content if c.type == "image"):
            data = base64.b64decode(block.data)
            stem.with_name(stem.name+f"-{index}.jpg").write_bytes(data)
            if index == 0:
                self.last_pixels = np.array(Image.open(BytesIO(data)).convert("RGB"))
        assert not result.isError and values, (name, values)
        return values[0]

    async def capture(self, label):
        self.last_capture = await self.tool("capture_screenshot", {"eye": "left", "max_width": 1280}, label)
        return self.last_capture

    async def click(self, label, index=0, hold_frames=2, retry=False, hand="right"):
        before = await self.state()
        controls = [c for c in before["vr"]["controls"] if c["label"] == label]
        assert len(controls) > index, (label, before["vr"]["controls"])
        control = controls[index]
        hx, hy = control["x"]+control["width"]/2, control["y"]+control["height"]/2
        capture = await self.capture("source")
        x, y = screenshot_pixel(before, capture, hx, hy)
        assert 0 <= x < capture["image_size"][0] and 0 <= y < capture["image_size"][1]
        aim = {"capture_id": capture["capture_id"], "x": float(x), "y": float(y),
               "origin_mode": "hand", "surface": ui_surface(before), "dwell_frames": 30, "hand": hand}
        if before["vr"]["hand"] != int(hand == "right"):
            # A stationary spare ray may already point at this button. Move it
            # within the button first so hover handoff represents real motion.
            approach_x, approach_y = screenshot_pixel(before, capture,
                control["x"]+control["width"]*.25, hy)
            approach = await self.tool("hover_ui_at", {**aim, "x": float(approach_x),
                "y": float(approach_y), "dwell_frames": 3}, "hover-approach")
            assert approach["status"] == "input_delivered", approach
        hover = await self.tool("hover_ui_at", aim, "hover")
        assert hover["status"] == "input_delivered", hover
        await asyncio.sleep(.3)
        hovered = await self.state()
        hit = hovered["vr"].get("hands", [hovered["vr"], hovered["vr"]])[hand == "right"]
        assert hit["ui_hit"] and max(abs(hit["u"]*1920-hx), abs(hit["v"]*1080-hy)) < 1, hit
        assert hovered["vr"]["hand"] == int(hand == "right"), "Deliberately re-aiming must transfer the idle UI hover"
        # The source image remains valid: the UI hover cannot move the XR camera.
        click = {**aim, "hold_frames": hold_frames, "command_id": uuid.uuid4().hex}
        receipt = await self.tool("click_ui_at", click, "click")
        assert receipt["status"] == "input_delivered", receipt
        await asyncio.sleep(.5)
        after = await self.state()
        if retry:
            repeated = await self.tool("click_ui_at", click, "idempotent-retry")
            assert repeated["delivery"]["sync_id"] == receipt["delivery"]["sync_id"]
            retried = await self.state()
            assert retried["document"] == after["document"] and retried["generation"] == after["generation"], retried
            for key in ("tab","page","mode","category","slot","selected","camera_view",
                        "aspect_locked","keyboard_target","keyboard_text","search"):
                assert retried[key] == after[key], ("Retry changed UI state",label,key)
            for key in ("open","tab","current_car","search","page","slice"):
                assert retried["workshop_browser"][key] == after["workshop_browser"][key], (label,key)
        self.cases.append({"control": label, "pixel": [float(x), float(y)], "hover": [hit["u"], hit["v"]],
                           "before": before, "after": after, "passed": True})
        print(label, "PASS", flush=True)
        return after

    async def hold_car(self, hand, x, y):
        before = await self.state()
        capture = await self.capture("car-source")
        px, py = screenshot_pixel(before, capture, x, y)
        receipt = await self.tool("hold_ui_at", {"capture_id": capture["capture_id"],
            "x": float(px), "y": float(py), "origin_mode": "hand", "surface": self.car_surface, "hand": hand,
            "dwell_frames": 12}, "car-hold-"+hand)
        assert receipt["status"] == "input_delivered", receipt
        after = await self.state()
        ray = after["vr"]["hands"][hand == "right"]
        assert ray["car_hit"] and not ray["ui_hit"] and ray["grabbed"], ray
        return after

    async def release(self, hand):
        await self.tool("release_ui_pointer", {"capture_id": self.last_capture["capture_id"],
                                               "hand": hand}, "release-"+hand)
        return await self.state()

    async def dual_hands(self):
        await self.click("STICKERS", hand="left")
        selected = (await self.state())["selected"]
        await self.click("Layer "+str(selected), hand="left")
        await self.click("STUDIO")
        state = await self.state()
        if any(c["label"] == "PLACE ON CAR" for c in state["vr"]["controls"]):
            await self.click("PLACE ON CAR")
        before = await self.state()
        # Obtain a depth plane from a real eye-ray pick, then aim from the
        # natural controller origins. The visual proof shows separated hands.
        capture = await self.capture("car-depth-source")
        x, y = screenshot_pixel(before, capture, 1000, 500)
        await self.tool("hover_ui_at", {"capture_id": capture["capture_id"], "x": float(x),
            "y": float(y), "hand": "left", "origin_mode": "eye"}, "car-depth-hover")
        picked = await self.state()
        assert picked["vr"]["hands"][0]["car_hit"]
        camera = picked["vr"]["frame"]["camera"]
        point = np.array(picked["vr"]["hands"][0]["end"])-camera["origin"]
        depth = float(point@np.array(camera["back"]))
        self.car_surface = {"pose": {"position": [0, 0, depth], "orientation": [0, 0, 0, 1]}, "size": [20, 20]}
        await self.release("left")
        one = await self.hold_car("left", 1000, 500)
        assert one["vr"]["grab_mask"] == 1
        # A spare trigger over UI must not steal or terminate the car grab.
        tab = next(c for c in one["vr"]["controls"] if c["label"] == "MY LIVERIES")
        capture = await self.capture("blocked-ui-source")
        x, y = screenshot_pixel(one, capture, tab["x"]+tab["width"]/2, tab["y"]+tab["height"]/2)
        receipt = await self.tool("click_ui_at", {"capture_id": capture["capture_id"],
            "x": float(x), "y": float(y), "hand": "right", "origin_mode": "hand",
            "surface": ui_surface(one)}, "blocked-ui-click")
        assert receipt["status"] == "input_delivered"
        blocked = await self.state()
        assert blocked["tab"] == 1 and blocked["page"] == 2 and blocked["vr"]["grab_mask"] == 1
        assert blocked["document"] == one["document"], "Spare UI ray modified an active car gesture"
        base = await self.hold_car("right", 1150, 500)
        assert base["vr"]["grab_mask"] == 3
        await self.hold_car("left", 970, 515)
        changed = await self.hold_car("right", 1200, 485)
        # Independently reconstruct scale and twist from the picked car points.
        layer = base["document"]["layers"][selected]
        u, n = np.array(layer["projectionTangent"]), np.array(layer["projectionNormal"])
        v = np.cross(n, u)
        def span(s):
            delta = np.array(s["vr"]["hands"][1]["surface_point"])-s["vr"]["hands"][0]["surface_point"]
            return np.array([delta@u, delta@v])
        a, b = span(base), span(changed)
        ratio = np.linalg.norm(b)/np.linalg.norm(a)
        angle = math.degrees(math.atan2(a[0]*b[1]-a[1]*b[0], a@b))
        actual = changed["document"]["layers"][selected]
        assert abs(ratio-1) > .05 and abs(angle) > 1, (ratio, angle)
        assert np.allclose(actual["scale"], np.clip(np.array(layer["scale"])*ratio, .01, 4), atol=.0001)
        assert abs(actual["rotation"]-layer["rotation"]-angle) < .001
        # A hand leaving the gesture cannot snap its translation or scale.
        released = await self.release("left")
        assert released["vr"]["grab_mask"] == 2
        assert released["document"] == changed["document"]
        moved = await self.hold_car("right", 1210, 500)
        assert moved["document"]["layers"][selected]["position"] != actual["position"]
        final = await self.release("right")
        assert final["vr"]["grab_mask"] == 0
        undone = await self.click("UNDO", hand="left")
        assert undone["document"] == before["document"], "One Undo must restore the entire two-hand grab"
        redone = await self.click("REDO")
        assert redone["document"] == final["document"]
        self.cases.append({"case": "dual-hand-car-grab", "passed": True, "before": before,
            "joined": base, "transformed": changed, "one-hand": released, "after": final,
            "scale_ratio": float(ratio), "rotation_delta": float(angle)})
        await self.capture("dual-hand-result")
        print("Dual hand grab, scale, rotate, hand release, move, Undo/Redo PASS", flush=True)

    async def footer(self):
        state = await self.state()
        control = next(c for c in state["vr"]["controls"] if c["label"] == "SAVE AND APPLY")
        capture = await self.capture("visible-footer")
        x, y = screenshot_pixel(state, capture, control["x"]+control["width"]*.03,
                                control["y"]+control["height"]*.25)
        x, y = round(x), round(y)
        rgb = self.last_pixels[y-1:y+2, x-1:x+2].mean(axis=(0, 1))
        visible = rgb[0] > 150 and rgb[1] > 80 and rgb[0] > rgb[1]*1.1 and rgb[2] < 80
        self.cases.append({"case": "visible-footer", "passed": bool(visible), "pixel": [x, y], "rgb": rgb.tolist()})
        assert visible, ("Save button is hidden by showroom geometry", [x, y], rgb)
        print("Footer visible in the actual eye image PASS", flush=True)

    async def stereo_rays(self):
        before = await self.state()
        for hand, label in (("left", "STICKERS"), ("right", "PAINT")):
            control = next(c for c in before["vr"]["controls"] if c["label"] == label)
            capture = await self.capture("stereo-source")
            x, y = screenshot_pixel(before, capture, control["x"]+control["width"]/2, control["y"]+control["height"]/2)
            await self.tool("hover_ui_at", {"capture_id": capture["capture_id"], "x": float(x), "y": float(y),
                "hand": hand, "origin_mode": "hand", "surface": ui_surface(before)}, "stereo-hover-"+hand)
        state = await self.state()
        capture = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "stereo-both-rays")
        self.last_capture = capture
        ribbons = capture["geometry"]["decorations"]
        assert len(ribbons) == 2, "Livery rays must use two actual compositor quads"
        assert not capture["geometry"]["surfaces"], "The livery UI remains a world-space surface"
        camera = state["vr"]["frame"]["camera"]
        inverse_camera = np.column_stack([camera[k] for k in ("right", "up", "back")]).T
        matched_ribbons = []
        observations = []
        for hand, ray in enumerate(state["vr"]["hands"]):
            assert ray["valid"] and ray["ui_hit"] and not ray["grabbed"]
            origin, end = np.array(ray["origin"]), np.array(ray["end"])
            direction = (end-origin) / np.linalg.norm(end-origin)
            local_start = inverse_camera @ (origin+direction*.05-camera["origin"])
            local_end = inverse_camera @ (end-camera["origin"])
            errors = []
            for ribbon in ribbons:
                basis = quaternion_matrix(ribbon["pose"]["orientation"])
                center = np.array(ribbon["pose"]["position"])
                extent = basis[:, 1] * ribbon["size"][1] * .5
                errors.append(float(np.linalg.norm(center-extent-local_start)+np.linalg.norm(center+extent-local_end)))
                assert abs(np.linalg.det(basis)-1) < 1e-4
                assert ribbon["visibility"] == 0 and abs(ribbon["size"][0]-.002) < 1e-6
            index = int(np.argmin(errors))
            assert errors[index] < .002, ("Livery compositor ray must match its native hand-to-hit segment", hand, errors)
            matched_ribbons.append(ribbons[index]["id"])
            self.cases.append({"case": "livery-metric-compositor-ray-"+str(hand), "passed": True,
                               "endpoint_error_m": errors[index], "ribbon": ribbons[index]})
            for eye in ("left", "right"):
                a = world_pixel(state, capture, origin+(end-origin)*.15, eye)
                b = world_pixel(state, capture, origin+(end-origin)*.7, eye)
                assert np.linalg.norm(b-a) > 20, "Ray collapsed into its eye camera"
                visible = 0
                for t in (.25, .4, .55):
                    x, y = np.round(world_pixel(state, capture, origin+(end-origin)*t, eye)).astype(int)
                    pixels = self.last_pixels[max(0,y-4):y+5, max(0,x-4):x+5].astype(float)
                    r, g, blue = pixels[:,:,0], pixels[:,:,1], pixels[:,:,2]
                    color = (blue > r+25) & (g > r+25) if hand == 0 else (r > blue+50) & (g > blue+25) & (r > 140)
                    visible += int(np.any(color))
                observations.append({"hand": hand, "eye": eye, "visible_samples": visible})
                assert visible >= 2, ("Controller ray missing from eye image", observations[-1])
        assert len(set(matched_ribbons)) == 2, "Each controller needs its own ribbon quad"
        self.cases.append({"case": "both-controller-rays-in-both-eyes", "passed": True, "observations": observations})
        print("Both controller rays visible in both eye images PASS", flush=True)

    async def layout(self):
        async def inspect(label, window=None):
            state = await self.state()
            await self.capture("layout-"+label)
            controls = state["vr"]["controls"]
            if window:
                controls = [c for c in controls if c.get("window", "").startswith(window)]
                assert controls, ("Missing modal controls", window)
            overlaps = []
            cramped = []
            for control in controls:
                assert (0 <= control["x"] < control["x"]+control["width"] <= 1920 and
                        0 <= control["y"] < control["y"]+control["height"] <= 1080), control
            for i, a in enumerate(controls):
                for b in controls[i+1:]:
                    dx = min(a["x"]+a["width"], b["x"]+b["width"])-max(a["x"], b["x"])
                    dy = min(a["y"]+a["height"], b["y"]+b["height"])-max(a["y"], b["y"])
                    if dx > 1 and dy > 1:
                        overlaps.append({"controls": [a["label"], b["label"]], "intersection": [dx, dy]})
                    # Palette cells intentionally form a contiguous color grid.
                    palette = any(c["label"].startswith(("Paint color ", "Sticker color ")) for c in (a, b))
                    if not palette and ((dy > min(a["height"],b["height"])*.5 and -5.5 < dx <= 0) or
                                        (dx > min(a["width"],b["width"])*.5 and -5.5 < dy <= 0)):
                        cramped.append({"controls": [a["label"], b["label"]], "gaps": [-dx, -dy]})
            self.cases.append({"case": "layout-"+label, "passed": not overlaps and not cramped,
                               "overlaps": overlaps, "cramped": cramped,
                               "controls": controls})
            assert not overlaps, (label, overlaps)
            assert not cramped, (label, cramped)
            print("Layout", label, "PASS", flush=True)

        await self.click("PAINT")
        await inspect("paint")
        document = (await self.state())["document"]
        for view, side in (("Roof", 4), ("Left", 0), ("Right", 1), ("Front", 2), ("Rear", 3), ("STUDIO", 6)):
            after = await self.click(view)
            assert after["camera_view"] == side and after["document"] == document, view
        self.cases.append({"case": "all-view-buttons-with-paint-panel", "passed": True})
        original = await self.state()
        for fraction in (.2, .8):
            current = await self.state()
            # The Gloss finish preset shares this label; the continuous field is wider.
            slider = max((c for c in current["vr"]["controls"] if c["label"] == "Gloss"),
                         key=lambda c: c["width"])
            capture = await self.capture("slider-drag-source")
            x, y = screenshot_pixel(current, capture, slider["x"]+slider["width"]*fraction,
                                    slider["y"]+slider["height"]*.5)
            receipt = await self.tool("hold_ui_at", {"capture_id": capture["capture_id"],
                "x": float(x), "y": float(y), "hand": "left", "origin_mode": "hand",
                "surface": ui_surface(current), "dwell_frames": 12}, "slider-drag")
            assert receipt["status"] == "input_delivered"
            value = (await self.state())["document"]["gloss"]
            assert abs(value-fraction) < .04, (fraction, value, slider)
        dragged = await self.release("left")
        assert abs(dragged["document"]["gloss"]-.8) < .04, dragged["document"]
        restored = await self.click("UNDO")
        assert restored["document"] == original["document"], "One Undo must restore the slider drag"
        self.cases.append({"case": "spaced-slider-drag-and-single-undo", "passed": True})
        await self.click("STICKERS")
        await inspect("layers")
        await self.click("Layer "+str((await self.state())["selected"]))
        for label in ("MOVE / ROTATE", "SCALE / SKEW", "COLOR", "PROJECTION", "CAMERA"):
            await self.click(label)
            await inspect(label.replace(" / ", "-").lower())
        await self.click("MY LIVERIES")
        await inspect("my-liveries")
        await self.click("Livery name")
        await inspect("keyboard", "Livery keyboard")
        await self.click("CANCEL")
        await self.click("BROWSE WORKSHOP")
        await inspect("workshop", "Browse Workshop liveries")
        await self.click("BACK TO MY LIVERIES")
        await self.click("STICKERS")
        await self.click("ADD STICKER")
        await inspect("library")
        before = await self.state()
        count = (await asyncio.to_thread(game_read, self.args.pid, "catalog"))["categories"]
        after = await self.click(">", hold_frames=45, retry=True)
        assert after["category"] == (before["category"]+1) % count
        after = await self.click("<", hold_frames=45, retry=True)
        assert after["category"] == before["category"] and after["document"] == document
        self.cases.append({"case": "category-arrows-single-step", "passed": True})

    async def ray_dialogs(self):
        """Ray-sized tool dialogs, reversible RGB edits, and an undimmed scene."""
        await self.click("MY LIVERIES")
        baseline = (await self.state())["document"]
        before = await self.state()
        source = await self.capture("modal-scene-source")
        source_pixels = self.last_pixels.copy().astype(float)
        await self.click("Livery name")
        modal = await self.state()
        await self.capture("modal-scene-keyboard")
        after_pixels = self.last_pixels.astype(float)
        assert source_pixels.shape == after_pixels.shape
        popup = next(w for w in modal["desktop_input"]["popup_windows"] if w["name"] == "Livery keyboard")
        x, y = popup["position"]; width, height = popup["size"]
        a, b = screenshot_pixel(before, source, x-12, y-12), screenshot_pixel(before, source, x+width+12, y+height+12)
        mask = np.ones(source_pixels.shape[:2], dtype=bool)
        l, r = sorted((round(a[0]), round(b[0]))); top, bottom = sorted((round(a[1]), round(b[1])))
        mask[max(0,top):max(0,bottom), max(0,l):max(0,r)] = False
        luma = lambda pixels: pixels @ np.array([.2126, .7152, .0722])
        bright = mask & (luma(source_pixels)>55)
        samples = int(bright.sum())
        ratio = float(np.median(luma(after_pixels)[bright]/luma(source_pixels)[bright])) if samples else 0
        self.cases.append({"case":"modal-preserves-visible-scene","passed":samples>100 and ratio>.85,
                           "samples":samples,"median_brightness_ratio":ratio,"popup":popup})
        assert samples>100 and ratio>.85, ("Livery modal darkens the scene behind it", samples, ratio)
        await self.click("CANCEL")

        await self.click("STICKERS")
        await self.click("Layer "+str((await self.state())["selected"]))
        await self.click("PROJECTION")
        for side, label in enumerate(("Left", "Right", "Front", "Rear", "Roof", "Bonnet")):
            original = await self.state()
            await self.click("Surface")
            opened = await self.state()
            controls = [c for c in opened["vr"]["controls"] if c["window"] == "Choose sticker surface"]
            assert len(controls)==7, ("Surface picker needs six choices and Cancel", controls)
            assert all(c["width"]>=200 and c["height"]>=60 for c in controls), controls
            for i,a in enumerate(controls):
                for b in controls[i+1:]:
                    dx=min(a["x"]+a["width"],b["x"]+b["width"])-max(a["x"],b["x"])
                    dy=min(a["y"]+a["height"],b["y"]+b["height"])-max(a["y"],b["y"])
                    assert dx<=-16 or dy<=-16, ("Surface choices need separated targets",a,b)
            blocked = await self.click("PAINT", hold_frames=45)
            assert blocked["tab"]==1 and blocked["vr"]["popup_open"], "A modal allowed click-through"
            changed = await self.click("Surface "+label, hold_frames=45, retry=True)
            layer = changed["document"]["layers"][changed["selected"]]
            assert layer["side"]==side and not layer["surfaceAligned"] and not changed["vr"]["popup_open"]
            assert changed["generation"]<=original["generation"]+1
            if changed["document"] != original["document"]:
                assert (await self.click("UNDO"))["document"] == original["document"]
            self.cases.append({"case":"surface-"+label,"passed":True,"controls":controls})
        await self.click("Surface")
        assert (await self.click("CANCEL"))["document"] == baseline

        async def channel(label, fraction):
            state = await self.state()
            c = next(c for c in state["vr"]["controls"] if c["label"]==label)
            capture = await self.capture("rgb-source")
            x,y = screenshot_pixel(state,capture,c["x"]+c["width"]*fraction,c["y"]+c["height"]*.5)
            r = await self.tool("hold_ui_at",{"capture_id":capture["capture_id"],"x":float(x),"y":float(y),
                "hand":"left","origin_mode":"hand","surface":ui_surface(state),"dwell_frames":12},"rgb-drag")
            assert r["status"]=="input_delivered"
            return await self.release("left")

        for paint in (True, False):
            await self.click("PAINT" if paint else "COLOR")
            label = "Custom paint color" if paint else "Custom sticker color"
            original = await self.state()
            await self.click(label)
            await channel("Color red",.25)
            await channel("Color green",.7)
            canceled = await self.click("CANCEL")
            assert canceled["document"] == original["document"], "Cancel lost the original color"
            await self.click(label)
            await channel("Color red",.2)
            changed = await channel("Color blue",.8)
            color = changed["document"]["paint"] if paint else changed["document"]["layers"][changed["selected"]]["color"]
            assert abs(color[0]-.2)<.035 and abs(color[2]-.8)<.035, color
            applied = await self.click("APPLY COLOR",hold_frames=45,retry=True)
            assert not applied["vr"]["popup_open"]
            assert (await self.click("UNDO"))["document"] == original["document"], "One Undo must restore all RGB edits"
            self.cases.append({"case":"custom-"+("paint" if paint else "sticker")+"-color","passed":True})
            if paint:
                await self.click("STICKERS")
                await self.click("Layer "+str((await self.state())["selected"]))
        assert (await self.state())["document"] == baseline

    async def galleries(self):
        """Every layer and saved slot has a full card reachable by ray buttons."""
        baseline=(await self.state())["document"]
        async def navigate(label, **kwargs):
            before=await self.state()
            after=await self.click(label, **kwargs)
            assert after["document"]==before["document"] and after["generation"]==before["generation"], (
                "Gallery navigation changed the design",label)
            return after
        assert len(baseline["layers"])>6, "Use at least seven layers in the isolated gallery fixture"
        await navigate("STICKERS")
        while (await self.state())["selected"]//6>0:
            page=(await self.state())["selected"]//6
            assert (await navigate("Layer page previous"))["selected"]//6==page-1
        for page in range((len(baseline["layers"])+5)//6):
            state=await self.state()
            expected=["Layer "+str(i) for i in range(page*6,min(page*6+6,len(baseline["layers"])))]
            cards=[c for c in state["vr"]["controls"] if c["label"].startswith("Layer ") and c["label"][6:].isdigit()]
            passed=[c["label"] for c in cards]==expected and all(c["width"]>=196 and c["height"]>=140 for c in cards)
            self.cases.append({"case":"layer-gallery-page-"+str(page),"passed":passed,"expected":expected,"cards":cards})
            await self.capture("layer-gallery-page-"+str(page))
            assert passed, ("Layer gallery has clipped or missing ray cards",expected,cards)
            for label in expected:
                selected=await navigate(label)
                assert selected["selected"]==int(label.split()[1]) and selected["page"]==2
                assert selected["document"]==baseline
                await navigate("BACK TO LAYERS")
            if page<(len(baseline["layers"])-1)//6:
                changed=await navigate("Layer page next",hold_frames=45,retry=True)
                assert changed["selected"]==(page+1)*6 and changed["document"]==baseline
        before=await self.state()
        assert (await navigate("Layer page next",hold_frames=45,retry=True))["selected"]==before["selected"]
        while (await self.state())["selected"]//6>0:
            page=(await self.state())["selected"]//6
            assert (await navigate("Layer page previous",hold_frames=45,retry=True))["selected"]//6==page-1
        before=await self.state()
        assert (await navigate("Layer page previous",hold_frames=45,retry=True))["selected"]==before["selected"]

        await navigate("MY LIVERIES")
        while (await self.state())["slot"]//6>0:
            page=(await self.state())["slot"]//6
            assert (await navigate("Saved page previous"))["slot"]//6==page-1
        visited=[]
        for page in range(6):
            state=await self.state();expected=["Slot "+str(i+1) for i in range(page*6,min(page*6+6,32))]
            cards=[c for c in state["vr"]["controls"] if c["label"].startswith("Slot ")]
            passed=[c["label"] for c in cards]==expected and all(c["width"]>=265 and c["height"]>=172 for c in cards)
            self.cases.append({"case":"saved-gallery-page-"+str(page),"passed":passed,"expected":expected,"cards":cards})
            await self.capture("saved-gallery-page-"+str(page))
            assert passed, ("Saved gallery has clipped or missing ray cards",expected,cards)
            for label in expected:
                selected=await navigate(label,hand="left" if page%2 else "right")
                slot=int(label.split()[1])-1;visited.append(slot)
                assert selected["slot"]==slot and selected["document"]==baseline, "Selecting a saved slot applied it"
            if page<5:
                changed=await navigate("Saved page next",hold_frames=45,retry=True)
                assert changed["slot"]==(page+1)*6 and changed["document"]==baseline
        assert visited==list(range(32))
        before=await self.state()
        assert (await navigate("Saved page next",hold_frames=45,retry=True))["slot"]==before["slot"]
        while (await self.state())["slot"]//6>0:
            page=(await self.state())["slot"]//6
            assert (await navigate("Saved page previous",hold_frames=45,retry=True))["slot"]//6==page-1
        assert (await navigate("Saved page previous",hold_frames=45,retry=True))["slot"]==0
        assert (await self.state())["document"]==baseline
        self.cases.append({"case":"all-32-slots-and-gallery-boundaries","passed":True,"slots":visited})
        assert not any(layer["locked"] for layer in baseline["layers"]), "Use unlocked layers for the empty-gallery fixture"
        await navigate("STICKERS")
        for _ in baseline["layers"]: await self.click("DELETE")
        empty=await self.state()
        assert not empty["document"]["layers"] and empty["selected"]==-1
        assert not any(c["label"].startswith("Layer ") and c["label"][6:].lstrip("-").isdigit() for c in empty["vr"]["controls"])
        for label in ("Layer page previous","Layer page next"):
            assert not (await self.click(label,hold_frames=45,retry=True))["document"]["layers"]
        assert (await self.click("ADD STICKER"))["page"]==1
        await self.click("BACK TO LAYERS")
        for _ in baseline["layers"]: await self.click("UNDO")
        assert (await self.state())["document"]==baseline
        self.cases.append({"case":"empty-gallery-and-undo-restoration","passed":True})

    async def workshop_controls(self):
        baseline=(await self.state())["document"]
        await self.click("MY LIVERIES")
        opened=await self.click("BROWSE WORKSHOP")
        assert opened["workshop_browser"]["open"]
        controls=[c for c in opened["vr"]["controls"] if c["label"] in ("PREV SORT","NEXT SORT","EDIT SEARCH","VEHICLE FILTER")]
        assert len(controls)==4 and all(c["width"]>=160 and c["height"]>=54 for c in controls), controls
        for a,b in zip(sorted(controls,key=lambda c:c["x"]),sorted(controls,key=lambda c:c["x"])[1:]):
            assert b["x"]-(a["x"]+a["width"])>=16, (a,b)
        for label,step in (("NEXT SORT",1),("PREV SORT",-1)):
            before=(await self.state())["workshop_browser"]
            changed=await self.click(label,hold_frames=45,retry=True)
            assert changed["workshop_browser"]["tab"]==(before["tab"]+step)%4
            assert changed["document"]==baseline
        for index,label in enumerate(("TRENDING","RECENT","TOP RATED","MY SUBSCRIPTIONS")):
            assert (await self.click(label))["workshop_browser"]["tab"]==index
        original=(await self.state())["workshop_browser"]["current_car"]
        assert (await self.click("VEHICLE FILTER",hold_frames=45,retry=True))["workshop_browser"]["current_car"]!=original
        assert (await self.click("VEHICLE FILTER"))["workshop_browser"]["current_car"]==original
        assert (await self.click("EDIT SEARCH"))["keyboard_target"]==5
        await self.click("CANCEL")
        closed=await self.click("BACK TO MY LIVERIES")
        assert not closed["workshop_browser"]["open"] and closed["document"]==baseline
        self.cases.append({"case":"workshop-ray-controls","passed":True,"controls":controls})

    async def footer_actions(self):
        baseline = (await self.state())["document"]
        await self.click("STICKERS")
        opened = await self.click("EDIT LAYER", hold_frames=45, retry=True)
        assert opened["tab"]==1 and opened["page"]==2 and opened["document"]==baseline
        await self.click("BACK TO LAYERS")
        await self.click("ADD STICKER")
        before = await self.state()
        assert any(c["label"]=="SEARCH" and c["window"]=="Livery footer" for c in before["vr"]["controls"])
        assert (await self.click("SEARCH"))["keyboard_target"]==1
        await self.click("CLEAR")
        for key in "circle": await self.click(key)
        searched = await self.click("DONE")
        assert searched["search"]=="circle" and 0<searched["filtered_stickers"]<before["filtered_stickers"], searched
        await self.click("SEARCH")
        await self.click("CLEAR")
        assert (await self.click("DONE"))["search"]==""
        await self.click("MY LIVERIES")
        assert (await self.click("RENAME"))["keyboard_target"]==2
        assert (await self.click("CANCEL"))["document"]==baseline
        self.cases.append({"case":"ray-footer-edit-search-rename","passed":True})

    async def layer_tools(self):
        baseline = (await self.state())["document"]
        await self.click("STICKERS")
        await self.click("EDIT LAYER")
        await self.click("SCALE / SKEW")
        original = await self.state()
        linked = original["aspect_locked"]
        if linked: assert not (await self.click("LINK RATIO",hold_frames=45,retry=True))["aspect_locked"]
        async def drag(label,fraction):
            before=await self.state();c=next(c for c in before["vr"]["controls"] if c["label"]==label)
            capture=await self.capture("layer-tool-source")
            x,y=screenshot_pixel(before,capture,c["x"]+c["width"]*fraction,c["y"]+c["height"]*.5)
            r=await self.tool("hold_ui_at",{"capture_id":capture["capture_id"],"x":float(x),"y":float(y),
                "hand":"left","origin_mode":"hand","surface":ui_surface(before),"dwell_frames":12},"layer-tool-drag")
            assert r["status"]=="input_delivered"
            return before,await self.release("left")
        before,after=await drag("Width",.4)
        a,b=[s["document"]["layers"][s["selected"]]["scale"] for s in (before,after)]
        assert abs(a[0]-b[0])>.1 and a[1]==b[1], "Unlinked Width changed Height"
        assert (await self.click("UNDO"))["document"]==before["document"]
        assert (await self.click("LINK RATIO",hold_frames=45,retry=True))["aspect_locked"]
        before,after=await drag("Height",.6)
        a,b=[s["document"]["layers"][s["selected"]]["scale"] for s in (before,after)]
        assert abs(a[1]-b[1])>.1 and abs(a[0]/a[1]-b[0]/b[1])<.001, "Linked dimensions lost their aspect ratio"
        assert (await self.click("UNDO"))["document"]==before["document"]
        if not linked: await self.click("LINK RATIO")
        self.cases.append({"case":"linked-and-unlinked-dimensions","passed":True})
        if len(baseline["layers"])>1:
            before=await self.state();index=before["selected"]
            direction=1 if index<len(baseline["layers"])-1 else -1
            first,second=("RAISE","LOWER") if direction>0 else ("LOWER","RAISE")
            after=await self.click(first,hold_frames=45,retry=True)
            assert after["selected"]==index+direction
            old_ids=[l["id"] for l in before["document"]["layers"]]
            ids=[l["id"] for l in after["document"]["layers"]]
            expected=old_ids.copy();expected[index],expected[index+direction]=expected[index+direction],expected[index]
            assert ids==expected, "Layer moved more than one place"
            returned=await self.click(second,hold_frames=45,retry=True)
            assert returned["document"]==before["document"] and returned["selected"]==index
            assert (await self.click("UNDO"))["document"]==after["document"]
            assert (await self.click("UNDO"))["document"]==before["document"]
            self.cases.append({"case":"single-step-layer-order-both-directions","passed":True})
        for mode in ("MOVE / ROTATE","SCALE / SKEW","COLOR","PROJECTION"):
            await self.click(mode)
            setup=None
            if mode=="COLOR":
                setup=await self.state()
                await self.click("Sticker color 5")
            if mode=="PROJECTION":
                setup,_=await drag("Opacity",.3)
            before=await self.state();after=await self.click("RESET",hold_frames=45,retry=True)
            layer=after["document"]["layers"][after["selected"]]
            if mode=="MOVE / ROTATE": assert layer["position"]==[0,0] and layer["rotation"]==0
            elif mode=="SCALE / SKEW": assert layer["skew"]==[0,0] and not layer["flipX"] and not layer["flipY"]
            elif mode=="COLOR": assert layer["color"]==[1,1,1,1]
            else: assert layer["opacity"]==1 and layer["angle"]==80 and layer["depth"]==1
            if after["document"]!=before["document"]:
                assert (await self.click("UNDO"))["document"]==before["document"]
            if setup and before["document"]!=setup["document"]:
                assert (await self.click("UNDO"))["document"]==setup["document"]
            if mode=="SCALE / SKEW":
                reset_document=after["document"]
                before,_=await drag("Skew horizontal",.7)
                skew_reset=await self.click("RESET",hold_frames=45,retry=True)
                assert skew_reset["document"]==reset_document, "Reset changes meaning after using the Skew track"
                assert (await self.click("UNDO"))["document"]!=skew_reset["document"]
                assert (await self.click("UNDO"))["document"]==before["document"]
            self.cases.append({"case":"reset-"+mode,"passed":True})
        assert (await self.state())["document"]==baseline

    async def saved_copies(self):
        original=await self.state();baseline=original["document"]
        assert not original["dirty"], "Start with a saved design in an isolated profile"
        await self.click("MY LIVERIES")
        await self.click("Slot 1")
        controls=(await self.state())["vr"]["controls"]
        label="SAVE COPY" if any(c["label"]=="SAVE COPY" for c in controls) else "REPLACE COPY"
        saved=await self.click(label,hold_frames=45,retry=True)
        assert "copy saved" in saved["status"].lower() and saved["document"]==baseline
        await self.click("PAINT")
        toggle="CUSTOM PAINT ON" if baseline["painted"] else "FACTORY PAINT"
        changed=await self.click(toggle)
        assert changed["document"]!=baseline
        await self.click("MY LIVERIES")
        loaded=await self.click("LOAD COPY",hold_frames=45,retry=True)
        assert loaded["document"]==baseline and "copy loaded" in loaded["status"].lower()
        assert (await self.click("UNDO"))["document"]==changed["document"]
        assert (await self.click("UNDO"))["document"]==baseline
        self.cases.append({"case":"save-copy-load-and-undo","passed":True})

    async def properties(self):
        """Change each numeric tool through its rendered track, then Undo once."""
        baseline = (await self.state())["document"]

        async def change_slider(label, lo, hi, read_value):
            before = await self.state()
            control = max((c for c in before["vr"]["controls"] if c["label"] == label),
                          key=lambda c: c["width"])
            fraction = .3 if abs(read_value(before)-(lo+(hi-lo)*.3)) > (hi-lo)*.08 else .7
            capture = await self.capture("property-source")
            x, y = screenshot_pixel(before, capture, control["x"]+control["width"]*fraction,
                                    control["y"]+control["height"]*.5)
            receipt = await self.tool("hold_ui_at", {"capture_id": capture["capture_id"],
                "x": float(x), "y": float(y), "hand": "left", "origin_mode": "hand",
                "surface": ui_surface(before), "dwell_frames": 12}, "property-drag")
            assert receipt["status"] == "input_delivered", receipt
            after = await self.release("left")
            value = read_value(after)
            assert abs(value-(lo+(hi-lo)*fraction)) < (hi-lo)*.045, (label, value, fraction)
            assert after["document"] != before["document"], label
            undone = await self.click("UNDO", hand="right")
            assert undone["document"] == before["document"], (label, "One Undo must restore the edit")
            self.cases.append({"case": "property-"+label, "passed": True,
                               "before": before, "after": after, "restored": True})
            print(label, "drag + Undo PASS", flush=True)

        async def reversible_button(label, changed):
            before = await self.state()
            after = await self.click(label, hold_frames=45, retry=True, hand="left")
            assert changed(before, after), label
            assert after["generation"] == before["generation"]+1, (label, "Held trigger repeated")
            undone = await self.click("UNDO")
            assert undone["document"] == before["document"], label
            self.cases.append({"case": "property-"+label, "passed": True, "restored": True})

        await self.click("PAINT")
        await change_slider("Gloss", 0, 1, lambda s: s["document"]["gloss"])
        await change_slider("Metallic", 0, 1, lambda s: s["document"]["metalness"])
        await reversible_button("Paint color 5", lambda a,b: a["document"]["paint"] != b["document"]["paint"])
        for label, values in (("Matte", [.08, 0, .05]), ("Gloss", [.8, .05, .3]),
                              ("Metallic", [.72, .8, .3]), ("Chrome", [1, 1, .6])):
            def finish_matches(a, b, expected=values):
                return np.allclose([b["document"][k] for k in ("gloss", "metalness", "clearCoat")], expected)
            await reversible_button(label, finish_matches)

        await self.click("STICKERS")
        await self.click("Layer "+str((await self.state())["selected"]))
        layer = lambda s: s["document"]["layers"][s["selected"]]
        assert not layer(await self.state())["locked"], "Use an unlocked, color-editable sticker"
        for mode, sliders in (
            ("MOVE / ROTATE", [("Horizontal position", -2, 2, "position", 0),
                                ("Vertical position", -2, 2, "position", 1),
                                ("Rotation", -180, 180, "rotation", None)]),
            ("SCALE / SKEW", [("Width", .005, 3, "scale", 0), ("Height", .005, 3, "scale", 1),
                               ("Skew horizontal", -.9, .9, "skew", 0), ("Skew vertical", -.9, .9, "skew", 1)]),
            ("PROJECTION", [("Opacity", 0, 1, "opacity", None), ("Angle limit", 0, 90, "angle", None),
                             ("Projection depth", .001, 4, "depth", None), ("Projection plane", -1, 1, "plane", None)])):
            await self.click(mode)
            for label, lo, hi, field, axis in sliders:
                def read(s, field=field, axis=axis):
                    value = layer(s)[field]
                    return value if axis is None else value[axis]
                await change_slider(label, lo, hi, read)
            if mode == "SCALE / SKEW":
                for label, field in (("FLIP X", "flipX"), ("FLIP Y", "flipY")):
                    await reversible_button(label, lambda a,b,field=field: layer(a)[field] != layer(b)[field])
        await self.click("COLOR")
        await reversible_button("Sticker color 5", lambda a,b: layer(a)["color"] != layer(b)["color"])
        await self.click("BACK TO LAYERS")
        await reversible_button("MIRROR", lambda a,b: len(b["document"]["layers"]) == len(a["document"]["layers"])+1)
        assert (await self.state())["document"] == baseline, "Property checks must restore the entire design"
        await self.capture("properties-restored")

    async def run(self):
        initial = await self.state()
        assert not initial["dirty"], "Start with no unsaved edits, using an isolated profile"
        await self.click("PAINT")
        before = await self.state()
        toggle = "CUSTOM PAINT ON" if before["document"]["painted"] else "FACTORY PAINT"
        changed = await self.click(toggle, hold_frames=45, retry=True)
        assert changed["document"]["painted"] != before["document"]["painted"]
        assert changed["generation"] == before["generation"]+1, "Held trigger toggled more than once"
        undone = await self.click("UNDO")
        assert undone["document"] == before["document"]
        await self.click("REDO")
        assert (await self.state())["document"] == changed["document"]
        await self.click("STICKERS")
        await self.click("ADD STICKER")
        catalog = await asyncio.to_thread(game_read, self.args.pid, "catalog")
        assert catalog["items"], catalog
        before = await self.state()
        added = await self.click("ADD STICKER", hold_frames=45, retry=True)
        assert len(added["document"]["layers"]) == len(before["document"]["layers"])+1
        assert added["generation"] == before["generation"]+1
        if any(c["label"] == "DONE PLACING" for c in added["vr"]["controls"]):
            await self.click("DONE PLACING")
        duplicated = await self.click("DUPLICATE", hold_frames=45, retry=True)
        assert len(duplicated["document"]["layers"]) == len(added["document"]["layers"])+1
        deleted = await self.click("DELETE")
        assert len(deleted["document"]["layers"]) == len(added["document"]["layers"])
        locked = await self.click("LOCK")
        current = locked["document"]["layers"][locked["selected"]]
        assert current["locked"]
        rejected = await self.click("DELETE")
        assert rejected["document"] == locked["document"], "Locked layer was deleted"
        await self.click("UNLOCK")
        hidden = await self.click("VISIBLE")
        assert not hidden["document"]["layers"][hidden["selected"]]["visible"]
        await self.click("HIDDEN")
        await self.click("MY LIVERIES")
        await self.click("Livery name")
        await self.click("CLEAR")
        for key in "ray":
            await self.click(key)
        named = await self.click("DONE")
        assert named["document"]["name"] == "ray"
        saved = await self.click("SAVE AND APPLY", retry=True)
        assert not saved["dirty"] and "saved" in saved["status"].lower(), saved
        assert saved["document"]["name"] == "ray"
        await self.capture("saved-livery")


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
                                   env={**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())})
    result = {"pid": args.pid, "passed": False, "scope": "Actual RC livery UI through simulator XR actions; game diagnostics read only"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = LiveryTest(args, session)
            try:
                if args.galleries_only:
                    await test.galleries()
                elif args.workshop_controls_only:
                    await test.workshop_controls()
                elif args.footer_actions_only:
                    await test.footer_actions()
                elif args.layer_tools_only:
                    await test.layer_tools()
                elif args.saved_copies_only:
                    await test.saved_copies()
                elif args.ray_dialogs_only:
                    await test.ray_dialogs()
                elif args.properties_only:
                    await test.properties()
                elif args.layout_only:
                    await test.layout()
                elif args.stereo_rays_only:
                    await test.stereo_rays()
                elif args.footer_only:
                    await test.footer()
                elif args.dual_hands_only:
                    await test.dual_hands()
                else:
                    await test.run()
                result["passed"] = True
            finally:
                result["cases"] = test.cases
                (args.evidence/"result.json").write_text(json.dumps(result, indent=2))
                if test.last_capture:
                    for hand in ("left", "right"):
                        await session.call_tool("release_ui_pointer", {"capture_id": test.last_capture["capture_id"], "hand": hand})
    print("Live RC livery regression passed:", args.evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--dual-hands-only", action="store_true", help="Test two-hand car gestures with an existing unlocked layer")
    parser.add_argument("--footer-only", action="store_true", help="Verify the rendered Save button is visible above showroom geometry")
    parser.add_argument("--stereo-rays-only", action="store_true", help="Verify cyan/amber controller rays remain visible in each eye")
    parser.add_argument("--layout-only", action="store_true", help="Check visible controls for overlaps across livery panels")
    parser.add_argument("--properties-only", action="store_true", help="Exercise paint/sticker properties and restore each change with Undo")
    parser.add_argument("--ray-dialogs-only", action="store_true", help="Check ray surface/color dialogs, modal isolation and scene visibility")
    parser.add_argument("--footer-actions-only", action="store_true", help="Use the ray footer buttons to edit, search and rename")
    parser.add_argument("--layer-tools-only", action="store_true", help="Check linked dimensions, layer ordering and per-tool Reset")
    parser.add_argument("--saved-copies-only", action="store_true", help="Save/load a local test copy in slot 1, then Undo back to the original design")
    parser.add_argument("--workshop-controls-only", action="store_true", help="Check Workshop sort, filter, search and Back through ray buttons")
    parser.add_argument("--galleries-only", action="store_true", help="With seven or more layers, ray-select every layer and all 32 saved slots through full cards")
    asyncio.run(run(parser.parse_args()))
