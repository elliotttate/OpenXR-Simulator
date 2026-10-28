"""Real Multiplayer cards, original-input fallback and one-shot local-party navigation."""
import argparse,asyncio,copy,json,os,sys,uuid
import numpy as np
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

NAMES=('multiplayer.online','multiplayer.host','multiplayer.party','helper.Cancel.MultiplayerBack','multiplayer.friends')
class MultiplayerTest(Frontend):
    async def layout(self,label):
        state=await self.query('menu.get_state');assert state['current_menu']=='MULTIPLAYER' and state['item_count']==2,state
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-stereo')
        self.last_capture=capture
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        root=next((n for n in nodes if n['path'].endswith('/MultiplayerWorkspace')),None)
        assert root and root['visible'],'Native Multiplayer photo-card board is missing'
        assert_reading_distance(capture,3.,4.5)
        targets=(await self.query('vr.targets'))['targets'];assert {t['name'] for t in targets}==set(NAMES),targets
        assert all(t['enabled'] and t['physical_hover'] for t in targets)
        assert all(20<=t['rect'][0]<t['rect'][2]<=1900 and 20<=t['rect'][1]<t['rect'][3]<=1060 for t in targets)
        rects={t['name']:t['rect'] for t in targets}
        cards=[rects[n] for n in NAMES[:3]]
        assert all(b[0]-a[2]>=20 for a,b in zip(cards,cards[1:]))
        assert all(rects[n][3]-rects[n][1]>=104 for n in NAMES[3:])
        assert not any(item['rendered_bounds']['visible'] for item in state['items']),'Legacy spatial choices draw behind native cards'
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/MultiplayerWorkspace/MultiplayerHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert not any(n['visible'] and n['path'].endswith(('/Header','/Helpers','/Content_Zone')) for n in nodes)
        await self.audit(label)
        self.cases.append({'case':label+'-native-stereo-original-font-and-layout','passed':True})
        return state,rects

    async def takeover(self):
        original,bounds=await self.layout('hub-before-stick')
        _,_,aim=await self.hover('helper.Cancel.MultiplayerBack')
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'hub-original-carrier');self.last_capture=source
        carrier=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'hub-real-stick-takeover')
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert not any(n['effective_visible'] and n['path'].endswith('/MultiplayerWorkspace') for n in nodes)
        legacy=await self.query('menu.get_state');assert [i['title'] for i in legacy['items']]==[i['title'] for i in original['items']]
        assert all(i['rendered_bounds']['visible'] for i in legacy['items'])
        fresh=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'hub-spatial-after-stick');self.last_capture=fresh
        projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[carrier]
        x,y=quad_pixel(projection,bounds['multiplayer.host']);await asyncio.sleep(.25)
        await self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},'dwell_frames':45},'hub-deliberate-ray-return')
        await asyncio.sleep(.6);await self.layout('hub-after-ray-return')
        assert (await self.query('vr.targets'))['hovered']=='multiplayer.host'
        assert not (await self.query('net.get_state'))['alive'],'Hover created a network session'
        self.cases.append({'case':'original-stick-controls-and-cross-hand-ray-return-without-activation','passed':True})

    async def prove(self):
        state,bounds=await self.layout('multiplayer-entry');selected=state['selection_index']
        for name in NAMES:
            await self.hover(name);await asyncio.sleep(.45)
            assert (await self.query('menu.get_state'))['selection_index']==selected
            assert {t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}==bounds
            assert_hover_lift(await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'depth-'+name))
            self.cases.append({'case':'stable-visual-only-physical-hover-'+name,'passed':True})
        await self.click('multiplayer.party');assert (await self.query('menu.get_state'))['current_menu']=='OFFLINE' and (await self.query('party.get_state'))['open']
        assert (await self.query('game.get_state'))['game_mode']=='party_game'
        cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(cancel);await self.layout('multiplayer-party-return')
        await self.click('helper.Cancel.MultiplayerBack');state=await self.query('menu.get_state')
        assert state['current_menu']=='MAIN MENU' and state['is_3d']
        self.cases.append({'case':'one-shot-party-native-callback-back-and-preserved-white-main','passed':True})

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=MultiplayerTest(a,session)
            try:
                if a.takeover_only:await t.takeover()
                elif a.layout_only:await t.layout('multiplayer-layout')
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0,report['errors'];report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true')
    asyncio.run(run(p.parse_args()))
