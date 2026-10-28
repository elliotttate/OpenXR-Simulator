"""Capture FlatOut's rendered UI as a searchable screenshot atlas.

This is a fast presentation survey, not a substitute for XR interaction E2E.
Native menu APIs stage real screens; screenshots come from the compositor.
Run --catalog-only first to inspect the source inventory without touching a game.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).with_name("flatout_ui_atlas_manifest.json")
READ_METHODS = {"game.get_state", "game.errors", "menu.get_state", "vr.targets", "vr.get_state",
                "hud.rects", "hud.get_node", "ui.get_message_box", "options.get_state", "statistics.get_state",
                "party.get_state", "leaderboard.get_state", "race.end_screen", "hud.pause_menu", "ui.reward_state",
                "net.get_state"}
STAGE_METHODS = {"menu.go_to", "menu.navigate", "menu.select", "menu.back", "menu.validate",
                 "ui.confirm_message_box", "ui.pop_message_box", "ui.preview_reward", "livery.command"}
LIVERY_OPS = {"state", "catalog", "browse", "view", "input", "vr_keyboard"}
# FOMainMenu callbacks establish the race mode/configuration before opening a
# submenu. menu.go_to only calls OpenMenu and cannot replace these callbacks.
ROOT_FLOWS={"career":(0,"FLATOUT","career"),"challenge":(1,"INSANITY","challenge"),
            "single_events":(3,"QUICK EVENTS","quick_events"),"multiplayer":(4,"MULTIPLAYER",None),
            "options":(5,"OPTIONS",None),"extra":(6,"EXTRAS",None)}
SHOWROOM_FLOWS={"garage":None,"cars":(0,"CARS"),"upgrade":(1,"UPGRADES"),
                "customization":(2,"SKINS"),"livery":(3,"SKINS")}
PROFILE_FLOWS=set(SHOWROOM_FLOWS)|{"career","class_selection","event_selection"}
NETWORK_FLOWS={"online","filter","game_browser","lobby","friends_lobby"}
NORMAL_FLOWS=set(ROOT_FLOWS)|set(SHOWROOM_FLOWS)|{"class_selection","event_selection","party_game","statistics","credits"}


class NativeTimeout(TimeoutError):
    pass


class UnknownModal(RuntimeError):
    pass


class CaptureMismatch(RuntimeError):
    pass


class FixtureUnavailable(RuntimeError):
    """Stop staging while preserving the failed fixture for diagnosis."""
    pass


class CaseBlocked(RuntimeError):
    """A documented prerequisite is absent; current menu remains safe to leave."""
    pass


def case_fixture_requirements(case):
    required=set(case.get("fixtures",[]))
    if any(str(step.get("goto","")).casefold() in PROFILE_FLOWS for step in case.get("route",[])):
        required.add("showroom")
    if any(str(step.get("goto","")).casefold() in NETWORK_FLOWS for step in case.get("route",[])):
        required.add("network")
    return required


def loading_widgets(nodes):
    return [n for n in nodes if n.get("visible") and
            {s.casefold() for s in n.get("path","").split("/")}&{"carloading","modelloading"}]


def career_car_caption(nodes):
    return next((n["text"].strip() for n in nodes if n.get("visible") and n.get("text","").strip() and
                 (("/info_3d_c/" in n.get("path","").casefold() and n["path"].casefold().endswith("/txt_value")) or
                  n.get("path","").endswith("/CareerWorkspace/CareerGarage/CareerGarageCar"))),None)


def livery_ready(state):
    return (state.get("ready") and state.get("vr",{}).get("active") and state.get("bindings",0)>0 and
            state.get("projection_primitives",0)>0 and bool(state.get("vr",{}).get("controls")))


def validate_capture(capture, pid, earliest, previous=None):
    geometry=capture.get("geometry",{})
    session=geometry.get("session_id","")
    if not session.startswith(str(pid)+"-") or not geometry.get("supported"):
        raise CaptureMismatch("Screenshot does not belong to the attached native VR process")
    if capture.get("created_at",0)<earliest-.5:
        raise CaptureMismatch("Screenshot predates this capture request")
    identity=(session,geometry.get("frame_id",0))
    if previous and identity[0]==previous[0] and identity[1]<=previous[1]:
        raise CaptureMismatch("Compositor did not advance to a fresh rendered frame")
    return identity


def bounded_pipe_read(pipe, count, deadline):
    """Peek before reading: an unfocused game's thread cannot strand us.

    The local pipe has one reader. Read only bytes already in its buffer.
    Requests are bounded below the server's empty 64 KiB input buffer.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    peek=kernel.PeekNamedPipe
    peek.argtypes=(wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
    peek.restype=wintypes.BOOL
    handle=msvcrt.get_osfhandle(pipe.fileno())
    data=bytearray()
    while len(data)<count:
        available=wintypes.DWORD()
        if not peek(handle,None,0,None,ctypes.byref(available),None):
            raise ctypes.WinError(ctypes.get_last_error())
        if available.value:
            chunk=pipe.read(min(count-len(data),available.value))
            if not chunk:raise RuntimeError("Native game pipe closed")
            data.extend(chunk)
        elif time.monotonic()>=deadline:
            raise NativeTimeout("Native game did not respond before the bounded pipe deadline; atlas stopped")
        else:time.sleep(.003)
    return bytes(data)


def slug(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value)).strip("-.")[:120] or "state"


def blocked_wreck(value):
    return bool(re.search(r"wreck[ _-]*run|\brogue\.", json.dumps(value), re.I))


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = set()
    for case in data["cases"]:
        if case["id"] in ids:
            raise ValueError("Duplicate case id: " + case["id"])
        ids.add(case["id"])
        if case.get("status") != "deferred" and blocked_wreck(case):
            raise ValueError("Wreck Run is deferred: " + case["id"])
    return data


def braced_block(text, start):
    """Read a source table; strings/comments are enough for this Lua inventory."""
    depth, quote, escaped = 0, None, False
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if c == quote and not escaped:
                quote = None
            escaped = c == "\\" and not escaped
            continue
        if c in ('"', "'"):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if not depth:
                return text[start:i + 1]
    return text[start:]


def discover_source(fox):
    fox = Path(fox)
    main = fox / "Flatout/Code/Sources/FOMain.h"
    bridge = fox / "Flatout/Code/Sources/FOMcpBridge.cpp"
    config = fox / "Flatout/Data/Common/Tuning/MessageBoxes.cfg"
    result = {"menus": [], "dialogs": [], "source_files": [], "errors": []}
    try:
        text = main.read_text(errors="replace")
        block = re.search(r"enum\s+Menus\s*\{(.*?)\}", text, re.S).group(1)
        for enum in dict.fromkeys(re.findall(r"\bE_[A-Z_]+\b", block)):
            result["menus"].append({"id": enum, "source": str(main.relative_to(fox)),
                "line": text[:text.index(enum, text.index("enum Menus"))].count("\n") + 1,
                "deferred": "WRECK" in enum})
        aliases = re.findall(r'\{\s*"([a-z_]+)"\s*,\s*FOMain::(E_[A-Z_]+)\s*\}', bridge.read_text(errors="replace"))
        for menu in result["menus"]:
            menu["aliases"] = [alias for alias, enum in aliases if enum == menu["id"]]
    except (OSError, AttributeError) as error:
        result["errors"].append(str(error))
    try:
        text = config.read_text(errors="replace")
        matches = list(re.finditer(r"^\t([A-Za-z]\w*)\s*=\s*\n\s*\{|^MessageBoxes\.([A-Za-z]\w*)\s*=\s*\n\s*\{", text, re.M))
        seen = set()
        for match in matches:
            name = match.group(1) or match.group(2)
            if name in seen:
                continue
            seen.add(name)
            block = braced_block(text, text.index("{", match.start()))
            mode = re.search(r"InputMode\s*=\s*MessageBoxInputMode\.(\w+)", block)
            result["dialogs"].append({"id": name, "source": str(config.relative_to(fox)),
                "line": text[:match.start()].count("\n") + 1, "input_mode": mode.group(1) if mode else None,
                "preview_safe": bool(mode and mode.group(1) in {"Ok", "OkCancel", "Cancel", "YesNo"}),
                "deferred": blocked_wreck(name)})
    except OSError as error:
        result["errors"].append(str(error))
    for folder in ("Flatout/Code/Sources/Menus", "Flatout/Code/Sources/HUD"):
        result["source_files"] += [str(p.relative_to(fox)) for p in sorted((fox / folder).rglob("*.h"))]
    result["discovered_count"] = len(result["menus"]) + len(result["dialogs"])
    return result


def state_signature(snapshot):
    """UI identity excludes camera motion, frame IDs, click counters and sounds."""
    menu = snapshot.get("menu", {})
    state = {"menu": menu.get("current_menu"), "selection": menu.get("selection_index"),
             "items": [{k: i.get(k) for k in ("index", "title", "selected", "locked", "installed")} for i in menu.get("items", [])],
             "targets": [{k: t.get(k) for k in ("name", "rect", "enabled")} for t in snapshot.get("targets", {}).get("targets", [])],
             "hovered": snapshot.get("targets", {}).get("hovered"),
             "nodes": [{k: n.get(k) for k in ("path", "rect", "text", "kind", "visible", "color")} for n in snapshot.get("nodes", [])],
             "message": snapshot.get("message", {}), "variant": snapshot.get("variant"),
             "framing":snapshot.get("framing"),
             "menu_context": {k: menu.get(k) for k in ("setup","career")},"options":snapshot.get("options"),
             "livery": {k: snapshot.get("livery", {}).get(k) for k in ("tab","page","mode","category","camera_view","keyboard_target","workshop_browser")},
             "livery_geometry": {k: snapshot.get("livery",{}).get("vr",{}).get("frame",{}).get(k) for k in ("layout","panels")}}
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pose_rotation(pose):
    """set_head_pose uses yaw(Y), pitch(X), roll(Z), in that order."""
    yaw,pitch,roll=(math.radians(pose.get(k,0)) for k in ("yaw_deg","pitch_deg","roll_deg"))
    cy,sy=math.cos(yaw),math.sin(yaw);cp,sp=math.cos(pitch),math.sin(pitch);cr,sr=math.cos(roll),math.sin(roll)
    return [[cy*cr+sy*sp*sr,-cy*sr+sy*sp*cr,sy*cp],
            [cp*sr,cp*cr,-sp],[-sy*cr+cy*sp*sr,sy*sr+cy*sp*cr,cy*cp]]


def tracking_head_pose(vr):
    eyes=[e["tracking_pose"] for e in vr["eyes"]]
    if len(eyes)!=2:raise ValueError("Both tracked eyes are required to preserve the head pose")
    position=[(eyes[0]["position"][i]+eyes[1]["position"][i])*.5 for i in range(3)]
    right,up,back=(eyes[0][k] for k in ("x","y","z"))
    pitch=math.asin(max(-1,min(1,-back[1])))
    yaw=math.atan2(back[0],back[2]) if abs(math.cos(pitch))>1e-6 else math.atan2(-right[2],right[0])
    roll=math.atan2(right[1],up[1]) if abs(math.cos(pitch))>1e-6 else 0
    return dict(zip(("x","y","z"),position),yaw_deg=math.degrees(yaw),pitch_deg=math.degrees(pitch),roll_deg=math.degrees(roll))


def head_pose_matches(vr, pose, tolerance=1e-4):
    expected=pose_rotation(pose)
    eyes=[e["tracking_pose"] for e in vr.get("eyes",[])]
    if len(eyes)!=2:return False
    position=[(eyes[0]["position"][i]+eyes[1]["position"][i])*.5 for i in range(3)]
    return (all(abs(position[i]-pose[k])<=tolerance for i,k in enumerate(("x","y","z"))) and
            all(abs(eye[k][i]-expected[i][j])<=tolerance for eye in eyes for j,k in enumerate(("x","y","z")) for i in range(3)))


