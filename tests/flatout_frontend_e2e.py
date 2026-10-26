"""Live FlatOut front-end checks through real XR input and read-only diagnostics.

Launch the current Fox Debug RC with this simulator and an isolated profile,
then enter a race. This test opens Pause, checks target spacing, resumes once,
cancels Restart and Main Menu confirmations, and opens Options for further QA.
With --options-only, start in Options to check every category and scroll both
ends using either the world-space front end or an in-race quad. Diagnostics are
read-only; input always travels through real XR actions.
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
from pathlib import Path

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import quaternion_matrix, ui_surface, screenshot_pixel, world_pixel

ROOT = Path(__file__).resolve().parents[1]
RAY_OFFSET = {"position": [.03, 0, 0], "orientation": [0, -.043619387365336, 0, .999048221581858]}


def read_game(pid, method, params=None):
    # RenderDoc records the rendered frame; it cannot activate or navigate UI.
    assert method in {"game.get_state", "game.errors", "menu.get_state", "vr.targets", "vr.get_state", "hud.pause_menu", "ui.get_message_box", "ui.reward_state", "ui.vr_button_map", "options.get_state", "statistics.get_state", "party.get_state", "race.end_screen", "leaderboard.get_state", "hud.rects", "hud.get_node", "rogue.state", "rogue.route_map", "renderdoc.capture", "audio.ui_feedback", "net.get_state"}
    if params is None:
        params = {"action": "list"} if method == "hud.pause_menu" else {}
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    with open(r"\\.\pipe\FlatoutMCP_"+str(pid), "r+b", buffering=0) as pipe:
        pipe.write(struct.pack("<I", len(request))+request)
        def read(n):
            data = b""
            while len(data) < n:
                chunk = pipe.read(n-len(data))
                if not chunk:
                    raise RuntimeError("Game pipe closed")
                data += chunk
            return data
        response = json.loads(read(struct.unpack("<I", read(4))[0]))
    assert "error" not in response, response
    return response["result"]


def quad_pixel(capture, rect, eye_name="left"):
    """Project the native 1920x1080 target through its submitted XR quad."""
    surface = max((s for s in capture["geometry"]["surfaces"] if s["visibility"] == 0), key=lambda s:s["size"][0]*s["size"][1])
    l, t, r, b = rect
    local = np.array([((l+r)/3840-.5)*surface["size"][0],
                      (.5-(t+b)/2160)*surface["size"][1], 0])
    point = quaternion_matrix(surface["pose"]["orientation"]) @ local + surface["pose"]["position"]
    eye = next(e for e in capture["geometry"]["eyes"] if e["eye"] == eye_name)
    local = quaternion_matrix(eye["pose"]["orientation"]).T @ (point-eye["pose"]["position"])
    left, right, up, down = map(math.tan, eye["fov"])
    u, v = (local[0]/-local[2]-left)/(right-left), (local[1]/-local[2]-up)/(down-up)
    ex, ey, ew, eh = eye["viewport"]
    cx, cy, cw, ch = capture["crop"]
    w, h = capture["image_size"]
    return (ex+u*ew-cx)/cw*w-.5, (ey+v*eh-cy)/ch*h-.5


class Frontend:
    def __init__(self, args, session):
        self.args, self.session, self.sequence, self.cases = args, session, 0, []
        self.last_capture = None
        self.calibrated_commands = {}
        self.query_lock = asyncio.Lock()

    async def query(self, method, params=None):
        async with self.query_lock:
            return await asyncio.wait_for(asyncio.to_thread(read_game, self.args.pid, method, params), 15)

    async def tool(self, name, args, label):
        if name in ('hover_ui_at','click_ui_at','hold_ui_at','scroll_ui_at'):
            args={**args,'timeout':args.get('timeout',30)}
        key=args.get('command_id')
        if key in self.calibrated_commands:
            args=self.calibrated_commands[key]
        elif name in ('hover_ui_at','click_ui_at','hold_ui_at','scroll_ui_at'):
            from flatout_ray_offset import native_pointer_offset
            calibration=native_pointer_offset(await self.query('vr.get_state'),args.get('hand',self.args.hand))
            if calibration is not None:
                args={**args,'ray_offset':calibration}
            if key:
                self.calibrated_commands[key]=dict(args)
        response = await self.session.call_tool(name, args)
        self.sequence += 1
        stem = self.args.evidence/f"{self.sequence:03}-{label}"
        values = [json.loads(c.text) for c in response.content if c.type == "text" and c.text.startswith("{")]
        stem.with_suffix(".json").write_text(json.dumps(values, indent=2))
        for i, block in enumerate(c for c in response.content if c.type == "image"):
            path = stem.with_name(stem.name+f"-{i}.jpg")
            path.write_bytes(base64.b64decode(block.data))
            if i == 0:
                self.last_image_path = path
        assert not response.isError and values, (name, values)
        return values[0]

    async def capture(self, label):
        capture = await self.tool("capture_screenshot", {"eye": "left", "max_width": 1280, "timeout": 30}, label)
        assert 'capture_id' in capture,capture
        self.last_capture = capture
        return self.last_capture

    async def credits_layout(self):
        assert (await self.query("menu.get_state"))["current_menu"]=="CREDITS"
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        full=[n for n in nodes if n["visible"] and n["kind"]=="panel" and n["color"]["a"]>.2 and n["rect"][2]-n["rect"][0]>=1900 and n["rect"][3]-n["rect"][1]>=1070]
        await self.capture("credits-reader-layout")
        assert not full,("Credits must keep the live scene visible outside its reading panel",full)
        panel=next(n for n in nodes if n["path"].endswith("/CreditsReadingPanel") and n["visible"])
        viewport=next(n for n in nodes if n["path"].endswith("/CreditsViewport") and n["visible"])
        l,t,r,b=panel["rect"];vl,vt,vr,vb=viewport["rect"]
        assert 0<l<vl<vr<r<1920 and 0<t<vt<vb<b<1080,(panel,viewport)
        actions=(await self.query("vr.targets"))["targets"]
        assert {a["name"] for a in actions}=={"helper.Y.0","helper.LB.1","helper.RB.2","helper.Cancel.3"}
        assert all(a["rect"][0]>=vl and a["rect"][2]<=vr and a["rect"][1]-b>=24 and a["rect"][3]<=1056 for a in actions),(panel,actions)
        assert all(a["rect"][2]-a["rect"][0]>=240 and a["rect"][3]-a["rect"][1]>=80 for a in actions),actions
        widths=[a["rect"][2]-a["rect"][0]for a in actions];assert max(widths)-min(widths)<1,"Credits transport controls need consistent widths"
        source=await self.tool("capture_screenshot",{"eye":"both","max_width":2048},"credits-reader-both-eyes")
        assert source["geometry"]["surfaces"],"Credits must use the stable reading surface"
        w,h=source["image_size"]
        for eye,index in [("left",0),("right",1)]:
            for rect in [panel["rect"],*[a["rect"]for a in actions]]:
                points=[quad_pixel(source,(x,y,x,y),eye)for x,y in [(rect[0],rect[1]),(rect[2],rect[3])]]
                assert all(index*w/2<=x<(index+1)*w/2 and 0<=y<h for x,y in points),(eye,rect,points)
        await self.audit("credits-control-spacing")
        self.cases.append({"case":"credits-bounded-reader-and-separate-controls","passed":True,"panel":panel,"viewport":viewport,"actions":actions})

    async def credits(self):
        await self.credits_layout()
        state=(await self.query("menu.get_state"))["credits"]
        if state["paused"]:await self.click("helper.Y.0")
        await self.click("helper.Y.0")
        assert (await self.query("menu.get_state"))["credits"]["paused"],"Pause did not stop playback"
        labels=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        assert any(n.get("text")=="Resume" and n["visible"] and n["path"].endswith("/RayActionLabel")for n in labels),"The paused control must say Resume"
        async def settle_paused():
            until=asyncio.get_running_loop().time()+4
            while True:
                s=(await self.query("menu.get_state"))["credits"]
                if abs(s["speed"])<1:return s
                assert asyncio.get_running_loop().time()<until,("Scrub kept running after releasing the trigger",s)
                await asyncio.sleep(.1)
        stopped=await settle_paused();await asyncio.sleep(.3)
        assert abs((await self.query("menu.get_state"))["credits"]["position"]-stopped["position"])<2,"Paused credits continued moving"
        for target,direction in [("helper.RB.2",-1),("helper.LB.1",1)]:
            before=(await self.query("menu.get_state"))["credits"]
            await self.click(target)
            after=(await self.query("menu.get_state"))["credits"]
            assert after["paused"],"Scrubbing changed pause state"
            assert direction*(after["position"]-before["position"])>50,("Held ray scrub moved in the wrong direction",target,before,after)
            await settle_paused()
        await self.click("helper.Y.0")
        assert not (await self.query("menu.get_state"))["credits"]["paused"],"Resume did not restart playback"
        await self.click("helper.Cancel.3")
        assert (await self.query("menu.get_state"))["current_menu"]=="EXTRAS", "Back did not return to Extras"
        self.cases.append({"case":"credits-pause-resume-and-held-scrub","passed":True,"hand":self.args.hand})

    async def credits_stop(self):
        assert (await self.query("menu.get_state"))["current_menu"]=="CREDITS"
        if (await self.query("menu.get_state"))["credits"]["paused"]:await self.click("helper.Y.0")
        await asyncio.sleep(.4)
        _,args=await self.point_args("helper.Y.0")
        assert (await self.tool("hover_ui_at",args,"credits-stop-hover"))["status"]=="input_delivered"
        args.update(command_id=uuid.uuid4().hex,hold_frames=45,timeout=30)
        pending=asyncio.create_task(self.tool("click_ui_at",args,"credits-stop-click"));samples=[]
        try:
            while not pending.done():
                samples.append((await self.query("menu.get_state"))["credits"])
                await asyncio.sleep(.015)
        finally:assert (await pending)["status"]=="input_delivered"
        paused=[s for s in samples if s["paused"]]
        assert paused,"The test did not observe the pause transition"
        self.cases.append({"case":"credits-pause-stops-immediately","passed":max(abs(s["speed"])for s in paused)<1,"samples":samples})
        assert max(abs(s["speed"])for s in paused)<1,("Pause must freeze the reading position immediately",paused)
        assert abs(paused[-1]["position"]-paused[0]["position"])<2,"Paused text coasted"

    async def credits_finish(self):
        await self.credits_layout()
        state=(await self.query("menu.get_state"))["credits"]
        if state["paused"]:await self.click("helper.Y.0")
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":5}))["nodes"]
        text=next(n for n in nodes if n["path"].endswith("/CreditsText"))
        viewport=next(n for n in nodes if n["path"].endswith("/CreditsViewport"))
        # Completion occurs when the final text leaves the viewport's top.
        finish_at=state["position"]-(text["rect"][3]-viewport["rect"][1])
        before,args=await self.point_args("helper.RB.2")
        held=await self.tool("hold_ui_at",args,"credits-full-roll-hold")
        assert held["status"]=="input_delivered",held
        started=asyncio.get_running_loop().time();refresh=started;trace=[];fades=[]
        try:
            while True:
                current=await self.query("menu.get_state")
                elapsed=asyncio.get_running_loop().time()-started
                if current["current_menu"]!="CREDITS":
                    assert current["current_menu"]=="EXTRAS",current
                    break
                assert elapsed<100,"Holding Forward never completed the credit roll"
                state=current["credits"]
                panel=await self.query("hud.get_node",{"root":"menu","name":"CreditsReadingPanel"})
                if panel["color"]["a"]<.99:fades.append({"seconds":elapsed,"alpha":panel["color"]["a"]})
                remaining=max(0.,(state["position"]-finish_at)/750.)
                if asyncio.get_running_loop().time()-refresh>=4 and remaining>2.:
                    trace.append({"seconds":elapsed,**state})
                    _,args=await self.point_args("helper.RB.2")
                    receipt=await self.tool("hold_ui_at",args,"credits-full-roll-refresh")
                    if receipt["status"]!="input_delivered":
                        assert (await self.query("menu.get_state"))["current_menu"]=="EXTRAS",receipt
                        break
                    refresh=asyncio.get_running_loop().time()
                    print("Credits roll",round(elapsed,1),"s",round(state["position"]),flush=True)
                await asyncio.sleep(.035 if state["position"]< -30000 else .15)
        finally:
            source=await self.capture("credits-finish-release-source")
            await self.tool("release_ui_pointer",{"capture_id":source["capture_id"],"hand":self.args.hand},"credits-finish-release")
        after=await self.query("vr.targets")
        assert after["clicks"]==before["clicks"]+1,("Refreshing a held Forward generated another click",before,after)
        assert not after["targets"],"Credits transport controls survived on the spatial Extras menu"
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":4,"all":True}))["nodes"]
        assert not any(n["visible"] and n["path"].endswith("/CreditsReadingPanel") for n in nodes),"The reader survived its automatic close"
        assert any(0<f["alpha"]<.99 for f in fades),"The ending skipped its bounded fade"
        self.cases.append({"case":"credits-full-roll-held-forward-and-ending-fade","passed":True,"hand":self.args.hand,"seconds":elapsed,"trace":trace,"fade":fades})

    async def popup_motion(self):
        """Observe a native popup entrance during an actual spatial-menu ray click."""
        assert not (await self.query("ui.get_message_box"))["open"]
        menu=await self.query("menu.get_state")
        assert menu["current_menu"]=="MAIN MENU" and menu["is_3d"],"Start in the spatial main menu"
        item=next(i for i in menu["items"] if i["title"]=="QUIT")
        async def aim_source():
            current=await self.query("menu.get_state")
            native=await self.query("vr.targets")
            capture=await self.capture("quit-motion-source")
            point=current["items"][item["index"]]["rendered_position"]
            frame={**native["vr_frame"],"panel":{**current["spatial_layout"],"origin":point},"panel_width":1.,"panel_height":1.}
            state={"vr":{"frame":frame}}
            x,y=world_pixel(state,capture,point,"left")
            assert 0<=x<capture["image_size"][0] and 0<=y<capture["image_size"][1]
            return {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":self.args.hand,
                    "origin_mode":"hand","surface":ui_surface(state),"ray_offset":RAY_OFFSET,"dwell_frames":30}
        aim=await aim_source()
        assert (await self.tool("hover_ui_at",aim,"quit-motion-hover"))["status"]=="input_delivered"
        assert (await self.query("menu.get_state"))["selection_index"]==item["index"]
        assert not (await self.query("ui.get_message_box"))["open"],"Hover opened Quit"
        args={**(await aim_source()),"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
        pending=asyncio.create_task(self.tool("click_ui_at",args,"quit-motion-click"))
        samples=[];start=asyncio.get_running_loop().time()
        try:
            while asyncio.get_running_loop().time()-start<3:
                nodes=(await self.query("hud.rects",{"root":"msgbox","max_depth":1,"all":True}))["nodes"]
                layer=next((n for n in nodes if n["path"]=="MESSAGEBOX_ROOT/Messagebox Layer 0" and n["visible"]),None)
                if layer:samples.append({"seconds":asyncio.get_running_loop().time()-start,"alpha":layer["color"]["a"]})
                if pending.done() and samples and samples[-1]["alpha"]>=.99:break
                await asyncio.sleep(.015)
        finally:
            clicked=await pending
        assert clicked["status"]=="input_delivered",clicked
        assert (await self.query("ui.get_message_box"))["open"],"The ray click did not open Quit"
        await self.tool("click_ui_at",args,"quit-motion-retry")
        intermediate=[s for s in samples if 0<s["alpha"]<.99]
        self.cases.append({"case":"native-popup-entrance-fade","passed":bool(intermediate) and samples[-1]["alpha"]>=.99,"samples":samples})
        assert intermediate,"The popup snapped on without an observable entrance fade"
        assert samples[-1]["alpha"]>=.99,"The popup did not become fully readable"
        assert all(b["alpha"]>=a["alpha"] for a,b in zip(samples,samples[1:])),"The entrance fade restarted or reversed"
        if self.args.max_popup_fade_seconds is not None:
            assert len(intermediate)>=2,"Need two intermediate native frames to measure the fade"
            slope=float(np.polyfit([s["seconds"]for s in intermediate],[s["alpha"]for s in intermediate],1)[0])
            duration=1./slope
            self.cases.append({"case":"popup-entrance-response-time","passed":0<duration<=self.args.max_popup_fade_seconds,
                               "estimated_seconds":duration,"maximum_seconds":self.args.max_popup_fade_seconds})
            assert 0<duration<=self.args.max_popup_fade_seconds,("Dialog entrance is too slow",duration)
        await self.popup_spacing()
        await self.popup_no_prompts()

    async def popup_no_prompts(self):
        """A modal suppresses the soundtrack HUD without hiding its underlying menu."""
        assert (await self.query("ui.get_message_box"))["open"], "Open a cancelable dialog over the front end first"
        before=await self.query("menu.get_state")
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":16,"all":True}))["nodes"]
        music=[n for n in nodes if "/Music/" in n["path"]]
        assert music,"The soundtrack HUD must exist for this regression"
        await self.capture("modal-suppresses-music-prompts")
        assert not any(n["visible"] for n in music),("Legacy soundtrack controls draw behind the dialog",music)
        native=await self.query("vr.targets")
        assert native["targets"] and all(t["name"].startswith("msgbox.") for t in native["targets"]),native
        await self.hover("msgbox.cancel")
        assert (await self.query("vr.targets"))["clicks"]==native["clicks"],"Hover activated the dialog"
        await self.click("msgbox.cancel")
        assert not (await self.query("ui.get_message_box"))["open"]
        after=await self.query("menu.get_state")
        assert after["current_menu"]==before["current_menu"] and after["is_open"],"Cancel lost the underlying menu"
        assert after["selection_index"]==before["selection_index"],"Cancel changed selection"
        restored=any(n["visible"] and n["path"].endswith("/Music") for n in (await self.query("hud.rects",{"root":"menu","max_depth":16,"all":True}))["nodes"])
        assert restored,"Soundtrack HUD did not restore after Cancel"
        await self.capture("modal-closed-menu-kept")
        self.cases.append({"case":"modal-suppresses-legacy-music-controls","passed":True,"hand":self.args.hand,"menu":after["current_menu"],"restored":restored})

    async def popup_anchor(self):
        """A standing modal is visible in both eyes and stays still during head motion."""
        assert (await self.query("ui.get_message_box"))["open"]
        before=await self.tool("capture_screenshot",{"eye":"both","max_width":2048},"popup-standing-source")
        self.last_capture=before
        head=before["geometry"]["head"]
        assert head["position"][1]>1.,"Start this regression at the simulator's standing pose"
        assert np.allclose(head["orientation"],[0,0,0,1]),"Start without a head rotation"
        base=next(s for s in before["geometry"]["surfaces"] if s["visibility"]==0)
        assert abs(base["pose"]["position"][1]-head["position"][1])<.1,("The popup must open at the user's eye height",head,base)
        nodes=(await self.query("hud.rects",{"root":"msgbox","max_depth":14}))["nodes"]
        frames=[n for n in nodes if n["visible"] and n["path"].endswith("/RayDialogFrame")]
        assert len(frames)==1,frames
        l,t,r,b=frames[0]["rect"]
        def visible(capture):
            w,h=capture["image_size"]
            assert capture["geometry"]["layout"]=="side_by_side"
            for eye,index in (("left",0),("right",1)):
                points=[quad_pixel(capture,(x,y,x,y),eye)for x,y in ((l,t),(r,t),(r,b),(l,b))]
                assert all(index*w/2<=x<(index+1)*w/2 and 0<=y<h for x,y in points),("The full dialog must remain visible in each eye",eye,points)
        visible(before)
        try:
            moved=await self.observed_head_pose("popup-standing-head-moved",{"x":.12,"y":1.82,"z":.10,"yaw_deg":8,"pitch_deg":-5,"roll_deg":4})
            current=next(s for s in moved["geometry"]["surfaces"] if s["visibility"]==0)
            assert np.allclose(current["pose"]["position"],base["pose"]["position"],atol=1e-5),"The dialog followed head translation"
            assert np.allclose(current["pose"]["orientation"],base["pose"]["orientation"],atol=1e-5),"The dialog followed head rotation"
            visible(moved)
            native=await self.query("vr.targets")
            target=next(t["name"]for t in native["targets"]if t["name"].startswith("msgbox."))
            await self.hover(target)
            assert (await self.query("vr.targets"))["clicks"]==native["clicks"],"Hover activated a dialog action"
            self.cases.append({"case":"standing-popup-world-anchor-and-ray-hover","passed":True,"head_before":head,"head_after":moved["geometry"]["head"],"surface_before":base,"surface_after":current,"dialog":frames[0],"hand":self.args.hand})
        finally:
            x,y,z=head["position"]
            await self.observed_head_pose("popup-standing-head-restored",{"x":x,"y":y,"z":z,"yaw_deg":0,"pitch_deg":0,"roll_deg":0})

    async def popup_spacing(self):
        modal=await self.query("ui.get_message_box")
        assert modal["open"],"Open a message box first"
        layer=max(modal["layers"])
        prefix="MESSAGEBOX_ROOT/Messagebox Layer "+str(layer)+"/MESSAGEBOX_CONTENTS/"
        nodes=(await self.query("hud.rects",{"root":"msgbox","max_depth":14}))["nodes"]
        visible=[n for n in nodes if n["visible"] and n["path"].startswith(prefix)]
        names={"Center","Left","Right","Top","Bottom","TopLeft","TopRight","BottomLeft","BottomRight","RayDialogFrame"}
        panels=[n for n in visible if n["kind"]=="panel" and n["path"].removeprefix(prefix) in names and n["color"]["a"]>0]
        assert panels,"Dialog body is missing"
        body=[min(n["rect"][0]for n in panels),min(n["rect"][1]for n in panels),max(n["rect"][2]for n in panels),max(n["rect"][3]for n in panels)]
        assert 0<=body[0]<body[2]<=1920 and 0<=body[1]<body[3]<=1080,("Dialog must fit the visible panel",body)
        content=[n for n in visible if n["path"].removeprefix(prefix) in ("Title","Content","ContentImage") and (n["kind"]!="text" or n.get("text"))]
        assert content
        buttons=[t for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("msgbox.")]
        assert buttons
        assert all(t["rect"][0]-body[0]>=31 and body[2]-t["rect"][2]>=31 for t in buttons),("Actions need horizontal inner padding",body,buttons)
        capture=await self.capture("popup-spacing")
        pixels=np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        image_checks=[]
        native=await self.query("vr.targets")
        for image in (n for n in content if n["path"].endswith("/ContentImage")):
            l,t,r,b=image["rect"]
            if capture["geometry"]["surfaces"]:
                points=[quad_pixel(capture,(x,y,x,y))for x,y in ((l,t),(r,b))]
            else:
                state={"vr":{"frame":native["vr_frame"]}}
                points=[screenshot_pixel(state,capture,x,y)for x,y in ((l,t),(r,b))]
            x0,y0=np.floor(np.min(points,axis=0)).astype(int)+4
            x1,y1=np.ceil(np.max(points,axis=0)).astype(int)-4
            assert 0<=x0<x1<=pixels.shape[1] and 0<=y0<y1<=pixels.shape[0],("Popup illustration must stay visible",points)
            patch=pixels[y0:y1,x0:x1]
            deviation=float(np.mean(np.std(patch,axis=(0,1))))
            image_checks.append({"path":image["path"],"image_rect":[int(x0),int(y0),int(x1),int(y1)],"rgb_deviation":deviation})
            assert deviation>8,("Dialog plate covered its illustration",image_checks)
        for n in content:
            l,t,r,b=n["rect"]
            assert min(l-body[0],body[2]-r,t-body[1])>=31,("Dialog content needs consistent inner padding",body,n)
        footer_top=min(t["rect"][1]for t in buttons)
        assert footer_top-max(n["rect"][3]for n in content)>=31,("Actions need a separate row below the content",body,content,buttons)
        assert min(body[3]-t["rect"][3]for t in buttons)>=31,("Actions must not touch the dialog edge",body,buttons)
        assert all(t["rect"][2]-t["rect"][0]>=199.5 and t["rect"][3]-t["rect"][1]>=79.5 for t in buttons),buttons
        for n in content:
            if not n["path"].endswith("/ContentImage"):continue
            for text in content:
                if not text["path"].endswith("/Content"):continue
                l,t,r,b=text["rect"];il,it,ir,ib=n["rect"]
                if min(r,ir)>max(l,il) and t>=it:
                    assert t-ib>=23,("Image and caption need breathing room",n,text)
        self.cases.append({"case":"popup-content-padding-and-action-row","passed":True,"body":body,"content":content,"buttons":buttons,"illustrations":image_checks})

    async def menu_button(self):
        capture = await self.capture("menu-button-source")
        result = await self.tool("click_ui_at", {"capture_id": capture["capture_id"],
            "x": capture["image_size"][0]/2, "y": capture["image_size"][1]/2,
            "hand": "left", "button": "menu", "origin_mode": "eye"}, "menu-button")
        assert result["status"] == "input_delivered"
        assert (await self.query("hud.pause_menu"))["is_open"]

    async def audit(self, label, actions_only=False):
        targets = (await self.query("vr.targets"))["targets"]
        await self.capture(label)
        overlaps = []
        cramped = []
        for i, a in enumerate(targets):
            for b in targets[i+1:]:
                if actions_only and not any(t["name"].startswith(("helper.","endhelper.","msgbox.")) for t in (a,b)):
                    continue # Settings rows intentionally sit inside their category targets.
                l, t, r, bottom = a["rect"]
                l2, t2, r2, b2 = b["rect"]
                dx, dy = min(r,r2)-max(l,l2), min(bottom,b2)-max(t,t2)
                if dx > 1 and dy > 1:
                    overlaps.append({"targets": [a["name"], b["name"]], "intersection": [dx, dy]})
                if a["name"].startswith("button.") and b["name"].startswith("button.") and dy > 1 and -5.5 < dx <= 1:
                    cramped.append({"targets": [a["name"], b["name"]], "gap": -dx})
        self.cases.append({"case": label, "passed": not overlaps and not cramped,
                           "targets": targets, "overlaps": overlaps, "cramped": cramped})
        assert not overlaps and not cramped, (overlaps, cramped)
        print(label, "PASS", flush=True)

    async def point_args(self, name, fraction=(.5, .5), hand=None):
        before = await self.query("vr.targets")
        rect = next(t["rect"] for t in before["targets"] if t["name"] == name)
        capture = await self.capture("source-"+name)
        l, t, r, b = rect
        px, py = l+(r-l)*fraction[0], t+(b-t)*fraction[1]
        surface = None
        frame = before.get("vr_frame")
        if frame and (await self.query("menu.get_state"))["is_3d"]:
            frame = None  # Spatial menus retain their anchor while a native modal covers them.
        if frame is not None:
            # Raised controls are compositor quads even when the base is an RTT.
            # Use the independently reported carrier, never the first hover tile.
            state = {"vr": {"frame": frame}}
            x, y = screenshot_pixel(state, capture, px, py)
            surface = ui_surface(state)
        else:
            assert capture["geometry"]["surfaces"], "No native or world-space menu carrier"
            x, y = quad_pixel(capture, (px, py, px, py))
        args = {"capture_id": capture["capture_id"], "x": float(x), "y": float(y),
                "hand": hand or self.args.hand, "origin_mode": "hand", "ray_offset": RAY_OFFSET, "dwell_frames": 30}
        if surface:
            args["surface"] = surface
        else:
            # Use the same stable carrier for projection and input. A returning
            # hover face can overlap this pixel while retiring during dwell.
            carrier=max((s for s in capture["geometry"]["surfaces"] if s["visibility"]==0),key=lambda s:s["size"][0]*s["size"][1])
            args["surface_id"]=carrier["id"]
        return before, args

    async def hover(self, name, fraction=(.5, .5)):
        before, args = await self.point_args(name, fraction)
        hover = await self.tool("hover_ui_at", args, "hover-"+name)
        assert hover["status"] == "input_delivered", hover
        hovered = await self.query("vr.targets")
        assert hovered["hovered"] == name, (name, hovered)
        return before, hovered, args

    async def click(self, name, fraction=(.5, .5)):
        before, hovered, args = await self.hover(name, fraction)
        args.update(command_id=uuid.uuid4().hex, hold_frames=45, timeout=30)
        clicked = await self.tool("click_ui_at", args, "click-"+name)
        assert clicked["status"] == "input_delivered", clicked
        after = await self.query("vr.targets")
        assert after["clicks"] == hovered["clicks"]+1, (name, hovered, after)
        await self.tool("click_ui_at", args, "retry-"+name)
        assert (await self.query("vr.targets"))["clicks"] == after["clicks"]
        self.cases.append({"case": "click-"+name, "passed": True, "before": before, "after": after})
        print(name, "one click + retry PASS", flush=True)

    async def options_comfort(self):
        assert (await self.query("options.get_state"))["open"],"Start in Options"
        return await self.reading_comfort("options")

    async def observed_head_pose(self, label, pose):
        # set_head_pose queues an asynchronous command. Observe its rendered
        # pose before projecting another ray; a fixed sleep can capture the old view.
        response=await self.session.call_tool("set_head_pose",pose)
        assert not response.isError,response
        assert any(getattr(c,"text","").startswith("Head pose command queued:") for c in response.content),response
        yaw,pitch,roll=(math.radians(pose.get(k,0)) for k in ("yaw_deg","pitch_deg","roll_deg"))
        cy,sy=math.cos(yaw),math.sin(yaw);cp,sp=math.cos(pitch),math.sin(pitch);cr,sr=math.cos(roll),math.sin(roll)
        expected=np.array([[cy,0,sy],[0,1,0],[-sy,0,cy]])@np.array([[1,0,0],[0,cp,-sp],[0,sp,cp]])@np.array([[cr,-sr,0],[sr,cr,0],[0,0,1]])
        deadline=asyncio.get_running_loop().time()+6
        attempt=0
        while asyncio.get_running_loop().time()<deadline:
            capture=await self.tool("capture_screenshot",{"eye":"both","max_width":2048},label+"-"+str(attempt))
            self.last_capture=capture
            actual=capture["geometry"]["head"]
            if np.allclose(actual["position"],[pose[k] for k in ("x","y","z")],atol=1e-5) and np.allclose(quaternion_matrix(actual["orientation"]),expected,atol=1e-5):
                return capture
            attempt+=1
            await asyncio.sleep(.05)
        raise AssertionError(("Queued head pose did not reach the rendered frame",pose,actual))

    async def reading_comfort(self, label, hover_target=None):
        before=await self.query("vr.targets")
        capture=await self.tool("capture_screenshot",{"eye":"both","max_width":2048},label+"-head-height")
        self.last_capture=capture
        def surface_for(source,native):
            quads=[s for s in source["geometry"]["surfaces"] if s["visibility"]==0]
            return max(quads,key=lambda s:s["size"][0]*s["size"][1]) if quads else ui_surface({"vr":{"frame":native["vr_frame"]}})
        surface=surface_for(capture,before)
        head=capture["geometry"]["head"]
        assert np.allclose(head["orientation"],[0,0,0,1]),"Start at a neutral head rotation"
        delta=np.array(surface["pose"]["position"])-head["position"]
        pitch=math.degrees(math.atan2(float(delta[1]),float(np.linalg.norm(delta[[0,2]]))))
        self.cases.append({"case":label+"-reading-surface-at-head-height","passed":-12<=pitch<=6,"pitch_degrees":pitch,
                           "head":head,"surface":surface})
        assert -12<=pitch<=6,(label+" requires looking too far from the neutral gaze",pitch)
        def visible(source,plane):
            eyes=source["geometry"]["eyes" if source["geometry"]["surfaces"] else "projection_eyes"]
            w,h=source["image_size"];cx,cy,cw,ch=source["crop"]
            for eye in eyes:
                index=int(eye["eye"]=="right")
                for target in before["targets"]:
                    l,t,r,b=target["rect"]
                    for x,y in ((l,t),(r,b)):
                        point=quaternion_matrix(plane["pose"]["orientation"])@np.array([(x/1920-.5)*plane["size"][0],(.5-y/1080)*plane["size"][1],0])+plane["pose"]["position"]
                        local=quaternion_matrix(eye["pose"]["orientation"]).T@(point-eye["pose"]["position"])
                        left,right,up,down=map(math.tan,eye["fov"])
                        ex,ey,ew,eh=eye["viewport"]
                        px=(ex+(local[0]/-local[2]-left)/(right-left)*ew-cx)/cw*w-.5
                        py=(ey+(local[1]/-local[2]-up)/(down-up)*eh-cy)/ch*h-.5
                        assert index*w/2<=px<(index+1)*w/2 and 0<=py<h,(label+" control clipped in an eye",eye["eye"],target,px,py)
        visible(capture,surface)
        try:
            moved=await self.observed_head_pose(label+"-head-moved",{"x":.1,"y":head["position"][1]+.12,"z":.05,"yaw_deg":6,"pitch_deg":-4,"roll_deg":2})
            current=surface_for(moved,await self.query("vr.targets"))
            assert np.allclose(current["pose"]["position"],surface["pose"]["position"],atol=1e-4),label+" followed head translation"
            assert np.allclose(current["pose"]["orientation"],surface["pose"]["orientation"],atol=1e-4),label+" followed head rotation"
            visible(moved,current)
        finally:
            await self.observed_head_pose(label+"-head-restored",{"x":head["position"][0],"y":head["position"][1],"z":head["position"][2],"yaw_deg":0,"pitch_deg":0,"roll_deg":0})
        action=hover_target or next(t["name"] for t in before["targets"] if t["name"].startswith("helper.Cancel."))
        await self.hover(action)
        self.cases.append({"case":label+"-world-locked-both-eye-controls-and-ray-hover","passed":True,"hand":self.args.hand})

    async def options_redesign_layout(self):
        native = await self.query("options.get_state")
        assert native["open"], native
        targets = (await self.query("vr.targets"))["targets"]
        await self.capture("options-focused-layout")
        categories = [t for t in targets if t["name"].startswith("settings.category.")]
        missing = len(categories)!=len(native["boxes"])
        self.cases.append({"case":"options-direct-category-rail","passed":not missing,"categories":categories,"expected":len(native["boxes"])})
        assert not missing, "Every Options category needs a directly clickable button"
        assert all(t["rect"][2]-t["rect"][0]>=300 and t["rect"][3]-t["rect"][1]>=80 for t in categories)
        assert not any(t["name"].startswith("strip.") for t in targets), "Legacy category carousel is still exposed"
        selected = native["selection_index"]
        rows = [t for t in targets if ".row" in t["name"]]
        assert rows and all(t["name"].startswith("box"+str(selected)+".row") for t in rows), "Neighboring category settings must stay hidden"
        assert all(t["rect"][3]-t["rect"][1]>=72 for t in rows), "Options still has tiny rows"
        await self.audit("options-focused-control-spacing")
        self.cases.append({"case":"options-one-focused-settings-list","passed":True,"selection":selected})

    async def career_welcome(self):
        menu=await self.query("menu.get_state")
        assert menu["current_menu"]=="MAIN MENU" and menu["is_3d"],"Start in Main with an isolated profile that owns no Career car"
        assert not (await self.query("ui.get_message_box"))["open"]
        item=menu["items"][0];assert item["title"].lower()=="career",item
        native=await self.query("vr.targets")
        capture=await self.capture("career-welcome-entry-source")
        frame={**native["vr_frame"],"panel":{**menu["spatial_layout"],"origin":item["rendered_position"]},"panel_width":1.,"panel_height":1.}
        state={"vr":{"frame":frame}}
        x,y=world_pixel(state,capture,item["rendered_position"],"left")
        aim={"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":self.args.hand,
             "origin_mode":"hand","surface":ui_surface(state),"ray_offset":RAY_OFFSET,"dwell_frames":30}
        assert (await self.tool("hover_ui_at",aim,"career-welcome-entry-hover"))["status"]=="input_delivered"
        assert not (await self.query("ui.get_message_box"))["open"],"Hover opened Career"
        args={**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
        assert (await self.tool("click_ui_at",args,"career-welcome-entry-click"))["status"]=="input_delivered"
        assert (await self.query("ui.get_message_box"))["open"],"The first Career welcome did not open"
        targets=await self.query("vr.targets")
        assert [t["name"]for t in targets["targets"]]==["msgbox.validate"],targets
        before_retry=targets["clicks"]
        assert (await self.tool("click_ui_at",args,"career-welcome-entry-retry"))["status"]=="input_delivered"
        assert (await self.query("vr.targets"))["clicks"]==before_retry,"Retry activated the popup"
        self.last_capture=await self.tool("capture_screenshot",{"eye":"both","max_width":2048},"career-welcome-scene-both-eyes")
        pixels=np.asarray(Image.open(self.last_image_path).convert("RGB"))
        h,w=pixels.shape[:2];fractions=[]
        for index,eye in enumerate(("left","right")):
            # The scene above the modal must retain the road/sky visible before entry.
            patch=pixels[int(h*.10):int(h*.30),int(w*(index/2+.075)):int(w*(index/2+.425))]
            fraction=float(np.mean(np.max(patch,axis=2)>45));fractions.append({"eye":eye,"visible_fraction":fraction})
        self.cases.append({"case":"career-welcome-keeps-live-scene-in-both-eyes","passed":all(f["visible_fraction"]>.5 for f in fractions),"scene":fractions})
        assert all(f["visible_fraction"]>.5 for f in fractions),("Career welcome replaced the live scene with black",fractions)
        current=await self.query("menu.get_state")
        assert current["current_menu"]=="MAIN MENU" and current["is_3d"],"Wait for acknowledgement before starting the showroom transition"
        await self.popup_spacing()
        await self.click("msgbox.validate")
        assert not (await self.query("ui.get_message_box"))["open"],"Welcome reopened after acknowledgement"
        assert (await self.query("menu.get_state"))["current_menu"]=="CARS","Welcome acknowledgement did not enter car selection"
        await self.capture("career-welcome-car-selection")
        cancel=next(t["name"]for t in (await self.query("vr.targets"))["targets"]if t["name"].startswith("helper.Cancel."))
        await self.click(cancel)
        assert (await self.query("menu.get_state"))["current_menu"]=="MAIN MENU","Back did not leave first-time car selection"
        self.cases.append({"case":"career-welcome-acknowledge-once-and-back-without-purchase","passed":True,"hand":self.args.hand})

    async def career_layout(self):
        native = await self.query("vr.targets")
        assert native["menu"]=="FLATOUT_MODE", "Open Career's cup/event grid first"
        await self.capture("career-grid-layout")
        targets = native["targets"]
        arrows = [t for t in targets if t["name"].startswith("page.")]
        invalid = [t for t in arrows if min(t["rect"][:2])<0 or t["rect"][2]>1920 or t["rect"][3]>952
                   or t["rect"][2]-t["rect"][0]<88 or t["rect"][3]-t["rect"][1]<88]
        self.cases.append({"case":"career-page-buttons-visible-and-large","passed":not invalid,"arrows":arrows,"invalid":invalid})
        assert not invalid, ("Career page arrows need full-size targets inside the menu",invalid)
        tiles = [t for t in targets if t["name"].startswith("tile.")]
        assert tiles
        assert all(min(t["rect"][:2])>=0 and t["rect"][2]<=1920 and t["rect"][3]<=952 for t in tiles), "Career cards extend beyond the menu"
        gaps = []
        for i,a in enumerate(tiles):
            for b in tiles[i+1:]:
                l,t,r,bottom = a["rect"];l2,t2,r2,b2 = b["rect"]
                dx,dy = min(r,r2)-max(l,l2),min(bottom,b2)-max(t,t2)
                if dy>1: gaps.append(max(l,l2)-min(r,r2))
                if dx>1: gaps.append(max(t,t2)-min(bottom,b2))
        self.cases.append({"case":"career-cards-have-breathing-room","passed":bool(gaps) and min(gaps)>=20,"gaps":gaps})
        assert gaps and min(gaps)>=20, ("Career cards are cramped",gaps)
        await self.audit("career-controls-do-not-overlap")

    async def career_sweep(self):
        menu = await self.query("menu.get_state")
        assert menu["current_menu"]=="FLATOUT_MODE" and menu.get("career"), "Open Career's cup/event grid on the current build"
        initial = menu["career"]["page"]
        count = menu["career"]["page_count"]
        while (await self.query("menu.get_state"))["career"]["page"]:
            before = (await self.query("menu.get_state"))["career"]["page"]
            await self.click("page.prev")
            assert (await self.query("menu.get_state"))["career"]["page"]==before-1
        locked_checked = 0
        for page in range(count):
            await self.career_layout()
            state = (await self.query("menu.get_state"))["career"]
            assert state["page"]==page
            targets = {t["name"] for t in (await self.query("vr.targets"))["targets"]}
            assert ("page.prev" in targets)==(page>0)
            assert ("page.next" in targets)==(page+1<count)
            for tile in state["tiles"]:
                assert tile["title"].strip(), "Career event has no readable name"
                name = f"tile.{tile['line']}.{tile['index']}"
                await self.hover(name)
                after = await self.query("menu.get_state")
                assert after["current_menu"]=="FLATOUT_MODE" and after["career"]["page"]==page
                assert after["career"]["selected_line"]==tile["line"] and after["selection_index"]==tile["index"], ("Ray selected a different Career tile",tile,after)
                self.cases.append({"case":"career-hover-"+str(tile["event_id"]),"passed":True,"title":tile["title"],"page":page})
            locked = next((tile for tile in state["tiles"] if tile["locked"]),None)
            if locked:
                await self.click(f"tile.{locked['line']}.{locked['index']}")
                after = await self.query("menu.get_state")
                assert after.get("career",{}).get("page")==page and after["current_menu"]=="FLATOUT_MODE", "A locked event opened"
                assert not (await self.query("game.get_state"))["is_loading"]
                assert not (await self.query("ui.get_message_box"))["open"]
                locked_checked += 1
                self.cases.append({"case":"career-locked-event-stays-closed-"+str(page),"passed":True,"event":locked})
            await self.capture("career-series-"+str(page)+"-verified")
            if page+1<count:
                await self.click("page.next")
                assert (await self.query("menu.get_state"))["career"]["page"]==page+1, "Held trigger skipped a Career series"
        while (await self.query("menu.get_state"))["career"]["page"]>initial:
            before = (await self.query("menu.get_state"))["career"]["page"]
            await self.click("page.prev")
            assert (await self.query("menu.get_state"))["career"]["page"]==before-1
        assert locked_checked, "The fixture must include a locked event"
        self.cases.append({"case":"career-all-series-and-original-page","passed":True,"pages":count,"original":initial,"locked_pages_checked":locked_checked})

    async def career_cup(self):
        menu = await self.query("menu.get_state")
        state = menu.get("career")
        assert state, "Open Career's event grid with an unlocked cup"
        cup = next(t for t in state["tiles"] if not t["locked"] and t["rounds"]>1)
        name = f"tile.{cup['line']}.{cup['index']}"
        await self.click(name)
        preview = await self.query("menu.get_state")
        assert preview["current_menu"]=="FLATOUT_MODE" and "career" not in preview and preview["item_count"]==1, preview
        await self.ray_buttons()
        await self.reading_comfort("career-cup-preview")
        labels=(await self.query("hud.rects",{"root":"menu","max_depth":12}))["nodes"]
        captions=[n["text"] for n in labels if n["visible"] and n["path"].endswith("/RayActionLabel")]
        assert "Start" in captions and "Confirm" not in captions,("Cup preview must name its Start action",captions)
        self.cases.append({"case":"career-cup-preview-names-start-action","passed":True,"captions":captions})
        reference = (await self.query("vr.targets"))["targets"][0]
        l,t,r,b = reference["rect"]
        for x,y in ((100,260),(960,900)):
            _,aim = await self.point_args(reference["name"],((x-l)/(r-l),(y-t)/(b-t)))
            assert (await self.tool("hover_ui_at",aim,"cup-background-hover"))["status"]=="input_delivered"
            before = await self.query("vr.targets")
            assert not before["hovered"], "The blank cup area exposes an action target"
            args = {**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
            assert (await self.tool("click_ui_at",args,"cup-background-click"))["status"]=="input_delivered"
            assert (await self.query("vr.targets"))["clicks"]==before["clicks"]
            assert not (await self.query("game.get_state"))["is_loading"]
            assert (await self.query("menu.get_state"))["item_count"]==1, "Pointing outside Confirm started the cup"
        self.cases.append({"case":"career-preview-background-cannot-start-race","passed":True})
        cancel = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
        await self.click(cancel)
        assert (await self.query("menu.get_state"))["career"]["page"]==state["page"]
        self.cases.append({"case":"career-cup-preview-back-to-grid","passed":True})
        await self.click(name)
        confirm = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Validate."))
        await self.click(confirm)
        for _ in range(120):
            game = await self.query("game.get_state")
            if not game["is_loading"] and not game["is_main_menu"] and game["state"]=="race": break
            await asyncio.sleep(.5)
        else: raise AssertionError(("Career cup did not finish loading",game))
        assert game["game_mode"]=="career",game
        self.cases.append({"case":"career-confirm-starts-selected-cup-once","passed":True,"event":cup,"level":game["current_level"]})
        await self.capture("career-cup-race-started")

    async def career_cancel(self):
        menu = await self.query("menu.get_state")
        assert menu["is_3d"] and menu["current_menu"]=="FLATOUT", "Open the Career hub with an active cup"
        index = next(i["index"] for i in menu["items"] if i["title"].lower()=="cancel cup")
        titles = [i["title"] for i in menu["items"]]
        self.args.spatial_click_index = index
        self.args.expected_menu = "FLATOUT"
        await self.spatial_hover()
        assert (await self.query("ui.get_message_box"))["open"]
        await self.ray_buttons()
        assert {t["name"] for t in (await self.query("vr.targets"))["targets"]}=={"msgbox.validate","msgbox.cancel"}
        await self.click("msgbox.cancel")
        assert [i["title"] for i in (await self.query("menu.get_state"))["items"]]==titles, "Cancel discarded the active cup"
        self.cases.append({"case":"career-abandon-dialog-cancel-keeps-cup","passed":True})
        await self.spatial_hover()
        await self.click("msgbox.validate")
        after = await self.query("menu.get_state")
        assert not (await self.query("ui.get_message_box"))["open"]
        assert after["is_3d"] and after["item_count"]==len(titles)-1
        assert all(i["title"].lower()!="cancel cup" for i in after["items"])
        self.cases.append({"case":"career-abandon-confirm-restores-go-race","passed":True,"items":after["items"]})

    async def career_wrong_class(self):
        menu = await self.query("menu.get_state")
        assert menu["current_menu"]=="FLATOUT" and not menu["is_3d"] and menu["item_count"]==3
        assert all(i["title"].strip() for i in menu["items"]), "Career class cards need names in the menu model"
        await self.reading_comfort("career-class-selection")
        labels=(await self.query("hud.rects",{"root":"menu","max_depth":12}))["nodes"]
        captions=[n["text"] for n in labels if n["visible"] and n["path"].endswith("/RayActionLabel")]
        assert "Select" in captions and "Confirm" not in captions,("Class selection must name its Select action",captions)
        self.cases.append({"case":"career-class-selection-names-select-action","passed":True,"captions":captions})
        for index,car_class in ((1,"classic"),(2,"allstars")):
            await self.capture("career-class-scene-before-"+car_class)
            source_capture = self.last_capture
            source_frame = (await self.query("vr.targets")).get("vr_frame")
            assert source_capture["geometry"]["surfaces"] or source_frame,"Class selection has no reading surface"
            before = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
            await self.click("item."+str(index))
            assert (await self.query("ui.get_message_box"))["open"]
            assert [t["name"] for t in (await self.query("vr.targets"))["targets"]]==["msgbox.validate"], "Class warning exposes background controls"
            await self.capture("career-wrong-class-"+car_class)
            after = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
            assert before.shape==after.shape, "Preview resized during class-warning comparison"
            # Above the dialog on its actual reading carrier: detect a scene
            # fade left black by opening the popup during a menu transition.
            state = {"vr":{"frame":source_frame}} if source_frame else None
            (x0,y0),(x1,y1) = [quad_pixel(source_capture,(x,y,x,y)) if source_capture["geometry"]["surfaces"]
                             else screenshot_pixel(state,source_capture,x,y) for x,y in ((600,120),(1320,210))]
            roi = (slice(math.floor(min(y0,y1)),math.ceil(max(y0,y1))),
                   slice(math.floor(min(x0,x1)),math.ceil(max(x0,x1))))
            source,actual = before[roi],after[roi]
            visible = (np.min(source,axis=2)>30)&(np.max(source,axis=2)>65)
            assert np.sum(visible)>100, "Class-warning fixture needs a visible scene above the dialog"
            ratio = float(np.mean(actual[visible])/np.mean(source[visible]))
            self.cases.append({"case":"career-class-warning-preserves-scene-"+car_class,
                               "passed":ratio>.5,"scene_pixels":int(np.sum(visible)),"brightness_ratio":ratio})
            assert ratio>.5, ("Class warning blacked out the scene",car_class,ratio)
            pending = await self.query("menu.get_state")
            assert pending["current_menu"]=="FLATOUT" and not pending["is_3d"] and pending["item_count"]==3, "Keep class selection behind the warning until acknowledged"
            await self.click("msgbox.validate")
            assert not (await self.query("ui.get_message_box"))["open"]
            cars = await self.query("menu.get_state")
            assert cars["current_menu"]=="CARS" and cars["items"], "Acknowledging the class warning must open the car shop"
            assert all(i["car_class"]==car_class for i in cars["items"]), ("Wrong-class routing offered the wrong cars",car_class,cars)
            cancel = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
            await self.click(cancel)
            back = await self.query("menu.get_state")
            assert back["current_menu"]=="FLATOUT" and not back["is_3d"] and back["item_count"]==3, "Car-shop Back did not return to class selection"
            self.cases.append({"case":"career-wrong-class-warning-and-back-"+car_class,"passed":True})

    async def options(self):
        native = await self.query("options.get_state")
        assert native["open"] and len(native["boxes"])==7, native
        initial = native["selection_index"]
        baseline = [[r["value"] for r in box["rows"]] for box in native["boxes"]]
        for i in range(7):
            await self.hover("settings.category."+str(i))
            assert (await self.query("options.get_state"))["selection_index"]==initial, "Hover switched category"
        assert [[r["value"] for r in box["rows"]] for box in (await self.query("options.get_state"))["boxes"]]==baseline
        # Jump directly across the rail, including non-adjacent categories.
        for index in (6,0,4,1,5,2,3):
            await self.click("settings.category."+str(index))
            native = await self.query("options.get_state")
            box = native["boxes"][index]
            assert native["selection_index"]==index and box["focused"], native
            title = box["title"]
            values = [r["value"] for r in box["rows"]]
            present = [r["index"] for r in box["rows"] if r["present"]]
            seen = set()
            while (await self.query("options.get_state"))["boxes"][index]["scroll"].get('position',0)>.5:
                await self.click("settings.scroll.up")
            for _ in range(len(present)+1):
                current = (await self.query("options.get_state"))["boxes"][index]
                first,last = current["scroll"]["first"],current["scroll"]["last"]
                visible = [r["index"] for r in current["rows"] if r["visible"]]
                assert visible==present[first:last+1] and 0<len(visible)<=7, (title,current)
                assert not any(r["visible"] for i,b in enumerate((await self.query("options.get_state"))["boxes"]) if i!=index for r in b["rows"]), "Neighbor settings leaked into the focused view"
                await self.options_redesign_layout()
                for row in visible:
                    await self.hover(f"box{index}.row{row}",(.1,.5))
                assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][index]["rows"]]==values, "Hover edited a setting"
                seen.update(visible)
                if last+1==len(present):
                    assert not any(t["name"]=="settings.scroll.down" for t in (await self.query("vr.targets"))["targets"])
                    break
                await self.click("settings.scroll.down")
                next_first=(await self.query("options.get_state"))["boxes"][index]["scroll"]["first"]
                assert first<next_first<=last+1, "Paging must advance without skipping settings"
            else:
                raise AssertionError(("Never reached the final settings page",title))
            assert seen==set(present), (title,seen,present)
            for _ in range(len(present)+1):
                first = (await self.query("options.get_state"))["boxes"][index]["scroll"]["first"]
                if first==0: break
                await self.click("settings.scroll.up")
                assert (await self.query("options.get_state"))["boxes"][index]["scroll"]["first"]==max(0,first-7)
            else: raise AssertionError("Previous Page never reached the first settings")
            assert not any(t["name"]=="settings.scroll.up" for t in (await self.query("vr.targets"))["targets"])
            after = (await self.query("options.get_state"))["boxes"][index]
            assert [r["value"] for r in after["rows"]]==values
            await self.capture("options-"+str(index)+"-verified")
            self.cases.append({"case":"options-"+title,"passed":True,"seen_rows":sorted(seen),"present_rows":present,"values_unchanged":True})
            print(title,len(seen),"settings reached through category/page buttons PASS",flush=True)
        await self.click("settings.category.0")
        self.cases.append({"case":"options-return-to-general","passed":True})

    async def options_layout(self, require_scroll=None):
        await self.options_redesign_layout()
        native = await self.query("options.get_state")
        box = native["boxes"][native["selection_index"]]
        targets = (await self.query("vr.targets"))["targets"]
        scroll = [t for t in targets if t["name"].startswith("settings.scroll.")]
        if box["scroll"]["scrollable"]:
            assert scroll and all(t["rect"][2]-t["rect"][0]>=160 and t["rect"][3]-t["rect"][1]>=72 for t in scroll), "Up/down buttons need spacious ray targets"
        else:
            assert not scroll, "A category that fits should not offer paging"

    async def options_interactions(self):
        await self.click("settings.category.0")
        state = (await self.query("options.get_state"))["boxes"][0]
        base = [r["value"] for r in state["rows"]]
        await self.click("box0.row0",(.1,.5))
        assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][0]["rows"]]==base, "A label click edited the value"
        direction = "next" if base[0]<state["rows"][0]["max"] else "prev"
        await self.click("box0.row0."+direction)
        changed = (await self.query("options.get_state"))["boxes"][0]["rows"][0]["value"]
        assert changed!=base[0]
        await self.click("box0.row0."+("prev" if direction=="next" else "next"))
        await self.click("box0.row4.toggle")
        assert (await self.query("options.get_state"))["boxes"][0]["rows"][4]["value"]!=base[4]
        await self.click("box0.row4.toggle")
        assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][0]["rows"]]==base
        self.cases.append({"case":"options-choice-toggle-and-inert-label","passed":True})
        await self.focus_audio()
        state = (await self.query("options.get_state"))["boxes"][3]
        baseline = [r["value"] for r in state["rows"]]
        direction = "prev" if baseline[0]>state["rows"][0]["min"] else "next"
        await self.click("box3.row0."+direction)
        edited = (await self.query("options.get_state"))["boxes"][3]
        assert abs(edited["rows"][0]["value"]-baseline[0])==state["rows"][0]["step"]
        assert edited["dirty"], "Changed volume has no pending-save indication"
        await self.click("box3.row0."+("next" if direction=="prev" else "prev"))
        await self.click("box3.row0.bar",(.37,.5))
        sample = (await self.query("options.get_state"))["boxes"][3]["rows"][0]
        assert abs(sample["value"]-37)<=sample["step"], ("Ray missed the selected slider position",sample)
        # Reset Cancel must leave the edited values alone; Yes must perform the reset.
        reset = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Secondary."))
        await self.click(reset)
        assert (await self.query("ui.get_message_box"))["open"]
        assert {t["name"] for t in (await self.query("vr.targets"))["targets"]}=={"msgbox.validate","msgbox.cancel"}, "Reset dialog exposes background settings"
        await self.capture("options-reset-dialog")
        await self.click("msgbox.cancel")
        assert (await self.query("options.get_state"))["boxes"][3]["rows"][0]["value"]==sample["value"]
        await self.click(reset)
        await self.click("msgbox.validate")
        assert (await self.query("options.get_state"))["boxes"][3]["rows"][0]["value"]==100
        for i,original in enumerate(baseline):
            row = (await self.query("options.get_state"))["boxes"][3]["rows"][i]
            if row["value"]==original: continue
            if row["type"]==2:
                fraction = min(.999,max(.001,(original-row["min"])/(row["max"]-row["min"])))
                await self.click(f"box3.row{i}.bar",(fraction,.5))
            else:
                assert row["type"]==1,row
                await self.click(f"box3.row{i}.toggle")
        assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][3]["rows"]]==baseline
        self.cases.append({"case":"options-slider-step-position-reset-and-restore","passed":True})
        await self.click("settings.category.4")
        binding = (await self.query("options.get_state"))["boxes"][4]
        assert not binding["dirty"], "Start binding capture with saved bindings"
        values = [r["value"] for r in binding["rows"]]
        row = next(r for r in binding["rows"] if r["visible"] and r["clickable"] and r["type"]==4)
        await self.click(f"box4.row{row['index']}.action")
        capture = (await self.query("options.get_state"))["boxes"][4]
        assert capture["capturing"], "Rebind did not start after trigger release"
        assert not capture["dirty"], "The initiating ray trigger modified the binding table"
        assert [r["value"] for r in capture["rows"]]==values, "The initiating ray trigger rebound the control"
        targets = (await self.query("vr.targets"))["targets"]
        assert targets and all(t["name"].startswith("helper.Cancel.") for t in targets), "Binding capture exposes other actions"
        await self.capture("options-binding-capture")
        await self.click(targets[0]["name"])
        after = await self.query("options.get_state")
        assert after["open"] and not after["boxes"][4]["capturing"], "Back failed to cancel capture in place"
        assert not after["boxes"][4]["dirty"], "Cancel modified the binding table"
        assert [r["value"] for r in after["boxes"][4]["rows"]]==values, "Cancel's ray trigger rebound the control"
        self.cases.append({"case":"options-rebind-release-cancel-without-editing","passed":True})
        await self.click("settings.category.0")

    async def focus_audio(self):
        native = await self.query("options.get_state")
        assert native["open"],native
        if native["selection_index"]!=3 or not native["boxes"][3]["focused"]:
            await self.click("settings.category.3")
        assert (await self.query("options.get_state"))["boxes"][3]["focused"]

    async def options_devices(self):
        """Rebuild the focused list for each available peripheral, then restore it."""
        await self.click("settings.category.4")
        initial = await self.query("options.get_state")
        device = initial["boxes"][4]["rows"][0]
        assert device["type"]==3 and device["clickable"], device
        original = device["value"]
        baseline = [r["value"] for r in initial["boxes"][5]["rows"]]

        async def choose(value):
            await self.click("settings.category.4")
            while True:
                current = (await self.query("options.get_state"))["boxes"][4]["rows"][0]["value"]
                if current==value: break
                await self.click("box4.row0."+("next" if current<value else "prev"))
                after = (await self.query("options.get_state"))["boxes"][4]["rows"][0]["value"]
                assert abs(after-value)<abs(current-value), "Peripheral selector did not advance"
            await self.click("settings.category.5")

        for value in range(device["min"],device["max"]+1):
            await choose(value)
            box = (await self.query("options.get_state"))["boxes"][5]
            present = [r["index"] for r in box["rows"] if r["present"]]
            values = [r["value"] for r in box["rows"]]
            assert present, "Peripheral has no input settings"
            while (await self.query("options.get_state"))["boxes"][5]["scroll"]["first"]:
                await self.click("settings.scroll.up")
            seen = set()
            for _ in range(len(present)+1):
                box = (await self.query("options.get_state"))["boxes"][5]
                visible = [r for r in box["rows"] if r["visible"]]
                first,last = box["scroll"]["first"],box["scroll"]["last"]
                assert [r["index"] for r in visible]==present[first:last+1]
                assert all(r["label"].strip() for r in visible), "Peripheral rebuild left blank labels"
                await self.options_redesign_layout()
                for row in visible:
                    await self.hover(f"box5.row{row['index']}",(.1,.5))
                seen.update(r["index"] for r in visible)
                if last+1==len(present): break
                await self.click("settings.scroll.down")
            else: raise AssertionError("Peripheral settings never reached the final page")
            assert seen==set(present)
            assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][5]["rows"]]==values
            self.cases.append({"case":"options-peripheral-"+str(value),"passed":True,"seen_rows":sorted(seen)})
        await choose(original)
        assert [r["value"] for r in (await self.query("options.get_state"))["boxes"][5]["rows"]]==baseline, "Peripheral settings changed during the rebuild sweep"
        assert not (await self.query("options.get_state"))["boxes"][4]["dirty"]
        await self.click("settings.category.0")
        self.cases.append({"case":"options-original-peripheral-restored","passed":True,"device":original})

    async def dual_hand_options(self):
        await self.focus_audio()
        baseline = [r["value"] for r in (await self.query("options.get_state"))["boxes"][3]["rows"]]

        async def hold(row, fraction, hand, label):
            _, args = await self.point_args("box3.row"+str(row)+".bar", (fraction, .5), hand)
            result = await self.tool("hold_ui_at", args, label)
            assert result["status"] == "input_delivered", result

        async def release(hand):
            result = await self.tool("release_ui_pointer", {"capture_id": self.last_capture["capture_id"], "hand": hand}, "dual-release-"+hand)
            assert result["status"] == "input_delivered", result

        # A deliberate grab on a separate slider must not be forgiven back to
        # the inert row label that was hovered immediately before it.
        _, panel = await self.point_args("box3.row0", (.2, .5), "left")
        assert (await self.tool("hover_ui_at", panel, "panel-before-direct-grab"))["status"] == "input_delivered"
        assert (await self.query("vr.targets"))["hovered"] == "box3.row0"
        await hold(0, .3, "left", "direct-slider-grab-from-panel")
        direct = (await self.query("options.get_state"))["boxes"][3]["rows"][0]["value"]
        assert direct == 30, ("The previous row label consumed the slider grab", direct)
        self.cases.append({"case":"direct-grab-separate-slider","passed":True,"value":direct})
        await release("left")
        _, right = await self.point_args("box3.row1.bar", (.9, .5), "right")
        assert (await self.tool("hover_ui_at", right, "right-music-before-grab"))["status"] == "input_delivered"
        assert (await self.query("vr.targets"))["hovered"] == "box3.row1.bar", "Right ray does not reach Music"
        await release("right")
        # Acquire the target before pressing, as in the screenshot -> hover ->
        # press workflow. Subsequent hold commands move the existing drag.
        _, left = await self.point_args("box3.row0.bar", (.3, .5), "left")
        assert (await self.tool("hover_ui_at", left, "left-master-before-grab"))["status"] == "input_delivered"
        assert (await self.query("vr.targets"))["hovered"] == "box3.row0.bar"
        await hold(0, .3, "left", "left-grab-master")
        first = await self.query("options.get_state")
        assert first["boxes"][3]["rows"][0]["value"] == 30, first
        clicks = (await self.query("vr.targets"))["clicks"]
        _, other = await self.point_args("box3.row1.bar", (.9, .5), "right")
        assert (await self.tool("hover_ui_at", other, "right-hover-during-left-drag"))["status"] == "input_delivered"
        await hold(0, .7, "left", "left-move-master")
        capture = await self.tool("capture_screenshot", {"eye": "both", "max_width": 2048}, "both-reticles-in-both-eyes")
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
        native = await self.query("vr.targets")
        targets = native["targets"]
        observations = []
        for hand, row, fraction in (("left", 0, .7), ("right", 1, .9)):
            l,t,r,b = next(x["rect"] for x in targets if x["name"] == "box3.row"+str(row)+".bar")
            hx,hy = l+(r-l)*fraction, (t+b)/2
            for eye in ("left", "right"):
                if capture["geometry"]["surfaces"]:
                    point = quad_pixel(capture, (hx,hy,hx,hy), eye)
                else:
                    frame = native["vr_frame"]
                    panel = frame["panel"]
                    world = (np.array(panel["origin"])+np.array(panel["right"])*(hx/1920-.5)*frame["panel_width"]
                             +np.array(panel["up"])*(.5-hy/1080)*frame["panel_height"])
                    point = world_pixel({"vr":{"frame":frame}}, capture, world, eye)
                x,y = np.round(point).astype(int)
                patch = pixels[max(0,y-7):y+8,max(0,x-7):x+8].astype(float)
                red,green,blue = patch[:,:,0],patch[:,:,1],patch[:,:,2]
                color = (blue > red+25)&(green > red+25) if hand == "left" else (red > blue+50)&(green > blue+25)&(red > 140)
                # The world-space dot turns white while grabbed; the quad's
                # independent HUD reticle retains its hand color.
                if hand == "left" and not capture["geometry"]["surfaces"]:
                    color = (np.min(patch,axis=2)>180)&(np.max(patch,axis=2)-np.min(patch,axis=2)<30)
                count = int(np.sum(color))
                observations.append({"hand":hand,"eye":eye,"pixel":[int(x),int(y)],"colored_pixels":count})
                assert count > 0, ("Reticle missing from eye image", observations[-1])
        await hold(1, .9, "right", "right-press-during-left-drag")
        blocked = await self.query("options.get_state")
        assert blocked["boxes"][3]["rows"][0]["value"] == 70, blocked
        assert blocked["boxes"][3]["rows"][1]["value"] == baseline[1], "Second hand stole the active drag"
        assert (await self.query("vr.targets"))["clicks"] == clicks, "Second trigger activated during an owned drag"
        # Releasing the owner must not turn the other hand's already-held trigger into a new click.
        await release("left")
        released = await self.query("options.get_state")
        assert released["boxes"][3]["rows"][1]["value"] == baseline[1], "Held spare trigger activated after handoff"
        assert (await self.query("vr.targets"))["hovered"] == "box3.row1.bar", "Ownership did not transfer to the spare ray"
        await release("right")
        _, left = await self.point_args("box3.row0.bar", (min(.999, baseline[0]/100), .5), "left")
        assert (await self.tool("hover_ui_at", left, "left-master-before-restore"))["status"] == "input_delivered"
        await hold(0, baseline[0]/100, "left", "restore-master-volume")
        await release("left")
        restored = await self.query("options.get_state")
        assert [r["value"] for r in restored["boxes"][3]["rows"]] == baseline, "Audio values were not restored"
        self.cases.append({"case": "dual-hand-slider-ownership", "passed": True,
                           "baseline": baseline, "during_drag": blocked, "after_release": released,
                           "reticles_in_both_eyes": observations, "restored": True})
        await self.capture("dual-hand-slider-result")
        print("Left drag, spare right ray/trigger, release handoff, restore PASS", flush=True)

    async def party_stunts_header(self):
        targets = await self.query("vr.targets")
        state = await self.query("party.get_state")
        first,last = state["visible"]
        capture = await self.tool("capture_screenshot", {"eye":"left","max_width":2048}, "party-stunts-page-count")
        rect = (1320,68,1580,116)
        frame = {"vr":{"frame":targets.get("vr_frame")}}
        projected = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"] else screenshot_pixel(frame,capture,x,y)
                     for x,y in ((rect[0],rect[1]),(rect[2],rect[3]))]
        (x0,y0),(x1,y1) = projected
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        patch = pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        bright = (np.min(patch,axis=2)>180)&(np.max(patch,axis=2)-np.min(patch,axis=2)<35)
        count = int(np.sum(bright))
        self.cases.append({"case":"party-stunt-page-count-readable","passed":count>=20,"visible":[first,last],"bright_letter_pixels":count})
        assert count >= 20, ("Page count needs bright lettering against a dark backing",count)

    async def party_stunt_endpoint(self, direction):
        before=await self.query("party.get_state")
        native=await self.query("vr.targets")
        assert "stunts.page."+direction not in [t["name"] for t in native["targets"]],native
        capture=await self.capture("party-disabled-page-"+direction)
        x=1660 if direction=="previous" else 1780
        corners=[quad_pixel(capture,(a,b,a,b)) for a,b in ((x-32,64),(x+32,120))]
        (x0,y0),(x1,y1)=corners
        pixels=np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        patch=pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        grey=(np.min(patch,axis=2)>58)&(np.max(patch,axis=2)<155)&(np.ptp(patch,axis=2)<28)
        assert int(np.sum(grey))>=8,("Unavailable Party page needs a grey arrow",direction,int(np.sum(grey)))
        px,py=quad_pixel(capture,(x,92,x,92))
        carrier=max((s for s in capture["geometry"]["surfaces"] if s["visibility"]==0),key=lambda s:s["size"][0]*s["size"][1])
        aim={"capture_id":capture["capture_id"],"x":float(px),"y":float(py),"surface_id":carrier["id"],
             "hand":self.args.hand,"origin_mode":"hand","dwell_frames":30,"ray_offset":RAY_OFFSET}
        await self.tool("hover_ui_at",aim,"party-disabled-page-hover-"+direction)
        args={**aim,"command_id":uuid.uuid4().hex,"hold_frames":45}
        receipt=await self.tool("click_ui_at",args,"party-disabled-page-held-"+direction)
        retry=await self.tool("click_ui_at",args,"party-disabled-page-retry-"+direction)
        assert retry["delivery"]["sync_id"]==receipt["delivery"]["sync_id"]
        assert (await self.query("vr.targets"))["clicks"]==native["clicks"],"Disabled Party page accepted a press"
        assert await self.query("party.get_state")==before,"Disabled Party page changed the playlist or visible cards"
        self.cases.append({"case":"party-disabled-"+direction+"-grey-and-inert","passed":True,"grey_pixels":int(np.sum(grey)),"state":before})

    async def party_stunts(self):
        targets = (await self.query("vr.targets"))["targets"]
        assert any(t["name"] == "stunts.page.next" for t in targets), "Party stunts need a ray-clickable Next page control"
        await self.party_stunts_header()
        state = await self.query("party.get_state")
        assert state["stunt_picker_open"] and state["visible"][0] == 0, state
        await self.party_stunt_endpoint("previous")
        if state["playlist"]:
            clear = next(t["name"] for t in targets if t["name"].startswith("helper.Y."))
            await self.click(clear)
        await self.audit("party-stunts-first-page-layout")
        start = next(t["name"] for t in targets if t["name"].startswith("helper.Validate."))
        await self.click(start)
        assert (await self.query("ui.get_message_box"))["open"], "Empty Start needs a clear selection prompt"
        await self.popup_spacing()
        await self.popup_anchor()
        assert all(t["name"].startswith("msgbox.") for t in (await self.query("vr.targets"))["targets"]),"Empty playlist dialog exposes menu actions"
        await self.click("msgbox.validate")
        selected = []
        for page in range(3):
            targets = (await self.query("vr.targets"))["targets"]
            cards = {t["name"]:t["rect"] for t in targets if t["name"].startswith("stunt.")}
            expected = ["stunt."+str(i) for i in range(page*4,page*4+4)]
            assert set(cards) == set(expected), (page,cards)
            await self.audit("party-stunts-page-"+str(page+1))
            for name in expected:
                await self.hover(name)
                assert [p["index"] for p in (await self.query("party.get_state"))["playlist"]] == selected
                await self.click(name)
                selected.append(int(name.split(".")[1]))
                state = await self.query("party.get_state")
                assert [p["index"] for p in state["playlist"]] == selected, state
                after = {t["name"]:t["rect"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("stunt.")}
                assert after == cards, ("Selecting a card moved the visible page",cards,after)
            await self.capture("party-stunts-selected-page-"+str(page+1))
            if page < 2:
                await self.click("stunts.page.next")
                assert (await self.query("party.get_state"))["visible"][0] == (page+1)*4
        assert not any(t["name"] == "stunts.page.next" for t in (await self.query("vr.targets"))["targets"])
        await self.party_stunt_endpoint("next")
        clear = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Y."))
        await self.click(clear)
        assert not (await self.query("party.get_state"))["playlist"]
        for expected_first in (4,0):
            await self.click("stunts.page.previous")
            assert (await self.query("party.get_state"))["visible"][0] == expected_first
        assert not any(t["name"] == "stunts.page.previous" for t in (await self.query("vr.targets"))["targets"])
        await self.click("stunt.1")
        await self.click("stunt.1")
        assert not (await self.query("party.get_state"))["playlist"], "Clicking a selected stunt did not remove it"
        await self.click("stunt.0")
        await self.click("stunt.3")
        secondary = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Secondary."))
        await self.click(secondary)
        assert [p["index"] for p in (await self.query("party.get_state"))["playlist"]] == [0]
        await self.click(secondary)
        assert [p["index"] for p in (await self.query("party.get_state"))["playlist"]] == [0,3]
        start = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Validate."))
        await self.click(start)
        menu = await self.query("menu.get_state")
        assert menu["current_menu"] == "DRIVERS", menu
        state = await self.query("party.get_state")
        assert [p["index"] for p in state["playlist"]] == [3], state
        self.cases.append({"case":"all-twelve-stunts-pages-toggle-clear-and-start","passed":True,"selected_in_order":selected,"state_after_start":state})
        await self.capture("party-stunts-driver-selection")

    async def party(self):
        targets = (await self.query("vr.targets"))["targets"]
        await self.capture("party-player-next-step-caption")
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":24}))["nodes"]
        captions=[n["text"] for n in nodes if n["visible"] and n["path"].endswith("/RayActionLabel")]
        named=any(c.lower()=="select stunts" for c in captions) and "Confirm" not in captions
        self.cases.append({"case":"party-action-names-next-step","passed":named,"captions":captions})
        assert named,("Player selection should name its next step",captions)
        names = {t["name"] for t in targets}
        required = {"players."+str(n) for n in range(2,9)}
        self.cases.append({"case":"all-player-counts-directly-available","passed":required<=names,
                           "missing":sorted(required-names)})
        assert required <= names, "All seven player counts need direct ray buttons, without carousel paging"
        await self.audit("party-player-choice-layout")
        for n in range(2,9):
            rect = next(t["rect"] for t in targets if t["name"] == "players."+str(n))
            assert rect[2]-rect[0] >= 400 and rect[3]-rect[1] >= 64, (n,rect)
            await self.hover("players."+str(n))
            assert (await self.query("menu.get_state"))["selection_index"] == n-2
        await self.capture("party-all-counts-visible")
        await self.click("players.8")
        state = await self.query("party.get_state")
        assert not state["open"] and state["players"] == 8, state
        assert (await self.query("menu.get_state"))["is_open"]
        self.cases.append({"case":"eight-player-choice-committed-once","passed":True,"state":state})
        await self.capture("party-stunt-menu-open")

    async def statistics_header(self):
        native=await self.query("vr.targets")
        assert (await self.query("statistics.get_state"))["open"],"Start in Statistics"
        capture=await self.tool("capture_screenshot",{"eye":"left","max_width":2048},"statistics-page-count")
        rect=(1300,68,1580,140)
        frame={"vr":{"frame":native.get("vr_frame")}}
        corners=[quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"] else screenshot_pixel(frame,capture,x,y)
                 for x,y in ((rect[0],rect[1]),(rect[2],rect[3]))]
        (x0,y0),(x1,y1)=corners
        pixels=np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        patch=pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        bright=(np.min(patch,axis=2)>180)&(np.max(patch,axis=2)-np.min(patch,axis=2)<35)
        dark=np.max(patch,axis=2)<100
        count=int(np.sum(bright));backing=float(np.mean(dark))
        self.cases.append({"case":"statistics-page-count-readable-on-bounded-backing","passed":count>=20 and backing>=.6,
                           "bright_letter_pixels":count,"dark_backing_fraction":backing})
        assert count>=20 and backing>=.6,("Statistics page count needs clear white lettering and a stable dark backing",count,backing)

    async def statistics(self):
        targets = (await self.query("vr.targets"))["targets"]
        has_page_down = any(t["name"] == "stats.scroll.down" for t in targets)
        self.cases.append({"case":"statistics-page-controls-present","passed":has_page_down})
        assert has_page_down, "Statistics needs a dedicated ray-clickable page-down control"
        initial = await self.query("statistics.get_state")
        assert initial["open"] and initial["first"] == 0, initial
        assert not any(t["name"] == "stats.scroll.up" for t in targets), "Page up must be disabled at the beginning"
        page_rect = next(t["rect"] for t in targets if t["name"] == "stats.scroll.down")
        l,t,r,b = page_rect
        footer_top=min(target["rect"][1] for target in targets if target["name"].startswith("helper."))
        assert r-l>=80 and b-t>=72 and b<=footer_top-20,("Statistics paging needs a large button clear of the action dock",page_rect,footer_top)
        inactive = "left" if self.args.hand == "right" else "right"
        _, args = await self.point_args("stats.scroll.down", ((100-l)/(r-l),(1040-t)/(b-t)), inactive)
        await self.tool("hover_ui_at", args, "statistics-offhand-rest")
        capture = await self.capture("statistics-page-arrow-art")
        frame = (await self.query("vr.targets")).get("vr_frame")
        state = {"vr":{"frame":frame}}
        corners = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"]
                   else screenshot_pixel(state,capture,x,y) for x,y in ((l,t),(r,b))]
        (x0,y0),(x1,y1) = corners
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        patch = pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        orange = (patch[:,:,0]>140)&(patch[:,:,1]>80)&(patch[:,:,2]<80)&(patch[:,:,0]>patch[:,:,2]+70)
        arrow_pixels = int(np.sum(orange))
        self.cases.append({"case":"statistics-page-arrow-visible","passed":arrow_pixels>=8,"colored_pixels":arrow_pixels})
        assert arrow_pixels >= 8, "Page down has no visible amber arrow"
        # Parking is only needed for the artwork capture. Release it before the
        # long sweep: its ten-second lease would otherwise expire mid-hover and
        # move the spare controller back to its normal pose, transferring focus.
        await self.tool("release_ui_pointer", {"capture_id":capture["capture_id"],"hand":inactive}, "statistics-offhand-park-release")
        await self.audit("statistics-layout-top")
        positions = [initial]
        for _ in range(initial["total"]//max(1,initial["visible"]-1)+3):
            current = await self.query("statistics.get_state")
            if current["last"] == current["total"]-1:
                break
            await self.click("stats.scroll.down")
            after = await self.query("statistics.get_state")
            assert after["first"] > current["first"], (current, after)
            positions.append(after)
        bottom = await self.query("statistics.get_state")
        assert bottom["last"] == bottom["total"]-1, bottom
        assert not any(t["name"] == "stats.scroll.down" for t in (await self.query("vr.targets"))["targets"])
        await self.audit("statistics-layout-bottom")
        rows = [t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("line.")]
        for row in rows:
            await self.hover(row)
            assert (await self.query("statistics.get_state"))["first"] == bottom["first"], "Hovering a visible row moved the list"
        self.cases.append({"case":"statistics-visible-row-hover-keeps-page","passed":True,"rows":rows})
        for _ in range(initial["total"]//max(1,initial["visible"]-1)+3):
            current = await self.query("statistics.get_state")
            if current["first"] == 0:
                break
            await self.click("stats.scroll.up")
            after = await self.query("statistics.get_state")
            assert after["first"] < current["first"], (current, after)
        assert (await self.query("statistics.get_state"))["first"] == 0
        targets = (await self.query("vr.targets"))["targets"]
        next_category = next(t["name"] for t in targets if t["name"].startswith("helper.RB."))
        await self.click(next_category)
        category = await self.query("statistics.get_state")
        assert category["first"] > 0, category
        previous = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.LB."))
        await self.click(previous)
        assert (await self.query("statistics.get_state"))["first"] == 0
        self.cases.append({"case":"statistics-scroll-both-ends-and-categories","passed":True,"page_positions":positions,"bottom":bottom})
        await self.capture("statistics-scroll-result")

    async def footer_contrast(self, prefix="helper."):
        """Check letters, excluding the bright icons, while neither ray hovers an action."""
        targets = (await self.query("vr.targets"))["targets"]
        helpers = [t for t in targets if t["name"].startswith(prefix)]
        assert helpers
        if all(t["rect"][3]-t["rect"][1]>=72 for t in helpers):
            # Primary ray buttons intentionally use dark wording on amber;
            # secondary buttons use light wording on dark plates.
            await self.ray_buttons()
            return
        first = helpers[0]["rect"]
        fraction = ((100-first[0])/(first[2]-first[0]), (1040-first[1])/(first[3]-first[1]))
        for hand in ("left", "right"):
            _, args = await self.point_args(helpers[0]["name"], fraction, hand)
            await self.tool("hover_ui_at", args, "move-off-footer-"+hand)
        assert not (await self.query("vr.targets"))["hovered"]
        capture = await self.tool("capture_screenshot", {"eye":"left", "max_width":2048}, "footer-idle-labels")
        state = None
        if not capture["geometry"]["surfaces"]:
            native = await self.query("vr.targets")
            state = {"vr":{"frame":native["vr_frame"]}}
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
        observations = []
        for target in helpers:
            l,t,r,b = target["rect"]
            # An icon is one unpadded row height wide; the wording starts after it.
            rect = (l+(b-t)+8, t+8, r-4, b-15)
            corners = [(rect[0],rect[1]),(rect[2],rect[3])]
            projected = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"]
                         else screenshot_pixel(state,capture,x,y) for x,y in corners]
            (x0,y0),(x1,y1) = projected
            patch = pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),
                           math.floor(min(x0,x1)):math.ceil(max(x0,x1))].astype(float)
            bright = (np.min(patch,axis=2)>180)&(np.max(patch,axis=2)-np.min(patch,axis=2)<45)
            observations.append({"target":target["name"],"text_rect":rect,
                                 "bright_letter_pixels":int(np.sum(bright))})
        passed = all(row["bright_letter_pixels"] >= 8 for row in observations)
        self.cases.append({"case":"idle-footer-label-contrast","passed":passed,"labels":observations})
        assert passed, observations
        print("Idle footer wording stays bright without a yellow bar PASS", flush=True)

    async def results_retry(self):
        initial = await self.query("race.end_screen")
        assert initial["can_retry"], initial
        game_before = await self.query("game.get_state")
        targets = (await self.query("vr.targets"))["targets"]
        name = next(t["name"] for t in targets if ".Secondary." in t["name"])
        await self.click(name)
        assert (await self.query("ui.get_message_box"))["open"], "Try Again did not open its restart confirmation"
        await self.audit("results-retry-confirmation-layout")
        _,hovered,args = await self.hover("msgbox.validate")
        args.update(command_id=uuid.uuid4().hex, hold_frames=45, timeout=30)
        clicked = await self.tool("click_ui_at", args, "results-retry")
        assert clicked["status"] == "input_delivered", clicked
        repeated = await self.tool("click_ui_at", args, "results-retry-idempotent")
        assert repeated["status"] == "input_delivered", repeated
        assert (await self.query("vr.targets"))["clicks"] == hovered["clicks"]+1
        # The receipt must retain the first application frame when the command is retried across loading.
        assert repeated["delivery"]["applied_frame"] == clicked["delivery"]["applied_frame"]
        for _ in range(60):
            current = await self.query("game.get_state")
            if not current["is_loading"] and not current["is_main_menu"]:
                after = await self.query("race.end_screen")
                if after["screen"] == 0:
                    break
            await asyncio.sleep(.5)
        else:
            raise AssertionError(("Try Again did not return to racing",current))
        assert current["current_level"] == game_before["current_level"], (game_before,current)
        if current["is_menu_active"]:
            targets = (await self.query("vr.targets"))["targets"]
            start = next(t["name"] for t in targets if t["name"].startswith("endhelper.Validate."))
            await self.click(start)
            current = await self.query("game.get_state")
        assert not current["is_menu_active"], current
        after = await self.query("race.end_screen")
        assert after["screen"] == 0 and not after["can_retry"], after
        self.cases.append({"case":"try-again-reloads-same-race-and-retry-does-not-redeliver","passed":True,
                           "before":initial,"after":after,"game_after":current,"applied_frame":clicked["delivery"]["applied_frame"]})
        await self.capture("results-retry-race-running")

    async def leaderboard_cancel(self):
        assert (await self.query("ui.get_message_box"))["open"], "Open the leaderboard loading dialog first"
        names = [t["name"] for t in (await self.query("vr.targets"))["targets"]]
        self.cases.append({"case":"leaderboard-loading-has-cancel","passed":"msgbox.cancel" in names,"targets":names})
        assert "msgbox.cancel" in names, "Loading leaderboards needs a ray-clickable Cancel action"
        before = await self.query("leaderboard.get_state")
        assert before["busy"] and not before["writing"], before
        await self.audit("leaderboard-loading-dialog-layout")
        await self.click("msgbox.cancel")
        after = await self.query("leaderboard.get_state")
        assert not after["busy"] and not after["reading"] and not after["writing"], after
        assert not (await self.query("ui.get_message_box"))["open"], "Cancelling the read left a popup open"
        targets = (await self.query("vr.targets"))["targets"]
        proceed = next(t["name"] for t in targets if t["name"].startswith("helper.Validate."))
        await self.hover(proceed)
        self.cases.append({"case":"leaderboard-read-cancelled-and-menu-usable","passed":True,"before":before,"after":after})
        await self.capture("leaderboard-loading-cancelled")

    async def leaderboard_empty(self):
        targets = (await self.query("vr.targets"))["targets"]
        names = [t["name"] for t in targets]
        assert not any(n.startswith("row.") for n in names), "Use an empty leaderboard for this check"
        invalid = [n for n in names if n.startswith("page.") or n.startswith("helper.Y.")]
        self.cases.append({"case":"empty-leaderboard-has-no-inactive-targets","passed":not invalid,"invalid_targets":invalid})
        nodes = (await self.query("hud.rects", {"root":"menu","max_depth":4,"all":True}))["nodes"]
        label = next((n for n in nodes if n["path"].endswith("/TXT_NoEntries") and n["visible"]), None)
        self.cases.append({"case":"empty-leaderboard-message-has-area","passed":label is not None,"label":label})
        assert label, "The empty-leaderboard message has no rendered area"
        assert not invalid, ("Empty leaderboard exposes unavailable page/profile actions",invalid)
        l,t,r,b = label["rect"]
        assert r-l > 500 and b-t > 50, label
        # HudNode dotted lookup selects the newest menu instance; slash lookup
        # chooses an older Pause Menu when both roots share the name Menu.
        name = label["path"].split("/",1)[1].replace("/", ".")
        native = await self.query("hud.get_node", {"root":"menu","name":name})
        assert native["text"].strip(), native
        capture = await self.tool("capture_screenshot", {"eye":"left","max_width":2048}, "leaderboard-empty-message")
        native_frame = (await self.query("vr.targets")).get("vr_frame")
        state = {"vr":{"frame":native_frame}} if native_frame else None
        (x0,y0),(x1,y1) = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"] else screenshot_pixel(state,capture,x,y) for x,y in ((l,t),(r,b))]
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        patch = pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        bright = (np.min(patch,axis=2)>200)&(np.max(patch,axis=2)-np.min(patch,axis=2)<35)
        count = int(np.sum(bright))
        assert count >= 100, ("Empty-leaderboard wording is not visibly rendered",count)
        await self.audit("empty-leaderboard-layout")
        self.cases.append({"case":"empty-leaderboard-message-visible","passed":True,"text":native["text"],"bright_pixels":count,"label":label})

    async def end_results(self):
        """From race results, verify footer artwork, wording, and one-page advancement."""
        initial = await self.query("race.end_screen")
        assert initial["screen"] == 1 and initial["can_continue"], initial
        await self.audit("race-results-layout")
        capture = await self.capture("race-results-footer")
        assert capture["geometry"]["surfaces"], "Race results must be on the submitted XR quad"
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        samples = []
        for y in np.linspace(996, 1028, 5):
            for x in np.linspace(80, 1100, 60):
                px, py = quad_pixel(capture, (x,y,x,y))
                px, py = round(px), round(py)
                assert 0 <= px < pixels.shape[1] and 0 <= py < pixels.shape[0], (px,py)
                samples.append(pixels[py,px])
        rgb = np.array(samples)
        orange = (rgb[:,0]>210)&(rgb[:,1]>110)&(rgb[:,1]<205)&(rgb[:,2]<55)
        fraction = float(np.mean(orange))
        self.cases.append({"case":"race-results-no-yellow-footer","passed":fraction<.1,
                           "orange_fraction":fraction,"sample_count":len(samples)})
        assert fraction < .1, ("Results still have a yellow footer strip",fraction)
        await self.footer_contrast("endhelper.")
        targets = (await self.query("vr.targets"))["targets"]
        for target in targets:
            await self.hover(target["name"])
        proceed = next(t["name"] for t in targets if t["name"].startswith("endhelper.Validate."))
        await self.click(proceed)
        after = await self.query("race.end_screen")
        assert after["screen"] == 2, ("A held trigger skipped or failed to advance the result page",initial,after)
        self.cases.append({"case":"race-results-advance-exactly-one-page","passed":True,"before":initial,"after":after})
        await self.capture("race-results-best-pilot")

    async def return_main(self):
        assert not (await self.query("game.get_state"))["is_main_menu"], "Start in a race"
        if (await self.query("options.get_state"))["open"]:
            targets = (await self.query("vr.targets"))["targets"]
            cancel = next(t["name"] for t in targets if t["name"].startswith("helper.Cancel."))
            await self.click(cancel)
            assert not (await self.query("ui.get_message_box"))["open"], "Unexpected pending settings changes"
        assert not (await self.query("options.get_state"))["open"], "One Back click did not leave Options"
        if not (await self.query("hud.pause_menu"))["is_open"]:
            await self.menu_button()
        pause=await self.query("hud.pause_menu")
        exits=[i for i,label in enumerate(pause["items"]) if label.lower()=="main menu"]
        assert len(exits)==1,("Expected one Main Menu action in Pause",pause)
        await self.click("button."+str(exits[0]))
        assert (await self.query("ui.get_message_box"))["open"]
        await self.click("msgbox.validate")
        for _ in range(120):
            state = await self.query("game.get_state")
            if state["is_main_menu"] and state["main_menu_ready"] and not state["is_loading"]:
                if (await self.query("menu.get_state"))["current_menu"]==self.args.expected_menu:
                    break
            await asyncio.sleep(.5)
        else:
            raise AssertionError(("Main Menu did not finish loading",state))
        assert (await self.query("menu.get_state"))["current_menu"]==self.args.expected_menu
        await self.capture("return-main-verified")
        self.cases.append({"case":"options-back-and-confirm-frontend-return","passed":True,"destination":self.args.expected_menu})

    async def spatial_hover(self):
        from flatout_livery_e2e import world_pixel
        assert not (await self.query("ui.get_message_box"))["open"], "Dismiss the startup/modal dialog before testing spatial labels"
        menu = await self.query("menu.get_state")
        assert menu["is_3d"] and menu.get("spatial_layout"), menu
        assert not (await self.query("vr.targets"))["targets"], "Spatial menus advertise hidden canvas helper buttons"
        for item in menu["items"]:
            if item["index"] in self.args.skip_menu_index:
                continue
            current = await self.query("menu.get_state")
            item = current["items"][item["index"]]
            native = await self.query("vr.targets")
            capture = await self.capture("spatial-source-"+str(item["index"]))
            frame = {**native["vr_frame"],"panel":{**current["spatial_layout"],"origin":item["rendered_position"]},
                     "panel_width":1.0,"panel_height":1.0}
            state = {"vr":{"frame":frame}}
            x,y = world_pixel(state,capture,item["rendered_position"],"left")
            width,height = capture["image_size"]
            assert 0<=x<width and 0<=y<height, ("Spatial label is offscreen",item,[x,y])
            aim = {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),
                   "hand":self.args.hand,"origin_mode":"hand","surface":ui_surface(state),
                   "ray_offset":RAY_OFFSET,"dwell_frames":30}
            result = await self.tool("hover_ui_at",aim,"spatial-hover-"+str(item["index"]))
            assert result["status"]=="input_delivered"
            assert not (await self.query("ui.get_message_box"))["open"], "A modal dialog blocked the spatial labels"
            after = await self.query("menu.get_state")
            passed = after["current_menu"]==current["current_menu"] and after["selection_index"]==item["index"]
            self.cases.append({"case":"spatial-label-hover-"+str(item["index"]),"passed":passed,
                               "title":item["title"],"pixel":[float(x),float(y)],"item":item,"after":after})
            assert passed, ("Ray did not select the rendered spatial label",item,after)
            if self.args.require_physical_lift:
                observed = after["items"][item["index"]]
                rest = np.array(observed["rest_position"])
                moved = np.array(observed["rendered_position"])
                normal = np.array(after["spatial_layout"]["back"])
                normal /= np.linalg.norm(normal)
                camera = native["vr_frame"]["camera"]
                basis = np.column_stack([camera[k] for k in ("right", "up", "back")])
                eyes = np.array(camera["origin"]) + basis @ np.array(capture["geometry"]["head"]["position"])
                if np.dot(normal, eyes - rest) < 0:
                    normal = -normal
                lift = float(np.dot(moved - rest, normal))
                expected = 0 if observed.get("locked") else .015
                assert abs(lift - expected) < .001, ("Incorrect physical spatial lift", observed["title"], lift)
                assert np.linalg.norm(moved - rest - normal * lift) < .001, "Spatial hover slid sideways"
                self.cases.append({"case": "spatial-metric-lift", "passed": True, "title": observed["title"], "meters": lift})
        if self.args.spatial_click_index is not None:
            index = self.args.spatial_click_index
            current = await self.query("menu.get_state")
            native = await self.query("vr.targets")
            capture = await self.capture("spatial-click-source")
            item = current["items"][index]
            frame = {**native["vr_frame"],"panel":{**current["spatial_layout"],"origin":item["rendered_position"]},
                     "panel_width":1.0,"panel_height":1.0}
            state = {"vr":{"frame":frame}}
            x,y = world_pixel(state,capture,item["rendered_position"],"left")
            aim = {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":self.args.hand,
                   "origin_mode":"hand","surface":ui_surface(state),"ray_offset":RAY_OFFSET,"dwell_frames":30}
            await self.tool("hover_ui_at",aim,"spatial-click-hover")
            assert (await self.query("menu.get_state"))["selection_index"]==index
            args = {**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
            clicked = await self.tool("click_ui_at",args,"spatial-click")
            assert clicked["status"]=="input_delivered"
            after = await self.query("menu.get_state")
            assert after["current_menu"]==self.args.expected_menu, after
            repeated = await self.tool("click_ui_at",args,"spatial-click-retry")
            assert repeated["delivery"]["sync_id"]==clicked["delivery"]["sync_id"]
            assert (await self.query("menu.get_state"))["current_menu"]==after["current_menu"]
            self.cases.append({"case":"spatial-held-click-and-retry","passed":True,"title":item["title"],"destination":after["current_menu"]})

    async def spatial_back(self):
        menu = await self.query("menu.get_state")
        self.cases.append({"case":"spatial-back-world-geometry-exposed","passed":bool(menu.get("spatial_back")),"menu":menu})
        assert menu["is_3d"] and menu.get("spatial_back"), "Open a spatial submenu with Back"
        native = await self.query("vr.targets")
        assert not native["targets"], "Spatial Back must use its actual world-space position"
        capture = await self.capture("spatial-back-source")
        position = menu["spatial_back"]["position"]
        frame = {**native["vr_frame"],"panel":{**menu["spatial_layout"],"origin":position},
                 "panel_width":1.0,"panel_height":1.0}
        state = {"vr":{"frame":frame}}
        scale = menu["spatial_layout"]["scale"]
        half = menu["spatial_back"].get("half_extents",[4*scale,1.12*scale])
        right,up = [np.asarray(menu["spatial_layout"][axis]) for axis in ("right","up")]
        center = np.asarray(position)
        def point(dx,dy): return (center+right*dx+up*dy).tolist()
        async def aim_at(position,label,hand=None):
            source = await self.capture(label)
            local_frame = {**frame,"panel":{**frame["panel"],"origin":position}}
            local_state = {"vr":{"frame":local_frame}}
            px,py = world_pixel(local_state,source,position,"left")
            assert 0<=px<source["image_size"][0] and 0<=py<source["image_size"][1], (label,[px,py])
            return {"capture_id":source["capture_id"],"x":float(px),"y":float(py),"hand":hand or self.args.hand,
                    "origin_mode":"hand","surface":ui_surface(local_state),"ray_offset":RAY_OFFSET,"dwell_frames":30}
        # A hand left at a previous menu's world pose can legitimately hover a
        # different row here. Park it before recording the selection baseline
        # for the active hand's miss tests; later checks deliberately use both.
        spare = "left" if self.args.hand=="right" else "right"
        parked = await aim_at(point(0,-4*half[1]),"spatial-back-initial-spare-away",spare)
        await self.tool("hover_ui_at",parked,"spatial-back-initial-spare-parked")
        menu = await self.query("menu.get_state")
        capture = await self.capture("spatial-back-plate-source")
        # A real secondary button needs an opaque, dark plate around its
        # caption; a floating word over the road is not a visible hit region.
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        # Inspect padding beside the caption, so legitimate lettering and the
        # chevron cannot make an opaque button look transparent to this check.
        corners = [list(map(float,world_pixel(state,capture,point(dx*half[0],dy*half[1]),"left")))
                   for dx,dy in ((.65,-.7),(.88,.7))]
        (x0,y0),(x1,y1) = corners
        patch = pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        neutral_plate = (np.max(patch,axis=2)<65)&(np.ptp(patch,axis=2)<45)
        fill = float(np.mean(neutral_plate))
        self.cases.append({"case":"spatial-back-visible-button-plate","passed":fill>.6,"plate_fraction":fill,"corners":corners})
        assert fill>.6, ("Spatial Back needs a visible button around the hit region",fill)
        right_capture = await self.tool("capture_screenshot",{"eye":"right","max_width":1280},"spatial-back-right-eye-plate")
        right_pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        (x0,y0),(x1,y1) = [world_pixel(state,right_capture,point(dx*half[0],dy*half[1]),"right")
                           for dx,dy in ((.65,-.7),(.88,.7))]
        right_patch = right_pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        right_fill = float(np.mean((np.max(right_patch,axis=2)<65)&(np.ptp(right_patch,axis=2)<45)))
        self.cases.append({"case":"spatial-back-plate-visible-in-right-eye","passed":right_fill>.6,"plate_fraction":right_fill})
        assert right_fill>.6, ("The Back button is missing from the right eye",right_fill)
        # Outside the drawn left/right/bottom edges must not retain the old
        # invisible row-band target or activate the selected menu action.
        for label,dx,dy in (("left",-1.12,0),("right",1.12,0),("below",0,-1.12)):
            miss = await aim_at(point(dx*half[0],dy*half[1]),"spatial-back-miss-"+label)
            await self.tool("hover_ui_at",miss,"spatial-back-miss-hover-"+label)
            assert not (await self.query("menu.get_state"))["spatial_back"]["hovered"], "Back hover extends beyond its visible button"
            args = {**miss,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
            clicked = await self.tool("click_ui_at",args,"spatial-back-miss-click-"+label)
            assert clicked["status"]=="input_delivered"
            after = await self.query("menu.get_state")
            assert after["current_menu"]==menu["current_menu"] and after["selection_index"]==menu["selection_index"], ("A ray beside Back activated a menu action",label,after)
            repeated = await self.tool("click_ui_at",args,"spatial-back-miss-retry-"+label)
            assert repeated["delivery"]["sync_id"]==clicked["delivery"]["sync_id"]
            self.cases.append({"case":"spatial-back-outside-button-inert-"+label,"passed":True})
        spare = "left" if self.args.hand=="right" else "right"
        spare_aim = await aim_at(position,"spatial-back-spare-hand-source",spare)
        await self.tool("hover_ui_at",spare_aim,"spatial-back-spare-hand-hover")
        assert (await self.query("menu.get_state"))["spatial_back"]["hovered"]
        miss = await aim_at(point(1.12*half[0],0),"spatial-back-active-hand-outside")
        await self.tool("hover_ui_at",miss,"spatial-back-active-hand-outside-hover")
        args = {**miss,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
        clicked = await self.tool("click_ui_at",args,"spatial-back-other-ray-trigger")
        assert clicked["status"]=="input_delivered"
        assert (await self.query("menu.get_state"))["current_menu"]==menu["current_menu"], "An off-button trigger activated the other hand's Back target"
        repeated = await self.tool("click_ui_at",args,"spatial-back-other-ray-trigger-retry")
        assert repeated["delivery"]["sync_id"]==clicked["delivery"]["sync_id"]
        self.cases.append({"case":"spatial-back-two-rays-trigger-isolation","passed":True,"trigger_hand":self.args.hand,"ui_hand":spare})
        park = await aim_at(point(0,-4*half[1]),"spatial-back-park-spare-hand",spare)
        await self.tool("hover_ui_at",park,"spatial-back-spare-hand-away")
        capture = await self.capture("spatial-back-center-source")
        x,y = world_pixel(state,capture,position,"left")
        assert 0<=x<capture["image_size"][0] and 0<=y<capture["image_size"][1], "Spatial Back is offscreen"
        aim = {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":self.args.hand,
               "origin_mode":"hand","surface":ui_surface(state),"ray_offset":RAY_OFFSET,"dwell_frames":30}
        result = await self.tool("hover_ui_at",aim,"spatial-back-hover")
        assert result["status"]=="input_delivered"
        hovered = await self.query("menu.get_state")
        assert hovered["spatial_back"]["hovered"] and hovered["selection_index"]==menu["selection_index"], "Back hover changed the selected menu action"
        assert hovered["spatial_back"]["position"]==position and hovered["spatial_back"]["half_extents"]==half, "Back moved or grew while aiming at it"
        self.cases.append({"case":"spatial-back-keeps-fixed-hover-footprint","passed":True})
        arrow_capture = await self.tool("capture_screenshot",{"eye":"left","max_width":2048},"spatial-back-chevron")
        arrow_pixels = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        (x0,y0),(x1,y1) = [world_pixel(state,arrow_capture,point(dx*scale,dy*scale),"left")
                           for dx,dy in ((-3.35,-.72),(-2.25,.72))]
        arrow_patch = arrow_pixels[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
        ys,xs = np.nonzero((np.min(arrow_patch,axis=2)>170)&(np.ptp(arrow_patch,axis=2)<45))
        assert len(xs)>=6, "Back needs a visible chevron"
        center_y = (int(np.min(ys))+int(np.max(ys)))/2
        extent = max(1,int(np.max(ys))-int(np.min(ys)))
        middle = xs[np.abs(ys-center_y)<=max(.5,extent*.2)]
        outer = xs[np.abs(ys-center_y)>=extent*.3]
        assert len(middle) and len(outer), "Chevron image is too small to distinguish its direction"
        direction = float(np.mean(outer)-np.mean(middle))
        self.cases.append({"case":"spatial-back-chevron-points-left","passed":direction>.3,"tip_to_wings_pixels":direction})
        assert direction>.3, ("Back chevron points forward",direction)
        args = {**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
        clicked = await self.tool("click_ui_at",args,"spatial-back-click")
        assert clicked["status"]=="input_delivered"
        after = await self.query("menu.get_state")
        assert after["current_menu"]==self.args.expected_menu, after
        retry = await self.tool("click_ui_at",args,"spatial-back-retry")
        assert retry["delivery"]["sync_id"]==clicked["delivery"]["sync_id"]
        assert (await self.query("menu.get_state"))["current_menu"]==after["current_menu"]
        self.cases.append({"case":"spatial-back-hover-held-click-and-retry","passed":True,
                           "source":menu["current_menu"],"destination":after["current_menu"],"pixel":[float(x),float(y)]})

    async def spatial_locked(self):
        menu = await self.query("menu.get_state")
        assert menu["current_menu"]=="MAIN MENU" and menu["is_3d"]
        item = next(i for i in menu["items"] if i["title"]=="WRECK RUN")
        passed = item["locked"] and item.get("coming_soon")
        self.cases.append({"case":"coming-soon-reported-as-unavailable","passed":bool(passed),"item":item})
        assert passed, "This RC's Coming Soon item must be reported as locked"
        self.args.spatial_click_index=item["index"]
        self.args.expected_menu=menu["current_menu"]
        await self.spatial_hover()
        assert not (await self.query("ui.get_message_box"))["open"]
        assert not (await self.query("game.get_state"))["is_loading"]
        self.cases.append({"case":"coming-soon-ray-click-remains-on-main-menu","passed":True})

    async def garage_two_rays(self):
        assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
        for ui_hand,car_hand in (("right","left"),("left","right")):
            _,ui_aim=await self.point_args("item.0",hand=ui_hand)
            hover=await self.tool("hover_ui_at",ui_aim,"garage-ui-ray-"+ui_hand)
            assert hover["status"]=="input_delivered"
            assert (await self.query("vr.targets"))["hovered"]=="item.0"
            native=await self.query("vr.targets");frame=native["vr_frame"]
            y=(.5-(.8-frame["panel"]["origin"][2])/frame["panel_height"])*1080
            l,t,r,b=next(t["rect"] for t in native["targets"] if t["name"]=="item.0")
            _,car_aim=await self.point_args("item.0",((960-l)/(r-l),(y-t)/(b-t)),car_hand)
            hover=await self.tool("hover_ui_at",car_aim,"garage-car-ray-"+car_hand)
            assert hover["status"]=="input_delivered"
            before=await self.query("vr.targets")
            await self.capture("garage-two-rays-before-"+car_hand+"-press")
            args={**car_aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
            receipt=await self.tool("click_ui_at",args,"garage-car-trigger-"+car_hand)
            assert receipt["status"]=="input_delivered"
            after=await self.query("vr.targets")
            assert after["clicks"]==before["clicks"], ("The car-side trigger activated the other ray's menu target",ui_hand,car_hand,before,after)
            repeated=await self.tool("click_ui_at",args,"garage-car-trigger-retry-"+car_hand)
            assert repeated["delivery"]["sync_id"]==receipt["delivery"]["sync_id"]
            assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
            self.cases.append({"case":"garage-car-trigger-isolated-"+car_hand,"passed":True,"ui_hand":ui_hand,"before":before,"after":after})
        await self.capture("garage-two-ray-safety-verified")

    async def garage_clearance(self):
        native = await self.query("vr.targets")
        assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
        frame = native["vr_frame"]
        height, origin = frame["panel_height"],frame["panel"]["origin"][2]
        buttons = [t for t in native["targets"] if t["name"].startswith("item.")]
        tops = [origin+(.5-t["rect"][1]/1080)*height for t in buttons]
        # The showroom floor is Z=0 and the car body is around Z=.8 m.
        # Aim at that height through the same rendered plane as the screenshot.
        y = (.5-(.8-origin)/height)*1080
        first=buttons[0];l,t,r,b=first["rect"]
        _,aim = await self.point_args(first["name"],((960-l)/(r-l),(y-t)/(b-t)))
        hovered = await self.tool("hover_ui_at",aim,"garage-car-body-hover")
        assert hovered["status"]=="input_delivered"
        actual = await self.query("vr.targets")
        passed = max(tops)<=.4 and not actual["hovered"]
        self.cases.append({"case":"garage-actions-clear-car-body","passed":passed,"button_top_heights":tops,"body_height":.8,"hovered":actual["hovered"]})
        await self.capture("garage-car-body-clearance")
        assert passed, ("Pointing at the car body must not select a garage action",tops,actual["hovered"])
        args={**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
        clicked=await self.tool("click_ui_at",args,"garage-car-body-no-action")
        assert clicked["status"]=="input_delivered"
        assert (await self.query("vr.targets"))["clicks"]==actual["clicks"]
        await self.tool("click_ui_at",args,"garage-car-body-no-action-retry")
        assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
        self.cases.append({"case":"garage-car-body-click-does-not-navigate","passed":True})

    async def garage_buttons(self):
        errors_before = await self.query("game.errors")
        menu = await self.query("menu.get_state")
        assert menu["current_menu"] == "GARAGE", menu
        targets = (await self.query("vr.targets"))["targets"]
        actions = [t for t in targets if t["name"].startswith("item.")]
        await self.capture("garage-action-buttons")
        small = [t for t in actions if t["rect"][2]-t["rect"][0]<200 or t["rect"][3]-t["rect"][1]<88]
        unlabeled = [i for i in menu["items"] if not i["title"].strip()]
        self.cases.append({"case":"garage-large-labeled-buttons","passed":not small and not unlabeled,"actions":actions,"small":small,"unlabeled":unlabeled})
        assert len(actions)==menu["item_count"] and not small and not unlabeled, ("Garage actions need large readable buttons",small,unlabeled)
        await self.audit("garage-action-button-spacing")
        await self.garage_clearance()
        await self.garage_two_rays()
        before = await self.query("game.get_state")
        for t in actions:
            await self.hover(t["name"])
            after = await self.query("menu.get_state")
            assert after["current_menu"]=="GARAGE" and after["selection_index"]==int(t["name"].split(".")[1]), after
        assert (await self.query("game.get_state"))["game_mode"]==before["game_mode"]
        self.cases.append({"case":"garage-all-action-hovers","passed":True})
        await self.capture("garage-buttons-hovered")
        quick = before["game_mode"]=="quick_events"
        expected = {1:"CARS",2:"SKINS",3:"SKINS",4:"DRIVERS"} if quick else {0:"CARS",1:"UPGRADES",2:"SKINS",3:"SKINS"} if before["game_mode"]=="career" else {0:"CARS",1:"SKINS",2:"SKINS",3:"DRIVERS"}
        livery_index = 3 if quick or before["game_mode"]=="career" else 2
        for index,destination in expected.items():
            await self.click("item."+str(index))
            reached = await self.query("menu.get_state")
            assert reached["current_menu"]==destination, (menu["items"][index],reached)
            if index==livery_index:
                await self.livery_back_to_garage()
            else:
                cancel = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
                await self.click(cancel)
            assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
            await self.audit("garage-return-from-"+str(index))
            self.cases.append({"case":"garage-action-round-trip-"+str(index),"passed":True,"title":menu["items"][index]["title"],"destination":destination})
            errors = await self.query("game.errors")
            clean = errors["total"] == errors_before["total"]
            self.cases.append({"case":"garage-no-engine-errors-"+str(index),"passed":clean,
                               "before_total":errors_before["total"],"after":errors})
            assert clean, ("Garage route raised an engine error", index, errors)
        await self.capture("garage-navigation-verified")

    async def livery_back_to_garage(self):
        # Livery owns its ImGui ray surface independently of the common menu pointer.
        from flatout_livery_e2e import game_read, LiveryTest
        livery = LiveryTest(self.args, self.session)
        before = await asyncio.to_thread(game_read,self.args.pid)
        assert before["ready"] and before["vr"]["active"] and not before["dirty"], before
        for step in range(3):
            controls = before["vr"]["controls"]
            control = next((c for c in controls if c["label"]=="BACK"), None)
            if control is None: control = next(c for c in controls if c["label"]=="BACK TO LAYERS")
            await livery.face_control(before, control)
            capture = await self.capture("garage-livery-back-source")
            x,y = screenshot_pixel(before,capture,control["x"]+control["width"]/2,control["y"]+control["height"]/2)
            aim = {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":self.args.hand,
                   "origin_mode":"hand","surface":ui_surface(before,control["x"]+control["width"]/2,control["y"]+control["height"]/2),"dwell_frames":30}
            hover = await self.tool("hover_ui_at",aim,"garage-livery-back-hover")
            assert hover["status"]=="input_delivered"
            hovered = await asyncio.to_thread(game_read,self.args.pid)
            assert hovered["vr"]["hands"][self.args.hand=="right"]["ui_hit"], hovered["vr"]
            args = {**aim,"hold_frames":45,"timeout":30,"command_id":uuid.uuid4().hex}
            clicked = await self.tool("click_ui_at",args,"garage-livery-back-click")
            assert clicked["status"]=="input_delivered"
            after = await self.query("menu.get_state")
            repeated = await self.tool("click_ui_at",args,"garage-livery-back-retry")
            assert repeated["delivery"]["sync_id"]==clicked["delivery"]["sync_id"]
            assert (await self.query("menu.get_state"))["current_menu"]==after["current_menu"]
            if after["current_menu"]=="GARAGE":
                self.cases.append({"case":"garage-livery-back-held-click-and-retry","passed":True})
                return
            before = await asyncio.to_thread(game_read,self.args.pid)
        raise AssertionError("Livery Back did not return to Garage")

    async def quick_layout(self):
        setup = (await self.query("menu.get_state"))["setup"]
        targets = (await self.query("vr.targets"))["targets"]
        await self.capture("quick-start-compact-groups")
        rows = [r for r in setup["rows"] if r["visible"] and r["index"]>0]
        for left in (True,False):
            group = [r for r in rows if (r["index"]<=3)==left]
            for slot,row in enumerate(group):
                arrows = [t for t in targets if t["name"].startswith("quick."+str(row["index"])+".")]
                expected = (620 if left else 460)+slot*(104 if left else 120)
                assert arrows and all(abs(t["rect"][1]-expected)<1 for t in arrows), ("Hidden settings left a gap",row,expected,arrows)
        self.cases.append({"case":"quick-start-compact-visible-groups","passed":True})
        capture = await self.capture("quick-start-heading-contrast")
        image = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        native = await self.query("vr.targets")
        state = None if capture["geometry"]["surfaces"] else {"vr":{"frame":native["vr_frame"]}}
        for title,rect in (("title",(100,64,730,194)),("steps",(1220,64,1818,194))):
            points = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"] else screenshot_pixel(state,capture,x,y)
                      for x,y in ((rect[0],rect[1]),(rect[2],rect[3]))]
            (x0,y0),(x1,y1) = points
            patch = image[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
            dark = float(np.mean(np.max(patch,axis=2)<110))
            self.cases.append({"case":"quick-start-"+title+"-contrast","passed":dark>.6,"dark_background_fraction":dark})
            assert dark>.6, ("Heading needs consistent contrast over a bright 3D scene",title,dark)

    async def quick_action_caption(self, label="current"):
        setup = (await self.query("menu.get_state"))["setup"]
        expected = "Choose Driver" if "stunt" in setup["rows"][0]["value"].lower() else "Choose Vehicle"
        actions = setup.get("actions", "")
        passed = bool(actions) and (("%%Validate%% "+expected in actions) if setup["can_continue"] else "%%Validate%%" not in actions)
        self.cases.append({"case":"quick-start-specific-action-"+label,"passed":passed,"expected":expected,
                           "can_continue":setup["can_continue"],"actual":actions})
        assert passed, ("Quick Start must name the next step after every setting refresh",expected,actions)

    async def quick_start(self):
        native = await self.query("vr.targets")
        assert native["menu"] == "QUICK EVENTS", native
        await self.capture("quick-start-layout")
        modes = [t for t in native["targets"] if t["name"].startswith("quick.mode.")]
        self.cases.append({"case":"quick-start-three-event-cards","passed":len(modes)==3,"targets":native["targets"]})
        assert len(modes)==3, "Quick Start needs three directly selectable event cards"
        assert all(t["rect"][2]-t["rect"][0]>=400 and t["rect"][3]-t["rect"][1]>=100 for t in modes)
        await self.audit("quick-start-separated-controls")
        await self.ray_buttons()
        await self.quick_action_caption("opened")
        # Direct event-card clicks must select only that mode; a hover must not.
        for index, title in ((1,"Arena"),(2,"Stunt"),(0,"Race")):
            before = (await self.query("menu.get_state"))["setup"]
            await self.hover("quick.mode."+str(index))
            assert (await self.query("menu.get_state"))["setup"] == before, "Hover changed the event configuration"
            await self.click("quick.mode."+str(index))
            setup = (await self.query("menu.get_state"))["setup"]
            assert title.lower() in setup["rows"][0]["value"].lower(), setup
            await self.audit("quick-start-"+title.lower()+"-layout")
            await self.quick_layout()
            await self.quick_action_caption(title.lower()+"-mode")
            controls = (await self.query("vr.targets"))["targets"]
            assert not any(t["name"].startswith("setup.") for t in controls), "Legacy controls leaked into the new board"
            for t in controls:
                if not t["name"].startswith("quick.") or t["name"].startswith("quick.mode."):
                    continue
                l,top,r,b = t["rect"]
                assert r-l >= 72 and b-top >= 72 and b <= 936, t
                values = [(r["index"],r["value"],r["visible"]) for r in (await self.query("menu.get_state"))["setup"]["rows"]]
                await self.hover(t["name"])
                assert [(r["index"],r["value"],r["visible"]) for r in (await self.query("menu.get_state"))["setup"]["rows"]] == values, "Hover edited the event settings"
            # Step each available setting once and restore it through its other
            # arrow. Both operations hold the trigger and replay the same UUID.
            rows = [r for r in setup["rows"] if r["visible"] and r["index"]>0]
            for row in rows:
                i = row["index"]
                current = (await self.query("menu.get_state"))["setup"]["rows"][i]
                controls = {t["name"] for t in (await self.query("vr.targets"))["targets"]}
                direction = "next" if f"quick.{i}.next" in controls else "prev"
                name = f"quick.{i}.{direction}"
                if name not in controls:
                    continue # A single-value setting has two visibly disabled arrows.
                await self.click(name)
                await self.quick_action_caption(title.lower()+"-edit-"+str(i))
                changed = (await self.query("menu.get_state"))["setup"]["rows"][i]
                assert changed["value"] != current["value"], (title,row,current,changed)
                await self.click(f"quick.{i}."+("prev" if direction=="next" else "next"))
                restored = (await self.query("menu.get_state"))["setup"]["rows"][i]
                assert restored["value"] == current["value"], (title,current,restored)
            # Stunts lead to drivers; races/derbies lead to the garage.
            controls = (await self.query("vr.targets"))["targets"]
            # Some profiles have locked tracks. Look for a playable choice using
            # real track arrows; if the entire category is locked, verify blocking.
            seen = set()
            while not any(t["name"].startswith("helper.Validate.") for t in controls):
                track = (await self.query("menu.get_state"))["setup"]["rows"][2]["value"]
                if track in seen:
                    break
                seen.add(track)
                await self.click("quick.2.next")
                controls = (await self.query("vr.targets"))["targets"]
            proceed = next((t["name"] for t in controls if t["name"].startswith("helper.Validate.")), None)
            if proceed is None:
                assert not (await self.query("menu.get_state"))["setup"]["can_continue"]
                await self.capture("quick-start-"+title.lower()+"-all-tracks-locked")
                self.cases.append({"case":"quick-start-"+title.lower()+"-locked-category-blocked","passed":True,"tracks_checked":len(seen)})
                continue
            await self.click(proceed)
            after = await self.query("menu.get_state")
            assert after["current_menu"] == ("DRIVERS" if index==2 else "GARAGE"), after
            cancel = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
            await self.click(cancel)
            assert (await self.query("menu.get_state"))["current_menu"] == "QUICK EVENTS"
            self.cases.append({"case":"quick-start-"+title.lower()+"-settings-and-next-step","passed":True})
        await self.capture("quick-start-redesigned-final")

    async def ray_buttons(self):
        native = await self.query("vr.targets")
        actions = [t for t in native["targets"] if t["name"].startswith(("helper.","endhelper.","msgbox."))]
        assert actions, "Open a menu, dialog or results screen with actions"
        await self.capture("ray-action-buttons-layout")
        small = [t for t in actions if t["rect"][2]-t["rect"][0]<160 or t["rect"][3]-t["rect"][1]<72]
        self.cases.append({"case":"large-ray-action-buttons","passed":not small,"actions":actions,"small":small})
        assert not small, ("Controller prompts need full-size ray buttons",small)
        await self.audit("ray-action-button-spacing", actions_only=True)
        capture = await self.tool("capture_screenshot", {"eye":"left","max_width":2048}, "ray-button-visuals")
        image = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        native = await self.query("vr.targets")
        state = None if capture["geometry"]["surfaces"] else {"vr":{"frame":native["vr_frame"]}}
        for target in actions:
            l,t,r,b = target["rect"]
            rect = (l+16,t+12,r-16,b-12)
            points = [quad_pixel(capture,(x,y,x,y)) if capture["geometry"]["surfaces"] else screenshot_pixel(state,capture,x,y)
                      for x,y in ((rect[0],rect[1]),(rect[2],rect[3]))]
            (x0,y0),(x1,y1) = points
            patch = image[math.floor(min(y0,y1)):math.ceil(max(y0,y1)),math.floor(min(x0,x1)):math.ceil(max(x0,x1))]
            primary = ".Validate." in target["name"] or ".FirstValidate." in target["name"] or target["name"]=="msgbox.validate"
            bright = (np.min(patch,axis=2)>180)&(np.max(patch,axis=2)-np.min(patch,axis=2)<45)
            dark = np.max(patch,axis=2)<95
            amber = (patch[:,:,0]>180)&(patch[:,:,1]>95)&(patch[:,:,2]<70)
            # Use luminance for dark text: amber antialiasing keeps red high.
            dark_ink = patch[:,:,0]*.2126+patch[:,:,1]*.7152+patch[:,:,2]*.0722 < 110
            ink = int(np.sum(dark_ink if primary else bright))
            fill = float(np.mean(amber if primary else dark))
            passed = ink>=8 and fill>.2
            self.cases.append({"case":"ray-button-visible-"+target["name"],"passed":passed,"ink_pixels":ink,"fill_fraction":fill})
            assert passed, ("Button art does not match its ray target",target,ink,fill)
        for target in actions:
            await self.hover(target["name"])
        await self.capture("ray-action-buttons-hovered")

    async def popup_scene(self):
        assert not (await self.query("ui.get_message_box"))["open"], "Start before opening the purchase dialog"
        targets = (await self.query("vr.targets"))["targets"]
        confirm = next(t["name"] for t in targets if t["name"].startswith("helper.Validate."))
        await self.capture("scene-before-popup")
        source_capture = self.last_capture
        source_frame = (await self.query("vr.targets"))["vr_frame"]
        before = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        await self.click(confirm)
        assert (await self.query("ui.get_message_box"))["open"]
        await self.capture("scene-behind-popup")
        after = np.asarray(Image.open(self.last_image_path).convert("RGB")).astype(float)
        assert before.shape == after.shape, "Preview resized during scene comparison"
        # The black backdrop is clipped to the world-space menu quad. Sample the
        # car inside that quad below the dialog body; floor outside it stays lit
        # even when the opaque backdrop is broken.
        state = {"vr":{"frame":source_frame}}
        (x0,y0),(x1,y1) = [screenshot_pixel(state,source_capture,x,y) for x,y in ((500,850),(1500,980))]
        x0,x1 = sorted((math.floor(x0),math.ceil(x1)))
        y0,y1 = sorted((math.floor(y0),math.ceil(y1)))
        roi = (slice(y0,y1),slice(x0,x1))
        source, actual = before[roi], after[roi]
        visible_scene = (np.min(source,axis=2)>30)&(np.max(source,axis=2)>65)
        assert np.sum(visible_scene)>100, "The source screenshot must contain a visible 3D scene"
        source_brightness = float(np.mean(source[visible_scene]))
        popup_brightness = float(np.mean(actual[visible_scene]))
        ratio = popup_brightness/source_brightness
        passed = ratio > .5
        self.cases.append({"case":"popup-preserves-visible-game-scene","passed":passed,
                           "scene_pixels":int(np.sum(visible_scene)),"brightness_ratio":ratio})
        assert passed, ("Popup blacked out the scene",ratio)
        await self.audit("popup-exclusive-input-layout")
        native = await self.query("vr.targets")
        assert all(t["name"].startswith("msgbox.") for t in native["targets"]), native
        await self.click("msgbox.cancel")
        assert not (await self.query("ui.get_message_box"))["open"]
        print("Popup keeps the 3D scene visible and Cancel works once PASS",flush=True)

    async def car_modal(self):
        assert (await self.query("menu.get_state"))["current_menu"] == "CARS", "Open a car purchase confirmation first"
        assert (await self.query("ui.get_message_box"))["open"]
        native = await self.query("vr.targets")
        assert native["targets"] and all(t["name"].startswith("msgbox.") for t in native["targets"]), native
        capture = await self.tool("capture_screenshot", {"eye":"left", "max_width":2048}, "car-modal-arrows")
        state = {"vr":{"frame":native["vr_frame"]}}
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
        observations = []
        for side, rect in (("left",(-220,640,20,940)), ("right",(1970,640,2230,940))):
            l,t,r,b = rect
            corners = [screenshot_pixel(state,capture,x,y) for x,y in ((l,t),(r,b))]
            (x0,y0),(x1,y1) = corners
            x0,x1 = max(0,math.floor(min(x0,x1))),min(pixels.shape[1],math.ceil(max(x0,x1)))
            y0,y1 = max(0,math.floor(min(y0,y1))),min(pixels.shape[0],math.ceil(max(y0,y1)))
            patch = pixels[y0:y1,x0:x1].astype(float)
            assert patch.size, (side,corners)
            amber = (patch[:,:,0]>210)&(patch[:,:,1]>110)&(patch[:,:,1]<205)&(patch[:,:,2]<55)
            observations.append({"side":side,"amber_pixels":int(np.sum(amber)),"image_rect":[x0,y0,x1,y1]})
        passed = all(row["amber_pixels"] <= 5 for row in observations)
        self.cases.append({"case":"car-modal-hides-background-carousel-arrows","passed":passed,"observations":observations})
        assert passed, observations
        await self.audit("car-purchase-dialog-layout")
        await self.click("msgbox.cancel")
        assert not (await self.query("ui.get_message_box"))["open"]
        await self.capture("car-modal-dismissed")

    async def car_classes(self):
        initial = await self.query("menu.get_state")
        assert initial["current_menu"] == "CARS", initial
        for index, car_class in ((0, "derby"), (1, "classic"), (2, "allstars"), (0, "derby")):
            await self.audit("car-class-layout-"+str(index))
            targets = (await self.query("vr.targets"))["targets"]
            tabs = [t for t in targets if t["name"].startswith("class.")]
            assert {t["name"] for t in tabs} == {"class.0", "class.1", "class.2"}, tabs
            assert all(t["rect"][2]-t["rect"][0] >= 160 and t["rect"][3]-t["rect"][1] >= 64 for t in tabs), tabs
            before = await self.query("menu.get_state")
            await self.hover("class."+str(index))
            assert (await self.query("menu.get_state"))["items"] == before["items"], "Hover changed the car class"
            await self.click("class."+str(index))
            after = await self.query("menu.get_state")
            # New vehicles need not encode their class in the enum name (the
            # motorcycle belongs to Classic). Read the actual factory class.
            assert after["items"] and all(t["car_class"] == car_class for t in after["items"]), after
            assert not (await self.query("ui.get_message_box"))["open"], "Class selection activated a car purchase"
            await self.capture("car-class-selected-"+str(index))
        self.cases.append({"case":"car-classes-hover-and-switch-both-directions","passed":True})

    async def footer(self):
        """Measure the old decorative strip, away from action text and Start."""
        initial = await self.query("vr.targets")
        targets = initial["targets"]
        helpers = [t for t in targets if t["name"].startswith("helper.")]
        assert helpers, "Open a 2D menu with footer actions first"
        capture = await self.capture("footer-background")
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
        native = await self.query("options.get_state")
        frame = initial.get("vr_frame") or native.get("vr_frame")
        assert capture["geometry"]["surfaces"] or frame, "The game must expose the active world-space panel geometry"
        state = None if capture["geometry"]["surfaces"] else {"vr": {"frame": frame}}
        samples = []
        for y in np.linspace(1022, 1050, 5):
            for x in np.linspace(80, 1100, 60):
                if any(t["rect"][0]-8 <= x <= t["rect"][2]+8 and t["rect"][1]-8 <= y <= t["rect"][3]+8 for t in helpers):
                    continue # Real amber action buttons are allowed; the decorative strip is not.
                if capture["geometry"]["surfaces"]:
                    px, py = quad_pixel(capture, (x, y, x, y))
                else:
                    px, py = screenshot_pixel(state, capture, x, y)
                px, py = round(px), round(py)
                assert 0 <= px < pixels.shape[1] and 0 <= py < pixels.shape[0], (px, py)
                samples.append(pixels[py, px])
        rgb = np.array(samples).astype(float)
        orange = (rgb[:, 0] > 210) & (rgb[:, 1] > 110) & (rgb[:, 1] < 205) & (rgb[:, 2] < 55)
        fraction = float(np.mean(orange))
        self.cases.append({"case": "no-yellow-footer-strip", "passed": fraction < .1,
                           "orange_fraction": fraction, "sample_count": len(samples), "helpers": helpers})
        assert fraction < .1, ("Yellow footer strip still visible", fraction)
        for helper in helpers:
            await self.hover(helper["name"])
        await self.capture("footer-actions-hovered")
        cancel = next(t["name"] for t in helpers if t["name"].startswith("helper.Cancel."))
        focused = native["open"] and any(box["focused"] for box in native["boxes"])
        await self.click(cancel)
        after = await self.query("options.get_state")
        if not native["open"]:
            menu_after = await self.query("menu.get_state")
            assert not menu_after["is_open"] or menu_after["current_menu"] != initial["menu"], "Back did not leave the current menu"
        else:
            assert not after["open"], "One Back click did not close Options"
        self.cases.append({"case": "footer-back-result", "passed": True, "was_focused": focused,
                           "options_open_after": after["open"]})
        print("Footer strip absent; action hovers and one Back click PASS", flush=True)

    async def stereo_menu_rays(self):
        assert (await self.capture("stereo-quad-source"))["geometry"]["surfaces"], "Run the beam-color pixel check from in-race quad Options; the world-space ownership test checks both reticles"
        await self.focus_audio()
        cancel = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
        rays = {}
        for hand, target in (("left", cancel), ("right", "box3.row1.bar")):
            _, args = await self.point_args(target, (.5, .5), hand)
            receipt = await self.tool("hover_ui_at", args, "stereo-menu-hover-"+hand)
            assert receipt["status"] == "input_delivered"
            rays[hand] = receipt["target"]["application_ray"]
        capture = await self.tool("capture_screenshot", {"eye":"both", "max_width":2048}, "stereo-menu-rays")
        pixels = np.asarray(Image.open(self.last_image_path).convert("RGB"))
        observations = []
        for hand, ray in rays.items():
            origin, direction = np.array(ray["position"]), np.array(ray["direction"])
            for eye_name in ("left", "right"):
                eye = next(e for e in capture["geometry"]["projection_eyes"] if e["eye"] == eye_name)
                left,right,up,down = map(math.tan, eye["fov"])
                ex,ey,ew,eh = eye["viewport"]
                cx,cy,cw,ch = capture["crop"]
                width,height = capture["image_size"]
                hits = []
                for distance in (.06,.10,.14,.18,.22,.26,.30,.34):
                    point = origin+direction*distance
                    local = quaternion_matrix(eye["pose"]["orientation"]).T @ (point-eye["pose"]["position"])
                    u,v = (local[0]/-local[2]-left)/(right-left), (local[1]/-local[2]-up)/(down-up)
                    x,y = round((ex+u*ew-cx)/cw*width-.5), round((ey+v*eh-cy)/ch*height-.5)
                    if not 4 <= x < width-4 or not 4 <= y < height-4:
                        continue
                    patch = pixels[y-4:y+5,x-4:x+5].astype(float)
                    red,green,blue = patch[:,:,0],patch[:,:,1],patch[:,:,2]
                    color = (blue > red+25)&(green > red+25) if hand == "left" else (red > blue+50)&(green > blue+25)&(red > 140)
                    hits.append({"distance":distance,"pixel":[x,y],"colored_pixels":int(np.sum(color))})
                visible = sum(h["colored_pixels"] > 0 for h in hits)
                observations.append({"hand":hand,"eye":eye_name,"visible_samples":visible,"samples":hits})
                assert visible >= 2, ("Controller beam color missing from eye image", observations[-1])
        self.cases.append({"case":"both-menu-rays-in-both-eyes","passed":True,"observations":observations})
        print("Cyan left and amber right beams visible in both eyes PASS",flush=True)

    async def run(self):
        if self.args.statistics_header_only:
            return await self.statistics_header()
        if self.args.reading_comfort_only:
            menu=await self.query("menu.get_state")
            assert menu["is_open"] and not menu["is_3d"],"Start in a flat reading menu"
            return await self.reading_comfort(menu["current_menu"].lower().replace(" ","-"))
        if self.args.quick_comfort_only:
            assert (await self.query("menu.get_state"))["current_menu"]=="QUICK EVENTS","Start in Quick Play"
            return await self.reading_comfort("quick-start")
        if self.args.quick_action_copy_only:
            await self.capture("quick-start-action-copy")
            return await self.quick_action_caption()
        if self.args.spatial_locked_only:
            return await self.spatial_locked()
        if self.args.spatial_back_only:
            return await self.spatial_back()
        if self.args.career_wrong_class_only:
            return await self.career_wrong_class()
        if self.args.career_cancel_only:
            return await self.career_cancel()
        if self.args.career_cup_only:
            return await self.career_cup()
        if self.args.career_welcome_only:
            return await self.career_welcome()
        if self.args.career_only:
            return await self.career_sweep()
        if self.args.career_layout_only:
            return await self.career_layout()
        if self.args.options_devices_only:
            return await self.options_devices()
        if self.args.options_interactions_only:
            return await self.options_interactions()
        if self.args.options_redesign_layout_only:
            return await self.options_redesign_layout()
        if self.args.return_main_only:
            return await self.return_main()
        if self.args.spatial_hover_only:
            return await self.spatial_hover()
        if self.args.garage_two_rays_only:
            return await self.garage_two_rays()
        if self.args.garage_clearance_only:
            return await self.garage_clearance()
        if self.args.garage_only:
            return await self.garage_buttons()
        if self.args.quick_layout_only:
            return await self.quick_layout()
        if self.args.quick_start_only:
            return await self.quick_start()
        if self.args.party_stunts_header_only:
            return await self.party_stunts_header()
        if self.args.party_stunts_only:
            return await self.party_stunts()
        if self.args.results_retry_only:
            return await self.results_retry()
        if self.args.leaderboard_cancel_only:
            return await self.leaderboard_cancel()
        if self.args.leaderboard_empty_only:
            return await self.leaderboard_empty()
        if self.args.end_results_only:
            return await self.end_results()
        if self.args.party_only:
            return await self.party()
        if self.args.statistics_only:
            return await self.statistics()
        if self.args.credits_stop_only:
            return await self.credits_stop()
        if self.args.credits_finish_only:
            return await self.credits_finish()
        if self.args.credits_only:
            return await self.credits()
        if self.args.credits_layout_only:
            return await self.credits_layout()
        if self.args.popup_motion_only:
            return await self.popup_motion()
        if self.args.popup_no_prompts_only:
            return await self.popup_no_prompts()
        if self.args.popup_anchor_only:
            return await self.popup_anchor()
        if self.args.popup_spacing_only:
            await self.popup_spacing()
            return
        if self.args.single_dialog_only:
            await self.popup_spacing()
            assert (await self.query("ui.get_message_box"))["open"], "Open a single-button informational dialog first"
            names = [t["name"] for t in (await self.query("vr.targets"))["targets"]]
            assert names == ["msgbox.validate"], names
            await self.audit("single-dialog-layout")
            await self.click("msgbox.validate")
            assert not (await self.query("ui.get_message_box"))["open"], "Confirm did not dismiss the informational dialog"
            await self.capture("single-dialog-dismissed")
            self.cases.append({"case":"single-dialog-dismissed","passed":True})
            return
        if self.args.menu_hover_only:
            targets = (await self.query("vr.targets"))["targets"]
            assert targets, "Open a menu with ray targets first"
            if self.args.audit_only:
                await self.audit("menu-layout")
            for target in targets:
                await self.hover(target["name"])
                self.cases.append({"case":"hover-"+target["name"],"passed":True})
            print("All current menu targets receive actual ray hovers PASS", flush=True)
            return
        if self.args.footer_contrast_only:
            return await self.footer_contrast()
        if self.args.car_classes_only:
            return await self.car_classes()
        if self.args.car_modal_only:
            return await self.car_modal()
        if self.args.popup_scene_only:
            return await self.popup_scene()
        if self.args.ray_buttons_only:
            return await self.ray_buttons()
        if self.args.footer_only:
            return await self.footer()
        if self.args.setup_layout_only:
            targets = (await self.query("vr.targets"))["targets"]
            assert any(t["name"].startswith(("setup.","quick.")) for t in targets), "Open a game-setup screen first"
            return await self.audit("game-setup-layout")
        if self.args.dual_hand_options:
            return await self.dual_hand_options()
        if self.args.stereo_menu_rays_only:
            return await self.stereo_menu_rays()
        if self.args.options_comfort_only:
            return await self.options_comfort()
        if self.args.options_layout_only or self.args.tall_layout_only:
            return await self.options_layout()
        if self.args.options_only:
            return await self.options()
        game = await self.query("game.get_state")
        assert not game["is_loading"] and not game["is_main_menu"], game
        if not (await self.query("hud.pause_menu"))["is_open"]:
            await self.menu_button()
        await self.audit("pause-layout")
        if self.args.audit_only:
            return
        await self.click("button.0")
        assert not (await self.query("hud.pause_menu"))["is_open"], "Resume did not close Pause"
        await self.menu_button()
        for name in ("button.1", "button.3"):
            await self.click(name)
            assert (await self.query("ui.get_message_box"))["open"], "Confirmation did not open"
            await self.audit("confirmation-"+name)
            await self.click("msgbox.cancel")
            assert not (await self.query("ui.get_message_box"))["open"]
            assert not (await self.query("game.get_state"))["is_main_menu"]
        await self.click("button.2")
        targets = (await self.query("vr.targets"))["targets"]
        assert len(targets) > 6, ("Options did not open", targets)
        self.cases.append({"case": "options-opened", "passed": True, "targets": targets})


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())})
    scope = ("Statistics page-count legibility" if args.statistics_header_only else
             "Reading-menu head-height placement and world-locked both-eye visibility" if args.reading_comfort_only else
             "Quick Start head-height placement and world-locked both-eye visibility" if args.quick_comfort_only else
             "Options head-height placement and world-locked both-eye visibility" if args.options_comfort_only else
             "Credits Pause freezes the reading position immediately" if args.credits_stop_only else
             "Complete Credits playback, continuous ray hold and automatic ending fade" if args.credits_finish_only else
             "Credits readable viewport, transport controls and live scene" if args.credits_only or args.credits_layout_only else
             "Native popup entrance fade and interruption-free ray input" if args.popup_motion_only else
             "Modal suppresses legacy soundtrack prompts without hiding its menu" if args.popup_no_prompts_only else
             "Standing popup placement, stable world anchor and ray hover" if args.popup_anchor_only else
             "Popup content and action spacing" if args.popup_spacing_only else
             "Quick Start explicit next-step caption" if args.quick_action_copy_only else
             "Coming Soon lock diagnostics and inert ray click" if args.spatial_locked_only else
             "Spatial Back visibility, edge misses, both hands and one activation" if args.spatial_back_only else
             "First Career welcome preserves the live scene and opens car selection after acknowledgement" if args.career_welcome_only else
             "Career unavailable-class warnings, filtered cars and Back" if args.career_wrong_class_only else
             "Career active-cup cancellation and confirmation" if args.career_cancel_only else
             "Career cup preview, safe background, Back and confirmed race start" if args.career_cup_only else
             "Career series paging, card hovers and locked event behavior" if args.career_only else
             "Career event-card spacing and page button layout" if args.career_layout_only else
             "Options peripheral selection and dynamic input-setting pages" if args.options_devices_only else
             "Options choices, toggles, sliders, reset and binding capture" if args.options_interactions_only else
             "Options focused category layout" if args.options_redesign_layout_only else
             "Options Back and confirmed return to the main menu" if args.return_main_only else
             "Spatial menu labels and screenshot aiming" if args.spatial_hover_only else
             "Garage two-ray trigger isolation" if args.garage_two_rays_only else
             "Garage car-body clearance and empty-space clicks" if args.garage_clearance_only else
             "Garage action sizing, spacing and navigation" if args.garage_only else
             "Quick Start compact setting groups" if args.quick_layout_only else
             "Quick Start event cards, settings and next step" if args.quick_start_only else
             "Large ray-action buttons and spacing" if args.ray_buttons_only else
             "Popup scene visibility and exclusive input" if args.popup_scene_only else
             "Car purchase modal focus and dismissal" if args.car_modal_only else
             "Car class button spacing and selection" if args.car_classes_only else
             "Party stunt page-count contrast" if args.party_stunts_header_only else
             "Party stunt pages, playlist and Start" if args.party_stunts_only else
             "Results Try Again and idempotent retry" if args.results_retry_only else
             "Leaderboard loading cancellation" if args.leaderboard_cancel_only else
             "Empty leaderboard layout and wording" if args.leaderboard_empty_only else
             "Post-race result footer and page advancement" if args.end_results_only else
             "Party player-count layout and selection" if args.party_only else
             "Statistics page controls and category navigation" if args.statistics_only else
             "Single informational dialog" if args.single_dialog_only else
             "Current menu ray hover" if args.menu_hover_only else
             "Idle footer label contrast" if args.footer_contrast_only else
             "Footer background and action hover" if args.footer_only else
             "Game setup target spacing" if args.setup_layout_only else
             "Both menu rays in both eyes" if args.stereo_menu_rays_only else
             "Dual-hand Options slider ownership" if args.dual_hand_options else
             "Options categories and ray scrolling" if args.options_only or args.options_layout_only or args.tall_layout_only else
             "Native race Pause and confirmations")
    result = {"passed": False, "pid": args.pid, "scope": scope+" through real XR actions"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = Frontend(args, session)
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
    print("Front-end regression passed:", args.evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--options-interactions-only", action="store_true", help="From Options: edit/restore choices, toggles and volume, confirm/cancel Reset, and start/cancel binding capture")
    parser.add_argument("--options-devices-only", action="store_true", help="From Options: select every available peripheral, page its input settings and restore the original device")
    parser.add_argument("--career-welcome-only", action="store_true", help="From Main with no owned Career car: verify the welcome keeps the live scene in both eyes, acknowledge once, and return without buying")
    parser.add_argument("--career-layout-only", action="store_true", help="From Career's cup/event grid: check visible page controls and breathing room between cards")
    parser.add_argument("--career-only", action="store_true", help="From Career's cup/event grid: page both ways, hover every card, reject locked events and restore the original page")
    parser.add_argument("--career-cup-only", action="store_true", help="From an unlocked Career cup: check preview actions, blank-area click safety, Back, and a confirmed race start")
    parser.add_argument("--career-cancel-only", action="store_true", help="From the Career hub with an active test cup: cancel abandonment, then confirm it and verify Go Race returns")
    parser.add_argument("--career-wrong-class-only", action="store_true", help="From Career class selection with a Derby car: verify Classic/AllStar warnings, filtered cars and Back without buying")
    parser.add_argument("--options-redesign-layout-only", action="store_true", help="From Options: require a category rail and a single spacious list with separate page controls")
    parser.add_argument("--return-main-only", action="store_true", help="From in-race Options or Pause: use ray Back and confirm Main Menu exactly once")
    parser.add_argument("--require-physical-lift", action="store_true", help="Require actual 15mm forward travel for enabled spatial labels")
    parser.add_argument("--spatial-hover-only", action="store_true", help="From a spatial text menu: screenshot and hover each rendered label, optionally activate one")
    parser.add_argument("--skip-menu-index", action="append", type=int, default=[], help="Exclude an explicitly deferred spatial menu item")
    parser.add_argument("--spatial-back-only", action="store_true", help="From a spatial submenu: check Back in both eyes, reject edge/other-ray presses, verify its chevron and click once; requires --expected-menu")
    parser.add_argument("--spatial-locked-only", action="store_true", help="From this RC's main menu: verify Wreck Run is reported as Coming Soon and a held ray click cannot enter it")
    parser.add_argument("--spatial-click-index", type=int)
    parser.add_argument("--expected-menu", help="Expected destination for --spatial-click-index or --return-main-only")
    parser.add_argument("--garage-two-rays-only", action="store_true", help="From Garage: press a trigger over the car while the other ray rests on an action, in both hand assignments")
    parser.add_argument("--garage-clearance-only", action="store_true", help="From Garage: verify the dock stays below the car and a car-body ray cannot activate its buttons")
    parser.add_argument("--garage-only", action="store_true", help="From Garage: verify large labeled action buttons, spacing, hovers and submenu routes")
    parser.add_argument("--car-classes-only", action="store_true", help="From unrestricted Cars: audit the three full-size class buttons and switch both ways without buying a car")
    parser.add_argument("--car-modal-only", action="store_true", help="From a car purchase confirmation: reject background carousel arrows and cancel exactly once")
    parser.add_argument("--popup-scene-only", action="store_true", help="From Upgrades over a visible showroom: open a purchase popup, check scene preservation, and cancel once")
    parser.add_argument("--quick-layout-only", action="store_true", help="From Quick Play: check compact placement of only the visible settings")
    parser.add_argument("--quick-start-only", action="store_true", help="From Quick Play: check event cards, dedicated stepper buttons and the next step")
    parser.add_argument("--quick-action-copy-only", action="store_true", help="From Quick Play: check that the ray action names the vehicle/driver selection step instead of generic Confirm")
    parser.add_argument("--ray-buttons-only", action="store_true", help="Check large menu, dialog or results action buttons and ray hovers")
    parser.add_argument("--party-stunts-header-only", action="store_true", help="From Party stunts: check readable page-count lettering over the 3D scene")
    parser.add_argument("--party-stunts-only", action="store_true", help="From the first Party stunt page: visit all twelve cards, select/remove/clear, page both ways, and start driver selection")
    parser.add_argument("--results-retry-only", action="store_true", help="From race results or the leaderboard: click Try Again, retry the same command, and verify the same race restarts")
    parser.add_argument("--leaderboard-cancel-only", action="store_true", help="From the leaderboard loading dialog: cancel the read with a ray, verify it stops, and verify the menu remains usable")
    parser.add_argument("--leaderboard-empty-only", action="store_true", help="From an empty leaderboard: reject invisible page/profile targets and verify a readable empty message")
    parser.add_argument("--end-results-only", action="store_true", help="From race results: verify no yellow footer, readable actions, and exactly one Continue page with a held trigger")
    parser.add_argument("--party-only", action="store_true", help="From Party Mode's player selector: hover every count and choose eight players exactly once")
    parser.add_argument("--statistics-only", action="store_true", help="From the top of Statistics: scroll both ends with page buttons, check spacing and category navigation")
    parser.add_argument("--statistics-header-only", action="store_true", help="From Statistics: require a readable page count with bright lettering on a stable dark backing")
    parser.add_argument("--credits-stop-only", action="store_true", help="From Credits: observe the real ray Pause transition and reject coasting after it")
    parser.add_argument("--credits-finish-only", action="store_true", help="From Credits: continuously hold Forward through the complete roll, check the ending fade and automatic return to Extras")
    parser.add_argument("--credits-only", action="store_true", help="From Credits: check the clipped reading panel, pause/resume, held forward/rewind and one Back click")
    parser.add_argument("--credits-layout-only", action="store_true", help="From Credits: check its bounded panel, separate transport dock and both-eye standing visibility")
    parser.add_argument("--popup-motion-only", action="store_true", help="From the spatial main menu: ray-hover Quit, click it once, observe the native entrance fade, then ray-cancel once")
    parser.add_argument("--max-popup-fade-seconds", type=float, help="With --popup-motion-only: bound the entrance duration estimated from intermediate native alpha frames")
    parser.add_argument("--popup-no-prompts-only", action="store_true", help="From a cancelable modal: verify soundtrack prompts are hidden, ray Cancel fires once and the same menu returns")
    parser.add_argument("--popup-anchor-only", action="store_true", help="From an open popup at the default standing head pose: check visibility in both eyes, stable placement during head motion and actual ray hover")
    parser.add_argument("--popup-spacing-only", action="store_true", help="From an open popup: verify content padding, caption spacing and the separate ray-button action row")
    parser.add_argument("--single-dialog-only", action="store_true", help="From a single-button informational popup: hover and confirm exactly once, then verify dismissal")
    parser.add_argument("--menu-hover-only", action="store_true", help="Hover every target in the current menu; combine with --audit-only for flat menu spacing")
    parser.add_argument("--footer-contrast-only", action="store_true", help="From Options: check idle action wording independently of its bright icons")
    parser.add_argument("--footer-only", action="store_true", help="From a 2D menu: verify the yellow strip is absent, hover its actions, and click Back once")
    parser.add_argument("--options-only", action="store_true", help="Audit all categories from already-open Options")
    parser.add_argument("--options-layout-only", action="store_true", help="Check dedicated navigation and scroll controls")
    parser.add_argument("--options-comfort-only", action="store_true", help="From Options: check neutral-gaze placement, both-eye control visibility, stable head movement and ray Back hover")
    parser.add_argument("--quick-comfort-only", action="store_true", help="From Quick Play: check neutral-gaze placement, both-eye control visibility, stable head movement and ray Back hover")
    parser.add_argument("--reading-comfort-only", action="store_true", help="From a flat reading menu: check neutral-gaze placement, both-eye control visibility, stable head movement and ray Back hover")
    parser.add_argument("--tall-layout-only", action="store_true", help="Check that Options uses the available vertical space")
    parser.add_argument("--hand", choices=("left", "right"), default="right", help="Controller used for pointer interactions")
    parser.add_argument("--dual-hand-options", action="store_true", help="Verify an Audio slider drag owns both rays until release, then restore its value")
    parser.add_argument("--setup-layout-only", action="store_true", help="Check game-setup row, Start and footer hit targets for overlap")
    parser.add_argument("--stereo-menu-rays-only", action="store_true", help="Verify cyan/amber controller-beam pixels in both eyes from in-race Options")
    args = parser.parse_args()
    if args.return_main_only and not args.expected_menu:
        parser.error("--return-main-only requires --expected-menu for the native mode-specific destination")
    if args.spatial_back_only and not args.expected_menu:
        parser.error("--spatial-back-only requires --expected-menu")
    if args.spatial_click_index is not None and (not args.spatial_hover_only or not args.expected_menu):
        parser.error("--spatial-click-index requires --spatial-hover-only and --expected-menu")
    asyncio.run(run(args))
