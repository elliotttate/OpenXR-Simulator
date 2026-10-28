"""Live Steam leaderboard values in the native HTML-derived reading layout."""
import argparse,asyncio,copy,json,os,sys,uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift


class LeaderboardTest(Frontend):
    async def settled(self):
        until=asyncio.get_running_loop().time()+30
        while True:
            state=await self.query('leaderboard.get_state')
            if not state['busy'] and not (await self.query('ui.get_message_box'))['open']:return state
            assert asyncio.get_running_loop().time()<until,state;await asyncio.sleep(.1)

    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':40,'all':True}))['nodes']

    async def layout(self,label):
        state=await self.settled();nodes=await self.nodes();assert (await self.query('game.get_state'))['diagnostic_save_slot']==641062
        node=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['effective_visible'])
        authored=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['visible'])
        header=node('/LeaderboardHeader');side=node('/LeaderboardContextPanel');table=node('/LeaderboardTablePanel')
        assert table['rect'][0]-side['rect'][2]>=24
        assert not any(n['effective_visible'] and ('/Element_Leaderboard_Line/' in n['path'] or n['path'].endswith('/LeaderboardScrollControls')) for n in nodes)
        for i,p in enumerate(state['players']):
            parent='/LeaderboardScoreRow'+str(i)
            assert authored(parent+'/LeaderboardDriver')['text']==p['name'],(state,i,nodes)
            assert authored(parent+'/LeaderboardRank')['text']==str(p['rank'])
            assert authored(parent+'/LeaderboardScore')['text']
            row=authored(parent);assert table['rect'][0]+15<=row['rect'][0] and row['rect'][2]<=table['rect'][2]-15
            assert table['rect'][1]+63<=row['rect'][1] and row['rect'][3]+19<=table['rect'][3]
        if state['players']:
            assert node('/LeaderboardNativeRange')['text']==f"{state['first_position']} - {state['first_position']+len(state['players'])-1} / {state['total']}"
            # Preserve the game's formatter instead of deriving fake HTML data.
            original=[n['text'] for n in nodes if '/Element_Leaderboard_Line/' in n['path'] and n['path'].endswith('/TXT_Entry_1') and n['visible']]
            actual=[authored('/LeaderboardScoreRow'+str(i)+'/LeaderboardScore')['text'] for i in range(len(state['players']))]
            assert actual==original[:len(actual)],(actual,original)
        else:assert node('/LeaderboardStateMessage')['text'],'The empty/error state must explain itself'
        targets=(await self.query('vr.targets'))['targets'];names={t['name'] for t in targets}
        assert {'leaderboard.filter.0','leaderboard.filter.1','leaderboard.filter.2','leaderboard.refresh','leaderboard.events.open'}<=names
        assert len([n for n in names if n.startswith('row.')])==len(state['players'])
        assert all(t['enabled'] and t['physical_hover'] for t in targets)
        assert ('page.up' in names)==(state['entries']>0 and state['first_rank']>1)
        assert ('page.down' in names)==(state['entries']>0 and state['first_rank']+state['entries']-1<state['total'])
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/LeaderboardWorkspace/LeaderboardHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label+'-stereo');self.last_capture=capture;assert_reading_distance(capture,3.,4.5)
        from flatout_header_geometry import measure_header_accent
        angles=measure_header_accent(capture,self.last_image_path,header['rect']);assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit(label)
        self.cases.append({'case':label+'-real-native-values-ten-row-layout-fonts-yellow-and-stereo','passed':True,'state':state,'header':angles})
        return state

    async def disabled_page(self,direction):
        state=await self.settled();targets=await self.query('vr.targets');assert 'page.'+direction not in {t['name'] for t in targets['targets']}
        plate=next(n for n in await self.nodes() if n['path'].endswith('/LeaderboardPage'+direction.title()) and n['visible'])
        capture=await self.capture('disabled-'+direction);x,y=quad_pixel(capture,plate['rect']);args={'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','dwell_frames':16,'hold_frames':45,'command_id':uuid.uuid4().hex}
        await self.tool('click_ui_at',args,'disabled-held-'+direction);await self.tool('click_ui_at',args,'disabled-retry-'+direction)
        assert (await self.query('vr.targets'))['clicks']==targets['clicks']
        assert await self.settled()==state
        self.cases.append({'case':'disabled-'+direction+'-visible-and-inert','passed':True})

    async def prove(self):
        initial=await self.layout('initial');assert initial['first_rank']==1 and initial['entries']>0,initial
        clicks=(await self.query('vr.targets'))['clicks']
        for name in ('row.0','leaderboard.filter.1','leaderboard.events.open','leaderboard.refresh'):
            await self.hover(name);capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'physical-'+name);self.last_capture=capture;assert_hover_lift(capture)
        assert (await self.query('vr.targets'))['clicks']==clicks
        await self.disabled_page('up')
        if initial['total']>initial['entries']:
            await self.click('page.down');next_page=await self.layout('next-page');assert next_page['first_rank']>initial['first_rank']
            await self.click('page.up');assert (await self.settled())['first_rank']==initial['first_rank']
        await self.click('leaderboard.refresh');refreshed=await self.layout('refreshed');assert refreshed['table']==initial['table']
        await self.click('leaderboard.filter.1');friends=await self.layout('friends-filter');assert friends['table']==initial['table']
        await self.click('leaderboard.filter.2');own=await self.layout('own-rank-filter');assert own['table']==initial['table']
        await self.click('leaderboard.filter.0');await self.layout('global-filter-restored')
        self.cases.append({'case':'actual-steam-page-round-trip-refresh-friends-and-own-rank-native-filters','passed':True})

    async def events(self):
        initial=await self.layout('before-events');await self.click('leaderboard.events.open')
        targets=(await self.query('vr.targets'))['targets'];names={t['name'] for t in targets}
        assert 'leaderboard.events.close' in names and not any(n.startswith(('row.','helper.','leaderboard.filter.')) for n in names)
        await self.audit('event-picker-spacing')
        choices=[n for n in names if n.startswith('leaderboard.event.')];assert len(choices)==6
        await self.hover(choices[0]);capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'event-picker-stereo');self.last_capture=capture;assert_hover_lift(capture)
        if 'leaderboard.events.next' in names:
            await self.click('leaderboard.events.next');await self.click('leaderboard.events.prev')
        await self.click('leaderboard.event.1');selected=await self.layout('changed-event');assert selected['table']!=initial['table'],(initial,selected)
        await self.click('leaderboard.events.open');await self.click('leaderboard.event.0');restored=await self.layout('restored-event');assert restored['table']==initial['table']
        await self.click('leaderboard.events.open');await self.click('leaderboard.events.close');assert (await self.settled())['table']==initial['table']
        self.cases.append({'case':'real-supported-event-picker-pages-select-restore-and-close-without-fallthrough','passed':True})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Real Steam read-only leaderboards; no score writes or illustrative player fixture'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=LeaderboardTest(a,session)
            try:
                if a.layout_only:await t.layout('layout')
                elif a.events_only:await t.events()
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--events-only',action='store_true');asyncio.run(run(p.parse_args()))