def livery_tracking_state(frame):
    return {"eyes":[{"tracking_pose":{"position":frame[eye]["origin"],
                     **{dst:frame[eye][src] for dst,src in (("x","right"),("y","up"),("z","back"))}}}
                    for eye in ("left_eye","right_eye")]}


def validate_capture_pose(capture, pose):
    """A queued command or a fresh unrelated frame cannot prove the requested glance."""
    from flatout_livery_e2e import quaternion_matrix
    try:
        eyes=capture["geometry"]["projection_eyes"]
        if {e["eye"] for e in eyes}!={"left","right"}:raise ValueError("Both projection eyes are required")
        native={"eyes":[]}
        for eye in eyes:
            basis=quaternion_matrix(eye["pose"]["orientation"])
            native["eyes"].append({"tracking_pose":{"position":eye["pose"]["position"],
                                  **{k:basis[:,j].tolist() for j,k in enumerate(("x","y","z"))}}})
        if not head_pose_matches(native,pose):raise ValueError("Submitted eye poses differ from the acknowledged glance")
    except (KeyError,TypeError,ValueError) as error:
        raise CaptureMismatch("Screenshot projection geometry does not match the requested head pose: "+str(error)) from error


def livery_focus_pose(state, x, y):
    from flatout_livery_e2e import ui_frame_at
    import numpy as np
    frame=ui_frame_at(state,x,y)
    camera,panel=frame["camera"],frame["panel"]
    world=(np.array(panel["origin"])+np.array(panel["right"])*(x/1920-.5)*frame["panel_width"]+
           np.array(panel["up"])*(.5-y/1080)*frame["panel_height"])
    inverse=np.column_stack([camera[k] for k in ("right","up","back")]).T
    head=(np.array(frame["left_eye"]["origin"])+frame["right_eye"]["origin"])*.5
    delta=inverse@(world-camera["origin"])-head
    return dict(x=float(head[0]),y=float(head[1]),z=float(head[2]),
                yaw_deg=math.degrees(math.atan2(-delta[0],-delta[2])),pitch_deg=0,roll_deg=0)


def coverage_summary(report):
    cases = report.get("cases", [])
    states = report.get("states", [])
    counts = {status: sum(c.get("status") == status for c in cases)
              for status in ("captured", "blocked", "failed", "deferred", "skipped")}
    controls = {c["key"] for s in states for c in s.get("controls", [])}
    return {"discovered": report.get("catalog", {}).get("discovered_count", 0),
            "planned": len(cases), "captured_states": sum(bool(s.get("image")) and not s.get("duplicate_of") for s in states),
            "duplicate_states": sum(bool(s.get("duplicate_of")) for s in states),
            "unique_controls": len(controls), "control_observations": sum(len(s.get("controls", [])) for s in states),
            "cases": counts, "whole_game_complete": False}


def source_coverage(report):
    rows=[]
    for kind,entries in (("menu",report.get("catalog",{}).get("menus",[])),("dialog",report.get("catalog",{}).get("dialogs",[]))):
        for entry in entries:
            owners=[c for c in report.get("cases",[]) if entry["id"] in c.get("source_enums",[]) or c["id"]=="dialog."+entry["id"]]
            captured=[c for c in owners if c.get("status")=="captured"]
            status="captured" if captured else "deferred" if entry.get("deferred") else "blocked"
            reason="; ".join(c.get("reason","") for c in owners if c.get("reason"))
            if not owners:reason="No executed recipe for this source entry; requires its owning flow/fixture"
            rows.append({"kind":kind,"id":entry["id"],"status":status,"cases":[c["id"] for c in owners],"reason":reason,"source":entry["source"],"line":entry["line"]})
    return rows


def control_inventory(snapshot):
    """Inventory exposed actions separately from native controls blocked by a task."""
    controls,background=[],[]
    menu=snapshot.get("menu",{})
    prefix=menu.get("current_menu","")+":"
    labels=[n for n in snapshot.get("nodes",[]) if n.get("text","").strip()]
    livery=snapshot.get("livery",{})
    livery_windows={"livery."+str(c["id"]):c.get("window") for c in livery.get("vr",{}).get("controls",[])}
    captured_popups={w["name"] for w in livery.get("desktop_input",{}).get("popup_windows",[]) if w.get("captured")}
    centered_task=bool(captured_popups & {"Livery keyboard","Browse Workshop liveries","Share livery to Workshop"})
    for target in snapshot.get("targets",{}).get("targets",[]):
        l,t,r,b=target["rect"]
        inside=[n["text"] for n in labels if n["rect"][0]>=l-2 and n["rect"][2]<=r+2 and n["rect"][1]>=t-2 and n["rect"][3]<=b+2]
        control={**target,"label":target.get("label") or " / ".join(dict.fromkeys(inside)),"key":prefix+target["name"]}
        livery_window=livery_windows.get(target["name"])
        if target["name"].startswith("livery.") and snapshot.get("message",{}).get("open"):
            control.update(enabled=False,blocked_by="modal");background.append(control)
        elif centered_task and livery_window is not None and livery_window not in captured_popups:
            # CaptureVRUI omits the normal tool columns for these centered tasks.
            # ImGui still records their widgets; they are not exposed actions.
            control.update(enabled=False,blocked_by="modal",hidden_by="centered_livery_task");background.append(control)
        else:controls.append(control)
    for item in menu.get("items",[]):
        if not item.get("rendered_position"):continue
        control={"name":"spatial."+str(item["index"]),"label":item.get("title"),
                 "enabled":not item.get("locked",False) and not item.get("coming_soon",False) and item.get("installed",True),
                 "key":prefix+"spatial."+str(item["index"])}
        if item.get("rendered_bounds"):
            control.update(rendered_bounds=item["rendered_bounds"],measurement_kind="label_layout")
        if snapshot.get("message",{}).get("open"):
            control.update(enabled=False,blocked_by="modal")
            background.append(control)
        elif item.get("rendered_bounds",{}).get("visible") is False:
            control.update(enabled=False,hidden_by="native_visibility");background.append(control)
        else:controls.append(control)
    if menu.get("spatial_back"):
        back=menu["spatial_back"];layout=menu.get("spatial_layout",{})
        control={"name":"spatial.back","label":"Back","enabled":True,"key":prefix+"spatial.back","measurement_kind":"ray_button"}
        if all(k in layout for k in ("right","up")) and back.get("half_extents"):
            center=back["position"];width,height=back["half_extents"]
            control["rendered_bounds"]={"space":"game_world","kind":"hud_layout_quad","visible":True,
                "corners":[[center[i]+x*width*layout["right"][i]+y*height*layout["up"][i] for i in range(3)]
                           for x,y in ((-1,1),(1,1),(1,-1),(-1,-1))]}
        if snapshot.get("message",{}).get("open"):
            control.update(enabled=False,blocked_by="modal");background.append(control)
        else:controls.append(control)
    return {"controls":controls,"background_controls":background}


def spatial_review_surface(bounds, camera):
    """Measured HUD layout quad in game space -> explicit XR-local surface."""
    import numpy as np
    from flatout_livery_e2e import ui_surface
    if bounds.get("space")!="game_world" or bounds.get("kind")!="hud_layout_quad":
        raise ValueError("Spatial label has no measured game-world layout quad")
    points=np.asarray(bounds.get("corners"),dtype=float)
    if points.shape!=(4,3) or not np.isfinite(points).all():raise ValueError("Invalid spatial layout corners")
    right,up=points[1]-points[0],points[0]-points[3]
    width,height=float(np.linalg.norm(right)),float(np.linalg.norm(up))
    if min(width,height)<=1e-6:raise ValueError("Degenerate spatial layout quad")
    right/=width;up/=height
    if abs(np.dot(right,up))>1e-4 or not np.allclose(points[2],points[1]+points[3]-points[0],atol=1e-4):
        raise ValueError("Spatial layout is not a rectangular plane")
    frame={"camera":camera,"panel_width":width,"panel_height":height,
        "panel":{"origin":points.mean(axis=0).tolist(),"right":right.tolist(),"up":up.tolist(),"back":(-np.cross(right,up)).tolist()}}
    return ui_surface({"vr":{"frame":frame}})


def comfort_review_targets(capture, snapshot):
    """Resolve measured UI rectangles; never guess spatial text or mesh depth."""
    from flatout_livery_e2e import ui_surface, ui_frame_at
    native=snapshot.get("targets",{})
    frame=native.get("vr_frame") if not snapshot.get("menu",{}).get("is_3d") else None
    quads=[s for s in capture.get("geometry",{}).get("surfaces",[]) if s.get("visibility",0)==0]
    carrier=max(quads,key=lambda s:s["size"][0]*s["size"][1]) if quads else None
    measured,unmeasured=[],[]
    for control in control_inventory(snapshot)["controls"]:
        if "rect" not in control:continue
        name=control["name"]
        rect=control.get("rect")
        if not rect or not (0<=rect[0]<rect[2]<=1920 and 0<=rect[1]<rect[3]<=1080):
            unmeasured.append({"name":name,"reason":"No complete rectangle inside the native canvas"});continue
        target={"name":name,"role":"control","rect":rect,"canvas_size":[1920,1080]}
        if frame:
            try:
                selected=ui_frame_at({"vr":{"frame":frame}},(rect[0]+rect[2])*.5,(rect[1]+rect[3])*.5)
                if selected.get("canvas_rect"):
                    l,t,r,b=selected["canvas_rect"]
                    if not (l<=rect[0]<rect[2]<=r and t<=rect[1]<rect[3]<=b):
                        raise ValueError("Control crosses physical panel boundaries")
                target["surface"]=ui_surface({"vr":{"frame":frame}},(rect[0]+rect[2])*.5,(rect[1]+rect[3])*.5)
            except (KeyError,ValueError) as error:
                unmeasured.append({"name":name,"reason":str(error)});continue
        elif carrier:
            target["surface_id"]=carrier["id"]
        else:
            unmeasured.append({"name":name,"reason":"No measured world plane or both-eye composition carrier"});continue
        if len(measured)>=256:
            unmeasured.append({"name":name,"reason":"MCP analysis limit is 256 regions per capture"});continue
        measured.append(target)
    for control in control_inventory(snapshot)["controls"]:
        if "rect" not in control:
            try:
                bounds=control.get("rendered_bounds",{})
                camera=native.get("vr_frame",{}).get("camera")
                if camera is None:raise ValueError("Native spatial camera is unavailable")
                if not bounds:raise ValueError("Spatial text has no measured rectangular surface")
                surface=spatial_review_surface(bounds,camera)
                if len(measured)>=256:raise ValueError("MCP analysis limit is 256 regions per capture")
                measured.append({"name":control["name"],"role":"control" if control.get("measurement_kind")=="ray_button" else "panel","surface":surface})
            except (KeyError,ValueError,TypeError) as error:
                unmeasured.append({"name":control["name"],"reason":str(error)})
    return measured,unmeasured


