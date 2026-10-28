"""Saved Cup standings: native profile data, reading stage and actual XR callbacks.

Start on Career's saved-cup standings in the owned isolated simulator game.
No race launch, reward, purchase, save edit or progression mutation is performed.
"""
import argparse,asyncio,copy,json,os,sys,uuid,re,threading,time
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_hover_lift,assert_reading_distance
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame

class StandingsTest(Frontend):
    async def standings(self):
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='FLATOUT' and menu['item_count']==0 and not menu['is_3d'],menu
        s=menu['cup_standings'];assert s['native'] and s['rows'],s
        return s

    async def nodes(self):return (await self.query('hud.rects',{'root':'menu','max_depth':24}))['nodes']

    async def stereo(self,label):
        c=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label);self.last_capture=c
        assert c['geometry']['session_id'].split('-')[0]==str(self.args.pid)
        assert c['geometry']['layout']=='side_by_side'
        return c

    async def layout(self,label):
        state=await self.standings();nodes=await self.nodes();targets=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in targets}=={'helper.Cancel.CupStandingsBack','helper.Validate.CupStandingsContinue'}
        assert all(t['enabled'] and t['physical_hover'] and t['rect'][3]-t['rect'][1]>=104 for t in targets)
        expected=sorted(state['saved_scores'],key=lambda x:(-x['score'],not x['player'],x['index']))
        assert state['rows']==expected and len({x['index'] for x in state['rows']})==len(expected)
        assert 1<=len(expected)<=12
        def node(name):return next(n for n in nodes if n['visible'] and n['path'].endswith('/'+name))
        frame=node('CupStandingsTable')['rect'];header=node('CupStandingsHeader')['rect']
        assert 60<header[1]<header[3]<frame[1] and frame[3]<=900
        assert all(frame[3]+16<=t['rect'][1]<t['rect'][3]<=1040 for t in targets)
        for i,row in enumerate(expected):
            group=node('CupStanding'+str(i));l,t,r,b=group['rect']
            assert frame[0]<=l<r<=frame[2] and frame[1]+60<=t<b<frame[3]
            assert b-t>=46
            prefix='/CupStanding'+str(i)+'/'
            rendered={n['path'].rsplit('/',1)[-1]:n for n in nodes if n['visible'] and prefix in n['path']}
            assert rendered['CupStandingPosition']['text']==f'{i+1:02}'
            assert rendered['CupStandingName']['text']==row['name']
            assert int(rendered['CupStandingPoints']['text'].replace(',','').replace(' ',''))==row['score']
            assert all(t<=n['rect'][1]<n['rect'][3]<=b+1 for n in rendered.values())
        title=await self.query('hud.get_node',{'root':'menu','name':'Menu/CupStandingsWorkspace/CupStandingsHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT' in title['presentation']['font_resource'].upper()
        assert node('CupStandingsTitle')['text']==state['title']
        assert not any(n['effective_visible'] and '/Element_Leaderboard_Labels/' in n['path'] for n in nodes)
        capture=await self.stereo(label);placement=assert_reading_distance(capture,3,4.5)
        self.cases.append({'case':'native-data-table-fonts-margins-and-reading-stage','passed':True,'rows':len(expected),'placement':placement})
        return state

    async def depth(self):
        before=await self.layout('standings-depth-source')
        for name in ('helper.Cancel.CupStandingsBack','helper.Validate.CupStandingsContinue'):
            pre,hovered,_=await self.hover(name)
            assert hovered['clicks']==pre['clicks'] and await self.standings()==before
            depth=assert_hover_lift(await self.stereo('physical-'+self.args.hand+'-'+name))
            self.cases.append({'case':'physical-hover-'+self.args.hand+'-'+name,'passed':True,'depth':depth})
        await self.layout('standings-unchanged-after-hover')

    async def takeover(self):
        before=await self.layout('standings-before-takeover')
        source=await self.stereo('standings-native-carrier')
        carrier=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        native_back=next(t['rect'] for t in (await self.query('vr.targets'))['targets'] if t['name']=='helper.Cancel.CupStandingsBack')
        _,aim=await self.point_args('helper.Validate.CupStandingsContinue')
        receipt=await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'standings-stick-takeover')
        assert receipt['status']=='input_delivered'
        legacy=(await self.query('menu.get_state'))['cup_standings'];assert not legacy['native']
        assert legacy['rows']==before['rows'] and legacy['saved_scores']==before['saved_scores']
        assert not any(n['visible'] and n['path'].endswith('/CupStandingsWorkspace') for n in await self.nodes())
        self.cases.append({'case':'actual-stick-restores-original-table-without-score-mutation','passed':True})
        fresh=await self.stereo('standings-original-table');projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[carrier]
        # Deliberately move from Continue on the right to Back on the left.
        # Tiny adjustments near the old right-hand helpers correctly stay in
        # stick mode under the shared ray-reactivation motion threshold.
        x,y=quad_pixel(projection,native_back)
        receipt=await self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':'left','surface':{k:carrier[k] for k in ('pose','size')},'origin_mode':'hand','dwell_frames':45},'ray-restores-standings')
        assert receipt['status']=='input_delivered'
        deadline=time.monotonic()+3
        while not (await self.query('menu.get_state'))['cup_standings']['native']:
            assert time.monotonic()<deadline, 'Fresh ray did not restore the native presentation'
            await asyncio.sleep(.03)
        assert await self.layout('standings-restored')==before
        self.cases.append({'case':'fresh-ray-restores-native-table-without-activation','passed':True})

    async def motion(self):
        original=await self.layout('standings-motion-entry')
        await self.click('helper.Cancel.CupStandingsBack')
        _,aim=await self.point_args('helper.Validate.CareerRace')
        native=NativeGame(self.args.pid,[],timeout=3);samples=[];errors=[]
        ready=threading.Event();stop=threading.Event()
        def collect():
            try:
                deadline=time.monotonic()+7
                while not stop.is_set() and time.monotonic()<deadline:
                    nodes=native.call('hud.rects',{'root':'menu','max_depth':4})['nodes']
                    root=next((n for n in nodes if n['path'].endswith('/CupStandingsWorkspace') and n.get('effective_visible')),None)
                    if root is not None:
                        rows=[n for n in nodes if re.search(r'/CupStanding[0-9]+$',n['path'])]
                        samples.append({'time':time.monotonic(),'alpha':root['color']['a'],'rect':root['rect'],
                            'rows':[(n['path'],n['color']['a'],n['rect']) for n in rows]})
                    ready.set();time.sleep(.005)
            except Exception as error:errors.append(repr(error));ready.set()
        thread=threading.Thread(target=collect,name='native-standings-motion-sampler',daemon=True);thread.start()
        try:
            assert await asyncio.to_thread(ready.wait,3) and not errors,errors
            before=(await self.query('vr.targets'))['clicks'];aim.update(command_id=uuid.uuid4().hex,hold_frames=45)
            receipt=await self.tool('click_ui_at',aim,'enter-standings-once')
            assert receipt['status']=='input_delivered' and await self.standings()==original
            await self.tool('click_ui_at',aim,'same-standings-entry-retry')
            assert (await self.query('vr.targets'))['clicks']==before+1
            await asyncio.sleep(.6)
        finally:
            stop.set();await asyncio.to_thread(thread.join,4)
            (self.args.evidence/'motion-samples.json').write_text(json.dumps({'samples':samples,'errors':errors},indent=2))
        assert not thread.is_alive() and not errors,errors
        levels={round(v['alpha']*255) for v in samples}
        assert len(levels)>=3 and min(levels)<250 and samples[-1]['alpha']>=.995,levels
        assert all(v['rect']==samples[0]['rect'] for v in samples)
        assert all(b['alpha']>=a['alpha'] for a,b in zip(samples,samples[1:])), 'Entrance restarted'
        assert all(len(v['rows'])==len(original['rows']) for v in samples)
        for path,_,_ in samples[0]['rows']:
            entries=[next((a,r) for p,a,r in sample['rows'] if p==path) for sample in samples]
            assert entries[-1][0]>=.995
            assert all(b[0]>=a[0] and b[1][1]<=a[1][1] for a,b in zip(entries,entries[1:])), entries
        self.cases.append({'case':'native-table-fade-and-staggered-rows-complete-once-with-stable-carrier','passed':True,'samples':len(samples),'alpha_levels':len(levels)})
        await self.layout('standings-motion-complete')

    async def prove(self):
        before=await self.layout('standings-full-entry')
        await self.depth()
        # An information row never inherits the old all-canvas Validate fallback.
        c=await self.stereo('static-table-source');x,y=quad_pixel(c,(700,480,700,480));old=await self.query('vr.targets')
        carrier=max((s for s in c['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        receipt=await self.tool('click_ui_at',{'capture_id':c['capture_id'],'surface_id':carrier['id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','hold_frames':30,'command_id':uuid.uuid4().hex},'static-table-click-inert')
        assert receipt['status']=='input_delivered' and (await self.query('vr.targets'))['clicks']==old['clicks']
        assert await self.standings()==before
        self.cases.append({'case':'static-table-cannot-start-or-advance-cup','passed':True})
        await self.click('helper.Cancel.CupStandingsBack')
        hub=(await self.query('menu.get_state'))['career_hub'];assert hub['active_cup']
        await self.click('helper.Validate.CareerRace');assert await self.layout('standings-back-reopen')==before
        await self.click('helper.Validate.CupStandingsContinue')
        cup=(await self.query('menu.get_state'))['cup'];assert cup['native'] and cup['saved_cup'] and not cup['can_change_car']
        assert not (await self.query('game.get_state'))['is_loading']
        self.cases.append({'case':'held-continue-enters-original-preview-once-without-launching-race','passed':True})
        await self.click('helper.Cancel.CupBack');assert await self.layout('standings-preview-back')==before
        await self.click('helper.Cancel.CupStandingsBack')
        assert (await self.query('menu.get_state'))['career_hub']==hub
        await self.click('helper.Validate.CareerRace');assert await self.layout('standings-history-preserved')==before
        self.cases.append({'case':'back-and-continue-preserve-history-and-native-saved-scores','passed':True})

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'scope':__doc__,'hand':a.hand}
    server=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=StandingsTest(a,session)
            try:
                if a.motion_only:await t.motion()
                elif a.layout_only:await t.layout('standings-layout')
                elif a.depth_only:await t.depth()
                elif a.takeover_only:await t.takeover()
                else:await t.prove()
                report['native_errors']=await t.query('game.errors');assert report['native_errors']['total']==0,report['native_errors']
                report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    m=p.add_mutually_exclusive_group();m.add_argument('--layout-only',action='store_true');m.add_argument('--depth-only',action='store_true');m.add_argument('--takeover-only',action='store_true');m.add_argument('--motion-only',action='store_true')
    asyncio.run(run(p.parse_args()))
