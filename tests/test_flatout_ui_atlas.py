"""Atlas coverage, evidence identity, safety and bounded native I/O contracts."""
import asyncio
import copy
import importlib.util
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

PATH=Path(__file__).resolve().parents[1]/"tools/flatout_ui_atlas.py"
spec=importlib.util.spec_from_file_location("flatout_ui_atlas",PATH)
atlas=importlib.util.module_from_spec(spec);spec.loader.exec_module(atlas)


class AtlasContracts(unittest.TestCase):
    def test_modal_exposes_only_native_targets_not_background_spatial_items(self):
        snapshot={"menu":{"current_menu":"MAIN MENU","items":[{"index":0,"title":"Career","rendered_position":[1,2,3],"locked":False},
                    {"index":1,"title":"Options","rendered_position":[1,2,2],"locked":False}]},
                  "message":{"open":True},"targets":{"targets":[{"name":"msgbox.validate","rect":[1,2,30,40]}]},"nodes":[]}
        inventory=atlas.control_inventory(snapshot)
        self.assertEqual([c["name"] for c in inventory["controls"]],["msgbox.validate"])
        self.assertEqual(len(inventory["background_controls"]),2)
        self.assertTrue(all(not c["enabled"] and c["blocked_by"]=="modal" for c in inventory["background_controls"]))
        snapshot["message"]["open"]=False;snapshot["targets"]["targets"]=[]
        inventory=atlas.control_inventory(snapshot)
        self.assertEqual([c["name"] for c in inventory["controls"]],["spatial.0","spatial.1"])
        self.assertEqual(inventory["background_controls"],[])

    def test_report_artifact_paths_cannot_escape_evidence_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            for reference in ("../outside.jpg",str(Path(tmp).resolve()/"absolute.jpg")):
                with self.assertRaises(ValueError):atlas.artifact_path(tmp,reference)
    def test_manifest_excludes_wreck_routes_and_duplicate_ids(self):
        manifest=atlas.load_manifest(atlas.DEFAULT_MANIFEST)
        self.assertTrue(manifest["cases"])
        self.assertTrue(all(not atlas.blocked_wreck(c) for c in manifest["cases"] if c.get("status")!="deferred"))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"manifest.json"
            path.write_text(json.dumps({"cases":[{"id":"bad","route":[{"goto":"wreck_run"}]}]}))
            with self.assertRaises(ValueError):atlas.load_manifest(path)
            path.write_text(json.dumps({"cases":[{"id":"same"},{"id":"same"}]}))
            with self.assertRaises(ValueError):atlas.load_manifest(path)

    def test_dedup_keeps_disabled_style_text_and_hover_changes(self):
        value={"menu":{"current_menu":"TEST","selection_index":0},"targets":{"targets":[{"name":"back","rect":[1,2,3,4]}],"clicks":0},
               "nodes":[{"path":"button","rect":[1,2,3,4],"text":"Back","color":{"r":1,"g":1,"b":1}}]}
        initial=atlas.state_signature(value)
        same=copy.deepcopy(value);same["targets"].update(clicks=200,vr_frame={"camera":"moved"})
        self.assertEqual(initial,atlas.state_signature(same))
        for mutation in (lambda v:v["nodes"][0]["color"].update(r=.3),lambda v:v["nodes"][0].update(text="Select"),
                         lambda v:v["targets"].update(hovered="back"),lambda v:v["targets"]["targets"][0].update(enabled=False)):
            changed=copy.deepcopy(value);mutation(changed)
            self.assertNotEqual(initial,atlas.state_signature(changed))

    def test_capture_must_match_process_fresh_session_and_frame(self):
        value={"created_at":50,"geometry":{"session_id":"123-abc-1","supported":True,"frame_id":9}}
        self.assertEqual(atlas.validate_capture(value,123,50),("123-abc-1",9))
        for pid,earliest,previous in ((999,50,None),(123,60,None),(123,50,("123-abc-1",9))):
            with self.assertRaises(atlas.CaptureMismatch):atlas.validate_capture(value,pid,earliest,previous)

    def test_livery_staging_cannot_invoke_editing_input(self):
        game=atlas.NativeGame(0,[])
        for params in ({"op":"save"},{"op":"add"},{"op":"input","action":"validate"},{"op":"input","action":"left"},
                       {"op":"input","action":"tab","repeat":100}):
            with self.assertRaises(ValueError):game.call("livery.command",params)

    def test_source_inventory_includes_unmapped_entries_and_wait_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"Flatout/Code/Sources").mkdir(parents=True);(root/"Flatout/Data/Common/Tuning").mkdir(parents=True)
            (root/"Flatout/Code/Sources/FOMain.h").write_text("enum Menus { E_OPTIONS, E_UNMAPPED, E_WRECK_RUN };")
            (root/"Flatout/Code/Sources/FOMcpBridge.cpp").write_text('{"options",FOMain::E_OPTIONS}')
            (root/"Flatout/Data/Common/Tuning/MessageBoxes.cfg").write_text('MessageBoxes = {\n\tOk =\n\t{ InputMode = MessageBoxInputMode.Ok },\n}\nMessageBoxes.Wait =\n{ InputMode = MessageBoxInputMode.None }')
            catalog=atlas.discover_source(root)
            self.assertEqual(catalog["discovered_count"],5)
            self.assertEqual(catalog["menus"][0]["aliases"],["options"])
            self.assertFalse(next(d for d in catalog["dialogs"] if d["id"]=="Wait")["preview_safe"])
            rows=atlas.source_coverage({"catalog":catalog,"cases":[{"id":"options","source_enums":["E_OPTIONS"],"status":"captured"}]})
            self.assertEqual(next(r for r in rows if r["id"]=="E_OPTIONS")["status"],"captured")
            self.assertEqual(next(r for r in rows if r["id"]=="E_UNMAPPED")["status"],"blocked")
            self.assertEqual(next(r for r in rows if r["id"]=="E_WRECK_RUN")["status"],"deferred")

    def test_report_preserves_pending_coverage_and_escapes_embedded_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            report={"catalog":{"discovered_count":2,"menus":[],"dialogs":[]},"cases":[{"id":"pending","status":"blocked","reason":"Needs a race"}],
                    "states":[],"started":"now","build":{},"danger":"</script><script>alert(1)</script>"}
            atlas.write_report(tmp,report)
            text=(Path(tmp)/"index.html").read_text(encoding="utf-8")
            self.assertNotIn(report["danger"],text)
            self.assertIn("\\u003c/script",text)
            saved=json.loads((Path(tmp)/"report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["cases"]["blocked"],1)
            self.assertFalse(saved["summary"]["whole_game_complete"])
            self.assertIn("Every discovered source entry",text)


class AttachMode(unittest.IsolatedAsyncioTestCase):
    async def test_current_mode_sends_no_native_mutations_or_xr_input(self):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=True)
        report={"cases":[],"states":[]}
        test=atlas.Atlas(args,None,report);calls=[]
        async def query(method,params=None):
            calls.append(method)
            self.assertIn(method,atlas.READ_METHODS)
            return {"game.get_state":{"is_main_menu":False,"state":"race"},"vr.get_state":{"active":True,"can_render":True},
                    "menu.get_state":{"current_menu":""},"game.errors":{"total":0}}[method]
        async def capture(title,*a):calls.append("capture-only")
        test.query=query;test.capture=capture
        await test.run({"cases":[]})
        self.assertEqual(calls.count("capture-only"),1)
        self.assertEqual(report["diagnostics"]["inputs_sent"],0)

    async def test_unknown_modal_is_not_confirmed(self):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1)
        test=atlas.Atlas(args,None,{"cases":[],"states":[]});calls=[]
        async def query(method,params=None):calls.append(method);return {"open":True}
        test.query=query
        with self.assertRaises(atlas.UnknownModal):await test.dismiss()
        self.assertEqual(calls,["ui.get_message_box"])

    async def test_desktop_process_cannot_accept_stale_simulator_capture(self):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=True)
        test=atlas.Atlas(args,None,{"cases":[],"states":[]})
        async def query(method,params=None):return {"active":False,"can_render":False}
        test.query=query
        with self.assertRaises(atlas.CaptureMismatch):await test.run({"cases":[]})


