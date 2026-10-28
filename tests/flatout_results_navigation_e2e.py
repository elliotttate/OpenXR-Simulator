"""Verify bounded online-result driver selection through screenshot-derived XR input.

Start on the actual private online bot-race result screen. No Profile action is
sent. --saved-red checks the missing-control contract against a recorded native
capture without connecting to or sending input to the game.
"""
import argparse, asyncio, json, os, sys, uuid
from pathlib import Path
import numpy as np
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT
from flatout_ui_depth_contract import assert_hover_lift, ui_planes
from flatout_livery_e2e import quaternion_matrix

PREVIOUS = "results.driver.previous"
NEXT = "results.driver.next"

def assert_navigation_controls(targets):
    names = {target["name"] for target in targets["targets"]}
    assert {PREVIOUS, NEXT} <= names, ("Online results have no ray controls for driver selection", sorted(names))

class ResultNavigation(Frontend):
    async def state(self):
        state = await self.query("race.end_screen")
        selection = state["result_selection"]
        assert state["screen"] == 1 and selection["active"] and selection["ready"], state
        assert 0 <= selection["selected"] < selection["count"] <= 12, state
        assert selection["previous_enabled"] == (selection["selected"] > 0), state
        assert selection["next_enabled"] == (selection["selected"] + 1 < selection["count"]), state
        return selection

    async def step(self, direction):
        name = PREVIOUS if direction < 0 else NEXT
        initial = await self.state()
        before, hovered, args = await self.hover(name)
        enabled = initial["previous_enabled" if direction < 0 else "next_enabled"]
        capture = await self.tool("capture_screenshot", {"eye":"both", "max_width":4096}, "stereo-"+name)
        self.last_capture = capture
        base, tiles = ui_planes(capture)
        l,t,r,b = self.rects[name]
        rotation = quaternion_matrix(base["pose"]["orientation"])
        expected_size = np.array([(r-l)/1920*base["size"][0],(b-t)/1080*base["size"][1]])
        expected_center = np.array(base["pose"]["position"]) + rotation @ np.array([
            ((l+r)/3840-.5)*base["size"][0],(.5-(t+b)/2160)*base["size"][1],HOVER_LIFT_METERS])
        matching = [tile for tile in tiles if np.allclose(tile["size"],expected_size,atol=.002) and
                    np.linalg.norm(np.array(tile["pose"]["position"])-expected_center)<.003]
        if enabled:
            assert matching, ("No physical tile at the hovered chevron's actual logical bounds",name,expected_center.tolist(),tiles)
            self.cases.append({"case":"physical-"+name,"passed":True,"matching_tiles":matching,**assert_hover_lift(capture)})
        else:
            assert not matching, ("Disabled endpoint received active hover depth",name,matching)
        # Re-capture after the hover settles; never bypass prepress geometry validation.
        before, args = await self.point_args(name)
        args.update(command_id=uuid.uuid4().hex, hold_frames=45, timeout=30)
        clicked = await self.tool("click_ui_at", args, "click-"+name)
        assert clicked["status"] == "input_delivered", clicked
        after = await self.state()
        target_state = await self.query("vr.targets")
        delta = direction if enabled else 0
        assert after["selected"] == initial["selected"] + delta, (initial, after, direction)
        assert target_state["clicks"] == before["clicks"] + int(enabled), (before,target_state)
        retry = await self.tool("click_ui_at", args, "retry-"+name)
        assert retry["delivery"]["sync_id"] == clicked["delivery"]["sync_id"], (clicked,retry)
        assert (await self.state())["selected"] == after["selected"], "Retry repeated driver selection"
        assert (await self.query("vr.targets"))["clicks"] == target_state["clicks"]
        current = await self.query("vr.targets")
        assert {t["name"]:t["rect"] for t in current["targets"] if t["name"] in (PREVIOUS,NEXT)} == self.rects
        self.cases.append({"case":"one-step-and-held-retry" if enabled else "disabled-endpoint-inert",
                           "passed":True,"direction":direction,"before":initial,"after":after})

    async def run(self):
        before = await self.query("vr.targets")
        assert_navigation_controls(before)
        self.rects = {t["name"]:t["rect"] for t in before["targets"] if t["name"] in (PREVIOUS,NEXT)}
        for _ in range(100):
            native = await self.query("race.end_screen")
            if native.get("result_selection",{}).get("ready"): break
            await asyncio.sleep(.1)
        state = await self.state()
        assert state["count"] > 1, "Use a private bot race with multiple drivers"
        # Exercise a valid step from whichever row the real race selected.
        await self.step(-1 if state["selected"] else 1)
        while (await self.state())["previous_enabled"]:
            await self.step(-1)
        await self.step(-1)
        while (await self.state())["next_enabled"]:
            await self.step(1)
        await self.step(1)
        await self.capture("result-selection-final")
        errors = await self.query("game.errors")
        assert errors["total"] == 0, errors

async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report = {"passed":False,"hand":args.hand,"scope":"actual XR hover/click; read-only native selection diagnostics; no Profile activation"}
    if args.saved_red:
        native = json.loads(args.saved_red.read_text())
        report.update(source_native_evidence=str(args.saved_red.resolve()), captured_images=native["capture"]["images"],
                      capture=native["capture"]["text"][0], scope="read-only assertion against saved native result capture")
        try: assert_navigation_controls(native["targets"]["result"])
        except AssertionError as error: report["error"] = repr(error)
        else: report["passed"] = True
        (args.evidence/"result.json").write_text(json.dumps(report,indent=2))
        assert report["passed"], report["error"]
        return
    assert args.pid and args.data_dir, "Live testing requires --pid and --data-dir"
    params = StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
                                  env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize(); test = ResultNavigation(args,session)
            try: await test.run(); report["passed"] = True
            except Exception as error: report["error"] = repr(error); raise
            finally:
                report["cases"] = test.cases
                (args.evidence/"result.json").write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ("left","right"):
                        await session.call_tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand})

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pid",type=int);p.add_argument("--data-dir",type=Path)
    p.add_argument("--evidence",type=Path,required=True);p.add_argument("--saved-red",type=Path)
    p.add_argument("--hand",choices=("left","right"),default="right")
    asyncio.run(main(p.parse_args()))
