"""Exercise native filter edits with screenshot-directed XR input on an owned test profile.

Opening Filter is explicit diagnostic staging. Every edit, reset and Back uses
actual controller actions; this does not create a party or certify matchmaking.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,RAY_OFFSET,quad_pixel
from flatout_livery_e2e import ui_surface,quaternion_matrix,screenshot_pixel
from flatout_ui_motion_policy import HOVER_LIFT_METERS
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas
from flatout_button_audit import inspect


class Filters(Frontend):
    async def nodes(self):return (await self.query('hud.rects',{'root':'menu','max_depth':48,'presentation':True}))['nodes']
    async def setup(self):return (await self.query('menu.get_state'))['setup']

    async def ray_mode(self):
        native=await self.query('vr.targets');cap=await self.capture('filter-ray-takeover-source')
        frame=native.get('vr_frame')
        carrier=ui_surface({'vr':{'frame':frame}}) if frame else max((s for s in cap['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        for x in (160,1760):
            px,py=screenshot_pixel({'vr':{'frame':frame}},cap,x,120) if frame else quad_pixel(cap,(x,120,x,120))
            receipt=await self.tool('hover_ui_at',{'capture_id':cap['capture_id'],'hand':self.args.hand,
                'x':float(px),'y':float(py),'surface':carrier,'origin_mode':'hand','ray_offset':RAY_OFFSET,'dwell_frames':8},'filter-ray-takeover-'+str(x))
            assert receipt['status']=='input_delivered',receipt
        assert (await self.query('vr.targets'))['clicks']==native['clicks']

    async def hover(self,name,fraction=(.5,.5)):
        # Simulator pointer leases expire. Refresh the spare wrist before each
        # action so its normal resting ray cannot take ownership of Reset.
        active=self.args.hand
        try:
            self.args.hand='right' if active=='left' else 'left';await self.ray_mode()
        finally:self.args.hand=active
        return await super().hover(name,fraction)

    async def entrance(self):
        samples=[];deadline=asyncio.get_running_loop().time()+15
        while asyncio.get_running_loop().time()<deadline:
            nodes=(await self.query('hud.rects',{'root':'menu','max_depth':48}))['nodes']
            root=next((n for n in nodes if n['path'].endswith('/FilterWorkspace') and n.get('effective_visible',n['visible'])),None)
            if root:
                samples.append(root['color']['a'])
                if len(samples)>=5 and samples[-1]>=.999:break
            await asyncio.sleep(.015)
        assert samples and samples[-1]>=.999 and any(.3<=v<.99 for v in samples),samples
        self.cases.append({'case':'observed-native-entrance-fade','passed':True,'hand':self.args.hand,'alpha':samples})

    async def layout(self):
        # Rest-layout audits must not mistake an intentionally promoted face
        # for missing artwork. Park both real wrists outside the board.
        active=self.args.hand
        try:
            for hand in ('left','right'):
                self.args.hand=hand;await self.ray_mode()
        finally:self.args.hand=active
        await asyncio.sleep(.25)
        nodes=await self.nodes();setup=await self.setup();g=self.geometry
        def node(name):return next(n for n in nodes if n['path'].endswith('/'+name) and n.get('effective_visible',n['visible']))
        assert np.allclose(node('FilterPanel')['rect'],[g['StageX'],g['BodyY'],g['StageX']+g['StageWidth'],g['BodyY']+g['BodyHeight']],atol=1)
        targets=(await self.query('vr.targets'))['targets'];visible=[r for r in setup['rows'] if r['visible']]
        assert 1<=len(visible)<=6,setup
        expected={'helper.Cancel.FilterBack','filter.reset','helper.Validate.FilterSearch'}
        for slot,row in enumerate(visible):
            plate=node('FilterSetting'+str(row['index']));l,t,r,b=plate['rect']
            assert abs(t-g['RowsY']-slot*g['RowStep'])<1 and b+27<=g['BodyY']+g['BodyHeight'],plate
            for suffix,key in [('FilterSettingTitle','label'),('FilterSettingValue','value')]:
                child=next(n for n in nodes if n['path']==plate['path']+'/'+suffix)
                assert child['text']==row[key],(child,row)
                assert child['rect'][0]-l>=15 and r-child['rect'][2]>=150,child
            for side,key in [('prev','previous'),('next','next')]:
                name='setup.'+str(row['index'])+'.'+side;expected.add(name)
                target=next(n for n in targets if n['name']==name)
                assert target.get('enabled',True)==row[key],(target,row)
                assert np.allclose([target['rect'][2]-target['rect'][0],target['rect'][3]-target['rect'][1]],[g['ArrowSize']]*2,atol=1),target
        assert {t['name'] for t in targets}==expected,targets
        audit=await inspect({'nodes':nodes,'message':await self.query('ui.get_message_box')},self.query,self.args.fox_root)
        assert audit['passed'] and len(audit['checks'])==3,audit
        captions=[n for n in nodes if n.get('text') and n.get('effective_visible',n['visible']) and '/FilterWorkspace/' in n['path']]
        assert all('FO_ALDO_THE_APACHE' in n['presentation']['font_resource'].upper() for n in captions),captions
        self.cases.append({'case':'all-real-filter-values-native-fonts-margins-and-independent-controls','passed':True,'hand':self.args.hand,'setup':setup,'audit':audit})

    async def depth(self,name):
        before,_,_=await self.hover(name);cap=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},self.args.hand+'-physical-'+name);self.last_capture=cap
        carrier=ui_surface({'vr':{'frame':before['vr_frame']}}) if before.get('vr_frame') else max((s for s in cap['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        basis=quaternion_matrix(carrier['pose']['orientation'])
        assert abs(basis[1,0])<1e-4 and abs(basis[1,2])<1e-4,basis.tolist()
        distance=abs(float(np.dot(np.array(carrier['pose']['position'])-cap['geometry']['head']['position'],basis[:,2])))
        assert abs(distance-3)<.01 and abs(carrier['size'][0]-4.5)<.01,(distance,carrier)
        rect=next(t['rect'] for t in before['targets'] if t['name']==name);l,t,r,b=rect
        point=np.array(carrier['pose']['position'])+basis[:,0]*((l+r)/3840-.5)*carrier['size'][0]+basis[:,1]*(.5-(t+b)/2160)*carrier['size'][1]
        tiles=[s for s in cap['geometry']['surfaces'] if s['visibility']==0 and s.get('id')!=carrier.get('id')];matches=[]
        for tile in tiles:
            lift=float(np.dot(np.array(tile['pose']['position'])-carrier['pose']['position'],basis[:,2]))
            local=quaternion_matrix(tile['pose']['orientation']).T@(point-tile['pose']['position'])
            if abs(lift-HOVER_LIFT_METERS)<.001 and all(abs(local[i])<=tile['size'][i]/2+.002 for i in (0,1)):matches.append(tile)
        assert matches and len(tiles)<=2,(name,tiles)
        self.cases.append({'case':'upright-shared-reading-distance-and-both-eye-physical-hover','passed':True,'hand':self.args.hand,'target':name,'tiles':matches})

    async def passive_and_takeover(self):
        before=await self.query('vr.targets');cap=await self.capture('passive-filter-label-source')
        px,py=self.geometry['StageX']+100,self.geometry['RowsY']+35
        if before.get('vr_frame'):
            frame={'vr':{'frame':before['vr_frame']}};x,y=screenshot_pixel(frame,cap,px,py);carrier=ui_surface(frame)
        else:
            x,y=quad_pixel(cap,(px,py,px,py));carrier=max((s for s in cap['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        aim={'capture_id':cap['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'surface':carrier,
             'origin_mode':'hand','ray_offset':RAY_OFFSET,'dwell_frames':8,'hold_frames':30,'command_id':uuid.uuid4().hex}
        setup=await self.setup()
        await self.tool('click_ui_at',aim,'passive-label-click');await self.tool('click_ui_at',aim,'passive-label-retry')
        assert (await self.query('vr.targets'))['clicks']==before['clicks'] and (await self.setup())==setup
        _,aim=await self.point_args('helper.Cancel.FilterBack')
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'hold_frames':8,'dwell_frames':2,'command_id':uuid.uuid4().hex},'left-navigation-restores-original-filter')
        assert not any(n['path'].endswith('/FilterWorkspace') and n.get('effective_visible',n['visible']) for n in await self.nodes())
        await self.ray_mode();assert (await self.query('vr.targets'))['clicks']==before['clicks']
        await self.layout()
        self.cases.append({'case':'passive-label-inert-and-original-controller-layout-restored','passed':True,'hand':self.args.hand})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);result={'passed':False,'pid':a.pid,'scope':__doc__}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Filters(a,session);t.geometry=json.loads((a.fox_root/'Flatout/Code/Sources/Menus/FOFilterHtmlLayout.json').read_text(encoding='utf-8'))['geometry']
            sa=SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand='right',ray_presentation=True)
            stage=Atlas(sa,session,{'operations':[]});stage.active_case={'id':'filter-qualification'};t.query_lock=stage.lock
            try:
                for hand in ('left','right'):
                    a.hand=hand;await stage.main()
                    observer=asyncio.create_task(t.entrance())
                    try:
                        await stage.query('menu.go_to',{'menu':'filter'});await t.ray_mode()
                    finally:await observer
                    await asyncio.sleep(.65)
                    defaults=await t.setup();await t.layout()
                    for mode in range(3):
                        rows=[r for r in (await t.setup())['rows'] if r['visible']]
                        for row in rows:
                            if row['index']==0:continue
                            if row['next']:
                                await t.click('setup.'+str(row['index'])+'.next')
                                updated=next(r for r in (await t.setup())['rows'] if r['index']==row['index'])
                                assert updated['value']!=row['value'],(row,updated)
                                if row['index']==1:
                                    # A concrete gameplay type reveals Track.
                                    # Exercise that dependent row before restoring Any.
                                    dependent=next(r for r in (await t.setup())['rows'] if r['index']==2)
                                    if dependent['visible'] and dependent['next']:
                                        await t.layout();await t.click('setup.2.next')
                                        changed=next(r for r in (await t.setup())['rows'] if r['index']==2)
                                        assert changed['value']!=dependent['value'],(dependent,changed)
                                        await t.click('setup.2.prev')
                                        assert next(r for r in (await t.setup())['rows'] if r['index']==2)['value']==dependent['value']
                                await t.click('setup.'+str(row['index'])+'.prev')
                                restored=next(r for r in (await t.setup())['rows'] if r['index']==row['index'])
                                assert restored['value']==row['value'],(row,restored)
                        await t.layout();await t.click('setup.0.next')
                    await t.click('filter.reset');reset=await t.setup()
                    assert [r['visible'] for r in reset['rows']]==[r['visible'] for r in defaults['rows']],(defaults,reset)
                    # Hidden, inapplicable Flash fields retain cached captions
                    # from a chosen track. They are not active search filters.
                    assert [(r['index'],r['value']) for r in reset['rows'] if r['visible']]==[(r['index'],r['value']) for r in defaults['rows'] if r['visible']],(defaults,reset)
                    await t.layout()
                    for name in ('setup.0.next','helper.Cancel.FilterBack','filter.reset','helper.Validate.FilterSearch'):await t.depth(name)
                    await t.passive_and_takeover();await t.click('helper.Cancel.FilterBack')
                    assert (await t.query('menu.get_state'))['current_menu']=='MAIN MENU'
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
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
