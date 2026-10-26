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
from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import world_pixel, ui_surface


class Reward(Frontend):
    async def return_controls(self):
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='MAIN MENU' and menu['is_3d'],menu
        native=await self.query('vr.targets')
        assert 'vr_frame' in native,('Reward return did not restore the spatial menu frame',native)
        item=next(i for i in menu['items'] if i['title']=='OPTIONS')
        capture=await self.capture('reward-return-options-source')
        frame={**native['vr_frame'],'panel':{**menu['spatial_layout'],'origin':item['rendered_position']},
               'panel_width':1.,'panel_height':1.}
        state={'vr':{'frame':frame}}
        x,y=world_pixel(state,capture,item['rendered_position'],'left')
        w,h=capture['image_size']
        assert 0<=x<w and 0<=y<h,('Returned Options label is outside the view',item,x,y)
        aim={'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
             'origin_mode':'hand','surface':ui_surface(state),'ray_offset':RAY_OFFSET,'dwell_frames':30}
        hovered=await self.tool('hover_ui_at',aim,'reward-return-options-hover')
        assert hovered['status']=='input_delivered',hovered
        assert (await self.query('menu.get_state'))['selection_index']==item['index']
        args={**aim,'hold_frames':45,'timeout':30,'command_id':uuid.uuid4().hex}
        receipt=await self.tool('click_ui_at',args,'reward-return-options-click')
        assert receipt['status']=='input_delivered',receipt
        assert (await self.query('options.get_state'))['open'],'Returned main-menu ray could not open Options'
        retry=await self.tool('click_ui_at',args,'reward-return-options-retry')
        assert retry['delivery']['sync_id']==receipt['delivery']['sync_id']
        self.cases.append({'case':'reward-return-spatial-options-held-click-and-retry','passed':True,'hand':self.args.hand})
        await self.options_comfort()
        cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(cancel)
        returned=await self.query('menu.get_state')
        assert returned['current_menu']=='MAIN MENU' and returned['is_open'],returned
        assert 'vr_frame' in await self.query('vr.targets'),'Options Back lost the spatial frame'
        await self.capture('reward-return-main-usable')
        self.cases.append({'case':'reward-return-options-back-restores-main','passed':True})

    async def run_reward(self, kind=None, has_next=False):
        kind=kind or self.args.reward_kind
        for _ in range(150):
            state=await self.query('ui.reward_state')
            targets=await self.query('vr.targets')
            if state['open'] and any(t['name']=='helper.Validate.0' for t in targets['targets']):
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError(('Reward never became ready',state,targets))
        assert state['credits']==self.args.expected_credits,state
        assert state['phase']=='ready' and state['queued']==has_next,state
        presentation=state['presentation']
        assert (await self.query('menu.get_state'))['current_menu']=='REWARD'
        if not self.args.closing_only:
            await self.reading_comfort('reward',hover_target='helper.Validate.0')
            await self.audit('reward-action-spacing')
            nodes=(await self.query('hud.rects',{'root':'menu','max_depth':24}))['nodes']
            markers={'money':'/Results/Menu_Icons_Main/Custom_Money',
                     'car':'/Results/Car/','driver':'/Results/Opponent/'}
            artwork=markers[kind]
            actual_art=[n for n in nodes if n['visible'] and n['kind']=='panel' and artwork in n['path']
                        and n['rect'][2]>n['rect'][0] and n['rect'][3]>n['rect'][1]]
            names=[n['text'] for n in nodes if n['visible'] and n['path'].endswith('/Results/TXT_Name')]
            assert actual_art and names and all(names),('Expected reward artwork or name missing',kind,actual_art,names)
            self.cases.append({'case':'reward-'+kind+'-artwork-and-name','passed':True,'presentation':presentation,'artwork':actual_art,'names':names})
            leftover=[n for n in nodes if n['visible'] and n['kind']=='panel'
                      and any(marker in n['path'] for other,marker in markers.items() if other!=kind)]
            assert not leftover,('Previous reward artwork is still visible',kind,leftover)
            self.cases.append({'case':'reward-'+kind+'-retires-previous-artwork','passed':True,'presentation':presentation})
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
            ready=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'reward-ready-both-eyes')
            assert len(ready['geometry']['surfaces'])<=2,('Reward retained old hover faces',ready['geometry']['surfaces'])
            self.cases.append({'case':'reward-'+kind+'-retains-only-current-hover-face','passed':True,'surfaces':ready['geometry']['surfaces']})
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
            if has_next:
                held_args={k:v for k,v in args.items() if k!='hold_frames'}
                receipt=await self.tool('hold_ui_at',held_args,'reward-continue-hold-across-presentation')
                assert receipt['delivery']['button_down'] is True,receipt
                deadline=asyncio.get_running_loop().time()+7
                while asyncio.get_running_loop().time()<deadline:
                    current=await self.query('ui.reward_state')
                    native=await self.query('vr.targets')
                    assert current['presentation']<=presentation+1,('Held press skipped a reward',current)
                    if current['presentation']==presentation+1 and current['phase']=='ready' and native['targets']:
                        break
                    await asyncio.sleep(.05)
                else:
                    raise AssertionError(('Next reward did not become ready within the held-trigger lease',current,native))
                assert current['open'] and native['clicks']==before['clicks']+1,(current,native)
                _,next_aim=await self.point_args('helper.Validate.0')
                refreshed=await self.tool('hold_ui_at',{**next_aim,'command_id':uuid.uuid4().hex},'next-reward-refresh-continuous-hold')
                assert refreshed['delivery']['button_down'] is True,refreshed
                native=await self.query('vr.targets')
                assert native['hovered']=='helper.Validate.0' and native['clicks']==before['clicks']+1,native
                assert (await self.query('ui.reward_state'))['presentation']==presentation+1
                retried=await self.tool('hold_ui_at',held_args,'prior-reward-held-command-retry')
                assert retried['delivery']['sync_id']==receipt['delivery']['sync_id']
                assert (await self.query('vr.targets'))['clicks']==before['clicks']+1
                self.cases.append({'case':'next-reward-rejects-continuous-trigger-and-old-command-retry','passed':True,
                                   'from_presentation':presentation,'to_presentation':current['presentation'],
                                   'held_delivery':receipt['delivery'],'refreshed_delivery':refreshed['delivery']})
            else:
                receipt=await self.tool('click_ui_at',args,'reward-continue-once')
        finally:
            try:
                if has_next:
                    await self.tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand},'next-reward-release-old-trigger')
            finally:
                observing=False
                await observer
        assert receipt['status']=='input_delivered',receipt
        closing=await self.query('ui.reward_state')
        native=await self.query('vr.targets')
        assert native['clicks']==before['clicks']+1,(before,native)
        outro=[s for s in samples if s['state']['presentation']==presentation and s['state']['phase']=='outro' and s['clicks']==before['clicks']+1]
        # The first observation can straddle the press frame; the next sampled
        # native update must already have retired the previous helper target.
        assert len(outro)>=2,('No observable native outro interval',samples)
        stale=[s for s in outro[1:] if any(t['name'].startswith('helper.Validate.') for t in s['targets'])]
        self.cases.append({'case':'closing-reward-retires-continue','passed':not stale,'samples':outro})
        assert not stale,('Continue remains active during reward dismissal',stale)
        if has_next:
            assert closing['presentation']==presentation+1 and closing['phase']=='ready',closing
            assert closing['credits']==self.args.expected_credits,closing
            await self.capture('reward-'+kind+'-next-presentation-ready')
            self.cases.append({'case':'next-reward-release-keeps-following-presentation-open','passed':True,'state':closing})
            return
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
        await self.return_controls()


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'hand':args.hand,'reward_kind':args.reward_kind,'sequence':args.sequence,
            'closing_only':args.closing_only,'hold_frames':args.hold_frames}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Reward(args,session)
            try:
                sequence=args.sequence.split(',') if args.sequence else [args.reward_kind]
                for index,kind in enumerate(sequence):
                    await test.run_reward(kind,has_next=index+1<len(sequence))
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
    p.add_argument('--reward-kind',choices=('money','car','driver'),default='money',help='Assert the actual loaded artwork for the staged presentation variant')
    p.add_argument('--sequence',help='Comma-separated kinds in the staged presentation order; keep each trigger held over the following ready reward')
    p.add_argument('--hold-frames',type=int,default=1)
    p.add_argument('--closing-only',action='store_true')
    args=p.parse_args()
    if args.sequence and any(k not in ('money','car','driver') for k in args.sequence.split(',')):
        p.error('--sequence contains an unsupported reward kind')
    asyncio.run(run(args))
