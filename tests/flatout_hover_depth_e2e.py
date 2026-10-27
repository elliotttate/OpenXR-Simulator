"""Verify real native-game button depth through submitted XR layers and ray input.

Start with a cancelable native dialog open. Diagnostics are read-only; the
hover and single Cancel click go through the simulator's real XR action path.
With --modal-replacement, the subsequent A/B/C fixtures use only the existing
non-Retail ui.pop_message_box bridge for setup/replacement. Every hover, hold,
release and activation still uses screenshot-derived real XR controller input.
"""
import argparse
import asyncio
import copy
import json
import os
import struct
from pathlib import Path
import sys
import uuid

import numpy as np
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from flatout_frontend_e2e import Frontend, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import quaternion_matrix

ROOT = Path(__file__).resolve().parents[1]


def pop_fixture_dialog(pid, content):
    """The only setup mutation: same native BASE container/type, no action callbacks."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "ui.pop_message_box",
               "params": {"type": "YesNo", "title": "VR INPUT LIFECYCLE", "content": content}}
    payload = json.dumps(request).encode()
    with open(r"\\.\pipe\FlatoutMCP_" + str(pid), "r+b", buffering=0) as pipe:
        pipe.write(struct.pack("<I", len(payload)) + payload)
        def read_exact(count):
            result = b""
            while len(result) < count:
                chunk = pipe.read(count - len(result))
                if not chunk:
                    raise RuntimeError("Game pipe closed during modal fixture setup")
                result += chunk
            return result
        response = json.loads(read_exact(struct.unpack("<I", read_exact(4))[0]))
    assert "error" not in response, response
    assert response["result"]["type"] == "YesNo", response
    return {"setup_only": True, "request": request, "response": response}


class NativeDepth(Frontend):
    async def modal_replacement(self):
        """Exercise same-type replacement and a held trigger without fabricating activation."""
        assert not (await self.query("ui.get_message_box"))["open"], "The original ray Cancel must finish first"
        initial_menu = (await self.query("menu.get_state"))["current_menu"]
        baseline_clicks = (await self.query("vr.targets"))["clicks"]
        epochs = []
        expected_names = {"msgbox.validate", "msgbox.cancel"}

        async def exclusive_targets():
            current = await self.query("vr.targets")
            assert len(current["targets"]) == 2 and {t["name"] for t in current["targets"]} == expected_names, ("Replacement dialog lost exclusive targets", current)
            return current

        async def pop(epoch):
            marker = "RAY PRESENTATION " + epoch
            evidence = await asyncio.wait_for(asyncio.to_thread(pop_fixture_dialog, self.args.pid, marker), 15)
            self.sequence += 1
            (self.args.evidence/f"{self.sequence:03}-modal-{epoch}-setup-only.json").write_text(json.dumps(evidence, indent=2))
            source = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "modal-"+epoch+"-source")
            self.last_capture = source
            modal = await self.query("ui.get_message_box")
            assert modal["open"] and modal["layers"] == [0], ("Fixture must reuse the single BASE layer", modal)
            nodes = (await self.query("hud.rects", {"root": "msgbox", "max_depth": 14}))["nodes"]
            visible_text = [n.get("text", "") for n in nodes if n["visible"]]
            assert any(marker in text for text in visible_text), ("Replacement content was not rendered", marker, visible_text)
            assert not any("RAY PRESENTATION "+old in text for old in "ABC" if old != epoch for text in visible_text), visible_text
            current = await exclusive_targets()
            action = next(t for t in current["targets"] if t["name"] == "msgbox.cancel")
            planes = [s for s in source["geometry"]["surfaces"] if s["visibility"] == 0]
            assert planes, "Fixture requires the native HUD composition path"
            base = max(planes, key=lambda s: s["size"][0]*s["size"][1])
            carrier = copy.deepcopy(source)
            carrier["geometry"]["surfaces"] = [base]
            x, y = quad_pixel(carrier, action["rect"])
            aim = {"capture_id": source["capture_id"], "surface_id": base["id"],
                   "x": float(x), "y": float(y), "hand": self.args.hand,
                   "origin_mode": "hand", "ray_offset": RAY_OFFSET, "dwell_frames": 30, "timeout": 30}
            record = {"marker": marker, "setup_only": True, "message_box": modal, "base": base,
                      "targets": current["targets"], "source_rect": action["rect"], "clicks": current["clicks"],
                      "presentation_generation": modal.get("presentation_generation"),
                      "generation_diagnostic": "Not exposed by ui.get_message_box; same BASE container and YesNo descriptor are selected by the setup bridge"}
            if epochs:
                assert record["source_rect"] == epochs[0]["source_rect"], "Same-type replacement changed the source hit rectangle"
                assert base["id"] == epochs[0]["base"]["id"] and np.allclose(base["size"], epochs[0]["base"]["size"], atol=1e-5), "Replacement changed the source carrier"
                for key in ("position", "orientation"):
                    assert np.allclose(base["pose"][key], epochs[0]["base"]["pose"][key], atol=1e-5), "Replacement moved the base plane"
            epochs.append(record)
            return aim, base

        async def delivered(name, arguments, label):
            result = await self.tool(name, arguments, label)
            if result["status"] != "input_delivered":
                self.cases.append({"case": "modal-input-delivery-error-state", "passed": False,
                                   "tool": name, "receipt": result,
                                   "message_box_after_error": await self.query("ui.get_message_box"),
                                   "native_after_error": await self.query("vr.targets")})
            assert result["status"] == "input_delivered", result
            self.last_capture = result["capture"]
            return result

        aim_a, _ = await pop("A")
        await delivered("hover_ui_at", aim_a, "modal-A-hover-cancel")
        current = await exclusive_targets()
        assert current["hovered"] == "msgbox.cancel" and current["clicks"] == baseline_clicks, current
        aim_b, _ = await pop("B")  # Same owner/type and semantic name, while the A hover is still leased.
        hovered_b = await delivered("hover_ui_at", aim_b, "modal-B-hover-after-replacement")
        current = await exclusive_targets()
        assert current["hovered"] == "msgbox.cancel" and current["clicks"] == baseline_clicks, current
        assert len(hovered_b["capture"]["geometry"]["surfaces"]) <= 2, "Replacement retained the previous presentation's transient tile"
        self.cases.append({"case": "same-native-modal-replacement-keeps-exclusive-stable-targets", "passed": True,
                           "setup_method": "ui.pop_message_box", "epochs": copy.deepcopy(epochs)})

        # The entrance capture can contain a tile still travelling toward the
        # camera. Reproject the hold from a new screenshot of the settled B
        # presentation, explicitly choosing its fixed base carrier.
        settled_b = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "modal-B-settled-before-hold")
        self.last_capture = settled_b
        current = await exclusive_targets()
        action_b = next(t for t in current["targets"] if t["name"] == "msgbox.cancel")
        assert action_b["rect"] == epochs[-1]["source_rect"], "Settled B changed its source hit rectangle"
        carrier_b = copy.deepcopy(settled_b)
        carrier_b["geometry"]["surfaces"] = [next(s for s in settled_b["geometry"]["surfaces"] if s["id"] == aim_b["surface_id"])]
        px, py = quad_pixel(carrier_b, action_b["rect"])
        held_b = {**aim_b, "capture_id": settled_b["capture_id"], "x": float(px), "y": float(py),
                  "command_id": uuid.uuid4().hex, "dwell_frames": 45}
        epochs[-1]["settled_hold_capture_id"] = settled_b["capture_id"]
        held = await delivered("hold_ui_at", held_b, "modal-B-hold-cancel-once")
        assert held["delivery"]["button_down"] is True, "The test must preserve an actual held XR trigger"
        assert not (await self.query("ui.get_message_box"))["open"], "The initial held Cancel did not dismiss B immediately"
        assert (await self.query("vr.targets"))["clicks"] == baseline_clicks+1, "Held Cancel did not fire exactly once"

        aim_c, base_c = await pop("C")  # Reopen before releasing the trigger; a new dialog must not consume the old press.
        current = await exclusive_targets()
        assert current["clicks"] == baseline_clicks+1, "Opening C consumed the still-held trigger"
        retry = await delivered("hold_ui_at", held_b, "modal-B-held-uuid-retry-over-C")
        assert retry["delivery"]["applied_frame"] == held["delivery"]["applied_frame"], "Retry was not the cached delivery"
        assert (await self.query("ui.get_message_box"))["open"], "Retry dismissed the replacement dialog"
        refresh = await delivered("hold_ui_at", {**aim_c, "dwell_frames": 45}, "modal-C-refresh-existing-hold")
        assert refresh["delivery"]["button_down"] is True, "The continuous-hold gate lost its trigger lease"
        current = await exclusive_targets()
        assert current["hovered"] == "msgbox.cancel" and current["clicks"] == baseline_clicks+1, current
        assert (await self.query("ui.get_message_box"))["open"], "A held press crossed into the replacement dialog"
        await delivered("release_ui_pointer", {"capture_id": self.last_capture["capture_id"], "hand": self.args.hand}, "modal-C-release-old-press")
        assert (await self.query("ui.get_message_box"))["open"], "Release activated an action belonging to the replaced dialog"
        assert (await self.query("vr.targets"))["clicks"] == baseline_clicks+1, "Release dispatched a stale action"
        self.cases.append({"case": "native-replacement-rejects-held-press-retry-and-release", "passed": True,
                           "hand": self.args.hand, "epoch_C": epochs[-1], "held_delivery": held["delivery"],
                           "cached_retry_delivery": retry["delivery"], "continuous_hold_delivery": refresh["delivery"]})

        final = {**aim_c, "command_id": uuid.uuid4().hex, "hold_frames": 45}
        await delivered("click_ui_at", final, "modal-C-fresh-click-once")
        assert not (await self.query("ui.get_message_box"))["open"], "A fresh press failed to dismiss C"
        assert (await self.query("vr.targets"))["clicks"] == baseline_clicks+2, "A fresh press did not fire exactly once"
        await delivered("click_ui_at", final, "modal-C-fresh-click-same-id-retry")
        closed = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "modal-C-final-dismissed")
        self.last_capture = closed
        after = await self.query("vr.targets")
        assert after["clicks"] == baseline_clicks+2 and not any(t["name"].startswith("msgbox.") for t in after["targets"]), after
        assert not any(s["id"] != base_c["id"] for s in closed["geometry"]["surfaces"]), "Final dismissal left a stale transient button layer"
        assert (await self.query("menu.get_state"))["current_menu"] == initial_menu, "Fixture activation changed the underlying menu"
        self.cases.append({"case": "native-replacement-fresh-press-dismisses-once-and-retires-tiles", "passed": True,
                           "hand": self.args.hand, "final_surfaces": closed["geometry"]["surfaces"], "epochs": epochs})

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
        if self.args.motion_lifecycle:
            observations=[]
            for name in ("msgbox.cancel","msgbox.validate","msgbox.cancel"):
                target=next(t for t in before["targets"] if t["name"]==name)
                px,py=quad_pixel(capture,target["rect"])
                receipt=await self.tool("hover_ui_at",{**aim,"x":float(px),"y":float(py),"dwell_frames":1},"native-rapid-"+name)
                assert receipt["status"]=="input_delivered",receipt
                current=await self.query("vr.targets")
                assert current["hovered"]==name and current["clicks"]==before["clicks"],current
                layers=receipt["capture"]["geometry"]["surfaces"]
                assert len(layers)<=3,("More than two transient button layers",layers)
                observations.append({"hovered":name,"submitted_surfaces":layers})
            px,py=quad_pixel(capture,(100,100,100,100))
            returned=await self.tool("hover_ui_at",{**aim,"x":float(px),"y":float(py)},"native-hover-return")
            assert returned["status"]=="input_delivered",returned
            current=await self.query("vr.targets")
            assert not current["hovered"] and current["clicks"]==before["clicks"],current
            layers=returned["capture"]["geometry"]["surfaces"]
            assert len(layers)==1 and layers[0]["id"]==base["id"],("Returned hover left a transient layer",layers)
            self.cases.append({"case":"rapid-focus-and-return-release-transient-layers","passed":True,
                               "observations":observations,"returned_surfaces":layers})
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
        if self.args.reduced_motion:
            assert np.linalg.norm(delta)<.001 and abs(approach)<.001,("Reduced motion still changes physical distance",delta.tolist(),approach)
            pixels=np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
            face=copy.deepcopy(raised);face["geometry"]["surfaces"]=[tile]
            feedback=[]
            for eye in ("left","right"):
                x0,y0=quad_pixel(face,(0,0,0,0),eye);x1,y1=quad_pixel(face,(76.8,1080,76.8,1080),eye)
                patch=pixels[int(np.floor(min(y0,y1))):int(np.ceil(max(y0,y1))),int(np.floor(min(x0,x1))):int(np.ceil(max(x0,x1)))]
                white=(np.min(patch,axis=2)>160)&(np.max(patch,axis=2)-np.min(patch,axis=2)<30)
                count=int(np.sum(white));feedback.append({"eye":eye,"white_border_pixels":count})
                assert count>=8,("Reduced motion lacks flat hover feedback",feedback)
            self.cases.append({"case":"reduced-motion-keeps-flat-color-feedback-in-both-eyes","passed":True,"feedback":feedback})
        else:
            assert abs(delta[2]-HOVER_LIFT_METERS) <= .001 and approach >= HOVER_LIFT_METERS*.8, ("Button did not physically approach the camera", delta.tolist(), approach)
        assert np.allclose(tile["size"], expected_size, atol=.006), ("Hover changed the physical button size", tile["size"], expected_size)
        rays = [layer for layer in raised["geometry"]["decorations"] if layer.get("role")=="one_texel_ribbon"]
        assert len(rays) == 2, "Both hand beams must stay on their compositor ribbon layers"
        origin = np.array(hovered["target"]["application_ray"]["position"])
        direction = np.array(hovered["target"]["application_ray"]["direction"])
        def endpoints(ray):
            axis = quaternion_matrix(ray["pose"]["orientation"])[:, 1] * ray["size"][1] * .5
            midpoint = np.array(ray["pose"]["position"])
            return midpoint-axis, midpoint+axis
        ray = min(rays, key=lambda r: np.linalg.norm(endpoints(r)[0] - origin - direction*.05))
        end_on_face = rotation.T @ (endpoints(ray)[1] - np.array(tile["pose"]["position"]))
        assert abs(end_on_face[2]) < .002, ("Ray extends past the raised button face", end_on_face.tolist())
        assert all(abs(end_on_face[i]) <= tile["size"][i]*.5+.002 for i in range(2)), end_on_face.tolist()
        assert ray["visibility"] == 0 and ray["id"] > max(s["id"] for s in layers), ray
        self.cases.append({"case": "compositor-ray-stops-at-raised-button-face", "passed": True,
                           "endpoint_on_face_m": end_on_face.tolist(), "ray": ray})
        self.cases.append({"case": "native-button-flat-without-scaling" if self.args.reduced_motion else "native-button-physically-approaches-without-scaling", "passed": True,
                           "local_delta_m": delta.tolist(), "camera_approach_m": approach,
                           "tile": tile, "source_rect": action["rect"]})
        if self.args.raised_edge_top:
            # Aim inside the visible top edge of the raised face. From the hand
            # below it, this ray crosses the base plane outside the source rect.
            face = copy.deepcopy(raised)
            face["geometry"]["surfaces"] = [tile]
            x,y = quad_pixel(face, (960,1.08,960,1.08))
            aim = {**aim,"capture_id":raised["capture_id"],"surface_id":tile["id"],"x":float(x),"y":float(y)}
            edge = await self.tool("hover_ui_at",aim,"native-raised-face-top-edge")
            assert edge["status"] == "input_delivered",edge
            ray = edge["target"]["application_ray"]
            origin,direction = np.array(ray["position"]),np.array(ray["direction"])
            normal = rotation[:,2]
            distance = float(np.dot(center-origin,normal)/np.dot(direction,normal))
            base_hit = rotation.T @ (origin+direction*distance-center)
            assert base_hit[1] > expected_size[1]/2+.001,("Edge ray also hits the base rectangle; union gate is ineffective",base_hit.tolist())
            current = await self.query("vr.targets")
            assert current["hovered"] == action["name"] and current["clicks"] == before["clicks"],current
            self.cases.append({"case":"raised-face-edge-outside-base-stays-targetable","passed":True,
                               "hand":self.args.hand,"source_plane_hit_local_m":base_hit.tolist(),"source_height_m":expected_size[1]})
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
        closed = await self.tool("capture_screenshot", {"eye": "both", "max_width": 4096}, "native-depth-dismissed")
        self.last_capture = closed
        assert not any(s["id"] == tile["id"] for s in closed["geometry"]["surfaces"]), "Dismissed dialog left a raised button layer behind"
        self.cases.append({"case": "moving-native-button-single-click-and-retry", "passed": True, "hand": self.args.hand,
                           "surfaces_after_dismissal": closed["geometry"]["surfaces"]})
        if self.args.modal_replacement:
            await self.modal_replacement()


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")], env=env)
    result = {"passed": False, "pid": args.pid, "hand": args.hand, "reduced_motion":args.reduced_motion, "modal_replacement":args.modal_replacement,
              "scope": "Native dialog intentional flat feedback, BOTH-eye XR layers and one ray click" if args.reduced_motion else "Native dialog physical depth, actual BOTH-eye XR layers and one ray click"}
    if args.modal_replacement:
        result["scope"] += "; setup-only same native BASE modal injection and real XR held-input/replacement lifecycle"
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
    parser.add_argument("--raised-edge-top", action="store_true", help="Hover and click the raised face near its top edge, proving the hand ray misses the original base rectangle")
    parser.add_argument("--motion-lifecycle", action="store_true", help="Move Cancel to Validate to Cancel, then exit and verify transient layers return before the final hover/click")
    parser.add_argument("--reduced-motion", action="store_true", help="Require zero physical travel with visible flat hover feedback in both eyes")
    parser.add_argument("--modal-replacement", action="store_true", help="After the ordinary real-XR depth proof, inject A/B/C using setup-only ui.pop_message_box and verify replacement, continuous held-trigger rejection, release and fresh XR activation")
    args=parser.parse_args()
    if args.reduced_motion and args.raised_edge_top:
        parser.error("--raised-edge-top needs physical movement; test reduced motion separately")
    asyncio.run(main(args))
