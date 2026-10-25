"""Ray-driven material edits must preserve the already-rendered sticker atlas.

Use an isolated profile with at least one sticker. Native generation diagnostics
are read-only; all edits and Undo steps use screenshot-derived real XR actions.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_direct_sticker_e2e import DirectSticker
from flatout_livery_e2e import ROOT, screenshot_pixel, ui_surface
from flatout_frontend_e2e import read_game

class MaterialEdits(DirectSticker):
    def unchanged_atlas(self, before, after, label):
        assert before["atlas_generation"]>0
        passed=after["atlas_generation"]==before["atlas_generation"] and not after["atlas_preview"]
        self.cases.append({"case":label,"passed":passed,"before":before,"after":after})
        assert passed, f"{label}: a material-only edit rebuilt/uploaded the sticker atlas"

    async def drag(self,label,fraction):
        before=await self.state()
        control=max((c for c in before["vr"]["controls"] if c["label"]==label),key=lambda c:c["width"])
        capture=await self.capture(label+"-source")
        x,y=screenshot_pixel(before,capture,control["x"]+control["width"]*fraction,control["y"]+control["height"]*.5)
        receipt=await self.tool("hold_ui_at",{"capture_id":capture["capture_id"],"x":float(x),"y":float(y),
            "hand":"left","origin_mode":"hand","surface":ui_surface(before),"dwell_frames":2},label+"-drag")
        assert receipt["status"]=="input_delivered"
        return await self.state()

    async def material_slider(self,label,field):
        before=await self.state()
        try:
            for fraction in (.2,.8):
                changed=await self.drag(label,fraction)
                assert abs(changed["document"][field]-fraction)<.04
                assert changed["document"]["layers"]==before["document"]["layers"]
                self.unchanged_atlas(before,changed,label+"-"+str(fraction))
                if self.args.gpu_capture and label=="Gloss":
                    queued=await asyncio.to_thread(read_game,self.args.pid,"renderdoc.capture",{
                        "frames":1,"path":str((self.args.evidence/("gloss-"+str(fraction))).resolve())})
                    (self.args.evidence/("gpu-gloss-"+str(fraction)+".json")).write_text(json.dumps({"state":changed,"queued":queued},indent=2))
                    await self.capture("gpu-queued-gloss-"+str(fraction))
            released=await self.release("left")
            self.unchanged_atlas(before,released,label+"-release-full-quality")
        finally:
            await self.release("left")
            if (await self.state())["document"]!=before["document"]:
                restored=await self.click("UNDO")
                assert restored["document"]==before["document"],"One Undo must restore the material drag"
        self.unchanged_atlas(before,await self.state(),label+"-undo")
        print(label,"changes material without atlas work PASS",flush=True)

    async def run_material(self):
        await self.click("PAINT")
        original=await self.state()
        assert original["document"]["layers"],"Use a nonempty sticker fixture"
        try:
            if self.args.gpu_capture and not original["document"]["painted"]:
                enabled=await self.click("FACTORY PAINT")
                assert enabled["document"]["painted"]
                self.unchanged_atlas(original,enabled,"enable-paint-for-gpu-proof")
            await self.material_slider("Gloss","gloss")
            if self.args.gloss_only:return
            await self.material_slider("Metallic","metalness")
            painted=(await self.state())["document"]["painted"]
            for label in ("CUSTOM PAINT ON" if painted else "FACTORY PAINT","Paint color 5","Matte"):
                before=await self.state()
                changed=await self.click(label)
                assert changed["document"]!=before["document"]
                assert changed["document"]["layers"]==before["document"]["layers"]
                self.unchanged_atlas(before,changed,label)
                restored=await self.click("UNDO")
                assert restored["document"]==before["document"]
                self.unchanged_atlas(before,restored,label+"-undo")
            await self.click("STICKERS")
            await self.click("Layer "+str((await self.state())["selected"]))
            before=await self.state()
            lock="UNLOCK" if before["document"]["layers"][before["selected"]]["locked"] else "LOCK"
            changed=await self.click(lock)
            assert changed["document"]!=before["document"]
            self.unchanged_atlas(before,changed,"lock-metadata")
            assert (await self.click("UNDO"))["document"]==before["document"]
            await self.click("MOVE / ROTATE")
            before=await self.state()
            edited=await self.drag("Rotation",.7)
            assert edited["document"]["layers"]!=before["document"]["layers"]
            assert edited["atlas_generation"]>before["atlas_generation"],"A real sticker edit must update its atlas"
            assert edited["atlas_preview"],"Sticker drag must use the interactive preview"
            released=await self.release("left")
            assert released["atlas_generation"]>edited["atlas_generation"],"Sticker release must restore full atlas quality"
            assert not released["atlas_preview"]
            restored=await self.click("UNDO")
            assert restored["document"]==before["document"]
            self.cases.append({"case":"sticker-edit-and-release-positive-control","passed":True,
                               "before":before,"edited":edited,"released":released,"restored":restored})
            print("Actual sticker edits invalidate atlas and release restores full quality PASS",flush=True)
        finally:
            await self.release("left")
            if (await self.state())["document"]!=original["document"]:
                restored=await self.click("UNDO")
                assert restored["document"]==original["document"],"Restore the test document"

async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"passed":False,"pid":args.pid,"scope":"Material-only ray edits preserve atlas; real sticker edits and release update it"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=MaterialEdits(args,session)
            try:
                await test.run_material();result["passed"]=True
            except Exception as error:
                result["error"]=repr(error);raise
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
    p.add_argument("--timeout",type=float,default=15)
    p.add_argument("--gloss-only",action="store_true")
    p.add_argument("--gpu-capture",action="store_true")
    asyncio.run(main(p.parse_args()))