def split_livery_fixture(center=False):
    c,s=math.cos(.45),math.sin(.45)
    camera={"origin":[0,0,0],"right":[1,0,0],"up":[0,0,1],"back":[0,-1,0]}
    base={"origin":[0,3,1.7],"right":[1,0,0],"up":[0,0,1],"back":[0,1,0]}
    left={"origin":[0,3,1.7],"right":[c,s,0],"up":[0,0,1],"back":[-s,c,0]}
    right={"origin":[0,3,1.7],"right":[c,-s,0],"up":[0,0,1],"back":[s,c,0]}
    panels=([{"id":"center","canvas_rect":[0,0,1920,1080],"panel":base,"panel_width":4.6,"panel_height":2.5875}] if center else
            [{"id":"left","canvas_rect":[0,0,540,1080],"panel":left,"panel_width":4.6,"panel_height":2.5875},
             {"id":"right","canvas_rect":[1380,0,1920,1080],"panel":right,"panel_width":4.6,"panel_height":2.5875}])
    return {"ready":True,"vr":{"active":True,"frame":{"camera":camera,"panel":base,"panel_width":4.6,"panel_height":2.5875,
             "left_eye":{"origin":[-.032,1.7,0]},"right_eye":{"origin":[.032,1.7,0]},
             "panels":panels,"layout":"center" if center else "side_panels"},
             "controls":[{"label":"RENAME","enabled":True,"x":1480,"y":200,"width":400,"height":100}]}}


