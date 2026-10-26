"""Verify the native reward presentation through screenshot-derived ray input.

Stage the existing development preview with ui.preview_reward before this test.
The test itself uses read-only diagnostics and real XR hover/trigger actions.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel


class Reward(Frontend):
    async def run_reward(self):
        for _ in range(150):
            state=await self.query('ui.reward_state')
            targets=await self.query('vr.targets')
            if state['open'] and any(t['name']=='helper.Validate.0' for t in targets['targets']):
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError(('Reward never became ready',state,targets))
        assert state['credits']==self.args.expected_credits,state
        assert (await self.query('menu.get_state'))['current_menu']=='REWARD'
        if not self.args.closing_only:
            await self.reading_comfort('reward',hover_target='helper.Validate.0')
            await self.audit('reward-action-spacing')
            nodes=(await self.query('hud.rects',{'root':'menu','max_depth':24}))['nodes']
            captions=[n['text'] for n in nodes if n['visible'] and n['path'].endswith('/RayActionLabel')]
            self.cases.append({'case':'reward-action-names-continue','passed':captions==['Continue'],'captions':captions})
            assert captions==['Continue'],('Reward needs a clear Continue action',captions)
            music=[n for n in nodes if n['visible'] and n['kind']=='panel' and '/Dummy/Music/' in n['path']]
            assert not music,('Legacy music controls remain beside the reward',music)
            self.cases.append({'case':'reward-has-no-legacy-music-controls','passed':True})
            content=[n for n in nodes if n['visible'] and n['kind'] in ('text','panel') and '/Menu/Dummy/Results/' in n['path']]
            assert content,'Reward artwork is missing'
            right=max(n['rect'][2] for n in content);bottom=max(n['rect'][3] for n in content)
            action=next(t for t in (await self.query('vr.targets'))['targets'] if t['name']=='helper.Validate.0')
            gap=action['rect'][1]-bottom
            assert 24<=gap<=48 and abs(action['rect'][2]-right)<=4,('Continue is detached from the reward',action,right,bottom)
            self.cases.append({'case':'continue-docks-below-reward-with-padding','passed':True,'gap':gap,'action':action,'body_right':right,'body_bottom':bottom})
            clicks=(await self.query('vr.targets'))['clicks']
            source=await self.capture('reward-blank-source')
            x,y=quad_pixel(source,(400,800,400,800))
            carrier=max((p for p in source['geometry']['surfaces'] if p['visibility']==0),key=lambda p:p['size'][0]*p['size'][1])
            blank={'capture_id':source['capture_id'],'x':float(x),'y':float(y),'surface_id':carrier['id'],'hand':self.args.hand,'origin_mode':'hand','dwell_frames':30,'hold_frames':45,'command_id':uuid.uuid4().hex}
            pressed=await self.tool('click_ui_at',blank,'reward-blank-held-click')
            assert pressed['status']=='input_delivered',pressed
            await self.tool('click_ui_at',blank,'reward-blank-retry')
            assert (await self.query('vr.targets'))['clicks']==clicks,'A blank-area press dismissed the reward'
            assert (await self.query('ui.reward_state'))['open']
            self.cases.append({'case':'reward-blank-area-held-click-and-retry-inert','passed':True})
            await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'reward-ready-both-eyes')
        before,hovered,aim=await self.hover('helper.Validate.0')
        args={**aim,'hold_frames':self.args.hold_frames,'timeout':30,'command_id':uuid.uuid4().hex}
        samples=[]
        observing=True
        async def observe_outro():
            while observing:
                current=await self.query('ui.reward_state')
                native=await self.query('vr.targets')
                samples.append({'state':current,'clicks':native['clicks'],'targets':native['targets']})
                await asyncio.sleep(.005)
        observer=asyncio.create_task(observe_outro())
        try:
            receipt=await self.tool('click_ui_at',args,'reward-continue-once')
        finally:
            observing=False
            await observer
        assert receipt['status']=='input_delivered',receipt
        closing=await self.query('ui.reward_state')
        native=await self.query('vr.targets')
        assert native['clicks']==before['clicks']+1,(before,native)
        outro=[s for s in samples if s['state']['open'] and s['clicks']==before['clicks']+1]
        # The first observation can straddle the press frame; the next sampled
        # native update must already have retired the previous helper target.
        assert len(outro)>=2,('No observable native outro interval',samples)
        stale=[s for s in outro[1:] if any(t['name'].startswith('helper.Validate.') for t in s['targets'])]
        self.cases.append({'case':'closing-reward-retires-continue','passed':not stale,'samples':outro})
        assert not stale,('Continue remains active during reward dismissal',stale)
        retry=await self.tool('click_ui_at',args,'reward-continue-retry')
        assert retry['delivery']['sync_id']==receipt['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks']==native['clicks'],'Retry activated Continue twice'
        for _ in range(150):
            state=await self.query('ui.reward_state')
            if not state['open'] and not state['queued']:
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError(('Reward did not close',state))
        menu=await self.query('menu.get_state')
        assert menu['is_open'] and menu['current_menu']!='REWARD',menu
        assert not (await self.query('ui.get_message_box'))['open']
        assert state['credits']==self.args.expected_credits,('Presentation preview changed progression',state)
        self.cases.append({'case':'reward-continue-and-retry-return-once-without-granting-credits','passed':True,'menu':menu,'state':state})
        await self.capture('reward-dismissed')


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'hand':args.hand,'closing_only':args.closing_only,'hold_frames':args.hold_frames}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Reward(args,session)
            try:
                await test.run_reward()
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
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--expected-credits',type=int,required=True)
    p.add_argument('--hold-frames',type=int,default=1)
    p.add_argument('--closing-only',action='store_true')
    asyncio.run(run(p.parse_args()))
