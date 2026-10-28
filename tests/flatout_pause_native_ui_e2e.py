"""Qualify the native in-race pause stack with the pause menu's real rows and XR input.

Race fixture: one lap race is staged through the bridge's race.launch (nobody
drives; the car waits on the grid). The real left-controller Menu button opens
pause; rays make every hover and click. Continue resumes the race, and the real
main-menu row (plus its confirmation, when the game asks) ends the fixture.
"""
import argparse,asyncio,json,os,struct,sys,uuid
from pathlib import Path
from types import SimpleNamespace
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT,RAY_OFFSET
from flatout_party_native_ui_e2e import Party
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas

FIXTURE_METHODS={'race.launch','race.get_state','race.skip_intro'}


def stage_race(pid,method,params=None):
    """Race fixture staging only; every UI action still goes through XR input."""
    assert method in FIXTURE_METHODS,method
    request=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}).encode()
    with open(r'\\.\pipe\FlatoutMCP_'+str(pid),'r+b',buffering=0) as pipe:
        pipe.write(struct.pack('<I',len(request))+request)
        def read(n):
            data=b''
            while len(data)<n:
                chunk=pipe.read(n-len(data))
                if not chunk:raise RuntimeError('Game pipe closed')
                data+=chunk
            return data
        response=json.loads(read(struct.unpack('<I',read(4))[0]))
    assert 'error' not in response,response
    return response['result']