def tracking_fixture(pose):
    rotation=atlas.pose_rotation(pose)
    eyes=[]
    for side in (-1,1):
        position=[pose[k]+rotation[i][0]*side*.032 for i,k in enumerate(("x","y","z"))]
        eyes.append({"tracking_pose":{"position":position,**{k:[rotation[i][j] for i in range(3)] for j,k in enumerate(("x","y","z"))}}})
    return {"active":True,"can_render":True,"eyes":eyes}


def projection_fixture(pose=None):
    pose=pose or dict(x=0,y=1.7,z=0,yaw_deg=0,pitch_deg=0,roll_deg=0)
    # These test captures use yaw only; real historical evidence is tested below.
    angle=math.radians(pose["yaw_deg"])*.5
    eyes=[{"eye":name,"pose":{"position":[offset,1.7,0],"orientation":[0,math.sin(angle),0,math.cos(angle)]},
           "fov":[-1.1,1.1,1,-1],"viewport":[index*1000,0,1000,1000]} for index,(name,offset) in enumerate((("left",-.032),("right",.032)))]
    return {"capture_id":"offline-projection-fixture","image_size":[2000,1000],"crop":[0,0,2000,1000],
            "geometry":{"projection_eyes":eyes,"eyes":eyes}}


class SplitLiveryGeometry(unittest.TestCase):
    def test_overlays_select_each_affine_plane_and_all_four_corners_in_both_eyes(self):
        from flatout_livery_e2e import ui_frame_at,world_pixel
        import numpy as np
        state=split_livery_fixture();capture=projection_fixture()
        # Roll the captured view: a two-corner shortcut no longer encloses the quad.
        for eye in capture["geometry"]["projection_eyes"]:eye["pose"]["orientation"]=[0,0,math.sin(.1),math.cos(.1)]
        targets=[{"name":"left","rect":[0,100,540,350]},{"name":"right","rect":[1380,100,1920,350]}]
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,eye="both"),None,{})
        overlays=test.project_targets(capture,{"menu":{"is_3d":False},"targets":{"vr_frame":state["vr"]["frame"],"targets":targets}})
        self.assertEqual(len(overlays),4)
        for target in targets:
            l,t,r,b=target["rect"];frame=ui_frame_at(state,(l+r)/2,(t+b)/2);panel=frame["panel"]
            for eye in ("left","right"):
                result=next(o for o in overlays if o["name"]==target["name"] and o["eye"]==eye)
                self.assertEqual(result["panel_id"],target["name"])
                points=[]
                for x,y in ((l,t),(r,t),(r,b),(l,b)):
                    world=np.array(panel["origin"])+np.array(panel["right"])*(x/1920-.5)*4.6+np.array(panel["up"])*(.5-y/1080)*2.5875
                    points.append(world_pixel({"vr":{"frame":frame}},capture,world,eye))
                expected=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
                self.assertTrue(np.allclose(result["projected_rect"],expected,atol=.01))
                x0,y0,x1,y1=result["rect"]
                self.assertGreaterEqual(x0,0 if eye=="left" else 999.5)
                self.assertLessEqual(x1,999.5 if eye=="left" else 1999.5)

    def test_offscreen_bounds_do_not_spill_into_the_other_eye(self):
        capture=projection_fixture()
        for eye in capture["geometry"]["projection_eyes"]:eye["fov"]=[-.05,.05,1,-1]
        state=split_livery_fixture()
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,eye="both"),None,{})
        overlays=test.project_targets(capture,{"menu":{"is_3d":False},"targets":{"vr_frame":state["vr"]["frame"],
                                  "targets":[{"name":"left","rect":[40,200,440,300]},{"name":"right","rect":[1480,200,1880,300]}]}})
        self.assertEqual(overlays,[])

    def test_focus_pose_uses_the_side_plane_without_translating_or_tilting_head(self):
        state=split_livery_fixture()
        left=atlas.livery_focus_pose(state,270,540);right=atlas.livery_focus_pose(state,1650,540)
        self.assertGreater(left["yaw_deg"],20);self.assertLess(right["yaw_deg"],-20)
        for pose in (left,right):self.assertEqual([pose[k] for k in ("x","y","z","pitch_deg","roll_deg")],[0,1.7,0,0,0])
        center=atlas.livery_focus_pose(split_livery_fixture(center=True),960,540)
        self.assertAlmostEqual(center["yaw_deg"],0)

    def test_panels_behind_the_head_do_not_get_mirrored_overlay_bounds(self):
        capture=projection_fixture(dict(x=0,y=1.7,z=0,yaw_deg=180,pitch_deg=0,roll_deg=0))
        state=split_livery_fixture()
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,eye="both"),None,{})
        overlays=test.project_targets(capture,{"menu":{"is_3d":False},"targets":{"vr_frame":state["vr"]["frame"],
                                  "targets":[{"name":"left","rect":[40,200,440,300]},{"name":"right","rect":[1480,200,1880,300]}]}})
        self.assertEqual(overlays,[])

    def test_head_restore_retains_nonzero_pitch_roll_and_glances_do_not_dedup(self):
        original=dict(x=.1,y=1.8,z=.05,yaw_deg=17,pitch_deg=-8,roll_deg=3)
        restored=atlas.tracking_head_pose(tracking_fixture(original))
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(original),restored))
        for key in original:self.assertAlmostEqual(original[key],restored[key],places=5)
        snapshot={"livery":split_livery_fixture(),"variant":{"ui":"same-layout","glance":"left"}}
        first=atlas.state_signature(snapshot);snapshot["variant"]["glance"]="right"
        self.assertNotEqual(first,atlas.state_signature(snapshot))


