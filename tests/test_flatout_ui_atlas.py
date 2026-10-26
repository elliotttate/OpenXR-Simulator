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


class SharedSkinsTitle(unittest.IsolatedAsyncioTestCase):
    def runner(self,case):
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1),None,{})
        test.active_case=case;calls=[]
        async def query(method,params=None):
            calls.append(method)
            if method=="menu.get_state":return {"current_menu":"SKINS"}
            if method=="vr.targets":return {"targets":[{"name":"customization.skin.0"}]}
            if method=="ui.get_message_box":return {"open":False}
            if method=="hud.rects":return {"nodes":[]}
            if method=="livery.command":raise RuntimeError({"code":-1,"message":"Open the livery menu first"})
            raise AssertionError(method)
        test.query=query
        return test,calls

    async def test_customization_does_not_call_livery_just_because_title_is_skins(self):
        test,calls=self.runner({"id":"customization","source_enums":["E_CUSTOMIZATION"]})
        snapshot=await test.observe()
        self.assertNotIn("livery.command",calls)
        self.assertEqual(snapshot["targets"]["targets"][0]["name"],"customization.skin.0")

    async def test_current_unknown_skins_accepts_only_explicit_native_not_livery_answer(self):
        test,calls=self.runner({"id":"current"})
        snapshot=await test.observe()
        self.assertEqual(snapshot["presentation"]["kind"],"customization")
        self.assertNotIn("livery",snapshot)
        self.assertEqual(calls.count("livery.command"),1)

    async def test_expected_livery_never_swallows_not_livery_error(self):
        test,calls=self.runner({"id":"livery","expand":"livery"})
        with self.assertRaisesRegex(RuntimeError,"Open the livery"):await test.observe()

    async def test_other_native_error_is_not_silenced_for_unknown_skins(self):
        test,calls=self.runner({"id":"current"});query=test.query
        async def broken(method,params=None):
            if method=="livery.command":raise RuntimeError({"code":-1,"message":"Native diagnostic failed"})
            return await query(method,params)
        test.query=broken
        with self.assertRaisesRegex(RuntimeError,"Native diagnostic failed"):await test.observe()

    async def test_livery_cleanup_capture_in_garage_does_not_query_closed_editor(self):
        test,calls=self.runner({"id":"livery","expand":"livery","source_enums":["E_LIVERY"]});query=test.query
        async def garage(method,params=None):
            if method=="menu.get_state":return {"current_menu":"GARAGE"}
            return await query(method,params)
        test.query=garage
        await test.observe()
        self.assertNotIn("livery.command",calls)


