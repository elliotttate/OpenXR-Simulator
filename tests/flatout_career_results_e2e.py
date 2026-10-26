"""Walk Career cup results into the next round or back to events through real XR input.

Start on Best Pilot, Race Recap or Cup Ranking. The default test cancels Main Menu
confirmation and continues to the next round. --final-round verifies the last
round returns to the event browser; use an isolated progression slot.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT
from flatout_ui_depth_contract import assert_reading_distance


class CareerResults(Frontend):
    async def run_results(self):
        game=await self.query('game.get_state')
        assert game['game_mode']=='career' and not game['is_main_menu'],game
        transitions={2:3,3:6,6:7}
        state=await self.query('race.end_screen')
        if self.args.final_round:
            assert state['screen'] in transitions,('Start on a result page of the final cup round',state)
        while state['screen']!=7:
            screen=state['screen']
            assert screen in transitions,('Start on Best Pilot, Race Recap or Cup Ranking',state)
            await self.audit('career-result-page-'+str(screen))
            targets=(await self.query('vr.targets'))['targets']
            for target in targets:
                await self.hover(target['name'])
            name=next(t['name'] for t in targets if t['name'].startswith('endhelper.Validate.'))
            if self.args.final_round and screen==6:
                await self.complete_cup(name)
                return
            await self.click(name)
            after=await self.query('race.end_screen')
            assert after['screen']==transitions[screen],('Held Continue skipped a results page',state,after)
            self.cases.append({'case':'career-result-page-advances-once','passed':True,'before':screen,'after':after['screen']})
            state=after
        await self.audit('career-next-race-actions')
        stereo=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'career-next-race-both-eyes')
        placement=assert_reading_distance(stereo,3,4.5)
        self.cases.append({'case':'next-race-preserves-reading-distance','passed':True,**placement})
        labels=[]
        for root in ('menu','hud'):
            labels+=(await self.query('hud.rects',{'root':root,'max_depth':22}))['nodes']
        captions=[n['text'] for n in labels if n['visible'] and n['path'].endswith('/RayActionLabel')]
        assert 'Continue' in captions and 'Confirm' not in captions and 'Start' not in captions,('Next round must say Continue',captions)
        self.cases.append({'case':'next-race-names-continue-action','passed':True,'captions':captions})
        targets=(await self.query('vr.targets'))['targets']
        cancel=next(t['name'] for t in targets if '.Cancel.' in t['name'])
        await self.click(cancel)
        assert (await self.query('ui.get_message_box'))['open'],'Main Menu must ask before leaving the cup'
        names=[t['name'] for t in (await self.query('vr.targets'))['targets']]
        assert set(names)=={'msgbox.validate','msgbox.cancel'},names
        await self.popup_spacing()
        await self.click('msgbox.cancel')
        assert not (await self.query('ui.get_message_box'))['open']
        assert (await self.query('race.end_screen'))['screen']==7,'Cancel lost the next-round preview'
        self.cases.append({'case':'next-race-main-menu-confirmation-cancel-preserves-cup','passed':True})
        targets=(await self.query('vr.targets'))['targets']
        validate=next(t['name'] for t in targets if '.Validate.' in t['name'])
        await self.click(validate)
        for _ in range(120):
            current=await self.query('game.get_state')
            if not current['is_loading'] and not current['is_main_menu'] and current['state']=='race':
                break
            await asyncio.sleep(.5)
        else:
            raise AssertionError(('Next round did not finish loading',current))
        assert current['game_mode']=='career',current
        assert (await self.query('race.end_screen'))['screen']==0,'Continue stayed on the old result screen'
        self.cases.append({'case':'next-race-continue-loads-career-round','passed':True,'previous_level':game['current_level'],'level':current['current_level']})
        await self.capture('career-next-round-loaded')


    async def complete_cup(self, name):
        targets=(await self.query('vr.targets'))['targets']
        assert len(targets)==1 and targets[0]['name']==name,('Final cup ranking needs one Continue action',targets)
        await self.footer_contrast('endhelper.')
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'career-final-cup-both-eyes')
        await self.click(name)
        for _ in range(120):
            game=await self.query('game.get_state')
            if game['is_main_menu'] and game['main_menu_ready'] and not game['is_loading']:
                break
            await asyncio.sleep(.5)
        else:
            raise AssertionError(('Final cup Continue did not return to the front end',game))
        assert not (await self.query('ui.get_message_box'))['open'],'Unexpected modal after final cup'
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='FLATOUT_MODE' and 'career' in menu,('Completed cup must return to the event browser',menu)
        await self.audit('career-complete-event-browser')
        self.cases.append({'case':'final-cup-continue-returns-to-events-once','passed':True,'game':game,'menu':menu})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'hand':args.hand,'final_round':args.final_round}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=CareerResults(args,session)
            try:
                await test.run_results()
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
    p.add_argument('--final-round',action='store_true',help='The cup is on its last round; Continue must return to the event browser')
    asyncio.run(run(p.parse_args()))