class LiveryGlances(unittest.IsolatedAsyncioTestCase):
    def runner(self,current=False):
        original=dict(x=0,y=1.7,z=0,yaw_deg=5,pitch_deg=-4,roll_deg=2)
        state=split_livery_fixture();shared={"pose":dict(original),"pending":None,"stale":False,"commands":[]}
        class Session:
            async def call_tool(self,name,args):
                if name!="set_head_pose":raise AssertionError(name)
                shared["commands"].append(dict(args));shared["pending"]=dict(args);shared["stale"]=True
                return SimpleNamespace(isError=False)
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=current,hand="right",dwell_frames=3,settle=0),Session(),{})
        async def query(method,params=None):
            if method=="livery.command":return copy.deepcopy(state)
            self.assertEqual(method,"vr.get_state")
            if shared["stale"]:shared["stale"]=False
            elif shared["pending"] is not None:shared["pose"]=shared["pending"];shared["pending"]=None
            return tracking_fixture(shared["pose"])
        test.query=query
        return test,state,shared,original

    async def test_split_views_wait_for_acknowledgement_and_restore_original_pose(self):
        test,state,shared,original=self.runner();captures=[]
        async def capture(title,kind,variant,view):
            self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),view["head_pose"]))
            captures.append(view["panel_id"])
        test.capture=capture
        await test.capture_livery_views("Livery")
        self.assertEqual(captures,["left","right"])
        self.assertEqual(len(shared["commands"]),3)
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),original))

    async def test_centered_overlay_has_one_center_glance(self):
        test,state,shared,original=self.runner();state.update(split_livery_fixture(center=True));captures=[]
        async def capture(title,kind,variant,view):captures.append(view["panel_id"])
        test.capture=capture
        await test.capture_livery_views("Keyboard")
        self.assertEqual(captures,["center"])
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),original))

    async def test_pose_restored_if_panel_capture_fails(self):
        test,state,shared,original=self.runner()
        async def capture(*a,**kw):raise RuntimeError("Offline capture failure")
        test.capture=capture
        with self.assertRaisesRegex(RuntimeError,"Offline capture failure"):await test.capture_livery_views("Livery")
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),original))

    async def test_current_view_does_not_glance_or_send_clicks(self):
        test,state,shared,original=self.runner(current=True);captures=[]
        async def capture(*a,**kw):captures.append(a)
        test.capture=capture
        await test.capture_livery_views("Current livery")
        with self.assertRaises(ValueError):await test.livery_ray("RENAME")
        with self.assertRaises(ValueError):await test.set_livery_head_pose(original)
        self.assertEqual(len(captures),1);self.assertEqual(shared["commands"],[])

    async def test_click_uses_the_correct_full_canvas_surface_after_glance(self):
        from flatout_livery_e2e import ui_surface
        test,state,shared,original=self.runner();clicks=[]
        async def tool(name,args):
            if name=="capture_screenshot":return projection_fixture(shared["pose"]),[]
            self.assertEqual(name,"click_ui_at");clicks.append(args)
            return {"status":"input_delivered"},[]
        test.tool=tool
        await test.livery_ray("RENAME")
        self.assertEqual(len(clicks),1)
        expected=ui_surface(state,1680,250)
        self.assertEqual(clicks[0]["surface"],expected)
        self.assertEqual(clicks[0]["surface"]["size"],[4.6,2.5875])
        self.assertLess(shared["commands"][0]["yaw_deg"],-20)
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),original))