class LiveryCloseAndScope(unittest.IsolatedAsyncioTestCase):
    def runner(self,overlays_only=True):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=False,settle=0,
                             fixture_timeout=.04,livery_overlays_only=overlays_only)
        test=atlas.Atlas(args,None,{"states":[],"cases":[]});test.active_case={"id":"livery","expand":"livery"}
        data={"menu":"SKINS","dirty":False,"tab":0,"overlay":None,"calls":[],"captures":[],"rays":[],
              "close_mode":"immediate","closing":False,"close_reads":0,"ownership_race":False}
        def state():return {"ready":True,"dirty":data["dirty"],"tab":data["tab"],"keyboard_target":data["overlay"]=="RENAME",
                           "workshop_browser":{"open":data["overlay"]=="BROWSE WORKSHOP"},
                           "exit_requested":data["closing"] and data["close_mode"]=="legacy"}
        async def query(method,params=None):
            data["calls"].append((method,copy.deepcopy(params)))
            if method=="menu.get_state":
                if data["closing"] and data["close_mode"]=="delayed":
                    data["close_reads"]+=1
                    data["menu"]="" if data["close_reads"]==1 else "GARAGE"
                return {"current_menu":data["menu"]}
            if method=="ui.get_message_box":return {"open":False}
            self.assertEqual(method,"livery.command")
            if data["menu"]!="SKINS":raise AssertionError("Closed editor must not be queried")
            if params["op"]=="state":
                if data["ownership_race"] and data["closing"]:
                    data["menu"]="GARAGE"
                    raise RuntimeError({"code":-1,"message":"Open the livery menu first"})
                return state()
            if params["op"]=="catalog":return {"categories":2}
            if params["op"]=="input":
                if params["action"]=="tab":data["tab"]=(data["tab"]+1)%3
                elif data["overlay"]:data["overlay"]=None
                else:
                    data["closing"]=True
                    if data["close_mode"]=="immediate":data["menu"]="GARAGE"
                    elif data["close_mode"]=="unexpected":data["menu"]="MAIN MENU"
                return {"success":True}
            if params["op"] in {"view","browse"}:return {"success":True}
            raise AssertionError(params)
        async def capture(title,*a,**kw):data["captures"].append(title)
        async def fixture():return state()
        async def ray(label):
            data["rays"].append(label)
            if label=="DISCARD":data["menu"]="GARAGE"
            else:data["overlay"]=label
        test.query=query;test.capture=capture;test.capture_livery_views=capture;test.livery_fixture=fixture;test.livery_ray=ray
        return test,data

    async def test_clean_cancel_closes_to_garage_without_another_livery_read(self):
        test,data=self.runner()
        await test.close_livery()
        self.assertEqual(data["menu"],"GARAGE")
        self.assertTrue(test.active_case["fixture_checks"][-1]["clean_livery_close"])
        self.assertIn("Livery — returned to Garage",data["captures"])

    async def test_close_allows_bounded_intermediate_blank_menu(self):
        test,data=self.runner();data["close_mode"]="delayed"
        await test.close_livery()
        self.assertEqual(data["menu"],"GARAGE")
        self.assertEqual(data["close_reads"],2)

    async def test_close_handles_ownership_changing_between_menu_and_state_read(self):
        test,data=self.runner();data.update(close_mode="waiting",ownership_race=True)
        await test.close_livery()
        self.assertTrue(test.active_case["fixture_checks"][-1]["clean_livery_close"])

    async def test_dirty_document_is_never_cancelled_or_discarded(self):
        test,data=self.runner();data["dirty"]=True
        with self.assertRaises(atlas.UnknownModal):await test.close_livery()
        self.assertFalse(data["closing"]);self.assertEqual(data["rays"],[])

    async def test_unfinished_close_has_a_deadline_and_never_repeats_cancel(self):
        test,data=self.runner();data["close_mode"]="waiting"
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"Cancel was not repeated"):await test.close_livery()
        self.assertEqual(sum(p=={"op":"input","action":"cancel"} for m,p in data["calls"]),1)
        self.assertEqual(data["rays"],[])

    async def test_unexpected_menu_blocks_without_recovery(self):
        test,data=self.runner();data["close_mode"]="unexpected"
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"unexpected menu"):await test.close_livery()
        self.assertFalse(any(m in {"menu.back","menu.go_to","menu.select"} for m,p in data["calls"]))

    async def test_clean_legacy_confirmation_is_captured_and_discarded_once(self):
        test,data=self.runner();data["close_mode"]="legacy"
        await test.close_livery()
        self.assertEqual(data["rays"],["DISCARD"])
        self.assertTrue(test.active_case["fixture_checks"][-1]["clean_legacy_discard"])

    async def test_overlay_only_scope_skips_view_tab_and_catalog_galleries(self):
        test,data=self.runner()
        await test.livery(test.active_case)
        self.assertEqual(data["captures"],["Livery — editor columns","Livery — large keyboard","Livery — Workshop browser","Livery — returned to Garage"])
        self.assertEqual(test.active_case["scope"]["mode"],"overlays-only")
        self.assertEqual(len(test.active_case["scope"]["omitted"]),3)
        self.assertFalse(any(m=="livery.command" and p["op"] in {"view","browse","catalog"} for m,p in data["calls"]))
        self.assertEqual(data["rays"],["RENAME","BROWSE WORKSHOP"])

    async def test_default_full_scope_keeps_all_gallery_loops(self):
        test,data=self.runner(overlays_only=False)
        # Camera-view settling is unrelated to this offline route contract.
        original_sleep=asyncio.sleep
        async def fast_settle(delay):await original_sleep(0)
        with mock.patch.object(atlas.asyncio,"sleep",side_effect=fast_settle):await test.livery(test.active_case)
        self.assertEqual(test.active_case["scope"]["mode"],"full-livery")
        self.assertEqual(test.active_case["scope"]["omitted"],[])
        self.assertEqual(len([p for m,p in data["calls"] if m=="livery.command" and p["op"]=="view"]),7)
        self.assertEqual(len([p for m,p in data["calls"] if m=="livery.command" and p["op"]=="browse"]),2)


