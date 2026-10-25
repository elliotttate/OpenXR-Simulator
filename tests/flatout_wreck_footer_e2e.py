"""Wreck Run footer navigation using screenshots and real simulator XR input.

Open the native Debug Wreck Run hub in an isolated between-tours profile first.
The test does not start a tour, buy items, apply settings, or issue game commands.
--practice-only starts in the native practice offer and tests its Back button.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend

ROOT=Path(__file__).resolve().parents[1]


class WreckFooterTest(Frontend):
    async def layout(self, label, required, prefix="run.hub.footer."):
        state=await self.query("vr.targets")
        buttons=[t for t in state["targets"] if t["name"].startswith(prefix)]
        ids={int(t["name"].removeprefix(prefix)) for t in buttons}
        assert ids.issuperset(required), (label,"Missing footer ray actions",required,buttons)
        assert all(t["rect"][2]-t["rect"][0]>=179.5 and t["rect"][3]-t["rect"][1]>=51.5 for t in buttons), buttons
        ordered=sorted(buttons,key=lambda t:t["rect"][0])
        assert all(b["rect"][0]-a["rect"][2]>=24 for a,b in zip(ordered,ordered[1:])), buttons
        await self.audit(label)
        async def outline(action):
            nodes=(await self.query("hud.rects",{"root":"menu","max_depth":12}))["nodes"]
            matches=[n for n in nodes if n["path"].endswith("/RunFooterButton_"+str(action))]
            assert len(matches)==1,(action,matches)
            return matches[0]["color"]
        # Hover must only change focus, never deliver a click or navigate.
        menu=await self.query("menu.get_state")
        previous=None
        colors=[]
        for button in buttons:
            before,hovered,_=await self.hover(button["name"])
            assert hovered["clicks"]==before["clicks"]
            after=await self.query("menu.get_state")
            assert after.get("native_page")==menu.get("native_page") and after["current_menu"]==menu["current_menu"]
            action=int(button["name"].removeprefix(prefix))
            await asyncio.sleep(.3)  # The highlight must survive more than its entry frame.
            color=await outline(action)
            assert min(color[c] for c in ("r","g","b"))>.8,("Hover outline disappeared",action,color)
            if previous is not None:
                restored=await outline(previous)
                assert min(restored[c] for c in ("r","g","b"))<.8,("Previous button stayed highlighted",previous,restored)
            colors.append({"action":action,"color_after_dwell":color})
            previous=action
        l,t,r,b=buttons[-1]["rect"]
        live=await self.query("vr.targets")
        empty=next((p for p in ((1800,500),(1800,700),(960,220),(1000,150))
                    if all(not (c["rect"][0]-24<=p[0]<=c["rect"][2]+24 and
                                c["rect"][1]-24<=p[1]<=c["rect"][3]+24) for c in live["targets"])),None)
        assert empty is not None,"No clear panel point for the hover-leave check"
        before,aim=await self.point_args(buttons[-1]["name"],((empty[0]-l)/(r-l),(empty[1]-t)/(b-t)))
        receipt=await self.tool("hover_ui_at",aim,"hover-empty-space")
        assert receipt["status"]=="input_delivered",receipt
        await asyncio.sleep(.3)
        cleared=await self.query("vr.targets")
        assert not cleared["hovered"] and cleared["clicks"]==before["clicks"],cleared
        assert max(abs(cleared["x"]-empty[0]),abs(cleared["y"]-empty[1]))<1,(
            "A stationary spare ray stole the active hand's empty-space hover",empty,cleared)
        color=await outline(previous)
        assert min(color[c] for c in ("r","g","b"))<.8,("Empty space retained a highlight",color)
        self.cases.append({"case":label+"-hover-only","passed":True,"buttons":buttons,"colors":colors})

    async def visit(self, target, page):
        await self.click(target)  # 45 held frames, one native click, same-command retry
        await asyncio.sleep(.25)
        menu=await self.query("menu.get_state")
        assert menu.get("native_page")==page,(target,page,menu)

    async def hub(self):
        assert (await self.query("menu.get_state")).get("native_page")=="hub"
        errors=(await self.query("game.errors"))["total"]
        await self.layout("wreck-hub-footer",{0,1,2,3})
        await self.visit("run.hub.footer.3","loadout")
        await self.layout("wreck-loadout-footer",{0,1})
        for target,label in ((3,"car"),(4,"style"),(5,"crew"),(6,"sponsor")):
            await self.visit("run.hub."+str(target),"other")
            await self.layout("wreck-"+label+"-picker-footer",{0,3})
            await self.visit("run.hub.footer.3","loadout")
        await self.visit("run.hub.footer.1","hub")
        # The Daily Wreck cards describe the rules. Its explicit Back action
        # must return to the hub without starting a ranked or unranked run.
        await self.visit("run.hub.1","other")
        await self.layout("wreck-daily-footer",{2})
        await self.visit("run.hub.footer.2","hub")
        # Workshop is a list screen. Its footer Back uses the native return path.
        await self.visit("run.hub.4","workshop")
        await self.layout("wreck-workshop-list-footer",{0,2})
        await self.visit("run.hub.footer.2","hub")
        await self.click("run.hub.footer.2")
        menu=await self.query("menu.get_state")
        assert any("text size" in i["title"].lower() for i in menu["items"]),menu
        await self.audit("wreck-interface-settings")
        await self.click("run.interface.4")  # Back discards the untouched draft.
        assert (await self.query("menu.get_state")).get("native_page")=="hub"
        assert (await self.query("game.errors"))["total"]==errors
        await self.capture("wreck-hub-final")

    async def practice(self):
        await self.layout("wreck-practice-footer",{4,5},"run.endgame.prompt.")
        await self.click("run.endgame.prompt.5")
        assert (await self.query("menu.get_state")).get("native_page")=="hub"


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"pid":args.pid,"passed":False,"scope":"Native Wreck Run footer UI; real XR input; read-only game diagnostics"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=WreckFooterTest(args,session)
            try:
                # The caller may have entered this fixture using the other hand.
                # Restore both staging leases before measuring deliberate motion.
                await test.capture("input-baseline")
                for hand in ("left","right"):
                    await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"baseline-"+hand)
                await asyncio.sleep(.3)
                await (test.practice() if args.practice_only else test.hub())
                result["passed"]=True
            finally:
                result["cases"]=test.cases
                (args.evidence/"result.json").write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ("left","right"):
                        await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"cleanup-"+hand)
    print("Wreck Run footer regression passed:",args.evidence,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid",required=True,type=int)
    parser.add_argument("--data-dir",required=True,type=Path)
    parser.add_argument("--evidence",required=True,type=Path)
    parser.add_argument("--hand",choices=("left","right"),default="right")
    parser.add_argument("--practice-only",action="store_true")
    asyncio.run(run(parser.parse_args()))
