"""Actual MCP screenshot -> XR aim -> independent application hit test -> stereo pixels.

Requires the same mcp/Pillow/numpy Python environment as tests/ui_automation_e2e.py.
This isolated probe never opens Fox or changes a game profile.
"""
import argparse
import asyncio
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from io import BytesIO

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def state(directory, predicate=lambda s: True, timeout=8):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        try:
            value = json.loads((directory/"hover_depth.json").read_text())
            if predicate(value):
                return value
        except (OSError, ValueError):
            pass
        await asyncio.sleep(.01)
    raise AssertionError("Probe did not reach expected state")


async def tool(session, name, args, directory, label):
    response = await session.call_tool(name, args)
    texts = [c.text for c in response.content if c.type == "text"]
    assert not response.isError, texts
    value = json.loads(texts[0])
    assert value.get("status") != "error", value
    (directory/f"{label}.json").write_text(json.dumps({"result":value,"texts":texts}, indent=2))
    images = [c for c in response.content if c.type == "image"]
    pixels = None
    if images:
        image = base64.b64decode(images[0].data)
        (directory/f"{label}.jpg").write_bytes(image)
        pixels = np.asarray(Image.open(BytesIO(image)).convert("RGB"))
    return value, pixels


def colored_centers(image, hovered=False):
    r, g, b = np.moveaxis(image, -1, 0)
    button = (g > 195) & (b > 150) & (r < 90)
    marker = (r > 150) & (b > 150) & (g < 100)
    result = []
    for left, right in ((0, image.shape[1]//2), (image.shape[1]//2, image.shape[1])):
        centers = []
        for mask_index, mask in enumerate((button, marker)):
            y, x = np.where(mask[:, left:right])
            assert len(x) > 25, "Missing button or source-plane reference in an eye"
            if mask_index==0:
                l, t, rr, bb = int(x.min()+left-1),int(y.min()-1),int(x.max()+left+2),int(y.max()+2)
                patch=image[t:bb,l:rr].astype(float)
                weight=np.maximum(0,np.minimum(patch[:,:,1],patch[:,:,2])-2*patch[:,:,0])
                yy, xx=np.indices(weight.shape)
                centers.append([float((xx*weight).sum()/weight.sum()+l),float((yy*weight).sum()/weight.sum()+t)])
            else:
                centers.append([float(x.mean()+left), float(y.mean())])
        result.append(centers)
    return result


def relative_disparity(capture, centers):
    # Normalize by each real eye's asymmetric FOV and viewport. Comparing the
    # button with a marker on its source plane rejects 2D scaling/shadow illusions.
    tangents = []
    width, height = capture["image_size"]
    crop_x, crop_y, crop_w, crop_h = capture["crop"]
    for eye, (button, marker) in zip(capture["geometry"]["eyes"], centers):
        left, right, up, down = map(math.tan, eye["fov"])
        ex, ey, ew, eh = eye["viewport"]
        tx = []
        for x, y in (button, marker):
            raw_x = crop_x+(x+.5)*crop_w/width
            tx.append(left+(raw_x-ex)/ew*(right-left))
        tangents.append(tx[0]-tx[1])
    return tangents[0]-tangents[1]


def surface_pixel(capture, surface, u, v):
    # This fixture's world-locked panel is level and faces -Z from an identity head.
    # Use the actual submitted metric pose/size, never a guessed screenshot box.
    eye = capture["geometry"]["eyes"][0]
    assert surface["pose"]["orientation"] == [0,0,0,1]
    position = np.array(surface["pose"]["position"])+np.array([(u-.5)*surface["size"][0],(.5-v)*surface["size"][1],0])
    local = position-np.array(eye["pose"]["position"])
    left, right, up, down = map(math.tan,eye["fov"])
    ex, ey, ew, eh = eye["viewport"]
    px, py = ex+(local[0]/-local[2]-left)/(right-left)*ew, ey+(local[1]/-local[2]-up)/(down-up)*eh
    cx, cy, cw, ch = capture["crop"]
    return (px-cx)/cw*capture["image_size"][0]-.5,(py-cy)/ch*capture["image_size"][1]-.5


async def run(args):
    directory = args.evidence.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    (directory/"settings.json").write_text(json.dumps({
        "headset_profile": "quest3", "layout": "side_by_side", "view_mode": "both",
        "asymmetric_fov": True, "zoom_mode": "fit", "window_width": 3200,
        "window_height": 1800, "preview_fps": -1}))
    env = {**os.environ, "SIMXR_DATA_DIR": str(directory), "SIMXR_RUNTIME_DLL": str(args.runtime.resolve()),
           "SIMXR_AUTOMATION_ONLY": "1", "VR_HOVER_HAND": args.hand}
    if args.reduced_motion or args.flat_control:
        env["VR_HOVER_REDUCED_MOTION"] = "1"
    log = (directory/"probe.log").open("w")
    process = subprocess.Popen([str(args.probe.resolve())], env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    checks = []
    try:
        idle = await state(directory, lambda s: s["frame"] >= 4)
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                before, image = await tool(session,"capture_screenshot",{"eye":"both","max_width":4096},directory,"01-idle-stereo")
                assert len(before["geometry"]["surfaces"]) == 2
                assert all(s["visibility"] == 0 for s in before["geometry"]["surfaces"]), "Each quad must be visible to BOTH eyes"
                assert abs(before["geometry"]["head"]["position"][1]-1.7)<.01
                assert abs(idle["panel"][1]-1.7)<.01, "Stable panel must spawn at standing head height"
                centers = colored_centers(image)
                flat_disparity = relative_disparity(before, centers)
                x, y = centers[0][0]
                target = {"capture_id":before["capture_id"],"x":x,"y":y,"hand":args.hand,"surface_id":0}
                await tool(session,"hover_ui_at",{**target,"dwell_frames":45},directory,"02-hover-delivery")
                hovered = await state(directory, lambda s: s["hovered"] and s["hover_frames"] >= 30)
                hovered_capture, hovered_image = await tool(session,"capture_screenshot",{"eye":"both","max_width":4096},directory,"03-hover-stereo")
                raised_centers = colored_centers(hovered_image, True)
                extra_disparity = relative_disparity(hovered_capture, raised_centers)-flat_disparity
                head = np.array(before["geometry"]["head"]["position"])
                before_button = np.array(before["geometry"]["surfaces"][1]["pose"]["position"])
                after_button = np.array(hovered_capture["geometry"]["surfaces"][1]["pose"]["position"])
                before_distance = float(np.linalg.norm(before_button-head))
                hover_distance = float(np.linalg.norm(after_button-head))
                eyes=before["geometry"]["eyes"]
                ipd=abs(eyes[1]["pose"]["position"][0]-eyes[0]["pose"]["position"][0])
                expected=ipd*(1/(head[2]-after_button[2])-1/(head[2]-before_button[2]))
                pixel_tangent=max((math.tan(e["fov"][1])-math.tan(e["fov"][0]))/e["viewport"][2]
                                  for e in eyes)*before["crop"][2]/before["image_size"][0]
                tolerance=.65*pixel_tangent
                checks.append({"case":"stereo-depth","flat_relative_disparity":flat_disparity,
                               "hover_extra_disparity":extra_disparity,"application_lift_m":hovered["lift_m"],
                               "expected_extra_tangent_disparity":expected,"tangent_per_image_pixel":pixel_tangent,
                               "disparity_tolerance":tolerance,"actual_image_size":before["image_size"],
                               "idle_button_pose":before_button.tolist(),"hover_button_pose":after_button.tolist(),
                               "idle_camera_distance_m":before_distance,"hover_camera_distance_m":hover_distance,
                               "camera_distance_decrease_m":before_distance-hover_distance})
                (directory/"checks.json").write_text(json.dumps(checks,indent=2))
                if args.reduced_motion:
                    assert hovered["lift_m"] == 0
                    assert abs(extra_disparity)<.0007, extra_disparity
                else:
                    assert extra_disparity>tolerance, f"No additional binocular disparity above pixel precision: {extra_disparity}"
                    assert .029 < hovered["lift_m"] <= .03001, hovered
                    assert abs(extra_disparity-expected)<tolerance, f"Measured stereo depth disagrees with actual quad pose: {extra_disparity}, expected {expected}, tolerance {tolerance}"
                    assert before_distance-hover_distance>.028, "The button did not physically approach the camera"
                    surfaces = hovered_capture["geometry"]["surfaces"]
                    assert abs((surfaces[1]["pose"]["position"][2]-surfaces[0]["pose"]["position"][2])-.030)<.001
                assert hovered["clicks"] == 0 and not hovered["down"]
                if args.edge_tests:
                    for label, u, v in (("left",.005,.5),("right",.995,.5),("top",.5,.005),("bottom",.5,.995)):
                        picture, _ = await tool(session,"capture_screenshot",{"eye":"both","max_width":4096},directory,"edge-"+label+"-before")
                        x, y = surface_pixel(picture,picture["geometry"]["surfaces"][1],u,v)
                        await tool(session,"hover_ui_at",{"capture_id":picture["capture_id"],"x":x,"y":y,"hand":args.hand,"surface_id":1,"dwell_frames":20},directory,"edge-"+label+"-hover")
                        at_edge = await state(directory)
                        assert at_edge["hovered"] and at_edge["hover_frames"]>10, f"Hover lost at visible raised {label} edge: {at_edge}"
                    checks.append({"case":"raised-visual-edges-keep-same-target","edges":4})
                # Retarget a fresh picture, but use the immutable base surface.
                # Motion must not invalidate the semantic target's source plane.
                x, y = raised_centers[0][0]
                target = {"capture_id":hovered_capture["capture_id"],"x":x,"y":y,"hand":args.hand,"surface_id":0}
                key = uuid.uuid4().hex
                click_args = {**target,"hold_frames":45,"command_id":key}
                observations = []
                click = asyncio.create_task(tool(session,"click_ui_at",click_args,directory,"04-click-once"))
                while not click.done():
                    observations.append(await state(directory))
                    await asyncio.sleep(.005)
                await click
                clicked = await state(directory,lambda s:s["release_edges"] == 1)
                assert clicked["clicks"] == clicked["press_edges"] == clicked["release_edges"] == 1
                pressed = [s for s in observations if s["down"]]
                assert pressed, "No actual application press state observed"
                if not args.reduced_motion:
                    assert min(s["lift_m"] for s in pressed)<.008, "Press did not depress the lifted button"
                await tool(session,"click_ui_at",click_args,directory,"05-idempotent-retry")
                retried = await state(directory,lambda s:s["frame"] > clicked["frame"]+5)
                assert retried["clicks"] == retried["press_edges"] == retried["release_edges"] == 1
                checks.append({"case":"one-click-and-retry","state":retried,
                               "pressed_min_lift_m":min(s["lift_m"] for s in pressed)})
                await tool(session,"release_ui_pointer",{"capture_id":hovered_capture["capture_id"],"hand":args.hand},directory,"06-release")
                exited = await state(directory,lambda s:not s["hovered"] and s["lift_m"]<.0001)
                after, _ = await tool(session,"capture_screenshot",{"eye":"both","max_width":4096},directory,"07-returned-to-plane")
                checks.append({"case":"hover-exit","state":exited})
                # A standing user can move their head without dragging the menu along.
                await session.call_tool("set_head_pose",{"x":.12,"y":1.82,"z":.10})
                until=time.monotonic()+5
                while True:
                    moved, moved_image = await tool(session,"capture_screenshot",{"eye":"both","max_width":4096},directory,"08-stable-local-panel")
                    if moved["geometry"]["head"]["position"][1]>1.8:
                        break
                    assert time.monotonic()<until, "Queued head movement did not arrive"
                    await asyncio.sleep(.1)
                assert moved["geometry"]["head"]["position"][1]>1.8
                for old, new in zip(before["geometry"]["surfaces"][0]["pose"]["position"],moved["geometry"]["surfaces"][0]["pose"]["position"]):
                    assert abs(old-new)<.00001
                assert np.count_nonzero(moved_image[:,:,2]>100)>1000, "Panel disappeared for standing user"
                checks.append({"case":"standing-head-relative-spawn-and-local-stability","head":moved["geometry"]["head"],"panel":idle["panel"]})
        (directory/"result.json").write_text(json.dumps({"status":"passed","hand":args.hand,"reduced_motion":args.reduced_motion,"checks":checks},indent=2))
    except BaseException as error:
        (directory/"result.json").write_text(json.dumps({"status":"failed","error":str(error),"checks":checks},indent=2))
        raise
    finally:
        (directory/"hover_depth_stop").touch()
        try:
            await asyncio.to_thread(process.wait, 15)
        finally:
            log.close()
        assert process.returncode == 0, f"Probe did not exit normally: {process.returncode}"
    print(json.dumps({"status":"passed","evidence":str(directory),"checks":len(checks)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe",type=Path,required=True)
    parser.add_argument("--runtime",type=Path,required=True)
    parser.add_argument("--evidence",type=Path,required=True)
    parser.add_argument("--hand",choices=("left","right"),default="right")
    parser.add_argument("--reduced-motion",action="store_true")
    parser.add_argument("--edge-tests",action="store_true")
    parser.add_argument("--flat-control",action="store_true",help="Intentionally disable metric lift; the normal stereo gate must fail")
    asyncio.run(run(parser.parse_args()))