class RetainedLeaderboardPage(unittest.IsolatedAsyncioTestCase):
    def runner(self):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=False,settle=0)
        test=atlas.Atlas(args,None,{"cases":[],"states":[]});test.active_case={"id":"leaderboards"}
        data={"selected":24,"capable":{25},"events":[24,25],"menu":"INSANITY","rays":[],"captures":[],"pages":[],"read_error":0}
        async def query(method,params=None):
            if method=="vr.targets":return {"targets":[{"name":"challenge.event."+str(i)} for i in data["events"]]+
                                           ([{"name":"helper.Secondary.1"}] if data["selected"] in data["capable"] else [])}
            if method=="menu.get_state":return {"current_menu":data["menu"],"selection_index":data["selected"]}
            if method=="leaderboard.get_state":return {"busy":False,"read_error":data["read_error"]}
            if method=="ui.get_message_box":return {"open":False}
            raise AssertionError(method)
        async def ray(name,hover=False):
            data["rays"].append((name,hover))
            if name.startswith("challenge.event."):data["selected"]=int(name.split(".")[-1])
            elif name=="helper.Secondary.1":data["menu"]="LEADERBOARD"
            else:raise AssertionError(name)
        async def capture(title,*a):data["captures"].append(title)
        async def pages(*a):data["pages"].append(a)
        test.query=query;test.ray=ray;test.capture=capture;test.pages=pages
        return test,data

    async def test_current_exposed_events_are_used_and_unsupported_card_is_skipped(self):
        test,data=self.runner()
        await test.leaderboard()
        self.assertEqual(data["rays"],[("challenge.event.24",True),("challenge.event.25",True),("helper.Secondary.1",False)])
        self.assertEqual(data["pages"],[("Leaderboard","page.down",64)])

    async def test_no_capable_event_captures_blocked_explanation_without_clicking_start(self):
        test,data=self.runner();data["capable"]=set()
        with self.assertRaisesRegex(atlas.CaseBlocked,"current FlatOut Mode page"):await test.leaderboard()
        self.assertIn("Leaderboard — unavailable for exposed events",data["captures"])
        self.assertTrue(all(hover for name,hover in data["rays"]))

    async def test_missing_event_targets_are_blocked_instead_of_false_interaction_failure(self):
        test,data=self.runner();data["events"]=[]
        with self.assertRaises(atlas.CaseBlocked):await test.leaderboard()
        self.assertEqual(data["rays"],[]);self.assertEqual(len(data["captures"]),1)

    async def test_native_read_error_is_reported_as_unavailable_fixture(self):
        test,data=self.runner();data["read_error"]=7
        with self.assertRaisesRegex(atlas.CaseBlocked,"native error 7"):await test.leaderboard()
        self.assertIn("Leaderboard — read unavailable",data["captures"])
        self.assertEqual(data["pages"],[])

    async def test_case_blocked_status_is_retained_and_later_safe_case_still_runs(self):
        test,data=self.runner()
        test.args.only=None;test.args.fixture=[];test.args.dialog_templates=False;test.args.fail_fast=False
        steps=[]
        async def query(method,params=None):
            return {"game.get_state":{"is_main_menu":True},"vr.get_state":tracking_fixture(dict(x=0,y=1.7,z=0,yaw_deg=0,pitch_deg=0,roll_deg=0)),
                    "ui.get_message_box":{"open":False},"game.errors":{"total":0},"menu.get_state":{"current_menu":"MAIN MENU"}}[method]
        async def main():pass
        async def stage(case):steps.append(case["id"])
        async def expand(case):
            if case["id"]=="leaderboards":raise atlas.CaseBlocked("Unavailable event/service fixture")
        test.query=query;test.main=main;test.stage=stage;test.expand=expand
        with tempfile.TemporaryDirectory() as tmp:
            test.output=Path(tmp)
            await test.run({"cases":[{"id":"leaderboards"},{"id":"next"}]})
        self.assertEqual(steps,["leaderboards","next"])
        self.assertEqual([c["status"] for c in test.report["cases"]],["blocked","captured"])


class PartyDriverRecipe(unittest.IsolatedAsyncioTestCase):
    """Offline navigation contracts; retained pages come from the saved native r18 report."""
    def runner(self):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=False,settle=0,fixture_timeout=.03)
        test=atlas.Atlas(args,None,{"cases":[],"states":[]});test.active_case={"id":"party-drivers"}
        data={"phase":"players","playlist":[],"clicks":[],"flows":[],"toggle_works":True,"start_works":True,"tiles":True}
        async def normal_flow(name):
            self.assertEqual(name,"party_game");data["flows"].append(name)
        async def query(method,params=None):
            if method=="party.get_state":
                return {"open":data["phase"]=="players","stunt_picker_open":data["phase"]=="stunts",
                        "players":8,"visible":[8,11],"playlist":[{"index":i} for i in data["playlist"]]}
            if method=="menu.get_state":return {"current_menu":"DRIVERS" if data["phase"]=="drivers" else "OFFLINE"}
            if method=="vr.targets":return {"targets":[{"name":"helper.Validate.0"},
                {"name":"stunts.page.previous"}]+([{"name":"stunt.0","enabled":False},{"name":"stunt.8"},{"name":"stunt.9"}] if data["tiles"] else [])}
            raise AssertionError(method)
        async def ray(name,*args,**kwargs):
            data["clicks"].append(name)
            if name=="players.8":data["phase"]="stunts"
            elif name=="stunt.8":
                if data["toggle_works"]:data["playlist"]=[8]
            elif name=="helper.Validate.0":
                if data["start_works"]:data["phase"]="drivers"
            else:raise AssertionError("Unexpected invisible target: "+name)
        test.normal_flow=normal_flow;test.query=query;test.ray=ray
        return test,data

    def test_default_manifest_uses_retained_page_aware_party_fixture(self):
        case=next(c for c in atlas.load_manifest(atlas.DEFAULT_MANIFEST)["cases"] if c["id"]=="party-drivers")
        self.assertEqual(case["route"],[{"party_drivers":8}])

    async def test_retained_third_page_chooses_exposed_stunt_eight_and_starts_once(self):
        test,data=self.runner()
        await test.party_drivers(8)
        self.assertEqual(data["flows"],["party_game"])
        self.assertEqual(data["clicks"],["players.8","stunt.8","helper.Validate.0"])
        self.assertEqual(test.active_case["fixture_checks"][0]["stunt_picker_visible"],[8,11])

    async def test_dirty_initial_playlist_is_never_toggled_or_started(self):
        test,data=self.runner();data["playlist"]=[5]
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"empty playlist"):await test.party_drivers(8)
        self.assertEqual(data["clicks"],[])

    async def test_unconfirmed_tile_toggle_never_sends_start(self):
        test,data=self.runner();data["toggle_works"]=False
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"exactly the selected"):await test.party_drivers(8)
        self.assertEqual(data["clicks"],["players.8","stunt.8"])

    async def test_no_visible_tiles_block_without_start(self):
        test,data=self.runner();data["tiles"]=False
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"no exposed enabled tile"):await test.party_drivers(8)
        self.assertEqual(data["clicks"],["players.8"])

    async def test_failed_start_has_a_deadline_and_is_not_repeated(self):
        test,data=self.runner();data["start_works"]=False
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"no retry or second Start"):await test.party_drivers(8)
        self.assertEqual(data["clicks"].count("helper.Validate.0"),1)

    async def test_current_attachment_cannot_stage_party_driver_fixture(self):
        test,data=self.runner();test.args.current=True
        with self.assertRaises(ValueError):await test.party_drivers(8)
        self.assertEqual(data["flows"],[]);self.assertEqual(data["clicks"],[])