class Pause(Party):
    async def until(self,check,label,timeout=180):
        loop=asyncio.get_running_loop();deadline=loop.time()+timeout
        while True:
            value=await check()
            if value:return value
            assert loop.time()<deadline,label
            await asyncio.sleep(.25)

    async def race_state(self):
        return await asyncio.to_thread(stage_race,self.args.pid,'race.get_state')

    async def _racing(self):
        s=await self.race_state();return s if s.get('state') in ('countdown','racing') else None

    async def _closed(self):
        s=await self.query('hud.pause_menu');return None if s['is_open'] else s

    async def _main(self):
        s=await self.query('game.get_state');return s if s.get('is_main_menu') and s.get('main_menu_ready') and not s.get('is_loading') else None

    async def press_menu(self):
        cap=await self.capture('pause-menu-button-source')
        # The Menu button needs no target; a racing view has no UI surface to aim at.
        width,height=cap.get('image_size',[1280,1369])
        aim={'capture_id':cap['capture_id'],'hand':'left','x':width/2.,'y':height/2.,
             'origin_mode':'eye','dwell_frames':4,'hold_frames':6,'button':'menu','command_id':uuid.uuid4().hex}
        receipt=await self.tool('click_ui_at',aim,'pause-menu-button')
        assert receipt.get('status')=='input_delivered',receipt
        return await self.until(lambda:self._pause_open(),'Menu button did not open pause',15)

    async def _pause_open(self):
        state=await self.query('hud.pause_menu');return state if state['is_open'] else None

    async def layout(self):
        await self.park();nodes=await self.nodes();g=self.geometry;items=(await self.query('hud.pause_menu'))['items']
        def node(name):return next(n for n in nodes if n['path'].endswith('/'+name) and n.get('effective_visible',n['visible']))
        panel=node('PausePanel');heading=node('PauseHeading');l,t,r,b=panel['rect']
        assert abs(l-g['PanelX'])<1 and abs(r-g['PanelX']-g['PanelWidth'])<1 and abs(t-g['BodyY'])<1 and b<=1080-48,panel
        assert heading['text'].strip(),heading
        targets={x['name']:x for x in (await self.query('vr.targets'))['targets']}
        assert set(targets)=={'pause.'+str(i) for i in range(len(items))},(targets.keys(),items)
        previous=None
        for i,title in enumerate(items):
            row=node('PauseRow'+str(i));label=next(n for n in nodes if n['path']==row['path']+'/PauseRowLabel')
            assert label['text']==title and title.strip(),(label,title)
            rl,rt,rr,rb=row['rect']
            assert abs(rl-g['ButtonX'])<1 and abs(rr-rl-g['ButtonWidth'])<1 and abs(rb-rt-g['ButtonHeight'])<1 and rb<=b-g['Inset']+1,(row,panel)
            if previous:assert abs(rt-previous[3]-g['ButtonGap'])<1,(previous,row['rect'])
            previous=row['rect']
        assert abs(b-previous[3]-g['Inset'])<1,(panel,previous)
        await self.audit('PauseWorkspace',len(items))
        self.cases.append({'case':'native-pause-real-rows-measured-stack-and-fonts','passed':True,'hand':self.args.hand,'items':items})
        return items

    async def hover_inert(self,items):
        for i in range(len(items)):
            await self.hover('pause.'+str(i))
            state=await self.query('hud.pause_menu');assert state['is_open'],state
        self.cases.append({'case':'every-row-hover-highlights-without-activation','passed':True,'hand':self.args.hand})

    async def takeover(self):
        before,aim=await self.point_args('pause.0')
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'hold_frames':8,'dwell_frames':2,'command_id':uuid.uuid4().hex},'pause-controller-takeover')
        assert not any(n['path'].endswith('/PauseWorkspace') and n.get('effective_visible',n['visible']) for n in await self.nodes())
        assert (await self.query('hud.pause_menu'))['is_open']
        await self.ray_mode();assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert any(n['path'].endswith('/PauseWorkspace') and n.get('effective_visible',n['visible']) for n in await self.nodes())
        self.cases.append({'case':'original-controller-layout-and-fresh-ray-restoration','passed':True,'hand':self.args.hand})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);result={'passed':False,'pid':a.pid,'scope':__doc__}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Pause(a,session)
            t.geometry=json.loads((a.fox_root/'Flatout/Code/Sources/Menus/FOPauseHtmlLayout.json').read_text(encoding='utf-8'))['geometry']
            sa=SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand='right',ray_presentation=True)
            stage=Atlas(sa,session,{'operations':[]});stage.active_case={'id':'native-pause-qualification'};t.query_lock=stage.lock
            try:
                for hand in a.hands:
                    a.hand=hand
                    if (await t.query('game.get_state')).get('is_main_menu'):
                        await stage.main()
                        launch=await asyncio.to_thread(stage_race,a.pid,'race.launch',{'gameplay_type':'race_lap'})
                        t.cases.append({'case':'race-fixture-staged','passed':True,'hand':hand,'launch':launch})
                    else:t.cases.append({'case':'race-fixture-reused','passed':True,'hand':hand})
                    await t.until(lambda:t._racing(),'Race fixture never reached the grid')
                    await asyncio.sleep(2)
                    observer=asyncio.create_task(t.entrance('PauseWorkspace'))
                    try:await t.press_menu();await t.ray_mode()
                    finally:await observer
                    items=await t.layout();await t.hover_inert(items);await t.depth('pause.0');await t.takeover()
                    await t.click('pause.0')
                    await t.until(lambda:t._closed(),'Continue did not resume the race',15)
                    t.cases.append({'case':'continue-resumes-race','passed':True,'hand':hand})
                    await t.press_menu();await t.ray_mode()
                    items=(await t.query('hud.pause_menu'))['items']
                    exit_row=next(i for i,title in enumerate(items) if title.strip().lower() in a.exit_titles)
                    await t.click('pause.'+str(exit_row))
                    box=await t.query('ui.get_message_box')
                    if box.get('open'):
                        await t.click('msgbox.validate')
                    await t.until(lambda:t._main(),'Main-menu row did not return to the frontend',240)
                    t.cases.append({'case':'real-main-menu-row-ends-race-fixture','passed':True,'hand':hand,'row':items[exit_row]})
                    if t.last_capture:
                        for h in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':h})
                result['errors']=await t.query('game.errors');assert result['errors']['total']==0,result['errors'];result['passed']=True
            except Exception as e:result['error']=repr(e);raise
            finally:
                result['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right')
    p.add_argument('--hands',nargs='+',default=['left','right']);p.add_argument('--exit-titles',nargs='+',default=['main menu','quit race','return to main menu'])
    a=p.parse_args();a.exit_titles={x.lower() for x in a.exit_titles};asyncio.run(run(a))