def artifact_path(root, reference):
    root=Path(root).resolve();relative=Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Report artifact must remain within its evidence directory")
    resolved=(root/relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Report artifact resolves outside its evidence directory")
    return resolved


def reprocess_report(source, output):
    """Rebuild inventory from immutable saved evidence, never call a runtime."""
    source=Path(source)
    source_file=source/"report.json" if source.is_dir() else source
    source_root=source_file.parent
    output=Path(output)
    if output.exists():raise ValueError("Use a new derived report directory")
    report=json.loads(source_file.read_text(encoding="utf-8"))
    source_hash=hashlib.sha256(source_file.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    report["reprocessing"]={"source_report":str(source_file.resolve()),"source_report_sha256":source_hash,
        "processed_at":datetime.now(timezone.utc).isoformat(),"live_capture":False,
        "note":"Derived inventory correction from saved native screenshots and metadata. No game, compositor or input was accessed; original capture identities and files are preserved."}
    for state in report.get("states",[]):
        metadata=artifact_path(source_root,state["metadata"])
        snapshot=json.loads(metadata.read_text(encoding="utf-8"))
        state["original_exposed_control_count"]=len(state.get("controls",[]))
        original_names={c["name"] for c in state.get("controls",[])}
        state.update(control_inventory(snapshot))
        exposed_names={c["name"] for c in state["controls"]}
        if "overlays" in state:
            state["overlays"]=[o for o in state["overlays"] if o["name"] in exposed_names]
            visible={o["name"] for o in state["overlays"]}
            state["visible_control_names"]=sorted(visible)
            state["offscreen_control_names"]=[c["name"] for c in state["controls"] if ("rect" in c or c.get("rendered_bounds")) and c["name"] not in visible]
        if original_names != exposed_names and state.get("comfort"):
            review=state["comfort"]
            review["source_status"]=review.get("status")
            if "summary" in review:review["source_summary"]=review.pop("summary")
            review.update(status="source_inventory_changed",note="Original comfort assets are preserved; repeat analysis with the corrected exposed controls.")
        state["derived_inventory"]=True
        state["provenance"]={"source_capture_id":snapshot.get("capture",{}).get("capture_id"),
                             "source_session_id":snapshot.get("capture",{}).get("geometry",{}).get("session_id")}
        for key in ("image","metadata"):
            if not state.get(key):continue
            old=artifact_path(source_root,state[key]);new=artifact_path(output,state[key])
            new.parent.mkdir(parents=True,exist_ok=True)
            new.write_bytes(old.read_bytes())
            state["provenance"][key+"_sha256"]=hashlib.sha256(old.read_bytes()).hexdigest()
        for key in ("report","image"):
            reference=state.get("comfort",{}).get(key)
            if not reference:continue
            old=artifact_path(source_root,reference);new=artifact_path(output,reference)
            new.parent.mkdir(parents=True,exist_ok=True);new.write_bytes(old.read_bytes())
            state["provenance"]["comfort_"+key+"_sha256"]=hashlib.sha256(old.read_bytes()).hexdigest()
    write_report(output,report)
    if hashlib.sha256(source_file.read_bytes()).hexdigest()!=source_hash:
        raise RuntimeError("Source report changed while reprocessing")
    return report


def write_report(output, report):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report["summary"] = coverage_summary(report)
    report["source_coverage"]=source_coverage(report)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    encoded = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlatOut UI atlas</title><style>
:root{color-scheme:dark;--bg:#171714;--panel:#242420;--ink:#f3f1e9;--muted:#b8b5a9;--accent:#ffb621;--line:#4c4b42}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}a{color:var(--accent);text-underline-offset:3px}button,input,select{font:inherit}button,input,select{background:#303029;color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:9px 12px}input{min-width:230px;flex:1}input:focus,select:focus,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}header{padding:30px max(24px,4vw) 22px;border-bottom:1px solid var(--line)}h1{font-size:30px;line-height:1.2;margin:0 0 9px;font-weight:650}h2{font-size:21px;margin:30px 0 12px}h3{font-size:17px;margin:12px 14px 4px}p{max-width:85ch;margin:8px 0;color:var(--muted)}nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:20px}.stats{font-variant-numeric:tabular-nums;color:var(--ink)}main{padding:0 max(24px,4vw) 40px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,450px),1fr));gap:24px}article{background:var(--panel);border:1px solid var(--line);overflow:hidden}article:target{outline:2px solid var(--accent)}.shot{position:relative;background:#080808}.shot img{display:block;width:100%;height:auto}.shot svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;display:none}.bounds .shot svg{display:block}.shot rect{fill:#ffb62116;stroke:#ffb621;stroke-width:1.5}.meta{margin:0 14px 12px;font-size:13px;color:var(--muted)}details{padding:0 14px 14px}summary{cursor:pointer;color:var(--accent)}table{border-collapse:collapse;width:100%;font-size:13px;margin:12px 0}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}th{color:var(--muted);font-weight:500}.status{font-weight:650;color:#e8c96f}.failed{color:#ffb4a6}.pending{background:var(--panel);padding:14px 18px;margin-bottom:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}footer{margin-top:30px;color:var(--muted)}::selection{background:var(--accent);color:#171714}::-webkit-scrollbar{width:11px}::-webkit-scrollbar-thumb{background:#686557}::-webkit-scrollbar-track{background:var(--bg)}[hidden]{display:none!important}@media(max-width:600px){header{padding:22px 18px}main{padding:0 18px 28px}.grid{gap:18px}nav>*{width:100%}h1{font-size:26px}}@media(prefers-reduced-motion:no-preference){a,summary{transition:color .12s ease-out}}
</style><header><h1>FlatOut UI atlas</h1><p>Native rendered screenshots for comparing layout, spacing, arrows and interaction states. Menu APIs stage these screens; this survey does not certify ray interactions.</p><p class="stats" id="counts"></p><nav><input id="search" type="search" aria-label="Find a screen or control" placeholder="Find a screen, button or label"><select id="group" aria-label="Screen group"><option value="">All groups</option></select><select id="kind" aria-label="Capture type"><option value="">All states</option><option>base</option><option>page</option><option>hover</option><option>dialog-template</option></select><label><input id="bounds" type="checkbox" style="min-width:0;width:auto"> Show target bounds</label><a href="report.json">JSON report</a></nav></header><main><h2>Captured screens <span id="visible"></span></h2><div class="grid" id="screens"></div><h2>Coverage and pending states</h2><p>“Discovered” counts source menu enums and dialog templates, not every possible gameplay combination. Captures inventory all currently exposed targets and visible HUD nodes. Required fixtures and deferred states remain listed below.</p><div id="coverage"></div><h2>Run details</h2><details><summary>Timing, build identity and diagnostics</summary><pre id="details"></pre></details><footer>Wreck Run is deferred. Source inventory and original per-screen metadata are retained with this report.</footer></main>
<script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.querySelector('#data').textContent),q=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
if(data.reprocessing){const note=document.createElement('p');note.className='status';note.textContent=data.reprocessing.note;q('header').append(note)}
const s=data.summary;q('#counts').textContent=`${s.captured_states} screenshots · ${s.unique_controls} distinct controls · ${s.discovered} source entries discovered · ${s.cases.blocked+s.cases.failed+s.cases.skipped} cases need coverage`;
for(const group of [...new Set(data.states.map(s=>s.group))].sort()){const o=document.createElement('option');o.value=group;o.textContent=group;q('#group').append(o)}
for(const state of data.states){if(!state.image)continue;const a=document.createElement('article');a.id=state.id;a.dataset.group=state.group;a.dataset.kind=state.kind;a.dataset.search=JSON.stringify(state).toLowerCase();const rects=(state.overlays||[]).map(r=>`<rect x="${r.rect[0]}" y="${r.rect[1]}" width="${r.rect[2]-r.rect[0]}" height="${r.rect[3]-r.rect[1]}"><title>${esc(r.name)}</title></rect>`).join('');const rows=[...(state.controls||[]),...(state.background_controls||[])].map(c=>`<tr><td>${esc(c.name)}</td><td>${esc(c.label||'')}</td><td>${c.blocked_by?'Blocked by '+esc(c.blocked_by):c.enabled===false?'Disabled':'Exposed'}</td></tr>`).join('');a.innerHTML=`<a class="shot" style="display:block" href="${esc(state.image)}" target="_blank" rel="noopener" aria-label="Open ${esc(state.title)} screenshot"><img loading="lazy" src="${esc(state.image)}" alt="${esc(state.title)}"><svg viewBox="0 0 ${state.image_size?.[0]||1} ${state.image_size?.[1]||1}" aria-hidden="true">${rects}</svg></a><h3><a href="#${esc(state.id)}">${esc(state.title)}</a></h3><p class="meta">${esc(state.group)} · ${esc(state.kind)} · ${(state.controls||[]).length} exposed controls${state.background_controls?.length?' · '+state.background_controls.length+' blocked by modal':''}${state.derived_inventory?' · derived from saved capture':''}${state.duplicate_of?' · same UI as '+esc(state.duplicate_of):''}</p><details><summary>Controls and capture details</summary><a href="${esc(state.metadata)}">Original metadata</a>${state.comfort?`<p>Comfort: ${esc(state.comfort.status)} <a href="${esc(state.comfort.report)}">Measurements</a>${state.comfort.image?` | <a href="${esc(state.comfort.image)}">Diagnostic guide</a>`:''}</p>`:''}<table><thead><tr><th>Target</th><th>Label</th><th>State</th></tr></thead><tbody>${rows}</tbody></table></details>`;q('#screens').append(a)}
function filter(){let n=0;for(const a of q('#screens').children){a.hidden=!(a.dataset.search.includes(q('#search').value.toLowerCase())&&(!q('#group').value||a.dataset.group===q('#group').value)&&(!q('#kind').value||a.dataset.kind===q('#kind').value));if(!a.hidden)n++}q('#visible').textContent=`(${n})`}for(const el of [q('#search'),q('#group'),q('#kind')])el.addEventListener('input',filter);q('#bounds').addEventListener('change',e=>document.body.classList.toggle('bounds',e.target.checked));filter();
q('#coverage').innerHTML='<table><thead><tr><th>Case</th><th>Status</th><th>Coverage / reason</th></tr></thead><tbody>'+data.cases.map(c=>`<tr><td>${esc(c.title||c.id)}</td><td class="status ${c.status==='failed'?'failed':''}">${esc(c.status)}</td><td>${esc(c.reason||'')}${c.states?.length?' · '+c.states.length+' states':''}</td></tr>`).join('')+'</tbody></table>';
q('#coverage').insertAdjacentHTML('beforeend','<h2>Every discovered source entry</h2><table><thead><tr><th>Source entry</th><th>Status</th><th>Case / reason</th></tr></thead><tbody>'+data.source_coverage.map(c=>`<tr><td>${esc(c.kind)}: ${esc(c.id)}</td><td>${esc(c.status)}</td><td>${esc(c.cases.join(', '))} ${esc(c.reason)}</td></tr>`).join('')+'</tbody></table>');
q('#details').textContent=JSON.stringify({started:data.started,elapsed_seconds:data.elapsed_seconds,build:data.build,initial:data.initial,final:data.final,diagnostics:data.diagnostics,framing:data.framing,reprocessing:data.reprocessing,catalog:data.catalog},null,2);
</script></html>'''
    (output / "index.html").write_text(document.replace("__DATA__", encoded), encoding="utf-8")


class NativeGame:
    def __init__(self, pid, log, timeout=5):
        self.pid, self.log, self.sequence = pid, log, 0
        self.timeout=timeout

    def call(self, method, params=None):
        if method not in READ_METHODS | STAGE_METHODS or blocked_wreck({"method": method, "params": params}):
            raise ValueError("Unsupported atlas operation: " + method)
        if method=="menu.go_to" and str((params or {}).get("menu","")).casefold() in NORMAL_FLOWS:
            raise ValueError("Atlas must enter this menu through its normal parent callback, not menu.go_to")
        if method == "livery.command" and (params or {}).get("op", "state") not in LIVERY_OPS:
            raise ValueError("Atlas never edits or publishes livery content")
        if method == "livery.command" and (params or {}).get("op")=="input":
            if params.get("action") not in {"tab","cancel"} or params.get("repeat",1)!=1:
                raise ValueError("Atlas livery input is restricted to single tab/cancel navigation")
        self.sequence += 1
        start = time.monotonic()
        payload = json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}}).encode()
        if len(payload)>8192:raise ValueError("Atlas requests must fit the native input buffer")
        deadline=time.monotonic()+self.timeout
        def read(pipe,count):
            return bounded_pipe_read(pipe,count,deadline)
        with open(r"\\.\pipe\FlatoutMCP_" + str(self.pid), "r+b", buffering=0) as pipe:
            pipe.write(struct.pack("<I", len(payload)) + payload)
            size = struct.unpack("<I", read(pipe, 4))[0]
            if size > 32 * 1024 * 1024:
                raise RuntimeError("Unbounded native response")
            response = json.loads(read(pipe, size))
        self.log.append({"method": method, "params": params, "seconds": round(time.monotonic() - start, 4),
                         "error": response.get("error")})
        if "error" in response:
            raise RuntimeError(response["error"])
        result = response.get("result", response)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"])
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(result)
        return result


def is_read_request(method, params=None):
    params=params or {}
    if method=="hud.pause_menu":return params.get("action","list")=="list"
    if method=="livery.command":return params.get("op","state") in {"state","catalog","vr_keyboard"}
    return method in READ_METHODS


class OwnerQueueGame:
    """Read-only proxy for a runtime owner that retains its native connection."""
    def __init__(self, root, log, timeout=5):
        self.root,self.log,self.timeout=Path(root),log,timeout

    def call(self, method, params=None):
        if not is_read_request(method,params):
            raise ValueError("Owner queue accepts read-only atlas diagnostics only")
        requests,responses=self.root/"requests",self.root/"responses"
        if not requests.is_dir() or not responses.is_dir():
            raise RuntimeError("Owner queue request/response directories are missing")
        name=uuid.uuid4().hex+".json"
        request=requests/name;temporary=request.with_suffix(".tmp")
        response=responses/name
        start=time.monotonic();deadline=start+self.timeout
        temporary.write_text(json.dumps({"op":"game","method":method,"params":params or {},"settle":0}),encoding="utf-8")
        temporary.replace(request)
        while True:
            try:
                if response.exists():
                    value=json.loads(response.read_text(encoding="utf-8-sig"))
                    self.log.append({"method":method,"params":params,"transport":"owner-queue","request":name,
                                     "seconds":round(time.monotonic()-start,4),"error":value.get("error")})
                    if "error" in value:raise RuntimeError(value["error"])
                    if "result" not in value:raise RuntimeError("Owner response lacks a native result")
                    result=value["result"]
                    if isinstance(result,dict) and ("jsonrpc" in result or "id" in result and "result" in result):
                        if "error" in result:raise RuntimeError(result["error"])
                        result=result["result"]
                    if isinstance(result,dict) and "error" in result:raise RuntimeError(result["error"])
                    return result
            except (PermissionError,json.JSONDecodeError):
                pass
            if time.monotonic()>=deadline:
                raise NativeTimeout("Read-only owner queue response deadline exceeded; request preserved")
            time.sleep(.01)


class Atlas:
    def __init__(self, args, session, report):
        self.args, self.session, self.report = args, session, report
        self.game = OwnerQueueGame(args.owner_queue,report.setdefault("operations",[]),args.native_timeout) if getattr(args,"owner_queue",None) else NativeGame(args.pid, report.setdefault("operations", []),args.native_timeout)
        self.seen, self.capture_cache, self.lock = {}, None, asyncio.Lock()
        self.output = args.output
        self.active_case = None
        self.last_capture_identity=None
        self.owned_pointers={}
        self.attachment_head_pose=None
        self.neutral_head_pose=None

    async def query(self, method, params=None):
        async with self.lock:
            try:
                return await asyncio.wait_for(asyncio.to_thread(self.game.call, method, params), self.args.native_timeout+2)
            except OSError as error:
                raise NativeTimeout("Native UI process/pipe is unavailable; further staging stopped") from error

    async def tool(self, name, args):
        started=time.time()
        response = await self.session.call_tool(name, args)
        values = [json.loads(c.text) for c in response.content if c.type == "text" and c.text.startswith("{")]
        if response.isError or not values:
            raise RuntimeError((name, values))
        if name=="capture_screenshot":
            self.last_capture_identity=validate_capture(values[0],self.args.pid,started,self.last_capture_identity)
        return values[0], [c for c in response.content if c.type == "image"]

    async def release_owned_pointers(self):
        if self.args.current:
            if self.owned_pointers:raise ValueError("Read-only attachment cannot own injected pointers")
            return
        for hand,source in list(self.owned_pointers.items()):
            # Release only hands acquired by this atlas. The latest capture is
            # still in this MCP session's cache; release needs its session ID,
            # not compatibility with the old aim plane.
            capture_id=self.capture_cache["capture_id"] if self.capture_cache else source
            result,_=await self.tool("release_ui_pointer",{"capture_id":capture_id,"hand":hand,"timeout":30})
            if result.get("status")!="input_delivered" or result.get("delivery",{}).get("button_down") is True:
                raise NativeTimeout("Injected pointer release was not acknowledged; further staging stopped")
            self.owned_pointers.pop(hand,None)
            self.report.setdefault("pointer_releases",[]).append({"hand":hand,"capture_id":capture_id,"acknowledged":True})

    def initialize_framing(self, vr):
        if self.args.current:raise ValueError("Read-only attachment cannot initialize navigating framing")
        self.attachment_head_pose=tracking_head_pose(vr)
        self.neutral_head_pose={**self.attachment_head_pose,"yaw_deg":0,"pitch_deg":0,"roll_deg":0}
        self.report["framing"]={"policy":"neutral before staged menu opening; keep attachment tracked position",
            "attachment_pose":dict(self.attachment_head_pose),"ordinary_pose":dict(self.neutral_head_pose),
            "livery":"opens neutral, then uses independent per-plane glances","checks":[]}

    async def prepare_neutral_frame(self, purpose):
        if self.neutral_head_pose is None:return
        if self.args.current:raise ValueError("Read-only attachment cannot reframe a menu")
        if (await self.query("ui.get_message_box")).get("open"):
            raise UnknownModal("Unknown real modal is open; no framing or confirmation was sent")
        vr=await self.query("vr.get_state")
        changed=not head_pose_matches(vr,self.neutral_head_pose)
        if changed:
            await self.set_head_pose(self.neutral_head_pose,"ordinary menu framing")
            vr=await self.query("vr.get_state")
        if not head_pose_matches(vr,self.neutral_head_pose):
            raise CaptureMismatch("Ordinary menu did not reach its neutral opening pose")
        self.report["framing"]["checks"].append({"purpose":purpose,"acknowledged":True,"input_sent":changed})

    async def restore_attachment_frame(self):
        if self.args.current or self.attachment_head_pose is None:return
        await self.release_owned_pointers()
        vr=await self.query("vr.get_state")
        changed=not head_pose_matches(vr,self.attachment_head_pose)
        if changed:await self.set_head_pose(self.attachment_head_pose,"attachment pose restore")
        vr=await self.query("vr.get_state")
        if not head_pose_matches(vr,self.attachment_head_pose):
            raise CaptureMismatch("Original attachment head pose was not restored")
        self.report["framing"]["restore"]={"pose":dict(self.attachment_head_pose),"acknowledged":True,"input_sent":changed}

    async def observe(self):
        snapshot = {"menu": await self.query("menu.get_state"), "targets": await self.query("vr.targets"),
                    "message": await self.query("ui.get_message_box")}
        root = "msgbox" if snapshot["message"].get("open") else "menu"
        snapshot["nodes"] = (await self.query("hud.rects", {"root": root, "max_depth": 48}))["nodes"]
        expected_livery=(self.active_case.get("expand")=="livery" or "E_LIVERY" in self.active_case.get("source_enums",[]))
        known_customization=(self.active_case.get("id")=="customization" or "E_CUSTOMIZATION" in self.active_case.get("source_enums",[]))
        if snapshot["menu"].get("current_menu")=="SKINS" and (expected_livery or not known_customization):
            try:
                lv=await self.query("livery.command",{"op":"state"})
            except RuntimeError as error:
                # Customization and Livery deliberately share the SKINS title.
                # A read-only current attachment can probe, but this specific
                # native negative answer identifies customization, not a race.
                native_error=error.args[0] if error.args else None
                if expected_livery or not isinstance(native_error,dict) or native_error.get("message")!="Open the livery menu first":raise
                lv={};snapshot["presentation"]={"kind":"customization","livery_probe":"native menu is not the livery editor"}
            if lv.get("vr",{}).get("active"):
                snapshot["livery"]=lv
                if not snapshot["message"].get("open"):
                    snapshot["targets"]={**snapshot["targets"],"vr_frame":lv["vr"]["frame"],"targets":[
                        {"name":"livery."+str(c["id"]),"label":c["label"],"enabled":c["enabled"],
                         "rect":[c["x"],c["y"],c["x"]+c["width"],c["y"]+c["height"]]} for c in lv["vr"]["controls"]]}
        if snapshot["menu"].get("current_menu")=="OPTIONS":
            snapshot["options"]=await self.query("options.get_state")
        return snapshot

    async def capture(self, title, kind="base", variant=None, view=None):
        snapshot = await self.observe()
        if view and not head_pose_matches(livery_tracking_state(snapshot["livery"]["vr"]["frame"]),view["head_pose"]):
            raise CaptureMismatch("Native livery frame no longer matches the acknowledged glance")
        ordinary_pose=self.neutral_head_pose if not self.args.current and not view and "livery" not in snapshot else None
        if ordinary_pose is not None:
            if not head_pose_matches(await self.query("vr.get_state"),ordinary_pose):
                raise CaptureMismatch("Ordinary-menu native head pose drifted from neutral; no screenshot was accepted")
            snapshot["framing"]={"kind":"ordinary neutral","pose":dict(ordinary_pose),"native_verified":True}
        snapshot["variant"] = {"ui":variant,"glance":view["panel_id"]} if view else variant
        if view:snapshot["view"]=view
        signature = state_signature(snapshot)
        ident = f"{len(self.report['states']) + 1:04}-{slug(self.active_case['id'])}-{slug(title)}"
        state = {"id": ident, "title": title, "group": self.active_case["group"], "case": self.active_case["id"],
                 "kind": kind, "signature": signature, "controls": [], "metadata": "states/" + ident + ".json"}
        if view:state["view"]=view
        state.update(control_inventory(snapshot))
        # Layout-only dedup cannot establish unchanged physical placement.
        # Comfort review takes a fresh frame for every requested state.
        if signature in self.seen and not getattr(self.args,"comfort_guides",False):
            old = self.seen[signature]
            state.update(duplicate_of=old["id"], dedup_reason="Identical menu, selection, targets, visible text and rectangles", image=old["image"], image_size=old.get("image_size"),overlays=old.get("overlays",[]))
        else:
            cap, images = await self.tool("capture_screenshot", {"eye": self.args.eye, "max_width": self.args.width, "timeout": 30})
            if view:validate_capture_pose(cap,view["head_pose"])
            if ordinary_pose is not None:validate_capture_pose(cap,ordinary_pose)
            if not images or "capture_id" not in cap:
                raise RuntimeError("Compositor did not return a native screenshot")
            state["image"] = "images/" + ident + ".jpg"
            state["image_size"] = cap["image_size"]
            (self.output / state["image"]).write_bytes(base64.b64decode(images[0].data))
            snapshot["capture"] = cap
            self.capture_cache = cap
            state["overlays"] = self.project_targets(cap, snapshot)
            if getattr(self.args,"comfort_guides",False):
                await self.capture_comfort_review(cap,snapshot,state)
            self.seen[signature] = state
        visible={o["name"] for o in state.get("overlays",[])}
        state["visible_control_names"]=sorted(visible)
        state["offscreen_control_names"]=[c["name"] for c in state["controls"] if ("rect" in c or c.get("rendered_bounds")) and c["name"] not in visible]
        (self.output / state["metadata"]).write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        self.report["states"].append(state)
        self.active_case.setdefault("states", []).append(ident)
        print(f"[{len(self.report['states']):03}] {self.active_case['id']}: {title}" + (" (deduplicated)" if state.get("duplicate_of") else ""), flush=True)
        write_report(self.output, self.report)
        return snapshot

    async def capture_comfort_review(self, capture, snapshot, state):
        folder=self.output/"comfort";folder.mkdir(exist_ok=True)
        targets,unmeasured=comfort_review_targets(capture,snapshot)
        relative="comfort/"+state["id"]
        state["comfort"]={"report":relative+".json","unmeasured_controls":unmeasured}
        request={"capture_id":capture["capture_id"],"targets":targets,"reference_mode":"level_head","include_overlay":True}
        if self.neutral_head_pose is not None:
            request["reference_pose"]={"position":[self.neutral_head_pose[k] for k in ("x","y","z")],"orientation":[0,0,0,1]}
        try:
            result,images=await self.tool("analyze_ui_visibility",request)
            if result.get("status")!="analyzed":raise RuntimeError(result)
            if not images:raise RuntimeError("Comfort analysis returned no requested guide image")
            state["comfort"].update(status="analyzed",summary=result["summary"])
            result["atlas_unmeasured_controls"]=unmeasured
            if images:
                if images[0].mimeType!="image/png":raise RuntimeError("Comfort overlay must be a separate PNG")
                state["comfort"]["image"]=relative+".png"
                (self.output/state["comfort"]["image"]).write_bytes(base64.b64decode(images[0].data))
        except (RuntimeError,ValueError) as error:
            result={"status":"error","error":str(error),"atlas_unmeasured_controls":unmeasured}
            state["comfort"].update(status="error",error=str(error))
        (self.output/state["comfort"]["report"]).write_text(json.dumps(result,indent=2),encoding="utf-8")

    def project_targets(self, capture, snapshot):
        from flatout_frontend_e2e import quad_pixel
        from flatout_livery_e2e import world_pixel, ui_frame_at, quaternion_matrix
        import numpy as np
        overlays = []
        native = snapshot["targets"]
        for target in control_inventory(snapshot)["controls"]:
            try:
                frame=None;world_corners=None
                if "rect" in target:
                    l,t,r,b = target["rect"]
                    if native.get("vr_frame") and not snapshot["menu"].get("is_3d"):
                        frame=ui_frame_at({"vr":{"frame":native["vr_frame"]}},(l+r)*.5,(t+b)*.5)
                elif target.get("rendered_bounds"):
                    frame=native["vr_frame"]
                    spatial_review_surface(target["rendered_bounds"],frame["camera"])
                    world_corners=target["rendered_bounds"]["corners"]
                else:continue
                eyes=("left","right") if self.args.eye=="both" else (self.args.eye,)
                for eye in eyes:
                    if frame is not None:
                        points=[]
                        camera=frame["camera"]
                        inverse=np.column_stack([camera[k] for k in ("right","up","back")]).T
                        submitted_eye=next(e for e in capture["geometry"]["projection_eyes"] if e["eye"]==eye)
                        corners=world_corners
                        if corners is None:
                            panel=frame["panel"]
                            corners=[np.array(panel["origin"])+np.array(panel["right"])*(x/1920-.5)*frame["panel_width"]+np.array(panel["up"])*(.5-y/1080)*frame["panel_height"]
                                     for x,y in ((l,t),(r,t),(r,b),(l,b))]
                        for point in corners:
                            point=np.asarray(point)
                            local=quaternion_matrix(submitted_eye["pose"]["orientation"]).T@(inverse@(point-camera["origin"])-submitted_eye["pose"]["position"])
                            if local[2]>=-1e-6:raise ValueError("Control reaches behind the eye; retain its native bounds without a screen overlay")
                            points.append(world_pixel({"vr":{"frame":frame}},capture,point,eye))
                    else:
                        points = [quad_pixel(capture, (x,y,x,y),eye) for x,y in ((l,t),(r,t),(r,b),(l,b))]
                    raw=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
                    viewport=next(e["viewport"] for e in capture["geometry"]["projection_eyes" if frame is not None else "eyes"] if e["eye"]==eye)
                    ex,ey,ew,eh=viewport;cx,cy,cw,ch=capture["crop"];w,h=capture["image_size"]
                    clip=[max(0,(ex-cx)/cw*w-.5),max(0,(ey-cy)/ch*h-.5),min(w,(ex+ew-cx)/cw*w-.5),min(h,(ey+eh-cy)/ch*h-.5)]
                    visible=[max(raw[0],clip[0]),max(raw[1],clip[1]),min(raw[2],clip[2]),min(raw[3],clip[3])]
                    if visible[2]<=visible[0] or visible[3]<=visible[1]:continue
                    overlays.append({"name": target["name"],"eye":eye,"panel_id":frame.get("id","center") if frame else "quad",
                                     "rect":[round(float(v),2) for v in visible],"projected_rect":[round(float(v),2) for v in raw],
                                     "clipped":not np.allclose(visible,raw,atol=.01)})
            except (KeyError, ValueError, StopIteration):
                pass  # Keep original canvas bounds in metadata when projection is unavailable.
        return overlays

    async def ray(self, name, hover=False):
        if self.args.current:raise ValueError("Read-only attachment cannot inject a ray")
        await self.prepare_neutral_frame("ordinary ray source")
        from flatout_frontend_e2e import quad_pixel, RAY_OFFSET
        from flatout_livery_e2e import screenshot_pixel, ui_surface
        from flatout_ray_offset import native_pointer_offset
        native = await self.query("vr.targets")
        target = next((t for t in native["targets"] if t["name"] == name), None)
        if target is None or target.get("enabled") is False:
            raise RuntimeError("Requested target is absent or disabled: " + name)
        cap, _ = await self.tool("capture_screenshot", {"eye": "left", "max_width": 1280, "timeout": 30})
        if self.neutral_head_pose is not None:validate_capture_pose(cap,self.neutral_head_pose)
        self.capture_cache = cap
        l,t,r,b = target["rect"]
        args = {"capture_id": cap["capture_id"], "hand": self.args.hand, "origin_mode": "hand", "dwell_frames": self.args.dwell_frames}
        if native.get("vr_frame") and not (await self.query("menu.get_state")).get("is_3d"):
            state = {"vr": {"frame": native["vr_frame"]}}
            x,y = screenshot_pixel(state, cap, (l+r)/2, (t+b)/2)
            args["surface"] = ui_surface(state,(l+r)/2,(t+b)/2)
        else:
            x,y = quad_pixel(cap, target["rect"])
            carrier = max((s for s in cap["geometry"]["surfaces"] if s["visibility"] == 0), key=lambda s:s["size"][0]*s["size"][1])
            args["surface_id"] = carrier["id"]
        args.update(x=float(x),y=float(y),ray_offset=native_pointer_offset(await self.query("vr.get_state"),self.args.hand) or RAY_OFFSET)
        if not hover:
            args.update(command_id=uuid.uuid4().hex,hold_frames=2)
        result,_ = await self.tool("hover_ui_at" if hover else "click_ui_at", {**args,"timeout":30})
        if result.get("status") != "input_delivered":
            raise RuntimeError(result)
        self.owned_pointers[self.args.hand]=cap["capture_id"]
        await asyncio.sleep(self.args.settle)
        if hover and (await self.query("vr.targets"))["hovered"] != name:
            raise RuntimeError("Native ray did not hover requested control: " + name)

    async def select(self, index):
        await self.prepare_neutral_frame("before menu selection callback")
        await self.release_owned_pointers()
        menu = await self.query("menu.get_state")
        items = menu.get("items", [])
        item = next((i for i in items if i["index"] == index), None)
        if not item or item.get("locked") or item.get("coming_soon") or item.get("installed") is False or blocked_wreck(item.get("title")):
            raise RuntimeError("Refusing to activate an unavailable menu item: " + str(item))
        for _ in range(len(items) + 1):
            if (await self.query("menu.get_state"))["selection_index"] == index:
                return await self.query("menu.select")
            await self.query("menu.navigate", {"direction": "down"})
        raise RuntimeError("Could not focus requested menu item")

    async def dismiss(self, owned_template=False):
        box = await self.query("ui.get_message_box")
        if box.get("open"):
            if not owned_template:
                raise UnknownModal("Unknown real modal is open; no confirmation was sent")
            names = {t["name"] for t in (await self.query("vr.targets"))["targets"]}
            if "msgbox.cancel" in names:
                await self.query("ui.confirm_message_box", {"cancel": True})
            elif "msgbox.validate" in names:
                await self.query("ui.confirm_message_box", {"cancel": False})
            else:
                raise RuntimeError("An unhandled wait/modal must be resolved before continuing")
            await asyncio.sleep(self.args.settle)

    async def main(self):
        await self.prepare_neutral_frame("return to Main")
        await self.release_owned_pointers()
        for _ in range(30):
            await self.dismiss()
            menu = await self.query("menu.get_state")
            if menu.get("current_menu") == "MAIN MENU":
                return
            await self.query("menu.back")
            await asyncio.sleep(self.args.settle)
        raise RuntimeError("Could not return through native Back to Main Menu")

    async def fixture_menu(self, title, mode=None, item_count=None, is_3d=None, car=False):
        """Observe initialized presentation twice; never repair a failed fixture by opening another menu."""
        deadline=time.monotonic()+getattr(self.args,"fixture_timeout",20)
        stable=0;last={}
        while True:
            if (await self.query("ui.get_message_box")).get("open"):
                raise FixtureUnavailable("Owned fixture opened a real entry modal; no confirmation or further staging was sent")
            menu=await self.query("menu.get_state")
            game=await self.query("game.get_state")
            nodes=(await self.query("hud.rects",{"root":"menu","max_depth":48}))["nodes"]
            loaders=loading_widgets(nodes);caption=career_car_caption(nodes)
            last={"menu":menu.get("current_menu"),"game_mode":game.get("game_mode"),
                  "item_count":menu.get("item_count"),"is_3d":menu.get("is_3d"),
                  "loading_widgets":[n["path"] for n in loaders],"car_caption":caption}
            valid=(last["menu"]==title and (mode is None or last["game_mode"]==mode) and
                   (item_count is None or last["item_count"]==item_count) and
                   (is_3d is None or last["is_3d"]==is_3d) and not loaders and (not car or bool(caption)))
            stable=stable+1 if valid else 0
            if stable>=2:
                self.active_case.setdefault("fixture_checks",[]).append(last)
                return menu
            if time.monotonic()>=deadline:
                self.active_case.setdefault("fixture_checks",[]).append(last)
                raise FixtureUnavailable("Normal menu fixture did not become ready; stopped in place: "+json.dumps(last))
            await asyncio.sleep(max(.02,min(self.args.settle,.15)))

    async def livery_fixture(self):
        deadline=time.monotonic()+getattr(self.args,"fixture_timeout",20)
        while True:
            if (await self.query("ui.get_message_box")).get("open"):
                raise FixtureUnavailable("Livery entry opened a real modal; no confirmation or recovery navigation was sent")
            state=await self.query("livery.command",{"op":"state"})
            check={"menu":"livery","ready":state.get("ready"),"vr_active":state.get("vr",{}).get("active"),
                   "bindings":state.get("bindings",0),"projection_primitives":state.get("projection_primitives",0),
                   "controls":len(state.get("vr",{}).get("controls",[]))}
            if livery_ready(state):
                self.active_case.setdefault("fixture_checks",[]).append(check)
                return state
            if time.monotonic()>=deadline:
                self.active_case.setdefault("fixture_checks",[]).append(check)
                raise FixtureUnavailable("Livery presentation did not bind; stopped in place without opening Garage: "+json.dumps(check))
            await asyncio.sleep(max(.02,min(self.args.settle,.15)))

    async def normal_flow(self, alias):
        """Activate owning callbacks, including race-mode and vehicle setup omitted by menu.go_to."""
        if self.args.current:raise ValueError("Read-only attachment cannot stage menu flows")
        if alias in PROFILE_FLOWS and "showroom" not in self.args.fixture:
            raise FixtureUnavailable("Normal Career/showroom routes require an existing owned-car showroom fixture")
        if alias in ROOT_FLOWS:
            await self.main()
            index,title,mode=ROOT_FLOWS[alias]
            await self.select(index)
            await asyncio.sleep(self.args.settle)
            return await self.fixture_menu(title,mode,is_3d=True if alias=="career" else None,car=alias=="career")
        if alias in SHOWROOM_FLOWS:
            await self.normal_flow("career")
            await self.select(1) # Career Garage callback initializes selected car/material/driver.
            await asyncio.sleep(self.args.settle)
            await self.fixture_menu("GARAGE","career",item_count=4,is_3d=False)
            if alias=="garage":return
            index,title=SHOWROOM_FLOWS[alias]
            await self.select(index)
            await asyncio.sleep(self.args.settle)
            await self.fixture_menu(title,"career")
            if alias=="livery":await self.livery_fixture()
            return
        if alias in {"class_selection","event_selection"}:
            menu=await self.normal_flow("career")
            if menu.get("item_count")!=2:
                raise FixtureUnavailable("Career has an active cup or unexpected layout; atlas will not Continue/Cancel Cup")
            await self.select(0)
            await asyncio.sleep(self.args.settle)
            await self.fixture_menu("FLATOUT","career",is_3d=False)
            if alias=="event_selection":
                await self.select(0)
                await asyncio.sleep(self.args.settle)
                await self.fixture_menu("FLATOUT_MODE","career")
            return
        if alias=="party_game":
            await self.normal_flow("multiplayer")
            await self.select(1) # Online item 0 creates a match and is never activated.
            await asyncio.sleep(self.args.settle)
            return await self.fixture_menu("OFFLINE","party_game")
        if alias in {"statistics","credits"}:
            await self.normal_flow("extra")
            await self.select(0 if alias=="statistics" else 1)
            await asyncio.sleep(self.args.settle)
            return await self.fixture_menu("PLAYER STATS" if alias=="statistics" else "CREDITS")
        raise ValueError("No normal menu flow for "+alias)

    async def stage(self, case):
        if self.args.current:raise ValueError("Read-only attachment cannot stage menu flows")
        await self.prepare_neutral_frame("before staged route")
        await self.main()
        for step in case.get("route", []):
            if "goto" in step:
                alias=step["goto"].casefold()
                if alias in NORMAL_FLOWS:await self.normal_flow(alias)
                else:await self.query("menu.go_to", {"menu": step["goto"]})
            elif "select" in step:
                await self.select(step["select"])
            elif "ray" in step:
                await self.ray(step["ray"])
            elif "ray_prefix" in step:
                ts = (await self.query("vr.targets"))["targets"]
                await self.ray(next(t["name"] for t in ts if t["name"].startswith(step["ray_prefix"])))
            elif "back" in step:
                await self.query("menu.back")
            elif "livery" in step:
                await self.query("livery.command", step["livery"])
            elif "party_drivers" in step:
                await self.party_drivers(step["party_drivers"])
            else:
                raise ValueError("Unknown route step: " + str(step))
            await asyncio.sleep(self.args.settle)
            if (await self.query("ui.get_message_box")).get("open"):
                await self.capture(case["title"] + " — entry dialog", "base")
                raise UnknownModal("Entry dialog needs its owning flow to resolve it; atlas did not validate it")
        if case.get("expect"):
            actual = (await self.query("menu.get_state")).get("current_menu")
            if actual not in case["expect"]:
                raise RuntimeError(f"Expected {case['expect']}, reached {actual}")

    async def party_drivers(self, players):
        """Enter drivers once through a fresh Party playlist; never assume the picker starts on page one."""
        if self.args.current:raise ValueError("Read-only attachment cannot stage Party drivers")
        if not isinstance(players,int) or isinstance(players,bool) or not 2<=players<=8:
            raise ValueError("Party driver fixture needs 2–8 players")
        # FOPartyGameMenu::Open runs PartyGameReset, so the first Start opens
        # drivers. Reusing an old playlist could instead launch the next race.
        await self.normal_flow("party_game")
        initial=await self.query("party.get_state")
        if not initial.get("open") or initial.get("playlist"):
            raise FixtureUnavailable("Fresh Party menu did not expose an empty playlist; no Start was sent")
        await self.ray("players."+str(players))
        deadline=time.monotonic()+getattr(self.args,"fixture_timeout",20)
        while True:
            party=await self.query("party.get_state")
            if party.get("stunt_picker_open") and party.get("players")==players:break
            if time.monotonic()>=deadline:
                raise FixtureUnavailable("Party player selection did not open its stunt picker")
            await asyncio.sleep(max(.02,min(self.args.settle,.15)))
        if party.get("playlist"):
            raise FixtureUnavailable("Fresh Party stunt picker has an unexpected playlist; no toggle or Start was sent")
        targets=(await self.query("vr.targets"))["targets"]
        exposed=sorted((t for t in targets if re.fullmatch(r"stunt\.\d+",t["name"]) and t.get("enabled",True)),
                       key=lambda t:int(t["name"].split(".")[1]))
        if not exposed:raise FixtureUnavailable("Party stunt picker has no exposed enabled tile")
        chosen=exposed[0]["name"];index=int(chosen.split(".")[1])
        await self.ray(chosen)
        selected=await self.query("party.get_state")
        if not selected.get("stunt_picker_open") or [p["index"] for p in selected.get("playlist",[])]!=[index]:
            raise FixtureUnavailable("Party tile did not create exactly the selected one-stunt playlist; no Start was sent")
        targets=(await self.query("vr.targets"))["targets"]
        start=next((t["name"] for t in targets if t["name"].startswith("helper.Validate.") and t.get("enabled",True)),None)
        if not start:raise FixtureUnavailable("Party stunt picker has no enabled Start action")
        await self.ray(start) # Exactly one Start; the fresh normal entry guarantees its driver-selection branch.
        deadline=time.monotonic()+getattr(self.args,"fixture_timeout",20)
        while True:
            menu=await self.query("menu.get_state")
            if menu.get("current_menu")=="DRIVERS":break
            if time.monotonic()>=deadline:
                raise FixtureUnavailable("Party Start did not reach driver selection; no retry or second Start was sent")
            await asyncio.sleep(max(.02,min(self.args.settle,.15)))
        self.active_case.setdefault("fixture_checks",[]).append({"menu":"DRIVERS","players":players,
            "stunt_target":chosen,"stunt_picker_visible":party.get("visible"),"start_count":1,
            "entry":"fresh normal Party callback; empty playlist verified"})

    async def variants(self, title):
        if self.args.hover == "none":
            return
        targets = (await self.query("vr.targets"))["targets"]
        candidates = [t for t in targets if t.get("enabled", True)]
        if self.args.hover == "sample" and candidates:
            candidates = [candidates[0]] + ([candidates[-1]] if len(candidates)>1 else [])
        for target in candidates:
            try:
                await self.ray(target["name"], hover=True)
                await self.capture(title + " — " + target["name"], "hover", target["name"])
            finally:
                await self.release_owned_pointers()

    async def pages(self, title, target, max_pages=64):
        seen = set()
        for page in range(max_pages):
            snapshot = await self.capture(title + " — page " + str(page+1), "page")
            sig = state_signature(snapshot)
            if sig in seen:
                raise RuntimeError("Pagination did not advance; stopping repeated captures")
            seen.add(sig)
            await self.variants(title + " — page " + str(page+1))
            targets = snapshot["targets"]["targets"]
            if not any(t["name"] == target and t.get("enabled",True) for t in targets):
                return
            await self.ray(target)
        raise RuntimeError("Pagination exceeded explicit bound")

    async def expand(self, case):
        expand = case.get("expand", "screen")
        if expand == "options":
            options = await self.query("options.get_state")
            for box in options["boxes"]:
                await self.ray("settings.category."+str(box["index"]))
                await self.pages(box["title"],"settings.scroll.down",32)
                current = await self.query("options.get_state")
                if any(b.get("dirty") for b in current["boxes"]):
                    raise RuntimeError("Atlas navigation unexpectedly changed settings")
        elif expand == "quick":
            for i,label in enumerate(("Race","Arena","Stunt")):
                await self.ray("quick.mode."+str(i))
                await self.capture("Quick Play — "+label)
                await self.variants("Quick Play — "+label)
        elif expand == "pages":
            await self.pages(case["title"],case["next"],case.get("max_pages",64))
        elif expand == "items":
            initial = await self.query("menu.get_state")
            total=initial.get("item_count",0)
            visited=set()
            direction="down" if initial.get("is_3d") else "right"
            for ordinal in range(total):
                current = await self.query("menu.get_state")
                selected=current.get("selection_index")
                if selected in visited:
                    raise RuntimeError("Carousel cycled before every logical item was reached")
                visited.add(selected)
                item = next((i for i in current.get("items",[]) if i["index"] == current["selection_index"]),{})
                capture_item=self.args.items=="all" or initial.get("is_3d") or ordinal%6==0 or ordinal==total-1
                if capture_item and not blocked_wreck(item.get("title","")):
                    await self.capture(case["title"]+" — "+(item.get("title") or "item "+str(current["selection_index"])),"selection",current["selection_index"])
                await self.query("menu.navigate",{"direction":direction,"handler":not initial.get("is_3d",False)})
                await asyncio.sleep(self.args.settle)
                if ordinal<total-1 and (await self.query("menu.get_state")).get("selection_index")==selected:
                    raise RuntimeError("Menu navigation did not advance; remaining items were not captured")
            if not initial.get("item_count"):
                await self.capture(case["title"])
        elif expand == "livery":
            await self.livery(case)
        elif expand == "rewards":
            await self.rewards()
        elif expand == "leaderboard":
            await self.leaderboard()
        else:
            await self.capture(case["title"])
            await self.variants(case["title"])

    async def set_livery_head_pose(self, pose):
        return await self.set_head_pose(pose,"livery glance")

    async def set_head_pose(self, pose, purpose):
        if self.args.current:raise ValueError("Read-only attachment cannot move the headset")
        await self.release_owned_pointers()
        started=time.monotonic()
        deadline=started+getattr(self.args,"head_pose_timeout",8)
        interval=getattr(self.args,"head_redelivery_interval",1)
        move={"pose":dict(pose),"purpose":purpose,"acknowledged":False,"deliveries":[],"retries":0}
        self.report.setdefault("head_moves",[]).append(move)
        next_delivery=started
        while True:
            now=time.monotonic()
            if len(move["deliveries"])<3 and now>=next_delivery:
                # Re-deliver only the same absolute pose. This is idempotent;
                # unlike clicks it cannot repeat an action or accumulate motion.
                response=await self.session.call_tool("set_head_pose",dict(pose))
                move["deliveries"].append({"seconds":round(time.monotonic()-started,3),"queued":not response.isError})
                move["retries"]=len(move["deliveries"])-1
                if response.isError:
                    detail="\n".join(c.text for c in getattr(response,"content",[]) if getattr(c,"type",None)=="text")
                    move["deliveries"][-1]["error"]=detail
                    move["seconds"]=round(time.monotonic()-started,3)
                    raise RuntimeError("Simulator rejected "+purpose+": "+detail)
                next_delivery=time.monotonic()+interval
            vr=await self.query("vr.get_state")
            try:move["observed_pose"]=tracking_head_pose(vr)
            except (KeyError,ValueError):move["observed_pose"]=None
            if head_pose_matches(vr,pose):
                await asyncio.sleep(.1) # Let the submitted eye image follow the acknowledged tracking pose.
                move.update(acknowledged=True,seconds=round(time.monotonic()-started,3))
                return
            if time.monotonic()>=deadline:
                move.update(seconds=round(time.monotonic()-started,3),error="Actual native head pose did not acknowledge the requested absolute pose")
                raise NativeTimeout(purpose+" head pose was not acknowledged before the deadline after bounded identical-pose delivery; requested="+
                                    json.dumps(pose)+" observed="+json.dumps(move["observed_pose"]))
            await asyncio.sleep(min(.05,max(.001,interval*.5)))

    async def face_livery_control(self, state, control):
        from flatout_livery_e2e import ui_frame_at
        x,y=control["x"]+control["width"]*.5,control["y"]+control["height"]*.5
        frame=ui_frame_at(state,x,y)
        if not frame.get("panels"):return None
        pose=livery_focus_pose(state,x,y)
        await self.set_livery_head_pose(pose)
        return {"panel_id":frame["id"],"head_pose":pose}

    async def capture_livery_views(self, title, kind="base", variant=None):
        # --current is a snapshot of the existing view, including its clipping.
        # It must never turn an attachment into a navigating/head-moving sweep.
        if self.args.current:return await self.capture(title,kind,variant)
        state=await self.query("livery.command",{"op":"state"})
        panels=state["vr"]["frame"].get("panels",[])
        if not panels:return await self.capture(title,kind,variant)
        original=tracking_head_pose(await self.query("vr.get_state"))
        try:
            for panel in panels:
                l,t,r,b=panel["canvas_rect"]
                current=await self.query("livery.command",{"op":"state"})
                view=await self.face_livery_control(current,{"x":l,"y":t,"width":r-l,"height":b-t})
                await self.capture(title+" — "+panel["id"]+" panel",kind,variant,view=view)
        finally:
            await self.set_livery_head_pose(original)

    async def livery(self, case):
        state = await self.livery_fixture()
        if state.get("dirty"):
            raise RuntimeError("Refusing to survey a livery with unsaved edits")
        # These public navigation handlers only change the displayed panel/view.
        overlays_only=getattr(self.args,"livery_overlays_only",False)
        self.active_case["scope"]={"mode":"overlays-only" if overlays_only else "full-livery",
            "includes":["initial side panels","large keyboard","Workshop browser","verified clean return to Garage"],
            "omitted":["tab gallery","camera view gallery","sticker category gallery"] if overlays_only else []}
        await self.capture_livery_views("Livery — editor columns")
        if not overlays_only:
            for _ in range(3):
                state=await self.query("livery.command",{"op":"state"})
                await self.capture_livery_views("Livery — "+("Paint","Stickers","My liveries")[state["tab"]])
                await self.query("livery.command",{"op":"input","action":"tab"})
                await asyncio.sleep(self.args.settle)
            for side in range(7):
                await self.query("livery.command",{"op":"view","side":side})
                await asyncio.sleep(.45)
                await self.capture_livery_views("Livery — view "+str(side),"page")
            count=(await self.query("livery.command",{"op":"catalog"}))["categories"]
            for category in range(count):
                await self.query("livery.command",{"op":"browse","category":category})
                await asyncio.sleep(self.args.settle)
                await self.capture_livery_views("Livery — sticker category "+str(category),"page")
        # My liveries has the keyboard and store entry points; neither action
        # changes or publishes the document. Cancel restores each overlay.
        for _ in range(3):
            state=await self.query("livery.command",{"op":"state"})
            if state["tab"]==2:break
            await self.query("livery.command",{"op":"input","action":"tab"})
            await asyncio.sleep(self.args.settle)
        for label,title in (("RENAME","Livery — large keyboard"),("BROWSE WORKSHOP","Livery — Workshop browser")):
            try:
                await self.livery_ray(label)
                await self.capture_livery_views(title,"page")
            finally:
                for _ in range(4):
                    if (await self.query("menu.get_state")).get("current_menu")!="SKINS":
                        raise FixtureUnavailable("Livery overlay unexpectedly left the editor; stopped without recovery navigation")
                    overlay=await self.query("livery.command",{"op":"state"})
                    if not overlay.get("keyboard_target") and not overlay.get("workshop_browser",{}).get("open"):break
                    await self.query("livery.command",{"op":"input","action":"cancel"})
                    await asyncio.sleep(self.args.settle)
        await self.close_livery()
        self.active_case["reason"] = ("Focused initial sides, large keyboard, Workshop and clean close only; tab/view/category galleries omitted. " if overlays_only else
            "Editor tabs/views, sticker categories, large keyboard, Workshop and verified clean close. ")+"Edit-only dialogs need a selected-layer fixture. No livery was saved or published."

    async def close_livery(self):
        state=await self.query("livery.command",{"op":"state"})
        if state.get("dirty"):
            raise UnknownModal("Livery survey unexpectedly changed the document; stopped without saving or discarding")
        await self.release_owned_pointers()
        await self.query("livery.command",{"op":"input","action":"cancel"})
        deadline=time.monotonic()+getattr(self.args,"fixture_timeout",20);discard_sent=False
        while True:
            menu=await self.query("menu.get_state")
            title=menu.get("current_menu")
            if title=="GARAGE":
                self.active_case.setdefault("fixture_checks",[]).append({"menu":"GARAGE","clean_livery_close":True,
                    "cancel_count":1,"clean_legacy_discard":discard_sent})
                await self.capture("Livery — returned to Garage","cleanup",{"clean_livery_close":True})
                return
            if (await self.query("ui.get_message_box")).get("open"):
                raise UnknownModal("Livery close opened an unexpected native modal; no confirmation was sent")
            if title=="SKINS":
                # A clean Cancel may close synchronously. Query the livery API
                # only while its owning menu remains current.
                try:
                    state=await self.query("livery.command",{"op":"state"})
                except RuntimeError as error:
                    native_error=error.args[0] if error.args else None
                    if not isinstance(native_error,dict) or native_error.get("message")!="Open the livery menu first":raise
                    # It closed between the menu observation and this read.
                    # Only a subsequent actual GARAGE observation proves close.
                    if time.monotonic()>=deadline:raise FixtureUnavailable("Livery close changed ownership without reaching Garage before its deadline") from error
                    await asyncio.sleep(.02)
                    continue
                if state.get("dirty"):raise UnknownModal("Livery became dirty during close; no save/discard was sent")
                if state.get("exit_requested") and not discard_sent:
                    await self.capture_livery_views("Livery — leave dialog","page")
                    await self.livery_ray("DISCARD")
                    discard_sent=True
            elif title:
                raise FixtureUnavailable("Livery clean close reached unexpected menu "+str(title)+"; no recovery navigation was sent")
            if time.monotonic()>=deadline:
                raise FixtureUnavailable("Livery clean close did not return to Garage before its deadline; Cancel was not repeated")
            await asyncio.sleep(max(.02,min(self.args.settle,.15)))

    async def livery_ray(self, label):
        if self.args.current:raise ValueError("Read-only attachment cannot click livery controls")
        from flatout_livery_e2e import screenshot_pixel, ui_surface
        from flatout_ray_offset import native_pointer_offset
        state=await self.query("livery.command",{"op":"state"})
        control=next(c for c in state["vr"]["controls"] if c["label"]==label and c["enabled"])
        original=tracking_head_pose(await self.query("vr.get_state"))
        moved=False
        try:
            view=await self.face_livery_control(state,control)
            moved=view is not None
            # Re-read after acknowledgement: do not combine an old camera frame
            # with pixels from the new eye direction.
            state=await self.query("livery.command",{"op":"state"})
            if view and not head_pose_matches(livery_tracking_state(state["vr"]["frame"]),view["head_pose"]):
                raise CaptureMismatch("Native livery control frame no longer matches the acknowledged glance")
            control=next(c for c in state["vr"]["controls"] if c["label"]==label and c["enabled"])
            hx,hy=control["x"]+control["width"]*.5,control["y"]+control["height"]*.5
            cap,_=await self.tool("capture_screenshot",{"eye":"left","max_width":1280,"timeout":30})
            if view:validate_capture_pose(cap,view["head_pose"])
            self.capture_cache=cap
            x,y=screenshot_pixel(state,cap,hx,hy)
            if not (0<=x<cap["image_size"][0] and 0<=y<cap["image_size"][1]):
                raise RuntimeError("Livery control remains outside the acknowledged glance")
            aim={"capture_id":cap["capture_id"],"x":float(x),"y":float(y),"surface":ui_surface(state,hx,hy),
                 "hand":self.args.hand,"origin_mode":"hand","dwell_frames":self.args.dwell_frames,
                 "hold_frames":2,"command_id":uuid.uuid4().hex,"timeout":30}
            offset=native_pointer_offset(await self.query("vr.get_state"),self.args.hand)
            if offset:aim["ray_offset"]=offset
            value,_=await self.tool("click_ui_at",aim)
            if value.get("status")!="input_delivered":raise RuntimeError(value)
            self.owned_pointers[self.args.hand]=cap["capture_id"]
            await asyncio.sleep(self.args.settle)
        finally:
            await self.release_owned_pointers()
            if moved:await self.set_livery_head_pose(original)

    async def rewards(self):
        initial=await self.query("ui.reward_state")
        await self.query("ui.preview_reward",{"sequence":[{"kind":k,"index":0} for k in ("money","car","driver")]})
        previous=initial["presentation"]
        for kind in ("money","car","driver"):
            deadline=time.monotonic()+20
            while True:
                reward=await self.query("ui.reward_state")
                if reward["presentation"]>previous and reward["phase"]=="ready":break
                if time.monotonic()>deadline:raise RuntimeError("Reward preview did not become ready")
                await asyncio.sleep(.08)
            if reward["credits"]!=initial["credits"]:raise RuntimeError("Presentation fixture changed credits")
            await self.capture("Reward — "+kind)
            previous=reward["presentation"]
            await self.ray("helper.Validate.0")
        deadline=time.monotonic()+20
        while (await self.query("ui.reward_state"))["open"]:
            if time.monotonic()>deadline:raise RuntimeError("Reward outro did not finish")
            await asyncio.sleep(.08)

    async def leaderboard(self):
        native=await self.query("vr.targets")
        events=sorted((t for t in native["targets"] if re.fullmatch(r"challenge\.event\.\d+",t["name"]) and t.get("enabled",True)),
                      key=lambda t:int(t["name"].split(".")[-1]))
        selected=None;button=None
        for event in events:
            await self.ray(event["name"],hover=True)
            menu=await self.query("menu.get_state");targets=(await self.query("vr.targets"))["targets"]
            if menu.get("selection_index")!=int(event["name"].split(".")[-1]):
                raise FixtureUnavailable("Leaderboard event hover did not establish its selected context")
            button=next((t["name"] for t in targets if t["name"].startswith("helper.Secondary.") and t.get("enabled",True)),None)
            if button:selected=event["name"];break
        if not selected:
            await self.release_owned_pointers()
            reason="No exposed event on the current FlatOut Mode page has an enabled leaderboard action; a leaderboard-capable/service fixture is required"
            await self.capture("Leaderboard — unavailable for exposed events","blocked",{"reason":reason,"exposed_events":[t["name"] for t in events]})
            raise CaseBlocked(reason)
        self.active_case.setdefault("fixture_checks",[]).append({"leaderboard_event":selected,"action":button})
        await self.ray(button)
        if (await self.query("menu.get_state")).get("current_menu")!="LEADERBOARD":
            reason="Selected event did not open a leaderboard; its service/network fixture is unavailable"
            await self.capture("Leaderboard — service unavailable","blocked",{"reason":reason})
            if (await self.query("ui.get_message_box")).get("open"):raise UnknownModal(reason+"; unresolved real modal was not confirmed")
            raise CaseBlocked(reason)
        deadline=time.monotonic()+20
        while (await self.query("leaderboard.get_state"))["busy"]:
            if time.monotonic()>deadline:
                await self.capture("Leaderboard — read still pending")
                raise UnknownModal("Leaderboard read remains pending; atlas did not confirm or cancel an unknown network flow")
            await asyncio.sleep(.1)
        if (await self.query("ui.get_message_box")).get("open"):
            await self.capture("Leaderboard — service dialog","blocked",{"reason":"Read finished with an unresolved native service dialog"})
            raise UnknownModal("Leaderboard service dialog needs its owning network fixture; no confirmation was sent")
        read=await self.query("leaderboard.get_state")
        if read.get("read_error") not in (None,0):
            reason="Leaderboard service read failed; network fixture unavailable (native error "+str(read["read_error"])+")"
            await self.capture("Leaderboard — read unavailable","blocked",{"reason":reason,"native_read":read})
            raise CaseBlocked(reason)
        await self.pages("Leaderboard","page.down",64)

    async def run(self, manifest):
        try:
            await self._run(manifest)
        except BaseException as error:
            try:
                await self.restore_attachment_frame()
            except Exception as restore_error:
                self.report.setdefault("framing",{})["restore_error"]=repr(restore_error)
                if hasattr(error,"add_note"):error.add_note("Attachment pose restore failed: "+repr(restore_error))
            raise
        else:
            try:
                await self.restore_attachment_frame()
            except Exception as error:
                self.report.setdefault("framing",{})["restore_error"]=repr(error)
                raise

    async def _run(self, manifest):
        self.report["initial"] = await self.query("game.get_state")
        vr=await self.query("vr.get_state")
        self.report["runtime"]={"active":vr.get("active"),"can_render":vr.get("can_render"),"runtime":vr.get("runtime")}
        if not vr.get("active") or not vr.get("can_render"):
            raise CaptureMismatch("Attached game is not rendering VR; stale simulator captures are not accepted")
        if self.args.current:
            self.active_case={"id":"current","title":"Attached current UI","group":"Current UI","status":"captured","states":[]}
            self.report["cases"].append(self.active_case)
            await self.capture("Current UI — "+str((await self.query("menu.get_state")).get("current_menu") or self.report["initial"].get("state")))
            self.report["final"]=await self.query("game.get_state")
            self.report["diagnostics"]={"after":await self.query("game.errors"),"inputs_sent":0}
            return
        if not self.report["initial"].get("is_main_menu"):
            raise RuntimeError("Front-end atlas starts in Main Menu; finish hardware/race testing first")
        if (await self.query("ui.get_message_box")).get("open"):
            self.active_case={"id":"unresolved-entry-modal","title":"Unresolved entry modal","group":"Entry","status":"blocked","states":[],"reason":"Atlas does not confirm unknown real modals"}
            self.report["cases"].append(self.active_case)
            await self.capture("Unresolved entry modal")
            raise UnknownModal("Resolve the current real modal before a navigating sweep")
        self.initialize_framing(vr)
        await self.prepare_neutral_frame("initial route setup")
        await self.main()
        before_errors = await self.query("game.errors")
        for original in manifest["cases"]:
            case = {**original,"status":"planned","states":[]}
            self.report["cases"].append(case)
            if original.get("status") in ("deferred","blocked"):
                case["status"] = original["status"]
                continue
            if self.args.only and case["id"] not in self.args.only:
                case.update(status="skipped",reason="Not selected by --only")
                continue
            missing = case_fixture_requirements(case) - set(self.args.fixture)
            if missing:
                case.update(status="blocked",reason="Required fixture(s): "+", ".join(sorted(missing)))
                continue
            self.active_case = case
            start=time.monotonic()
            try:
                await self.stage(case)
                await self.expand(case)
                case["status"]="captured"
            except CaseBlocked as error:
                case.update(status="blocked",reason=str(error))
            except (NativeTimeout,UnknownModal,CaptureMismatch,FixtureUnavailable) as error:
                case.update(status="blocked",reason=str(error))
                raise
            except Exception as error:
                case.update(status="failed",reason=str(error))
                print("FAILED",case["id"],str(error),flush=True)
                if self.args.fail_fast:
                    raise
            finally:
                case["seconds"]=round(time.monotonic()-start,3)
                write_report(self.output,self.report)
        if self.args.dialog_templates:
            for template in self.report["catalog"]["dialogs"]:
                case={"id":"dialog."+template["id"],"title":template["id"],"group":"Dialog templates","states":[]}
                self.report["cases"].append(case)
                if template["deferred"] or not template["preview_safe"]:
                    case.update(status="deferred" if template["deferred"] else "blocked",reason="Template has no explicitly dismissible input mode; not opened without its owning flow")
                    continue
                self.active_case=case
                try:
                    await self.main()
                    await self.query("ui.pop_message_box",{"type":template["id"],"title":template["id"],"content":"UI atlas template preview. Inspect spacing, text wrapping and actions. This preview does not execute an original game action."})
                    await asyncio.sleep(self.args.settle)
                    await self.capture(template["id"],"dialog-template")
                    await self.dismiss(owned_template=True)
                    case.update(status="captured",reason="Synthetic template preview; original localized flow/content not certified")
                except (NativeTimeout,UnknownModal,CaptureMismatch,FixtureUnavailable):
                    case.update(status="blocked",reason="Native capture deadline or unresolved modal interrupted template survey")
                    raise
                except Exception as error:
                    case.update(status="failed",reason=str(error))
        await self.main()
        self.report["final"] = await self.query("menu.get_state")
        after_errors = await self.query("game.errors")
        self.report["diagnostics"]={"before":before_errors,"after":after_errors,"new_errors":after_errors.get("total",0)-before_errors.get("total",0)}
        await self.release_owned_pointers()


async def run_live(args, manifest, report):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    sys.path.insert(0,str(ROOT/"tests"))
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            await Atlas(args,session,report).run(manifest)


def cli():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fox-root",type=Path)
    p.add_argument("--manifest",type=Path,default=DEFAULT_MANIFEST)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--launch",type=Path,help="Current launch.json; carries PID, isolated test slot and build hashes")
    p.add_argument("--pid",type=int)
    p.add_argument("--data-dir",type=Path)
    p.add_argument("--isolated-profile",action="store_true",help="Confirm this process uses a disposable test profile (required without --launch)")
    p.add_argument("--catalog-only",action="store_true")
    p.add_argument("--reprocess",type=Path,help="Create a derived report from a saved report directory/JSON; no runtime is accessed")
    p.add_argument("--current",action="store_true",help="Read-only attach: capture the current UI and report, without navigation, pointer movement or release")
    p.add_argument("--owner-queue",type=Path,help="Owner-managed read-only file queue, permitted only with --current")
    p.add_argument("--only",nargs="*")
    p.add_argument("--fixture",action="append",default=[],choices=("showroom","career","network"))
    p.add_argument("--hover",choices=("none","sample","all"),default="none")
    p.add_argument("--items",choices=("pages","all"),default="pages",help="Fast default samples carousel pages; all captures every selection")
    p.add_argument("--comfort-guides",action="store_true",help="Analyze each fresh capture with read-only MCP comfort guides; save separate PNG/JSON without layout-only dedup")
    p.add_argument("--livery-overlays-only",action="store_true",help="Narrow livery pass: initial side panels, keyboard, Workshop, verified clean close; omit tab/view/category galleries")
    p.add_argument("--dialog-templates",action=argparse.BooleanOptionalAction,default=True)
    p.add_argument("--eye",choices=("left","right","both"),default="both")
    p.add_argument("--width",type=int,default=2048)
    p.add_argument("--settle",type=float,default=.25)
    p.add_argument("--dwell-frames",type=int,default=3)
    p.add_argument("--hand",choices=("left","right"),default="right")
    p.add_argument("--native-timeout",type=float,default=5,help="Bounded native pipe response deadline in seconds")
    p.add_argument("--fixture-timeout",type=float,default=20,help="Bounded normal-menu/loading/livery binding readiness deadline in seconds")
    p.add_argument("--fail-fast",action="store_true")
    args=p.parse_args()
    if args.reprocess:
        report=reprocess_report(args.reprocess,args.output)
        print(json.dumps({"report":str(args.output/"index.html"),"derived":True,**report["summary"]},indent=2))
        return
    if not args.fox_root:p.error("--fox-root is required unless --reprocess is used")
    if args.owner_queue and not args.current:p.error("--owner-queue is permitted only with --current")
    if args.output.exists():
        p.error("Use a new output directory to preserve earlier evidence")
    manifest=load_manifest(args.manifest)
    if args.only and set(args.only)-{c["id"] for c in manifest["cases"]}:
        p.error("Unknown --only case")
    launch=json.loads(args.launch.read_text(encoding="utf-8-sig")) if args.launch else {}
    args.pid=args.pid or launch.get("pid")
    args.data_dir=args.data_dir or (Path(launch["data"]) if launch.get("data") else None)
    if not args.catalog_only:
        if not args.pid or not args.data_dir:
            p.error("Provide --launch or both --pid and --data-dir")
        if not args.current and not args.isolated_profile and int(launch.get("test_slot",0))<1000:
            p.error("Atlas navigation requires an isolated test profile")
    args.output.mkdir(parents=True)
    for folder in ("images","states"):(args.output/folder).mkdir()
    report={"version":1,"started":datetime.now(timezone.utc).isoformat(),"build":launch,
            "attachment":{"pid":args.pid,"data_dir":str(args.data_dir) if args.data_dir else None,"owner_queue":str(args.owner_queue) if args.owner_queue else None},
            "catalog":discover_source(args.fox_root),"cases":[],"states":[],"mode":"native menu staging + compositor capture; not ray E2E",
            "limits":["Wreck Run deferred","Network, progression and race states need their owning fixtures","Template previews use diagnostic text", "Carousel default samples pages; --items all captures every selection"],
            "configuration":{"current":args.current,"hover":args.hover,"items":args.items,"dialog_templates":args.dialog_templates,"comfort_guides":args.comfort_guides,
                             "livery_overlays_only":args.livery_overlays_only}}
    started=time.monotonic()
    try:
        if args.catalog_only:
            report["cases"]=[{**c,"status":c.get("status","skipped"),"reason":c.get("reason","Catalog only; game was not touched")} for c in manifest["cases"]]
        else:
            asyncio.run(run_live(args,manifest,report))
    except Exception as error:
        report["fatal_error"]=repr(error)
        raise
    finally:
        report["elapsed_seconds"]=round(time.monotonic()-started,3)
        write_report(args.output,report)
        print(json.dumps({"report":str(args.output/"index.html"),**report["summary"]},indent=2))


if __name__=="__main__":
    cli()