class NormalMenuFixtures(unittest.IsolatedAsyncioTestCase):
    """Offline callback/order models, not rendered evidence or a native flow proof."""
    def runner(self,**changes):
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=False,settle=0,
                             fixture_timeout=.08,fixture=["showroom","career"],only=None,fail_fast=False,dialog_templates=False)
        for k,v in changes.items():setattr(args,k,v)
        test=atlas.Atlas(args,None,{"cases":[],"states":[]})
        test.active_case={"id":"fixture","group":"Garage","states":[]}
        data={"menu":"MAIN MENU","mode":"unknown","career_mode":"career","selection":0,"owned":True,"caption":True,
              "garage_loading":0,"livery_checks":0,"bound":True,"career_count":2,"actions":[],"reads":[]}
        counts={"MAIN MENU":8,"FLATOUT":2,"GARAGE":4,"SKINS":0,"CARS":6,"UPGRADES":5,"MULTIPLAYER":2,"OFFLINE":0}
        async def query(method,params=None):
            data["reads"].append(method)
            if method=="ui.get_message_box":return {"open":data["menu"]=="WELCOME"}
            if method=="game.get_state":return {"game_mode":data["mode"],"is_main_menu":True}
            if method=="vr.get_state":return tracking_fixture(dict(x=0,y=1.7,z=0,yaw_deg=0,pitch_deg=0,roll_deg=0))
            if method=="game.errors":return {"total":0}
            if method=="menu.get_state":
                count=data["career_count"] if data["menu"]=="FLATOUT" else counts.get(data["menu"],0)
                return {"current_menu":data["menu"],"selection_index":data["selection"],"item_count":count,
                        "is_3d":data["menu"] in {"MAIN MENU","FLATOUT","MULTIPLAYER"},
                        "items":[{"index":i,"title":"item"+str(i),"locked":False} for i in range(count)]}
            if method=="hud.rects":
                nodes=[]
                if data["caption"] and data["menu"]=="FLATOUT":nodes.append({"visible":True,"path":"Menu/Info_3D_C/TXT_Value","text":"Owned fixture car"})
                if data["menu"]=="GARAGE" and data["garage_loading"]:
                    data["garage_loading"]-=1;nodes.append({"visible":True,"path":"Menu/ModelLoading/Cog"})
                return {"nodes":nodes}
            if method=="menu.navigate":
                data["actions"].append((method,params["direction"]))
                data["selection"]+=1;return {"success":True}
            if method=="menu.back":
                data["actions"].append((method,data["menu"]))
                data["menu"]="MAIN MENU";data["selection"]=0;return {"success":True}
            if method=="menu.select":
                before,index=data["menu"],data["selection"];data["actions"].append((method,before,index));data["selection"]=0
                if before=="MAIN MENU" and index==0:data["mode"]=data["career_mode"];data["menu"]="FLATOUT" if data["owned"] else "WELCOME"
                elif before=="FLATOUT" and index==1:data["menu"]="GARAGE"
                elif before=="GARAGE":data["menu"]={0:"CARS",1:"UPGRADES",2:"SKINS",3:"SKINS"}[index]
                elif before=="MAIN MENU" and index==4:data["menu"]="MULTIPLAYER"
                elif before=="MULTIPLAYER" and index==1:data["mode"]="party_game";data["menu"]="OFFLINE"
                else:raise AssertionError((before,index))
                return {"success":True}
            if method=="livery.command":
                self.assertEqual(params,{"op":"state"});data["livery_checks"]+=1
                bound=data["bound"] and data["livery_checks"]>=2
                return {"ready":True,"bindings":28 if bound else 0,"projection_primitives":26229 if bound else 0,
                        "vr":{"active":True,"controls":[{"id":1}]}}
            raise AssertionError("Unexpected/unsafe native operation: "+method)
        test.query=query
        return test,data

    def test_direct_showroom_opens_are_rejected_before_pipe_access(self):
        game=atlas.NativeGame(0,[])
        for name in atlas.NORMAL_FLOWS:
            with self.assertRaisesRegex(ValueError,"normal parent callback"):game.call("menu.go_to",{"menu":name})

    def test_implicit_fixture_requirements_apply_to_old_and_custom_recipes(self):
        for alias in atlas.PROFILE_FLOWS:
            self.assertEqual(atlas.case_fixture_requirements({"route":[{"goto":alias}]}),{"showroom"})
        self.assertEqual(atlas.case_fixture_requirements({"route":[{"goto":"options"}]}),set())
        self.assertEqual(atlas.case_fixture_requirements({"route":[{"goto":"filter"}]}),{"network"})

    async def test_livery_uses_normal_callbacks_and_waits_for_real_bind_counts(self):
        test,data=self.runner();data["garage_loading"]=2
        await test.stage({"route":[{"goto":"livery"}],"expect":["SKINS"]})
        self.assertEqual([a for a in data["actions"] if a[0]=="menu.select"],
                         [("menu.select","MAIN MENU",0),("menu.select","FLATOUT",1),("menu.select","GARAGE",3)])
        self.assertGreaterEqual(data["livery_checks"],2)
        self.assertEqual(data["garage_loading"],0)
        checks=test.active_case["fixture_checks"]
        self.assertEqual(checks[0]["car_caption"],"Owned fixture car")
        self.assertEqual(checks[-1]["bindings"],28)
        self.assertNotIn("menu.go_to",data["reads"])

    async def test_missing_owned_car_is_blocked_without_confirming_or_opening_garage(self):
        test,data=self.runner();data["owned"]=False
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"real entry modal"):
            await test.stage({"route":[{"goto":"livery"}]})
        self.assertEqual(data["menu"],"WELCOME")
        self.assertEqual(data["actions"],[("menu.select","MAIN MENU",0)])
        self.assertNotIn("ui.confirm_message_box",data["reads"])

    async def test_wrong_mode_blocks_even_if_car_caption_and_menu_look_ready(self):
        test,data=self.runner();data["career_mode"]="unknown"
        with self.assertRaisesRegex(atlas.FixtureUnavailable,'"game_mode": "unknown"'):
            await test.stage({"route":[{"goto":"livery"}]})
        self.assertEqual(data["actions"],[("menu.select","MAIN MENU",0)])

    async def test_all_showroom_aliases_use_their_own_garage_callback(self):
        for alias in ("cars","upgrade","customization"):
            with self.subTest(alias=alias):
                test,data=self.runner()
                await test.stage({"route":[{"goto":alias}]})
                self.assertEqual([a for a in data["actions"] if a[0]=="menu.select"],
                                 [("menu.select","MAIN MENU",0),("menu.select","FLATOUT",1),
                                  ("menu.select","GARAGE",atlas.SHOWROOM_FLOWS[alias][0])])
                self.assertNotIn("menu.go_to",data["reads"])

    async def test_declared_fixture_does_not_replace_positive_car_readiness(self):
        test,data=self.runner();data["caption"]=False
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"did not become ready"):
            await test.stage({"route":[{"goto":"livery"}]})
        self.assertEqual(data["menu"],"FLATOUT")
        self.assertEqual(data["actions"],[("menu.select","MAIN MENU",0)])

    async def test_unfinished_garage_loading_blocks_before_editor(self):
        test,data=self.runner();data["garage_loading"]=1000
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"ModelLoading"):
            await test.stage({"route":[{"goto":"livery"}]})
        self.assertEqual(data["menu"],"GARAGE")
        self.assertEqual(data["livery_checks"],0)

    async def test_zero_livery_binding_is_fatal_and_never_recovers_through_another_menu(self):
        with tempfile.TemporaryDirectory() as tmp:
            test,data=self.runner(output=Path(tmp));data["bound"]=False
            cases=[{"id":"livery","route":[{"goto":"livery"}]},{"id":"garage","route":[{"goto":"garage"}]}]
            with self.assertRaisesRegex(atlas.FixtureUnavailable,"did not bind"):
                await test.run({"cases":cases})
            self.assertEqual(data["menu"],"SKINS")
            self.assertEqual(len(test.report["cases"]),1)
            self.assertEqual(test.report["cases"][0]["status"],"blocked")
            self.assertFalse(any(a[0]=="menu.back" for a in data["actions"]))
            self.assertEqual(sum(a==("menu.select","FLATOUT",1) for a in data["actions"]),1)

    async def test_active_cup_is_never_continued_or_cancelled(self):
        test,data=self.runner();data["career_count"]=3
        with self.assertRaisesRegex(atlas.FixtureUnavailable,"active cup"):
            await test.stage({"route":[{"goto":"class_selection"}]})
        self.assertEqual(data["actions"],[("menu.select","MAIN MENU",0)])

    async def test_party_callback_sets_mode_without_online_match_creation(self):
        test,data=self.runner()
        await test.stage({"route":[{"goto":"party_game"}],"expect":["OFFLINE"]})
        self.assertEqual(data["mode"],"party_game")
        self.assertIn(("menu.select","MULTIPLAYER",1),data["actions"])
        self.assertNotIn(("menu.select","MULTIPLAYER",0),data["actions"])

    async def test_current_mode_cannot_enter_a_normal_flow(self):
        test,data=self.runner(current=True)
        with self.assertRaises(ValueError):await test.stage({"route":[{"goto":"livery"}]})
        with self.assertRaises(ValueError):await test.normal_flow("livery")
        self.assertEqual(data["reads"],[])

    async def test_dead_native_pipe_becomes_fatal_instead_of_retrying_staging(self):
        test,data=self.runner()
        with mock.patch.object(test.game,"call",side_effect=FileNotFoundError("closed native process")):
            with self.assertRaisesRegex(atlas.NativeTimeout,"process/pipe is unavailable"):
                await atlas.Atlas.query(test,"menu.get_state")


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
            if method=="livery.command":
                value=copy.deepcopy(state)
                eyes=tracking_fixture(shared["pose"])["eyes"]
                for eye,key in zip(eyes,("left_eye","right_eye")):
                    native=eye["tracking_pose"]
                    value["vr"]["frame"][key]={"origin":native["position"],"right":native["x"],"up":native["y"],"back":native["z"]}
                return value
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
            if name=="release_ui_pointer":return {"status":"input_delivered","delivery":{"button_down":False}},[]
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

    async def test_absolute_pose_redelivery_recovers_dropped_first_command(self):
        test,state,shared,original=self.runner();test.args.head_pose_timeout=.2;test.args.head_redelivery_interval=.02
        commands=[]
        async def delivery(name,args):
            commands.append(dict(args))
            if len(commands)==2:shared["pose"]=dict(args)
            return SimpleNamespace(isError=False)
        test.session.call_tool=delivery
        requested={**original,"yaw_deg":35}
        await test.set_livery_head_pose(requested)
        self.assertEqual(commands,[requested,requested])
        move=test.report["head_moves"][-1]
        self.assertTrue(move["acknowledged"]);self.assertEqual(move["retries"],1)
        self.assertAlmostEqual(move["observed_pose"]["yaw_deg"],35)

    async def test_never_acknowledged_commands_are_bounded_and_report_actual_pose(self):
        test,state,shared,original=self.runner();test.args.head_pose_timeout=.045;test.args.head_redelivery_interval=.01
        commands=[]
        async def lost(name,args):commands.append(dict(args));return SimpleNamespace(isError=False)
        test.session.call_tool=lost
        with self.assertRaisesRegex(atlas.NativeTimeout,"observed="):
            await test.set_livery_head_pose({**original,"yaw_deg":35})
        self.assertEqual(len(commands),3)
        move=test.report["head_moves"][-1]
        self.assertFalse(move["acknowledged"])
        self.assertAlmostEqual(move["observed_pose"]["yaw_deg"],original["yaw_deg"])

    async def test_queue_error_retains_exact_native_transport_diagnostic(self):
        test,state,shared,original=self.runner()
        message="[WinError 5] Access is denied: 'owned.tmp' -> 'head_pose_command.json'"
        async def denied(name,args):return SimpleNamespace(isError=True,content=[SimpleNamespace(type="text",text=message)])
        test.session.call_tool=denied
        with self.assertRaisesRegex(RuntimeError,"WinError 5"):await test.set_livery_head_pose(original)
        move=test.report["head_moves"][-1]
        self.assertEqual(move["deliveries"][0]["error"],message)
        self.assertFalse(move["acknowledged"])

    async def test_acknowledged_native_pose_cannot_authorize_stale_capture_geometry(self):
        test,state,shared,original=self.runner();clicks=[]
        async def stale(name,args):
            if name=="capture_screenshot":return projection_fixture(),[]
            clicks.append(name);raise AssertionError("Stale image must not send ray input")
        test.tool=stale
        with self.assertRaises(atlas.CaptureMismatch):await test.livery_ray("RENAME")
        self.assertEqual(clicks,[])
        self.assertTrue(atlas.head_pose_matches(tracking_fixture(shared["pose"]),original))


