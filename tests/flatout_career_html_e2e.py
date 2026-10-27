"""Native Career hub: screenshot-derived ray input and original navigation."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel,RAY_OFFSET
from flatout_livery_e2e import world_pixel,ui_surface
from flatout_ui_depth_contract import assert_reading_distance


class CareerTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def layout(self,label):
        state=await self.query('menu.get_state');hub=state['career_hub']
        assert state['current_menu']=='FLATOUT'
        nodes=await self.nodes();targets=(await self.query('vr.targets'))['targets']
        expected={'career.hero','career.garage','career.statistics','helper.Cancel.CareerBack','helper.Validate.CareerRace'}
        if hub['active_cup']:expected.add('career.cancel-cup')
        assert {t['name'] for t in targets}==expected,targets
        root=next(n for n in nodes if n['path'].endswith('/CareerWorkspace'))
        assert root['visible']
        assert all(not x.get('rendered_bounds',{}).get('visible',True) for x in state['items']), 'Legacy spatial labels remain visible'
        assert 'spatial_back' not in state,'Legacy spatial Back remains exposed'
        assert all(20<=t['rect'][0]<t['rect'][2]<=1900 and 20<=t['rect'][1]<t['rect'][3]<=1060 for t in targets)
        assert all(t['rect'][3]-t['rect'][1]>=96 for t in targets)
        def text(name):return next(n['text'] for n in nodes if n['path'].endswith('/'+name))
        assert text('CareerGarageCar')==hub['car'] and hub['car']
        assert text('CareerMetric0')==str(hub['wins'])
        assert text('CareerMetric1')==str(hub['owned_cars'])
        assert text('CareerMetric2')==str(hub['progress_percent'])+'%'
        assert [text('CareerClassPercent'+str(i)) for i in range(3)]==[str(x)+'%' for x in hub['class_percent']]
        assert text('CareerEvent')==hub['event'] and hub['event']
        assert text('CareerHeroTitle')==state['items'][0]['title']
        assert text('CareerFooterLabel') # Back is localized, not a gamepad glyph.
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/CareerWorkspace/CareerHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        photo=await self.query('hud.get_node',{'root':'menu','name':'Menu/CareerWorkspace/CareerHero/CareerTrackPhoto','presentation':True})
        source=self.args.fox/'Flatout/Data/Assets'/hub['photo_pack'].removeprefix('DATA:/')
        skin=next(n for n in ET.parse(str(source)+'.XML').iter('skin') if n.attrib['Name']==hub['photo_skin'])
        original=list(map(float,skin.attrib['uvs'].split(';')));actual=photo['presentation']['uv']
        assert original[0]-1e-5<=actual[0]<actual[2]<=original[2]+1e-5
        assert original[1]-1e-5<=actual[1]<actual[3]<=original[3]+1e-5
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-both-eyes')
        assert_reading_distance(capture,3.,4.5)
        await self.audit(label)
        self.cases.append({'case':'native-career-layout-original-data-art-fonts-reading-surface','passed':True,'context':hub})
        return hub

    async def cancel(self):
        name=next(x['name'] for x in (await self.query('vr.targets'))['targets'] if x['name'].startswith('helper.Cancel.'))
        await self.click(name)

    async def takeover(self):
        state=await self.query('menu.get_state');original=state['career_hub']
        _,_,aim=await self.hover('career.hero',(.5,.75))
        capture=await self.capture('takeover-before-stick')
        rect=next(x['rect'] for x in (await self.query('vr.targets'))['targets'] if x['name']=='career.hero')
        _,aim=await self.point_args('career.hero',(.5,.75))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-controller-stick-takeover')
        nodes=await self.nodes()
        assert not any(n['visible'] and n['path'].endswith('/CareerWorkspace') for n in nodes),'Gamepad did not restore spatial presentation'
        fallback=await self.query('menu.get_state')
        assert fallback['career_hub']==original
        assert all(x['rendered_bounds']['visible'] for x in fallback['items'])
        self.cases.append({'case':'real-xr-stick-restores-original-spatial-controls','passed':True})
        # The old capture is invalid once the presentation changes. Use a fresh
        # spatial screenshot and the previously observed world-locked plane to
        # aim back into the board's physical location without pressing a button.
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        fresh=await self.capture('original-spatial-controls-after-stick')
        projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[carrier]
        l,t,r,b=rect;px,py=l+(r-l)*.7,t+(b-t)*.65
        x,y=quad_pixel(projection,(px,py,px,py))
        receipt=await self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':'left',
            'surface':{k:carrier[k] for k in ('pose','size')},'origin_mode':'hand','dwell_frames':45},'actual-ray-reaim-restores-board')
        assert receipt['status']=='input_delivered',receipt
        assert any(n['visible'] and n['path'].endswith('/CareerWorkspace') for n in await self.nodes())
        assert (await self.query('vr.targets'))['hovered']=='career.hero'
        assert (await self.query('menu.get_state'))['career_hub']==original
        self.cases.append({'case':'real-ray-reaim-restores-native-board','passed':True})

    async def prove(self):
        original=await self.layout('career-entry')
        if self.args.layout_only:return
        assert not original['active_cup'],'Start with no active cup for this non-destructive hub navigation pass'
        native=await self.query('vr.targets');selection=(await self.query('menu.get_state'))['selection_index']
        for target in native['targets']:
            await self.hover(target['name']);await asyncio.sleep(.25)
            state=await self.query('menu.get_state')
            assert state['career_hub']==original and state['selection_index']==selection,'Hover edited career state or selection'
        self.cases.append({'case':'all-hover-is-visual-only','passed':True})
        before,aim=await self.point_args('career.hero',(1.2,.1))
        aim.update(command_id=uuid.uuid4().hex,hold_frames=45)
        await self.tool('click_ui_at',aim,'stats-panel-is-not-an-action')
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert (await self.query('menu.get_state'))['career_hub']==original
        await self.click('career.garage')
        assert (await self.query('menu.get_state'))['current_menu']=='GARAGE'
        await self.cancel();await asyncio.sleep(.8)
        assert (await self.query('menu.get_state'))['career_hub']==original
        await self.click('career.statistics')
        assert (await self.query('menu.get_state'))['current_menu']=='PLAYER STATS'
        await self.cancel();await asyncio.sleep(.8)
        assert (await self.query('menu.get_state'))['career_hub']==original
        for name in ('career.hero','helper.Validate.CareerRace'):
            await self.click(name)
            state=await self.query('menu.get_state')
            assert state['current_menu']=='FLATOUT' and not state['is_3d'] and state['item_count']==3
            assert 'career_hub' not in state
            await self.cancel();await asyncio.sleep(.8)
            assert (await self.query('menu.get_state'))['career_hub']==original
        nodes=await self.nodes()
        assert next(n for n in nodes if n['path'].endswith('/CareerWorkspace'))['color']['a']>.99
        assert all(n['color']['a']>.99 for n in nodes if n['path'].count('/')==3 and '/CareerWorkspace/Career' in n['path'] and n['visible'] and n['kind']=='panel')
        self.cases.append({'case':'garage-statistics-and-both-race-actions-original-history-and-state-preserved','passed':True})
        await self.layout('career-return-fully-visible')
        assert (await self.query('game.errors'))['total']==0

    async def cup_flow(self):
        assert self.args.fixture_slot==641062,'Abandon flow is restricted to the isolated Career fixture'
        original=await self.layout('before-cup')
        assert not original['active_cup']
        await self.click('career.hero')
        await self.click('item.0')
        grid=(await self.query('menu.get_state'))['career']
        cup=next(x for x in grid['tiles'] if x['rounds']>1 and not x['locked'])
        await self.click('tile.'+str(cup['line'])+'.'+str(cup['index']))
        assert (await self.query('menu.get_state'))['item_count']==1
        assert not (await self.query('game.get_state'))['is_loading']
        confirm=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Validate.'))
        await self.click(confirm)
        for _ in range(180):
            game=await self.query('game.get_state')
            if not game['is_loading'] and not game['is_main_menu'] and game['state']=='race':break
            await asyncio.sleep(.5)
        else:raise AssertionError(('Isolated cup race did not finish loading',game))
        await self.menu_button()
        pause=await self.query('hud.pause_menu')
        index=next(i for i,label in enumerate(pause['items']) if label.lower()=='main menu')
        await self.click('button.'+str(index));await self.click('msgbox.validate')
        for _ in range(180):
            game=await self.query('game.get_state')
            if game['is_main_menu'] and game['main_menu_ready'] and not game['is_loading']:break
            await asyncio.sleep(.5)
        else:raise AssertionError(('Frontend return did not finish loading',game))
        menu=await self.query('menu.get_state')
        if menu['current_menu']=='MAIN MENU':
            # The preserved white main menu has actual spatial text colliders.
            native=await self.query('vr.targets');capture=await self.capture('white-main-menu-after-cup-return')
            item=menu['items'][0]
            frame={**native['vr_frame'],'panel':{**menu['spatial_layout'],'origin':item['rendered_position']},'panel_width':1.,'panel_height':1.}
            surface={'vr':{'frame':frame}};x,y=world_pixel(surface,capture,item['rendered_position'],'left')
            aim={'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand',
                 'surface':ui_surface(surface),'ray_offset':RAY_OFFSET,'dwell_frames':30,'hold_frames':45,'command_id':uuid.uuid4().hex}
            await self.tool('click_ui_at',aim,'original-white-career-entry')
        active=await self.layout('active-cup')
        assert active['active_cup'] and active['event_id']==cup['event_id'] and active['round_count']==cup['rounds']
        stable_keys=('wins','owned_cars','progress_percent','credits','car','class_percent')
        assert all(active[k]==original[k] for k in stable_keys)
        await self.click('helper.Validate.CareerRace')
        assert (await self.query('menu.get_state'))['item_count']==1
        assert not (await self.query('game.get_state'))['is_loading']
        await self.cancel()
        assert (await self.query('menu.get_state'))['career_hub']['event_id']==cup['event_id']
        await self.click('career.cancel-cup')
        assert {t['name'] for t in (await self.query('vr.targets'))['targets']}=={'msgbox.validate','msgbox.cancel'}
        await self.click('msgbox.cancel')
        assert (await self.query('menu.get_state'))['career_hub']==active
        await self.click('career.cancel-cup');await self.click('msgbox.validate')
        final=await self.layout('cup-cancelled')
        assert not final['active_cup']
        assert all(final[k]==original[k] for k in stable_keys)
        assert not (await self.query('game.get_state'))['is_loading']
        assert (await self.query('game.errors'))['total']==0
        self.cases.append({'case':'native-started-cup-continue-abandon-no-and-yes-without-finishing-or-progress-edit','passed':True,'event':cup})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    server=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    scope='layout only' if args.layout_only else 'Controller takeover' if args.takeover_only else 'Isolated cup Continue/Cancel' if args.cup_flow else 'No-active-cup navigation, hover, artwork and state preservation'
    result={'passed':False,'scope':scope}
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=CareerTest(args,session)
            try:
                if args.takeover_only:
                    await t.layout('takeover-entry');await t.takeover()
                elif args.cup_flow:await t.cup_flow()
                else:await t.prove()
                result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['cases']=t.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True)
    p.add_argument('--hand',default='right',choices=('left','right'));p.add_argument('--layout-only',action='store_true')
    p.add_argument('--takeover-only',action='store_true')
    p.add_argument('--cup-flow',action='store_true');p.add_argument('--fixture-slot',type=int)
    asyncio.run(run(p.parse_args()))
