"""Prove Sound FX and Master mute ray feedback through actual Options sliders.

Start in Options. Original volumes are restored with real XR actions in finally.
The game bridge is exclusively a read-only oracle.
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_ui_audio_e2e import AudioTest


class VolumeTest(AudioTest):
    async def point(self,name,hand="right"):
        _,aim=await self.point_args(name,hand=hand)
        return aim

    async def focus_sound(self):
        state=await self.query("options.get_state")
        assert state["open"],"Start in Options"
        if state["selection_index"]!=3:
            await self.click("settings.category.3")

    async def set_volume(self,row,value):
        await self.focus_sound()
        before=(await self.query("options.get_state"))["boxes"][3]["rows"][row]
        fraction=min(.999,max(.001,(value-before["min"])/(before["max"]-before["min"])))
        await self.click(f"box3.row{row}.bar",(fraction,.5))
        after=(await self.query("options.get_state"))["boxes"][3]["rows"][row]
        assert after["value"]==value,(row,value,after)

    async def silent_hover(self,control,label):
        await self.release_all()
        aim=await self.point(control,"left")
        baseline=await self.audio()
        await self.record(label,lambda:self.tool("hover_ui_at",aim,label),None)
        after=await self.audio()
        self.delta(baseline,after,1,0)
        reports=self.recordings[-1]["outputs"]
        matches=[r["matches"]["hover"] for r in reports]
        assert not any(m["correlation"]>.45 and m["gain"]>.005 for m in matches),("Muted feedback remained in speaker output",matches)
        self.cases.append(dict(case=label,passed=True,before=baseline,after=after,outputs=reports))

    async def run(self):
        await self.focus_sound()
        baseline=(await self.query("options.get_state"))["boxes"][3]
        original={row:baseline["rows"][row]["value"] for row in (0,2)}
        assert original[0]>0 and original[2]>0,"Start with audible Master and Sound FX"
        try:
            await self.set_volume(2,0)
            muted=await self.audio()
            assert muted["gui"]==0 and muted["master"]>0,muted
            await self.silent_hover("settings.category.0","sound-fx-muted")
            await self.set_volume(2,original[2])
            restored=await self.audio()
            assert abs(restored["gui"]-original[2]/100)<.001,restored
            await self.enter("settings.category.0","left")
            await self.set_volume(0,0)
            muted=await self.audio()
            assert muted["master"]==0 and muted["gui"]>0,muted
            await self.silent_hover("settings.category.0","master-muted")
            await self.set_volume(0,original[0])
            restored=await self.audio()
            assert abs(restored["master"]-original[0]/100)<.001,restored
            await self.enter("settings.category.0","right")
        finally:
            for row,value in original.items():
                await self.focus_sound()
                current=(await self.query("options.get_state"))["boxes"][3]["rows"][row]["value"]
                if current!=value:
                    await self.set_volume(row,value)
            after=(await self.query("options.get_state"))["boxes"][3]
            assert all(after["rows"][row]["value"]==value for row,value in original.items())
        self.cases.append(dict(case="original-master-and-sound-fx-restored",passed=True,original=original,audio=await self.audio()))


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result=dict(passed=False,pid=args.pid,input="screenshot-derived actual XR actions",scope="Sound FX and Master mute and restoration")
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=VolumeTest(args,session)
            try:
                await test.run()
                result["passed"]=True
            except Exception as error:
                result["error"]=repr(error)
                raise
            finally:
                result["cases"]=test.cases
                (args.evidence/"result.json").write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ("left","right"):
                        await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"final-release-"+hand)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--fox',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--hand',default='right')
    parser.add_argument('--mode',default='options')
    parser.add_argument('--loopback-dependencies',type=Path,default=ROOT/'build-ui-automation/audio-loopback-deps')
    asyncio.run(main(parser.parse_args()))
