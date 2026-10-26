"""Keep a menu leaderboard usable while the other ray moves within one card.

Start on the first Flatout Mode page with its first three events unlocked.
All interaction uses screenshot-derived XR input; native diagnostics are read-only.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from flatout_frontend_e2e import Frontend, ROOT, ClientSession, StdioServerParameters, stdio_client


class LeaderboardHover(Frontend):
    async def hand_hover(self, name, hand, fraction=(.5,.5)):
        _,aim=await self.point_args(name,fraction,hand=hand)
        receipt=await self.tool('hover_ui_at',aim,hand+'-hover-'+name)
        assert receipt['status']=='input_delivered',receipt

    async def enabled_context(self, index):
        menu=await self.query('menu.get_state')
        native=await self.query('vr.targets')
        assert menu['current_menu']=='INSANITY' and menu['selection_index']==index,menu
        assert any(t['name']=='helper.Secondary.1' for t in native['targets']),('Other ray disabled the active leaderboard',menu,native)
        return native

    async def run_hover(self):
        owner=self.args.owner_hand
        spare='left' if owner=='right' else 'right'
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='INSANITY' and not (await self.query('ui.get_message_box'))['open'],menu
        # Forest Arena has no leaderboard. Leave its ray resting there while
        # the right hand chooses Total Destruction and points at Leaderboard.
        await self.hand_hover('challenge.event.1',spare)
        assert (await self.query('menu.get_state'))['selection_index']==1
        assert not any(t['name']=='helper.Secondary.1' for t in (await self.query('vr.targets'))['targets'])
        self.cases.append({'case':'unsupported-event-keeps-leaderboard-legitimately-disabled','passed':True})
        await self.hand_hover('challenge.event.0',owner)
        await self.hand_hover('helper.Secondary.1',owner)
        await self.enabled_context(0)
        samples=[]
        for fraction in ((.65,.5),(.35,.5),(.6,.5)):
            await self.hand_hover('challenge.event.1',spare,fraction)
            for _ in range(12):
                native=await self.enabled_context(0)
                samples.append({'hovered':native['hovered'],'enabled':True})
                await asyncio.sleep(.02)
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'leaderboard-keeps-context-with-two-rays')
        self.cases.append({'case':'same-card-spare-ray-motion-keeps-leaderboard-enabled','passed':True,'samples':samples})
        # A different target is deliberate hover intent and still transfers ownership.
        await self.hand_hover('challenge.event.2',spare)
        await self.enabled_context(2)
        self.cases.append({'case':'different-card-deliberate-ray-entry-transfers-context','passed':True})
        # Both rays now rest on Leaderboard. A stationary spare hand must still
        # be able to press it, even though it did not enter a different target.
        await self.hand_hover('helper.Secondary.1',spare)
        await self.hand_hover('challenge.event.0',owner)
        await self.hand_hover('helper.Secondary.1',owner)
        before=await self.enabled_context(0)
        _,aim=await self.point_args('helper.Secondary.1',hand=spare)
        command={**aim,'command_id':uuid.uuid4().hex,'hold_frames':45}
        receipt=await self.tool('click_ui_at',command,'stationary-spare-hand-opens-leaderboard')
        retry=await self.tool('click_ui_at',command,'leaderboard-press-same-command-retry')
        assert retry['delivery']['sync_id']==receipt['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks']==before['clicks']+1
        assert (await self.query('menu.get_state'))['current_menu']=='LEADERBOARD'
        deadline=asyncio.get_running_loop().time()+20
        while (await self.query('leaderboard.get_state'))['busy']:
            assert asyncio.get_running_loop().time()<deadline,'Leaderboard read did not finish'
            await asyncio.sleep(.2)
        assert not (await self.query('ui.get_message_box'))['open']
        self.cases.append({'case':'stationary-spare-trigger-opens-leaderboard-once','passed':True})
        cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(cancel)
        await self.enabled_context(0)
        self.cases.append({'case':'leaderboard-back-preserves-chosen-event','passed':True})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'owner_hand':args.owner_hand}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=LeaderboardHover(args,session)
            try:
                await test.run_hover()
                report['native_errors']=await test.query('game.errors')
                assert report['native_errors']['total']==0,report['native_errors']
                report['passed']=True
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                report['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--owner-hand',choices=('left','right'),default='right',help='Ray that stays on Leaderboard while the spare wrist moves')
    p.add_argument('--hand',choices=('left','right'),default='right',help='Hand used for the final Back action')
    asyncio.run(run(p.parse_args()))
