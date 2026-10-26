"""Real screenshot -> XR hover/click, native voices and WASAPI output evidence.

Start in MAIN MENU (--mode world), INSANITY (--mode quad), a native confirmation
(--mode modal), or the livery editor (--mode livery). The bridge is read-only.
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

from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET
from flatout_livery_e2e import game_read, screenshot_pixel, ui_surface, world_pixel
from ui_audio_loopback import OutputRecording


class AudioTest(Frontend):
    def __init__(self,args,session):
        super().__init__(args,session)
        self.clips={"hover":args.fox/"Flatout/Data/Assets/Sounds/UI/TromboneHover.wav",
                    "click":args.fox/"Flatout/Data/Assets/Sounds/UI/TromboneClick.wav"}
        self.recordings=[]

    async def audio(self):
        return await self.query("audio.ui_feedback")

    async def livery(self):
        return await asyncio.wait_for(asyncio.to_thread(game_read,self.args.pid),15)

    async def release_all(self):
        capture=await self.capture("release-source")
        for hand in ("left","right"):
            await self.tool("release_ui_pointer",{"capture_id":capture["capture_id"],"hand":hand},"release-"+hand)
        await asyncio.sleep(.25)

    async def record(self,label,operation,expected):
        recording=OutputRecording(self.args.loopback_dependencies,self.args.evidence/(label+".wav"))
        try:
            await asyncio.sleep(.15)
            value=await operation()
            await asyncio.sleep(.35)
        finally:
            reports=recording.finish(self.clips)
            self.recordings.append(dict(case=label,expected=expected,outputs=reports))
            (self.args.evidence/"speaker-output.json").write_text(json.dumps(self.recordings,indent=2))
        if expected:
            matches=[r["matches"][expected] for r in reports]
            assert any(m["correlation"]>.45 and m["gain"]>.005 for m in matches), ("Expected source clip absent from speaker output",expected,matches)
        return value

    @staticmethod
    def delta(before,after,hover,click):
        assert after["hover"]-before["hover"]==hover,("hover count",before,after)
        assert after["click"]-before["click"]==click,("click count",before,after)
        assert after["failed"]==before["failed"],("Native voice submission failed",before,after)

    async def point(self,name,hand="right"):
        if self.args.mode=="world":
            menu=await self.query("menu.get_state")
            native=await self.query("vr.targets")
            capture=await self.capture("source-world-"+name)
            assert not capture["geometry"]["surfaces"],"Require the actual world-space menu"
            item=menu["items"][int(name)]
            frame={**native["vr_frame"],"panel":{**menu["spatial_layout"],"origin":item["rendered_position"]},"panel_width":1.,"panel_height":1.}
            state={"vr":{"frame":frame}}
            x,y=world_pixel(state,capture,item["rendered_position"],"left")
            return {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":hand,
                "surface":ui_surface(state),"origin_mode":"hand","ray_offset":RAY_OFFSET,"dwell_frames":30}
        if self.args.mode.startswith("livery"):
            state=await self.livery()
            control=next(c for c in state["vr"]["controls"] if c["label"]==name)
            capture=await self.capture("source-livery-"+name)
            x,y=screenshot_pixel(state,capture,control["x"]+control["width"]*.5,control["y"]+control["height"]*.5)
            return {"capture_id":capture["capture_id"],"x":float(x),"y":float(y),"hand":hand,
                "surface":ui_surface(state),"origin_mode":"hand","dwell_frames":30}
        _,aim=await self.point_args(name,hand=hand)
        assert self.last_capture["geometry"]["surfaces"],"Require an actual compositor UI quad"
        return aim

    async def enter(self,name,hand="right",reenter=True):
        await self.release_all()
        aim=await self.point(name,hand)
        baseline=await self.audio()
        async def hover():
            result=await self.tool("hover_ui_at",aim,"hover-"+name)
            assert result["status"]=="input_delivered",result
            return result
        await self.record("entry-"+name+"-"+hand,hover,"hover")
        entered=await self.audio()
        self.delta(baseline,entered,1,0)
        await self.tool("hover_ui_at",{**aim,"dwell_frames":90},"stationary-"+name)
        stationary=await self.audio()
        self.delta(entered,stationary,0,0)
        self.cases.append(dict(case="entry-and-stationary-"+name+"-"+hand,passed=True,before=baseline,after=stationary))
        if reenter:
            await self.release_all()
            aim=await self.point(name,hand)
            before=await self.audio()
            await self.tool("hover_ui_at",aim,"reenter-"+name)
            after=await self.audio()
            self.delta(before,after,1,0)
            self.cases.append(dict(case="leave-reenter-"+name,passed=True,before=before,after=after))
        return aim

    async def activate(self,name,hand="right",hover=True):
        aim=await self.enter(name,hand) if hover else await self.point(name,hand)
        baseline=await self.audio()
        command={**aim,"hold_frames":45,"command_id":uuid.uuid4().hex,"timeout":30}
        async def click():
            result=await self.tool("click_ui_at",command,"accepted-"+name)
            assert result["status"]=="input_delivered",result
            return result
        delivered=await self.record("accepted-"+name+"-"+hand,click,"click")
        after=await self.audio()
        self.delta(baseline,after,0,1)
        repeated=await self.tool("click_ui_at",command,"same-uuid-"+name)
        assert repeated["delivery"]["sync_id"]==delivered["delivery"]["sync_id"]
        retried=await self.audio()
        self.delta(after,retried,0,0)
        self.cases.append(dict(case="held-click-and-retry-"+name+"-"+hand,passed=True,before=baseline,after=retried))

    async def run(self):
        if self.args.mode=="world":
            assert (await self.query("menu.get_state"))["current_menu"]=="MAIN MENU"
            await self.enter("1","left")
            await self.activate("5","right")
            assert (await self.query("options.get_state"))["open"]
        elif self.args.mode=="quad":
            assert (await self.query("menu.get_state"))["current_menu"]=="INSANITY"
            await self.activate("challenge.page.next","left")
            await self.activate("challenge.page.prev","right")
        elif self.args.mode=="modal":
            assert (await self.query("ui.get_message_box"))["open"]
            await self.enter("msgbox.validate","left")
            await self.activate("msgbox.cancel","right")
            assert not (await self.query("ui.get_message_box"))["open"]
        elif self.args.mode=="livery-exit":
            assert not (await self.livery())["dirty"],"Use a clean document for the reversible editor exit"
            await self.activate("BACK","right")
            assert (await self.query("menu.get_state"))["current_menu"]=="GARAGE"
            self.cases.append(dict(case="livery-to-garage-owner-handoff",passed=True))
        else:
            # Reversible tab navigation; do not save or publish the document.
            await self.activate("STICKERS","left")
            for _ in range(32):
                state=await self.livery()
                if state["selected"]<6: break
                await self.activate("Layer page previous","right")
            else: raise AssertionError("Could not reach the first layer page")
            await self.release_all()
            aim=await self.point("Layer page previous","right")
            before=await self.audio(); document=(await self.livery())["document"]
            await self.record("disabled-hover",lambda:self.tool("hover_ui_at",aim,"disabled-hover"),None)
            held={**aim,"hold_frames":45,"command_id":uuid.uuid4().hex,"timeout":30}
            await self.record("disabled-click",lambda:self.tool("click_ui_at",held,"disabled-click"),None)
            await self.tool("click_ui_at",held,"disabled-retry")
            after=await self.audio()
            self.delta(before,after,0,0)
            assert (await self.livery())["document"]==document
            self.cases.append(dict(case="disabled-page-arrow-silent",passed=True,before=before,after=after))
            await self.activate("PAINT","right")
            await self.activate("Custom paint color","left")
            await self.activate("CANCEL","right")
            assert (await self.livery())["document"]==document
            await self.activate("MY LIVERIES","left")
            await self.activate("Livery name","right")
            assert (await self.livery())["keyboard_target"]==2
            await self.enter("q","left")
            await self.capture("keyboard-audio-and-ray-button-hint")
            await self.activate("CANCEL","right")
            assert (await self.livery())["keyboard_target"]==0
            assert (await self.livery())["document"]==document
            self.cases.append(dict(case="keyboard-hover-cancel-preserves-document",passed=True))
        await asyncio.sleep(2.1)
        finished=await self.audio()
        assert all(finished["loaded"]) and finished["using_event_system"],finished
        assert finished["failed"]==0 and finished["started"]>=finished["hover"]+finished["click"],finished
        assert finished["ended"]>=finished["hover"]+finished["click"],finished
        assert finished["master"]>0 and finished["gui"]>0,"Audible proof requires nonzero ordinary mixer volumes"
        self.cases.append(dict(case="native-voices-complete-through-gui-master",passed=True,audio=finished))


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result=dict(passed=False,pid=args.pid,mode=args.mode,input="actual screenshot-derived XR actions",audio="native voices plus WASAPI speaker output")
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=AudioTest(args,session)
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
    parser.add_argument('--mode',choices=['world','quad','modal','livery','livery-exit'],required=True)
    parser.add_argument('--hand',default='right')
    parser.add_argument('--loopback-dependencies',type=Path,default=ROOT/'build-ui-automation/audio-loopback-deps')
    asyncio.run(main(parser.parse_args()))
