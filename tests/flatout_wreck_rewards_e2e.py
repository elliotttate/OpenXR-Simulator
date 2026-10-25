"""Native Wreck Run build/result/reward/pause UI using real XR screenshot input.

Debug fixture preparation is separate. Game diagnostics are read-only. Start on
the requested native screen in an isolated profile; --choose commits one actual
reward through the ray button after the read-only navigation checks.
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
SAVED_FIELDS=("sequence","phase","inventory","reward","scrap","health","tiers","route","receipt_count","transaction_count")


def verify_reward_choice(initial,final):
    offer=initial["reward"]["offers"][0]
    assert offer["type"] in ("scrap","category"),"--choose requires a first-card scrap or category fixture"
    # The menu automatically opens a pending roadside stop after the reward.
    # Those are two separate journal commits, with one reward payout.
    opened=bool(final["phase"]=="route_stop" and final["route_stop"] and final["route_stop"]["opened"]
                and (not initial["route_stop"] or initial["route_stop"]["id"]!=final["route_stop"]["id"]))
    expected=initial["sequence"]+1+int(opened)
    assert final["sequence"]==expected,("Unexpected reward/stop settlement count",expected,final["sequence"])
    scrap=offer["scrap"] if offer["type"]=="scrap" else 0
    tiers=initial["tiers"].copy()
    if offer["type"]=="category":
        category=offer["item"]
        assert offer["card"]["tier_from"]==tiers[category] and offer["card"]["tier_to"]==tiers[category]+1,offer
        tiers[category]+=1
    assert final["tiers"]==tiers,("Category reward changed more than the chosen tier once",initial["tiers"],tiers,final["tiers"])
    assert final["scrap"]==initial["scrap"]+scrap,("Reward paid more or less than once",initial["scrap"],offer,final["scrap"])
    assert final["inventory"]==initial["inventory"] and final["receipt_count"]==initial["receipt_count"]
    assert final["reward"]["round_id"]!=initial["reward"]["round_id"]
    return {"before_sequence":initial["sequence"],"after_sequence":final["sequence"],"offer_type":offer["type"],"awarded_scrap":scrap,"tiers_before":initial["tiers"],"tiers_after":final["tiers"],"auto_stop_opened":opened,"phase":final["phase"]}


class RewardTest(WreckFooterTest):
    async def saved(self,initial,label):
        assert initial["ready"] and initial["save_stage"]=="ready",initial
        for _ in range(3):
            await asyncio.sleep(.25)
            current=await self.query("rogue.state")
            assert current["ready"] and current["save_stage"]=="ready",(label,"Navigation started a save",current)
            assert {k:current[k] for k in SAVED_FIELDS}=={k:initial[k] for k in SAVED_FIELDS},(label,initial,current)
        self.cases.append({"case":label+"-saved-state-unchanged","passed":True,"sequence":current["sequence"],"scrap":current["scrap"],"transaction_count":current["transaction_count"]})

    async def exterior(self,label,root):
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        panels=[n for n in nodes if root in n["path"] and n["kind"]=="panel" and n["visible"] and n["color"]["a"]>0]
        assert panels,(label,"Requested native screen is not visible")
        full=[n for n in panels if n["rect"][0]<=1 and n["rect"][1]<=1 and n["rect"][2]>=1919 and n["rect"][3]>=1079]
        assert not full,(label,"Full-screen overlay hides the game",full)
        self.cases.append({"case":label+"-clear-exterior","passed":True})

    async def build_view(self,label="build"):
        initial=await self.query("rogue.state")
        await self.layout(label+"-footer",{1},"run.build.footer.")
        await self.exterior(label,"WreckRunBuildScreen")
        targets=(await self.query("vr.targets"))["targets"]
        tiles=[t for t in targets if t["name"].startswith("run.build.") and ".footer." not in t["name"]]
        assert tiles,"Use an isolated fixture with at least one installed part"
        for tile in tiles:
            before,after,_=await self.hover(tile["name"])
            assert after["clicks"]==before["clicks"]
        if "run.build.footer.2" in {t["name"] for t in targets}:
            await self.click("run.build.footer.2")
            await self.audit(label+"-next-page")
        await self.saved(initial,label+"-hover-and-page")
        await self.click("run.build.footer.1")
        await self.saved(initial,label+"-back")

    async def result_and_cards(self):
        initial=await self.query("rogue.state")
        assert initial["phase"]=="reward" and initial["ready"]
        await self.layout("result-footer",{1,2},"run.result.footer.")
        await self.exterior("result","WreckRunResultScreen")
        await self.click("run.result.footer.2")
        await self.build_view("result-build")
        await self.layout("result-returned",{1,2},"run.result.footer.")
        await self.click("run.result.footer.1")
        await self.layout("reward-footer",{1,2,3,4},"run.reward.footer.")
        await self.exterior("reward","WreckRunRewardScreen")
        targets=(await self.query("vr.targets"))["targets"]
        cards=[t for t in targets if t["name"].startswith("run.reward.") and ".footer." not in t["name"]]
        assert cards,targets
        for card in cards:
            before,after,_=await self.hover(card["name"])
            assert after["clicks"]==before["clicks"]
        await self.saved(initial,"reward-card-hover")
        await self.click("run.reward.footer.2")
        await self.build_view("reward-build")
        await self.layout("reward-returned",{1,2,3,4},"run.reward.footer.")
        await self.click("run.reward.footer.4")
        await self.layout("reward-back-to-result",{1,2},"run.result.footer.")
        await self.saved(initial,"reward-back")
        await self.click("run.result.footer.1")
        if self.args.choose:
            assert initial["reward"]["offers"][0]["type"] in ("scrap","category"),"--choose requires a first-card scrap or category fixture"
            await self.hover(cards[0]["name"])
            await self.click("run.reward.footer.1")
            for _ in range(60):
                final=await self.query("rogue.state")
                if final["ready"] and final["save_stage"]=="ready" and final["sequence"]>initial["sequence"]:
                    await asyncio.sleep(.3)
                    settled=await self.query("rogue.state")
                    if settled["ready"] and settled["sequence"]==final["sequence"]:
                        final=settled
                        break
                await asyncio.sleep(.2)
            self.cases.append({"case":"one-reward-application","passed":True,**verify_reward_choice(initial,final)})
        else:
            await self.saved(initial,"result-reward-navigation")
        await self.capture("reward-final")

    async def pause(self):
        initial=await self.query("rogue.state")
        assert (await self.query("hud.pause_menu"))["is_open"]
        await self.layout("wreck-pause-footer",{0,1},"run.pause.footer.")
        await self.exterior("pause","WreckRunPause")
        # Select operates on the hovered row, rather than on its own position.
        await self.hover("run.pause.1")
        await self.click("run.pause.footer.0")
        await self.layout("pause-build-footer",{0,1},"run.pause.build.footer.")
        await self.exterior("pause-build","WreckRunPauseBuild")
        await self.click("run.pause.build.footer.1")
        await self.click("run.pause.3")
        live=await self.query("vr.targets")
        assert {t["name"] for t in live["targets"]}=={"run.pause.confirm.keep","run.pause.confirm.quit"},live
        await self.audit("pause-confirm-spacing")
        await self.exterior("pause-confirm","WreckRunPause")
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        highlights=[n for n in nodes if n["visible"] and n["path"].endswith(("/PauseRowFill","/PauseRowRule"))]
        assert not highlights,("Background pause row still looks active during confirmation",highlights)
        words=[n for n in nodes if n["visible"] and n["path"].endswith("/PauseWord")]
        assert len(words)==4 and all(max(n["color"][c] for c in ("r","g","b"))<=.65 for n in words),words
        self.cases.append({"case":"pause-confirm-background-muted","passed":True})
        await self.click("run.pause.confirm.keep")
        nodes=(await self.query("hud.rects",{"root":"menu","max_depth":14}))["nodes"]
        assert sum(n["visible"] and n["path"].endswith("/PauseRowFill") for n in nodes)==1,"Closing confirmation did not restore pause focus"
        await self.saved(initial,"pause-build-and-confirm")
        await self.click("run.pause.footer.1")
        assert not (await self.query("hud.pause_menu"))["is_open"]
        await self.saved(initial,"resume")


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ,"SIMXR_DATA_DIR":str(args.data_dir.resolve())})
    result={"pid":args.pid,"passed":False,"scope":__doc__,"screen":args.screen,"hand":args.hand}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=RewardTest(args,session)
            try:
                await test.capture("input-baseline")
                for hand in ("left","right"):
                    await test.tool("release_ui_pointer",{"capture_id":test.last_capture["capture_id"],"hand":hand},"baseline-"+hand)
                await asyncio.sleep(.3)
                errors=(await test.query("game.errors"))["total"]
                if args.screen=="result":await test.result_and_cards()
                elif args.screen=="build":await test.build_view()
                else:await test.pause()
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
    print("Wreck Run reward/pause regression passed:",args.evidence,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid",required=True,type=int)
    parser.add_argument("--data-dir",required=True,type=Path)
    parser.add_argument("--evidence",required=True,type=Path)
    parser.add_argument("--hand",choices=("left","right"),default="right")
    parser.add_argument("--screen",choices=("build","result","pause"),default="result")
    parser.add_argument("--choose",action="store_true",help="Select the first scrap/category card once; requires that fixture")
    asyncio.run(run(parser.parse_args()))