class OwnerQueue(unittest.TestCase):
    def test_queue_retries_brief_response_file_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"requests").mkdir();(root/"responses").mkdir()
            response=root/"responses"/"fixed.json"
            response.write_text(json.dumps({"result":{"id":"1","result":{"active":True}}}))
            read_text=Path.read_text;attempts=[]
            def locked_once(path,*a,**kw):
                if path==response:
                    attempts.append(1)
                    if len(attempts)==1:raise PermissionError("Owner still writing")
                return read_text(path,*a,**kw)
            with mock.patch.object(atlas.uuid,"uuid4",return_value=SimpleNamespace(hex="fixed")),mock.patch.object(Path,"read_text",locked_once):
                result=atlas.OwnerQueueGame(root,[],.3).call("vr.get_state")
            self.assertEqual(result,{"active":True});self.assertGreaterEqual(len(attempts),2)
    def test_queue_retries_partial_json_and_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"requests").mkdir();(root/"responses").mkdir()
            owner_errors=[]
            def owner():
                try:
                    deadline=time.monotonic()+1
                    while not list((root/"requests").glob("*.json")):
                        if time.monotonic()>deadline:raise TimeoutError("No request")
                        time.sleep(.005)
                    request=next((root/"requests").glob("*.json"))
                    value=json.loads(request.read_text())
                    self.assertEqual(value,{"op":"game","method":"menu.get_state","params":{},"settle":0})
                    response=root/"responses"/request.name
                    response.write_text('{"result":')
                    time.sleep(.03)
                    response.write_text('{"result":{"current_menu":"MAIN MENU"}}')
                    request.rename(request.with_suffix(".handled"))
                except Exception as error:owner_errors.append(error)
            thread=threading.Thread(target=owner);thread.start()
            log=[]
            value=atlas.OwnerQueueGame(root,log,.7).call("menu.get_state")
            thread.join(1)
            self.assertEqual(owner_errors,[]);self.assertEqual(value,{"current_menu":"MAIN MENU"})
            self.assertTrue(list((root/"requests").glob("*.handled")));self.assertTrue(list((root/"responses").glob("*.json")))
            self.assertEqual(log[0]["transport"],"owner-queue")

    def test_queue_times_out_and_rejects_mutations_before_enqueueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"requests").mkdir();(root/"responses").mkdir()
            game=atlas.OwnerQueueGame(root,[],.03)
            for method,params in (("menu.back",{}),("menu.go_to",{"menu":"options"}),("hud.pause_menu",{"action":"open"}),
                                  ("livery.command",{"op":"input","action":"cancel"})):
                with self.assertRaises(ValueError):game.call(method,params)
            self.assertEqual(list((root/"requests").iterdir()),[])
            with self.assertRaises(atlas.NativeTimeout):game.call("game.get_state")
            self.assertEqual(len(list((root/"requests").glob("*.json"))),1)

    def test_queue_owner_error_is_reported_without_native_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"requests").mkdir();(root/"responses").mkdir()
            def owner():
                deadline=time.monotonic()+1
                while not list((root/"requests").glob("*.json")) and time.monotonic()<deadline:time.sleep(.005)
                request=next((root/"requests").glob("*.json"))
                (root/"responses"/request.name).write_text(json.dumps({"error":"Owner diagnostic failed"}))
            thread=threading.Thread(target=owner);thread.start()
            try:
                with self.assertRaisesRegex(RuntimeError,"Owner diagnostic failed"):
                    atlas.OwnerQueueGame(root,[],.7).call("game.get_state")
            finally:thread.join(1)


