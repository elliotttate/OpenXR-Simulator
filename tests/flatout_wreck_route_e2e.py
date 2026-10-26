"""Route and Interface Settings navigation via screenshot pixels and real XR input.

Start on a saved native Wreck Run route in an isolated test profile. The game
bridge only reads state. Never starts an event, places a bet or applies settings.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_wreck_footer_e2e import WreckFooterTest

ROOT=Path(__file__).resolve().parents[1]


class RouteTest(WreckFooterTest):
    async def transparent_scene(self,label,root_name):
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        drawn=[n for n in nodes if root_name in n["path"] and n["kind"]=="panel" and n["visible"] and n["color"]["a"]>0]
        assert drawn,(label,"No visible native panels")
        full=[n for n in drawn if n["rect"][0]<=1 and n["rect"][1]<=1 and n["rect"][2]>=1919 and n["rect"][3]>=1079]
        strips=[n for n in drawn if n["rect"][2]-n["rect"][0]>=1800 and n["rect"][1]>=980
                and n["color"]["r"]>.7 and n["color"]["g"]>.2 and n["color"]["b"]<.2]
        assert not full and not strips,(label,"Scene or footer overlay remains",full,strips)
        self.cases.append({"case":label+"-live-scene","passed":True,"panel_count":len(drawn)})

    async def shell_layout(self,label):
        await self.audit(label)
        targets=(await self.query("vr.targets"))["targets"]
        footer=[t for t in targets if t["name"].startswith("run.interface.footer.")]
        assert {t["name"] for t in footer}=={"run.interface.footer.Validate.0","run.interface.footer.Cancel.1"},footer
        assert all(t["rect"][2]-t["rect"][0]>=199 and t["rect"][3]-t["rect"][1]>=79 for t in footer),footer
        for t in footer:
            before,after,_=await self.hover(t["name"])
            assert after["clicks"]==before["clicks"]
        await self.transparent_scene(label,"WreckRunMenuShell")
        self.cases.append({"case":label+"-footer","passed":True,"buttons":footer})

    async def exercise(self):
        initial=await self.query("rogue.route_map")
        assert initial["active"],"Open the native saved route first"
        errors=(await self.query("game.errors"))["total"]
        # Only the actual footer IDs, rather than the tiles/sector dots, use
        # the common footer's size, persistent highlight and leave checks.
        await self.route_footer("route-initial")
        await self.transparent_scene("route-initial","WreckRunRouteMap")
        for sector in range(3):
            await self.click("run.route."+str(7+sector))
            route=await self.query("rogue.route_map")
            assert route["sector"]==sector and route["sequence"]==initial["sequence"],route
            await self.route_footer("route-sector-"+str(sector))
            for node in route["nodes"]:
                name="run.route."+str(node["slot"])
                live=await self.query("vr.targets")
                if name not in {t["name"] for t in live["targets"]}:
                    continue
                before,after,_=await self.hover(name)
                focused=await self.query("rogue.route_map")
                assert after["clicks"]==before["clicks"] and focused["focus"]==node["slot"] and focused["sequence"]==initial["sequence"]
                self.cases.append({"case":"hover-sector-"+str(sector)+"-tile-"+str(node["slot"]),"passed":True})
        await self.click("run.route."+str(7+initial["sector"]))
        await self.click("run.route.12")
        assert (await self.query("menu.get_state")).get("native_page")=="hub"
        await self.click("run.hub.footer.2")
        original=await self.query("menu.get_state")
        assert "Text size:" in original["items"][0]["title"],original
        # All 8 draft combinations preview standard/large, contrast and motion.
        # The explicit Select footer invokes the focused row once; Back restores
        # applied preferences. No Apply action is issued.
        for index,setting in enumerate((0,1,0,2,0,1,0,2)):
            before=[i["title"] for i in (await self.query("menu.get_state"))["items"]]
            expected=before.copy()
            on,off=("Large","Standard") if setting==0 else ("On","Off")
            old=expected[setting].rsplit(": ",1)[-1]
            assert old in (on,off),expected
            expected[setting]=expected[setting].rsplit(": ",1)[0]+": "+(off if old==on else on)
            await self.hover("run.interface."+str(setting))
            await self.click("run.interface.footer.Validate.0")
            actual=[i["title"] for i in (await self.query("menu.get_state"))["items"]]
            assert actual==expected,("Select must change only the focused draft setting once",before,expected,actual)
            await self.shell_layout("interface-preview-"+str(index))
            self.cases.append({"case":"interface-preview-value-"+str(index),"passed":True,"titles":actual})
        await self.click("run.interface.footer.Cancel.1")
        assert (await self.query("menu.get_state")).get("native_page")=="hub"
        await self.click("run.hub.footer.2")
        restored=await self.query("menu.get_state")
        assert [i["title"] for i in restored["items"]]==[i["title"] for i in original["items"]],restored
        await self.click("run.interface.footer.Cancel.1")
        await self.click("run.hub.0")  # Continue the existing tour, without starting an event.
        final=await self.query("rogue.route_map")
        assert final["active"] and final["sequence"]==initial["sequence"] and final["graph_seed"]==initial["graph_seed"]
        assert (await self.query("game.errors"))["total"]==errors
        await self.capture("route-final")
        self.cases.append({"case":"draft-discard-and-tour-unchanged","passed":True,"initial":initial,"final":final})

    async def route_footer(self,label):
        state=await self.query("vr.targets")
        buttons=[t for t in state["targets"] if t["name"] in {"run.route.10","run.route.11","run.route.12","run.route.13","run.route.14"}]
        assert {"run.route.12","run.route.14"}.issubset({t["name"] for t in buttons}),buttons
        assert all(t["rect"][2]-t["rect"][0]>=179.5 and t["rect"][3]-t["rect"][1]>=51.5 for t in buttons),buttons
        ordered=sorted(buttons,key=lambda t:t["rect"][0])
        assert all(b["rect"][0]-a["rect"][2]>=24 for a,b in zip(ordered,ordered[1:])),buttons
        await self.audit(label)
        for button in buttons:
            before,after,_=await self.hover(button["name"])
            assert before["clicks"]==after["clicks"]
            await asyncio.sleep(.3)
            nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
            edge=next(n for n in nodes if n["path"].endswith("/RunFooterButton_"+button["name"].split(".")[-1]))
            assert min(edge["color"][c] for c in ("r","g","b"))>.8,edge
        self.cases.append({"case":label+"-footer","passed":True,"buttons":buttons})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"pid":args.pid,"passed":False,"scope":__doc__}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=RouteTest(args,session)
            try:
                await test.capture("input-baseline")
                for hand in ("left","right"):
                    await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"baseline-"+hand)
                await asyncio.sleep(.3)
                await test.exercise()
                result["passed"]=True
            except BaseException as e:
                result["error"]=repr(e)
                raise
            finally:
                result["cases"]=test.cases
                (args.evidence/"result.json").write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ("left","right"):
                        await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"cleanup-"+hand)
    print("Wreck Run route regression passed:",args.evidence,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid",required=True,type=int)
    parser.add_argument("--data-dir",required=True,type=Path)
    parser.add_argument("--evidence",required=True,type=Path)
    parser.add_argument("--hand",choices=("left","right"),default="right")
    asyncio.run(run(parser.parse_args()))