class OrdinaryMenuFraming(unittest.IsolatedAsyncioTestCase):
    """Offline pose/action and rejection contracts; no generated pixels prove native rendering."""
    def runner(self):
        original=dict(x=.17,y=1.68,z=-.08,yaw_deg=33.437751,pitch_deg=-7,roll_deg=2)
        data={"pose":dict(original),"commands":[],"modal":False,"selection":2,"menu":"GARAGE","actions":[]}
        class Session:
            async def call_tool(self,name,args):
                if name!="set_head_pose":raise AssertionError(name)
                data["commands"].append(dict(args));data["pose"]=dict(args)
                return SimpleNamespace(isError=False)
        args=SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=False,settle=0,eye="both",width=2048)
        test=atlas.Atlas(args,Session(),{"cases":[],"states":[]})
        test.active_case={"id":"customization","group":"Garage","states":[]}
        async def query(method,params=None):
            if method=="vr.get_state":return tracking_fixture(data["pose"])
            if method=="ui.get_message_box":return {"open":data["modal"]}
            if method=="menu.get_state":return {"current_menu":data["menu"],"selection_index":data["selection"],
                "item_count":4,"items":[{"index":i,"title":"Item "+str(i)} for i in range(4)]}
            if method=="menu.select":
                self.assertTrue(atlas.head_pose_matches(tracking_fixture(data["pose"]),test.neutral_head_pose))
                data["actions"].append((method,dict(data["pose"])));data["menu"]="SKINS"
                return {"success":True}
            raise AssertionError(method)
        test.query=query
        test.initialize_framing(tracking_fixture(original))
        return test,data,original

    async def test_stage_neutralizes_before_menu_opening_and_preserves_tracked_position(self):
        test,data,original=self.runner()
        async def main():
            self.assertTrue(atlas.head_pose_matches(tracking_fixture(data["pose"]),test.neutral_head_pose))
        test.main=main
        await test.stage({"route":[{"select":2}],"expect":["SKINS"]})
        self.assertEqual(len(data["commands"]),1)
        self.assertEqual([data["commands"][0][k] for k in ("yaw_deg","pitch_deg","roll_deg")],[0,0,0])
        for key in ("x","y","z"):self.assertAlmostEqual(data["commands"][0][key],original[key])
        self.assertEqual(data["actions"][0][0],"menu.select")

    async def test_livery_reference_is_opened_neutral_before_any_per_plane_glance(self):
        test,data=NormalMenuFixtures().runner();native_query=test.query
        original=dict(x=.1,y=1.7,z=0,yaw_deg=33,pitch_deg=4,roll_deg=-2)
        tracked={"pose":dict(original),"opening":[]}
        class Session:
            async def call_tool(self,name,args):tracked["pose"]=dict(args);return SimpleNamespace(isError=False)
        test.session=Session();test.initialize_framing(tracking_fixture(original))
        async def query(method,params=None):
            if method=="vr.get_state":return tracking_fixture(tracked["pose"])
            if method=="menu.select" and data["menu"]=="GARAGE" and data["selection"]==3:
                tracked["opening"].append(dict(tracked["pose"]))
            return await native_query(method,params)
        test.query=query
        await test.stage({"route":[{"goto":"livery"}],"expect":["SKINS"]})
        self.assertEqual(len(tracked["opening"]),1)
        self.assertEqual([tracked["opening"][0][k] for k in ("yaw_deg","pitch_deg","roll_deg")],[0,0,0])
        self.assertAlmostEqual(tracked["opening"][0]["x"],original["x"])

    async def test_stale_native_glance_cannot_accept_an_ordinary_menu_screenshot(self):
        test,data,original=self.runner();data["pose"]["yaw_deg"]=-35
        async def observe():return {"menu":{"current_menu":"SKINS"},"targets":{"targets":[]},"nodes":[],"message":{"open":False}}
        async def tool(*a):raise AssertionError("A stale native pose must stop before screenshot acquisition")
        test.observe=observe;test.tool=tool
        with self.assertRaisesRegex(atlas.CaptureMismatch,"native head pose drifted"):await test.capture("Historical clipped-menu regression")
        self.assertEqual(test.report["states"],[])

    async def test_stale_submitted_glance_is_rejected_even_after_native_neutral_ack(self):
        test,data,original=self.runner();data["pose"]=dict(test.neutral_head_pose)
        async def observe():return {"menu":{"current_menu":"SKINS"},"targets":{"targets":[]},"nodes":[],"message":{"open":False}}
        stale=projection_fixture({**test.neutral_head_pose,"yaw_deg":33.437751})
        native=tracking_fixture(test.neutral_head_pose)
        for eye,tracked in zip(stale["geometry"]["projection_eyes"],native["eyes"]):
            eye["pose"]["position"]=tracked["tracking_pose"]["position"]
        # Translation agrees exactly; the rejection must detect stale rotation.
        async def tool(*a):return stale,[]
        test.observe=observe;test.tool=tool
        with tempfile.TemporaryDirectory() as tmp:
            test.output=Path(tmp);(test.output/"images").mkdir();(test.output/"states").mkdir()
            with self.assertRaises(atlas.CaptureMismatch):await test.capture("Rejected stale compositor view")
            self.assertEqual(list((test.output/"images").iterdir()),[])
        self.assertEqual(test.report["states"],[])

    async def test_success_and_failure_restore_the_original_attachment_pose(self):
        for fails in (False,True):
            with self.subTest(fails=fails):
                test,data,original=self.runner()
                async def body(manifest):
                    await test.prepare_neutral_frame("test route")
                    await test.set_livery_head_pose({**test.neutral_head_pose,"yaw_deg":-40})
                    if fails:raise ValueError("Original case failure")
                test._run=body
                if fails:
                    with self.assertRaisesRegex(ValueError,"Original case failure"):await test.run({})
                else:await test.run({})
                self.assertTrue(atlas.head_pose_matches(tracking_fixture(data["pose"]),original))
                self.assertTrue(test.report["framing"]["restore"]["acknowledged"])
                self.assertEqual(data["commands"][-1],test.attachment_head_pose)

    async def test_restore_failure_is_reported_without_masking_the_original_error(self):
        test,data,original=self.runner()
        async def body(manifest):raise atlas.UnknownModal("Unresolved real dialog")
        async def restore():raise atlas.NativeTimeout("Native process unavailable during restore")
        test._run=body;test.restore_attachment_frame=restore
        with self.assertRaisesRegex(atlas.UnknownModal,"Unresolved real dialog"):await test.run({})
        self.assertIn("Native process unavailable",test.report["framing"]["restore_error"])

    async def test_midroute_unknown_modal_prevents_neutral_input_or_confirmation(self):
        test,data,original=self.runner();data["modal"]=True
        with self.assertRaises(atlas.UnknownModal):await test.prepare_neutral_frame("blocked route")
        self.assertEqual(data["commands"],[]);self.assertEqual(data["actions"],[])

    async def test_initial_unknown_modal_keeps_original_pose_and_captures_without_framing(self):
        test,data,original=self.runner()
        test.attachment_head_pose=None;test.neutral_head_pose=None;data["modal"]=True;captures=[]
        query=test.query
        async def initial(method,params=None):
            if method=="game.get_state":return {"is_main_menu":True}
            return await query(method,params)
        async def capture(*a):captures.append(dict(data["pose"]))
        test.query=initial;test.capture=capture
        with self.assertRaises(atlas.UnknownModal):await test.run({"cases":[]})
        self.assertEqual(captures,[original]);self.assertEqual(data["commands"],[])

    def test_framing_policy_is_part_of_dedup_identity(self):
        snapshot={"menu":{"current_menu":"SKINS"},"framing":{"kind":"ordinary neutral","pose":{"yaw_deg":0}}}
        before=atlas.state_signature(snapshot);snapshot["framing"]["pose"]["yaw_deg"]=33.437751
        self.assertNotEqual(before,atlas.state_signature(snapshot))