@unittest.skipUnless(os.name=="nt","Native named pipes are Windows-only")
class BoundedPipe(unittest.TestCase):
    def test_partial_response_deadline_returns_without_waiting_for_game(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateNamedPipeW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p)
        kernel.CreateNamedPipeW.restype=wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes=(wintypes.HANDLE,ctypes.c_void_p)
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        kernel.DisconnectNamedPipe.argtypes=(wintypes.HANDLE,)
        kernel.WriteFile.argtypes=(wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
        name=r"\\.\pipe\atlas-bounded-read-test-"+uuid.uuid4().hex
        handle=kernel.CreateNamedPipeW(name,3,0,1,65536,65536,1000,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        release=threading.Event();errors=[]
        def server():
            try:
                if not kernel.ConnectNamedPipe(handle,None) and ctypes.get_last_error()!=535:
                    raise ctypes.WinError(ctypes.get_last_error())
                data=ctypes.create_string_buffer(struct.pack("<I",200));written=wintypes.DWORD()
                if not kernel.WriteFile(handle,data,4,ctypes.byref(written),None):raise ctypes.WinError(ctypes.get_last_error())
                release.wait(2) # Never send the declared body.
            except Exception as error:errors.append(error)
            finally:kernel.DisconnectNamedPipe(handle);kernel.CloseHandle(handle)
        thread=threading.Thread(target=server);thread.start()
        try:
            with open(name,"r+b",buffering=0) as pipe:
                deadline=time.monotonic()+.15
                size=struct.unpack("<I",atlas.bounded_pipe_read(pipe,4,deadline))[0]
                start=time.monotonic()
                with self.assertRaises(atlas.NativeTimeout):atlas.bounded_pipe_read(pipe,size,deadline)
                self.assertLess(time.monotonic()-start,.5)
        finally:release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])


