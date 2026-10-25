"""Roadside shop / Parts Swap navigation via real XR input in an isolated profile.

Start on an offered Parts Swap with multiple eligible installed parts. Setup is
separate. Arrow navigation must never accept a swap or write saved progress.
--leave uses the actual Drive On button after the read-only checks.
--shop-only checks shop hover, View Build and Back without buying anything.
--activity-only checks Stunt Trial selection, readable rules and Back without starting.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_wreck_rewards_e2e import RewardTest

ROOT=Path(__file__).resolve().parents[1]


class StopTest(RewardTest):
    async def description_page(self):
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        pages=[n["text"] for n in nodes if n["visible"] and n["path"].endswith("/DescriptionPage") and n.get("text")]
        assert len(pages)<=1,pages
        if not pages:return 1,1
        match=re.fullmatch(r"(\d+)/(\d+)",pages[0])
        assert match,pages
        return int(match[1]),int(match[2])

    async def info_visibility(self):
        page,total=await self.description_page()
        names={t["name"] for t in (await self.query("vr.targets"))["targets"]}
        assert ("run.stop.footer.5" in names)==(total>1),(page,total,names)
        return page,total

    async def activity(self):
        initial=await self.query("rogue.state")
        assert initial["phase"]=="service",initial["phase"]
        await self.layout("activity-footer",{1,3,5},"run.stop.footer.")
        await self.exterior("activity","WreckRunEncounterScreen")
        page,total=await self.info_visibility()
        assert total>1,"Use an activity fixture with multi-page rules"
        original_page=page
        for _ in range(total):
            await self.click("run.stop.footer.5")
            page=page%total+1
            assert await self.description_page()==(page,total)
        assert page==original_page
        self.cases.append({"case":"activity-rules-pages-and-wrap","passed":True,"pages":total})
        original,index,count=await self.picker()
        assert count>1
        if page==1:
            await self.click("run.stop.footer.5")
        assert (await self.description_page())[0]>1
        await self.click("run.encounter.picker.0.next")
        assert (await self.description_page())[0]==1,"New activity retained the previous activity's later rules page"
        await self.click("run.encounter.picker.0.previous")
        assert (await self.picker())[0]==original and (await self.description_page())[0]==1
        self.cases.append({"case":"new-activity-starts-at-first-rules-page","passed":True})
        for _ in range(count):
            await self.click("run.encounter.picker.0.next")
            text,actual,choices=await self.picker()
            index=index%count+1
            assert actual==index and choices==count,(text,index,count)
            await self.info_visibility()
            await self.saved(initial,"activity-picker")
        assert (await self.picker())[0]==original
        await self.click("run.encounter.picker.0.previous")
        assert (await self.picker())[1]==(index-2)%count+1
        await self.click("run.encounter.picker.0.next")
        assert (await self.picker())[0]==original
        await self.audit("activity-picker-restored")
        final=await self.query("rogue.state")
        assert final["stunts"]==initial["stunts"] and final["rivals"]==initial["rivals"]
        await self.click("run.stop.footer.3")
        assert any(t["name"].startswith("run.shop.") for t in (await self.query("vr.targets"))["targets"])
        await self.saved(initial,"activity-back-without-start")

    async def shop(self):
        initial=await self.query("rogue.state")
        assert initial["phase"] in ("route_stop","service") and initial["shop"]["id"]>0,initial["phase"]
        await self.layout("shop-footer",{1,2,3},"run.stop.footer.")
        await self.exterior("shop","WreckRunShopScreen")
        targets=(await self.query("vr.targets"))["targets"]
        cards=[t for t in targets if t["name"].startswith("run.shop.")]
        assert cards,targets
        descriptions=[]
        for card in cards:
            before,after,_=await self.hover(card["name"])
            assert after["clicks"]==before["clicks"]
            page,total=await self.info_visibility()
            descriptions.append({"target":card["name"],"page":page,"pages":total})
            if total>1:
                await self.click("run.stop.footer.5")
                assert await self.description_page()==(page%total+1,total)
        self.cases.append({"case":"shop-info-visibility-and-paging","passed":True,"descriptions":descriptions})
        await self.saved(initial,"shop-hover")
        await self.click("run.stop.footer.2")
        await self.build_view("shop-build")
        await self.layout("shop-returned",{1,2,3},"run.stop.footer.")
        await self.saved(initial,"shop-build-and-back")
        if self.args.leave:
            await self.leave_stop(initial)

    async def audit(self,label,actions_only=False):
        targets=(await self.query("vr.targets"))["targets"]
        await self.capture(label)
        overlaps=[]
        nested=[]
        for i,a in enumerate(targets):
            for j,b in enumerate(targets[i+1:],i+1):
                l,t,r,bottom=a["rect"];l2,t2,r2,b2=b["rect"]
                if min(r,r2)-max(l,l2)<=1 or min(bottom,b2)-max(t,t2)<=1:continue
                child,parent=(a,b) if ".picker." in a["name"] else (b,a)
                card=child["name"].split(".")[3] if ".picker." in child["name"] else ""
                if card and parent["name"] in ("run.encounter."+card,"run.activity."+card):
                    cl,ct,cr,cb=child["rect"];pl,pt,pr,pb=parent["rect"]
                    assert pl<=cl<cr<=pr and pt<=ct<cb<=pb,(child,parent)
                    assert targets.index(child)<targets.index(parent),"Picker must win over its containing card"
                    nested.append([child["name"],parent["name"]])
                else:overlaps.append([a["name"],b["name"]])
        assert not overlaps,(label,overlaps)
        self.cases.append({"case":label,"passed":True,"targets":targets,"intentional_picker_containment":nested})

    async def picker(self):
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        labels=[n for n in nodes if "WreckRunEncounterScreen" in n["path"] and n["path"].endswith("/EncounterPicker")]
        assert len(labels)==1,labels
        text=labels[0]["text"]
        match=re.search(r"(\d+)\s*/\s*(\d+)",text)
        assert match,text
        return text,int(match[1]),int(match[2])

    async def stop(self):
        initial=await self.query("rogue.state")
        assert initial["phase"]=="route_stop" and initial["route_stop"]["encounter"]["name"]=="Parts Swap",initial["route_stop"]
        await self.layout("stop-footer",{1,3},"run.stop.footer.")
        await self.exterior("stop","WreckRunEncounterScreen")
        targets=(await self.query("vr.targets"))["targets"]
        arrows=[t for t in targets if t["name"].startswith("run.encounter.picker.0.")]
        assert len(arrows)==2 and all(t["rect"][2]-t["rect"][0]>=55.5 and t["rect"][3]-t["rect"][1]>=47.5 for t in arrows),arrows
        original,index,count=await self.picker()
        assert count>1
        for _ in range(count):
            await self.click("run.encounter.picker.0.next")
            text,actual,total=await self.picker()
            index=index%count+1
            assert actual==index and total==count,(text,index,count)
            await self.saved(initial,"picker-next")
        assert (await self.picker())[0]==original
        await self.click("run.encounter.picker.0.previous")
        _,actual,total=await self.picker()
        assert actual==(index-2)%count+1 and total==count
        await self.click("run.encounter.picker.0.next")
        assert (await self.picker())[0]==original
        await self.saved(initial,"picker-restored")
        self.cases.append({"case":"picker-wrap-and-reverse","passed":True,"parts":count,"original":original})
        if self.args.leave:
            await self.leave_stop(initial)

    async def leave_stop(self,initial):
        await self.click("run.stop.footer.3")
        for _ in range(60):
            final=await self.query("rogue.state")
            if final["ready"] and final["save_stage"]=="ready" and final["phase"]=="route":break
            await asyncio.sleep(.2)
        assert final["phase"]=="route" and final["sequence"]==initial["sequence"]+1,final
        for field in ("inventory","scrap","health","tiers","receipt_count"):
            assert final[field]==initial[field],(field,initial[field],final[field])
        self.cases.append({"case":"drive-on-once-without-purchase-or-swap","passed":True,"before_sequence":initial["sequence"],"after_sequence":final["sequence"]})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"pid":args.pid,"passed":False,"scope":__doc__,"hand":args.hand}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=StopTest(args,session)
            try:
                await test.capture("input-baseline")
                for hand in ("left","right"):
                    await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"baseline-"+hand)
                await asyncio.sleep(.3)
                errors=(await test.query("game.errors"))["total"]
                await (test.activity() if args.activity_only else test.shop() if args.shop_only else test.stop())
                assert (await test.query("game.errors"))["total"]==errors
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
    print("Wreck Run stop regression passed:",args.evidence,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid",required=True,type=int)
    parser.add_argument("--data-dir",required=True,type=Path)
    parser.add_argument("--evidence",required=True,type=Path)
    parser.add_argument("--hand",choices=("left","right"),default="right")
    parser.add_argument("--leave",action="store_true")
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--shop-only",action="store_true")
    mode.add_argument("--activity-only",action="store_true")
    asyncio.run(run(parser.parse_args()))