class PointerLifecycle(unittest.IsolatedAsyncioTestCase):
    def runner(self,current=False):
        test=atlas.Atlas(SimpleNamespace(pid=1,output=Path("unused"),native_timeout=.1,current=current,hover="all"),None,{})
        calls=[]
        async def tool(name,args):
            calls.append((name,args.copy()))
            return {"status":"input_delivered","delivery":{"button_down":False}},[]
        test.tool=tool
        return test,calls

    async def test_release_clears_only_owned_hands_using_fresh_session_capture(self):
        test,calls=self.runner();test.owned_pointers={"right":"old-aim"};test.capture_cache={"capture_id":"latest-native"}
        await test.release_owned_pointers();await test.release_owned_pointers()
        self.assertEqual(calls,[("release_ui_pointer",{"capture_id":"latest-native","hand":"right","timeout":30})])
        self.assertEqual(test.owned_pointers,{})
        self.assertTrue(test.report["pointer_releases"][0]["acknowledged"])

    async def test_hover_state_is_captured_before_its_pointer_is_released(self):
        test,calls=self.runner()
        async def query(method,params=None):return {"targets":[{"name":"button","enabled":True}]}
        async def ray(name,hover=False):test.owned_pointers["right"]="source";calls.append(("hover",name))
        async def capture(*a):
            self.assertIn("right",test.owned_pointers);calls.append(("capture",a[0]))
        test.query=query;test.ray=ray;test.capture=capture
        await test.variants("Menu")
        self.assertEqual([c[0] for c in calls],["hover","capture","release_ui_pointer"])
        self.assertEqual(test.owned_pointers,{})

    async def test_unacknowledged_release_stops_before_main_navigation(self):
        test,calls=self.runner();test.owned_pointers={"right":"source"};native=[]
        async def tool(*a):return {"status":"error"},[]
        async def query(*a):native.append(a);raise AssertionError("Must not navigate after failed release")
        test.tool=tool;test.query=query
        with self.assertRaises(atlas.NativeTimeout):await test.main()
        self.assertEqual(native,[])

    async def test_readonly_attachment_never_sends_a_release(self):
        test,calls=self.runner(current=True)
        await test.release_owned_pointers()
        self.assertEqual(calls,[])
        test.owned_pointers={"right":"unexpected"}
        with self.assertRaises(ValueError):await test.release_owned_pointers()
        self.assertEqual(calls,[])


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
    def test_saved_r18_native_livery_pose_and_submitted_stereo_geometry_agree(self):
        root=PATH.parents[1]/"build-ui-automation/flatout4-evidence/livery-side-panels-r18-final"
        source=root/"left-left-state.json";capture_path=root/"004-left-left-both.json"
        if not source.exists() or not capture_path.exists():self.skipTest("Historical r18 livery evidence is unavailable")
        state=json.loads(source.read_text(encoding="utf-8"))
        capture=json.loads(capture_path.read_text(encoding="utf-8"))[0]
        # Read immutable real evidence; this validates processing, not a new live run.
        pose=atlas.tracking_head_pose(atlas.livery_tracking_state(state["vr"]["frame"]))
        atlas.validate_capture_pose(capture,pose)
        self.assertGreater(abs(pose["yaw_deg"]),20)
        stale=copy.deepcopy(capture);stale["geometry"]["projection_eyes"][1]["pose"]["orientation"]=[0,0,0,1]
        with self.assertRaises(atlas.CaptureMismatch):atlas.validate_capture_pose(stale,pose)
        moved=copy.deepcopy(capture)
        for eye in moved["geometry"]["projection_eyes"]:eye["pose"]["position"][0]+=.01
        with self.assertRaises(atlas.CaptureMismatch):atlas.validate_capture_pose(moved,pose)

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
