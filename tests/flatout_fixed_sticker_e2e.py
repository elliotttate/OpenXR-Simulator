"""Native ray regression for the catalog's fixed-size artwork; isolated profile only."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest, ROOT, game_read, screenshot_pixel

class FixedSticker(LiveryTest):
    async def run_fixed(self):
        await self.click("STICKERS")
        original = (await self.state())["document"]
        await self.click("ADD STICKER")
        await self.click("Category: Audio riot")
        await self.click("Search stickers")
        await self.click("CLEAR")
        for key in "bassl":
            await self.click(key)
        await self.click("DONE")
        catalog = await asyncio.to_thread(game_read,self.args.pid,"catalog")
        definition = next(c for c in catalog["items"] if c["id"]=="FO_STREET_ART_026")
        assert not definition["scale_allowed"] and definition["rotation_allowed"]
        await self.click("ADD STICKER")
        before = await self.click("STUDIO")
        selected = before["selected"]
        assert before["document"]["layers"][selected]["asset"] == definition["id"]
        capture = await self.capture("fixed-body-depth-source")
        x,y = screenshot_pixel(before,capture,1000,500)
        receipt = await self.tool("hover_ui_at",{"capture_id":capture["capture_id"],"x":float(x),"y":float(y),
            "hand":"left","origin_mode":"eye"},"fixed-body-depth")
        assert receipt["status"]=="input_delivered"
        state = await self.state();hit=state["vr"]["hands"][0]
        assert hit["car_hit"] and not hit["ui_hit"]
        camera=state["vr"]["frame"]["camera"]
        depth=float((np.array(hit["end"])-camera["origin"])@np.array(camera["back"]))
        self.car_surface={"pose":{"position":[0,0,depth],"orientation":[0,0,0,1]},"size":[20,20]}
        await self.release("left")
        await self.hold_car("left",1000,500)
        joined=await self.hold_car("right",1150,500)
        await self.hold_car("left",970,515)
        changed=await self.hold_car("right",1200,485)
        a=joined["document"]["layers"][selected];b=changed["document"]["layers"][selected]
        assert changed["vr"]["grab_mask"]==3
        assert a["scale"]==b["scale"],"Two-ray gestures must preserve fixed-size artwork"
        assert abs(a["rotation"]-b["rotation"])>1
        assert a["position"]!=b["position"]
        await self.release("left");await self.release("right")
        undone=await self.click("UNDO")
        assert undone["document"]==before["document"],"The restricted gesture remains a single Undo operation"
        removed=await self.click("UNDO")
        assert removed["document"]==original
        self.cases.append({"case":"fixed-size-artwork-allows-move-turn-without-resize","passed":True,
                           "definition":definition,"joined":joined,"changed":changed,"restored":removed})
        print("Fixed-size artwork: move/turn, preserve size, one Undo and fixture restoration PASS",flush=True)

async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"passed":False,"pid":args.pid}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=FixedSticker(args,session)
            try:
                await test.run_fixed();result["passed"]=True
            finally:
                result["cases"]=test.cases
                (args.evidence/"result.json").write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ("left","right"):
                        await session.call_tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand})
if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pid",type=int,required=True)
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--evidence",type=Path,required=True)
    asyncio.run(main(p.parse_args()))