class SavedNativeEvidence(unittest.TestCase):
    def test_derived_modal_inventory_preserves_original_real_native_evidence(self):
        source=PATH.parents[1]/"build-ui-automation/flatout4-evidence/ui-atlas-native-current-r1"
        if not (source/"report.json").exists():self.skipTest("Saved native atlas fixture is not present")
        original=json.loads((source/"report.json").read_text(encoding="utf-8"))
        state=original["states"][0]
        metadata=json.loads((source/state["metadata"]).read_text(encoding="utf-8"))
        if not metadata.get("message",{}).get("open"):self.skipTest("Saved fixture is not modal")
        files=[source/"report.json",source/state["image"],source/state["metadata"]]
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"derived"
            corrected=atlas.reprocess_report(source,target)
            current=corrected["states"][0]
            self.assertEqual([c["name"] for c in current["controls"]],[t["name"] for t in metadata["targets"]["targets"]])
            self.assertTrue(current["background_controls"])
            self.assertTrue(all(c["blocked_by"]=="modal" and c["enabled"] is False for c in current["background_controls"]))
            self.assertFalse(corrected["reprocessing"]["live_capture"])
            self.assertEqual(corrected["reprocessing"]["source_report_sha256"],hashes[source/"report.json"])
            for key in ("image","metadata"):
                self.assertEqual((target/current[key]).read_bytes(),(source/state[key]).read_bytes())
            self.assertEqual(current["provenance"]["source_capture_id"],metadata["capture"]["capture_id"])
        self.assertEqual(hashes,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})

    def test_historical_native_jpeg_and_metadata_report_round_trip(self):
        # Optional local native evidence, never a stand-in for a fresh live run.
        base=PATH.parents[1]/"build-ui-automation/flatout4-evidence/polish-r17c-drivers-full"
        sources=list(base.glob("*-party-drivers-head-height.json"))
        if not sources:self.skipTest("Saved native capture fixture is not present in this checkout")
        source=sources[0];image=source.with_name(source.stem+"-0.jpg")
        if not image.exists():self.skipTest("Saved native JPEG is unavailable")
        capture=json.loads(source.read_text(encoding="utf-8"))[0]
        self.assertTrue(capture["geometry"]["supported"])
        self.assertRegex(capture["geometry"]["session_id"],r"^\d+-\d+-\d+$")
        from PIL import Image
        with Image.open(image) as pixels:self.assertEqual(list(pixels.size),capture["image_size"])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"images").mkdir();(root/"states").mkdir()
            (root/"images/native.jpg").write_bytes(image.read_bytes())
            (root/"states/native.json").write_text(json.dumps({"historical":True,"capture":capture}))
            report={"catalog":{"menus":[],"dialogs":[],"discovered_count":0},"cases":[{"id":"native","status":"captured","reason":"Historical saved native evidence, not a current runtime test"}],
                    "states":[{"id":"native","case":"native","title":"Historical native driver capture","group":"Archive","kind":"historical","historical":True,
                               "image":"images/native.jpg","metadata":"states/native.json","image_size":capture["image_size"],"controls":[]}],
                    "started":"historical","build":{},"mode":"historical saved native evidence"}
            atlas.write_report(root,report)
            saved=json.loads((root/"report.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["states"][0]["historical"])
            self.assertEqual((root/saved["states"][0]["image"]).read_bytes(),image.read_bytes())
            self.assertEqual(json.loads((root/saved["states"][0]["metadata"]).read_text())["capture"]["capture_id"],capture["capture_id"])


if __name__=="__main__":unittest.main()
