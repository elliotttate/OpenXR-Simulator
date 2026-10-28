"""Native Cup overview: real factory content, ray-only actions and filtered car return."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from PIL import Image
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance
import sys
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame
from flatout_events_html_e2e import factory_entries

class CupTest(Frontend):
    async def cup(self):
        state=await self.query('menu.get_state')
        assert state['current_menu']=='FLATOUT_MODE' and state['item_count']==1 and not state['is_3d'],state
        assert state['cup']['native'],state
        return state['cup']

    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def art(self,name,pack,skin):
        node=await self.query('hud.get_node',{'root':'menu','name':'Menu/CupWorkspace/'+name,'presentation':True})
        path=self.args.fox/'Flatout/Data/Assets'/pack.removeprefix('DATA:/')
        original=next(n for n in ET.parse(str(path)+'.XML').iter('skin') if n.attrib['Name']==skin)
        expected=list(map(float,original.attrib['uvs'].split(';')));actual=node['presentation']['uv']
        assert expected[0]-1e-5<=actual[0]<actual[2]<=expected[2]+1e-5
        assert expected[1]-1e-5<=actual[1]<actual[3]<=expected[3]+1e-5
        if name=='CupCarPhoto' and pack=='DATA:/GUI_FLATOUT/TEXTURES/MENUS/CARS.PSK':
            # Pixel alpha is the independent oracle: the old padded skin leaves
            # over a hundred empty source pixels at the top/left.
            with Image.open(path.with_name('Cars.png')) as image:
                w,h=image.size
                box=tuple(round(v*(w if i%2==0 else h))for i,v in enumerate(expected))
                content=image.crop(box).getchannel('A').getbbox();assert content
                bounds=[box[0]+content[0],box[1]+content[1],box[0]+content[2],box[1]+content[3]]
                pixels=[v*(w if i%2==0 else h) for i,v in enumerate(actual)]
                assert all(abs(a-b)<=2.01 for a,b in zip(pixels,bounds)),(pixels,bounds)

    async def layout(self,label):
        cup=await self.cup();nodes=await self.nodes();targets=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in targets}=={'helper.Cancel.CupBack','cup.car','helper.Validate.CupStart'}
        assert all(20<=t['rect'][0]<t['rect'][2]<=1900 and 20<=t['rect'][1]<t['rect'][3]<=1060 for t in targets)
        assert all(t['rect'][3]-t['rect'][1]>=104-.001 for t in targets)
        ordered=sorted(targets,key=lambda t:t['rect'][0])
        assert all(b['rect'][0]-a['rect'][2]>=24 for a,b in zip(ordered,ordered[1:]))
        enabled={'helper.Cancel.CupBack':True,'cup.car':cup['can_change_car'],'helper.Validate.CupStart':cup['can_start']}
        assert all(t['enabled']==enabled[t['name']] and t['physical_hover']==enabled[t['name']] for t in targets)
        def node(name):return next(n for n in nodes if n['path'].endswith('/'+name))
        def text(name):return node(name)['text']
        assert node('CupWorkspace')['visible']
        assert text('CupHeading')==cup['title'] and text('CupCarName')==cup['car'] and text('CupCarClass')==cup['car_class']
        assert text('CupCurrentTrackTitle')==cup['rounds'][cup['preview_round']]['title']
        assert text('CupTrackDescription')==cup['rounds'][cup['preview_round']]['description']
        assert all(not n['visible'] for n in nodes if n['path'].endswith(('/Content_Zone','/Helpers','/Header')))
        panels=[n for n in nodes if n['kind']=='panel' and n['visible'] and n['color']['a']>.2]
        assert not any(n['rect'][2]-n['rect'][0]>=1900 and n['rect'][3]-n['rect'][1]>=1070 for n in panels),'Full scene-covering panel'
        assert node('CupRoundsPanel')['rect'][3]+24<=min(t['rect'][1] for t in targets)+4.001
        for i,round in enumerate(cup['rounds']):
            row=node('CupRound'+str(i));l,t,r,b=row['rect']
            assert row['visible'] and 232<=t<b<=896
            title=next(n for n in nodes if n['path'].endswith('/CupRound'+str(i)+'/CupRoundTitle'))
            metadata=next(n for n in nodes if n['path'].endswith('/CupRound'+str(i)+'/CupRoundMetadata'))
            assert title['text']==round['title'] and round['best'] in metadata['text'] and str(round['info']) in metadata['text']
            await self.art('CupRound'+str(i)+'/CupRoundPhoto'+str(i),round['pack'],round['skin'])
        assert [int(''.join(filter(str.isdigit,text('CupPrize'+str(i))))) for i in range(3)]==cup['prizes']
        current=cup['rounds'][cup['preview_round']]
        await self.art('CupCurrentTrackPhoto',current['pack'],current['skin'])
        await self.art('CupCarPhoto',cup['car_pack'],cup['car_skin'])
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/CupWorkspace/CupHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-both-eyes')
        assert_reading_distance(capture,3.,4.5)
        await self.audit(label)
        self.cases.append({'case':label+'-real-content-art-original-font-layout','passed':True,'cup':cup})
        return cup

    def factory(self,cup):
        prefix=next(c for c in ('Derby','Classic','AllStars') if cup['source_index'].startswith(c))
        entry=factory_entries(self.args.fox,prefix)[cup['source_index']]
        assert cup['prizes']==entry['prizes'] and len(cup['rounds'])==entry['rounds']
        found=[]
        for path in (self.args.fox/'Flatout/Data/Common/Tuning/Factories').glob('Events'+prefix+'Level*.cfg'):
            text='\n'.join(l.split('--',1)[0]for l in path.read_text(encoding='latin1').splitlines())
            blocks=re.split(r'^\s*Event\d+\s*=\s*\{',text,flags=re.M)
            for block in blocks:
                if re.search(r'\bIndex\s*=\s*"'+re.escape(cup['source_index'])+'"',block):
                    tracks=block.split('Tracks',1)[1]
                    found=re.findall(r'TrackName\s*=\s*LocalesId\.(\w+).*?Laps\s*=\s*(\d+)',tracks,re.S)
        loca=(self.args.fox/'Flatout/Code/Sources/LOCALISATION.h').read_text(encoding='latin1')
        assert len(found)==len(cup['rounds'])
        for actual,(name,laps) in zip(cup['rounds'],found):
            expected=int(re.search(r'\b'+name+r'\s*=\s*(\d+)',loca).group(1))
            assert actual['track_id']==expected and actual['info']==int(laps),(actual,name,laps)
        self.cases.append({'case':'independent-factory-round-ids-laps-prizes-and-count','passed':True})

    async def cancel(self):
        name=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(name)

    async def prove(self):
        original=await self.layout('cup-entry');self.factory(original)
        assert not original['saved_cup'] and original['can_change_car'] and original['can_start'],'Use an unstarted native cup for vehicle-return qualification'
        before=await self.query('vr.targets')
        for target in before['targets']:
            await self.hover(target['name']);assert await self.cup()==original
        self.cases.append({'case':'all-hover-visual-only-no-round-car-or-prize-edit','passed':True})
        native,aim=await self.point_args('helper.Validate.CupStart',(-2.,-5.))
        receipt=await self.tool('click_ui_at',{**aim,'hold_frames':20,'command_id':uuid.uuid4().hex},'round-photo-is-not-start')
        assert receipt['status']=='input_delivered' and (await self.query('vr.targets'))['clicks']==native['clicks']
        assert await self.cup()==original and not (await self.query('game.get_state'))['is_loading']
        self.cases.append({'case':'static-round-information-does-not-start-race','passed':True})
        await self.click('cup.car')
        shop=await self.query('menu.get_state');assert shop['current_menu']=='CARS'
        prefix=next(c for c in ('derby','classic','allstars') if original['source_index'].lower().startswith(c))
        assert shop['items'] and all(i['car_class']==prefix for i in shop['items'])
        # All category buttons remain present but reject changes to the cup's class.
        assert all(not t['enabled'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('class.'))
        await self.cancel();assert await self.layout('cup-car-cancel-return')==original
        await self.click('cup.car')
        shop=await self.query('menu.get_state');index=next(i['index'] for i in shop['items'] if i['title']==original['car'])
        target='cars.item.'+str(index)
        assert target in {t['name']for t in (await self.query('vr.targets'))['targets']},'Already-equipped fixture car must be on the visible page'
        await self.click(target);await self.click('helper.Validate.3')
        assert await self.layout('cup-equipped-car-return')==original
        assert not (await self.query('ui.get_message_box'))['open']
        self.cases.append({'case':'class-filtered-car-cancel-and-original-equipped-selection-return-to-same-cup','passed':True})
        await self.cancel()
        grid=(await self.query('menu.get_state'))['career']
        assert grid['native_catalog'] and grid['selected_event_id']==original['event_id']
        self.cases.append({'case':'back-restores-actual-event-and-catalog-page','passed':True})
        await self.click(next(e['target'] for e in grid['tiles'] if e['event_id']==original['event_id']))
        assert await self.layout('cup-reopened')==original
        assert (await self.query('game.errors'))['total']==0

    async def motion(self):
        original=await self.layout('motion-entry')
        await self.cancel()
        _,aim=await self.point_args('tile.0.0')
        native=NativeGame(self.args.pid,[],timeout=3);samples=[];errors=[]
        ready=threading.Event();stop=threading.Event()
        def collect():
            try:
                deadline=time.monotonic()+6
                while not stop.is_set() and time.monotonic()<deadline:
                    nodes=native.call('hud.rects',{'root':'menu','max_depth':4})['nodes']
                    root=next((n for n in nodes if n['path'].endswith('/CupWorkspace')),None)
                    if root is not None:
                        rows=[n for n in nodes if re.search(r'/CupRound\d+$',n['path'])]
                        samples.append({'time':time.monotonic(),'alpha':root['color']['a'],'rect':root['rect'],'rows':[(n['path'],n['color']['a'],n['rect'])for n in rows]})
                    ready.set();time.sleep(.005)
            except Exception as error:errors.append(repr(error));ready.set()
        thread=threading.Thread(target=collect,name='native-cup-motion-sampler',daemon=True);thread.start()
        try:
            assert await asyncio.to_thread(ready.wait,3) and not errors,errors
            before=(await self.query('vr.targets'))['clicks']
            aim.update(command_id=uuid.uuid4().hex,hold_frames=45)
            receipt=await self.tool('click_ui_at',aim,'enter-cup-once')
            assert receipt['status']=='input_delivered' and await self.cup()==original
            assert (await self.query('vr.targets'))['clicks']==before+1
            await self.tool('click_ui_at',aim,'same-entry-command-retry')
            assert (await self.query('vr.targets'))['clicks']==before+1
            await asyncio.sleep(.4)
        finally:
            stop.set();await asyncio.to_thread(thread.join,4)
            (self.args.evidence/'motion-samples.json').write_text(json.dumps({'samples':samples,'errors':errors},indent=2))
        assert not thread.is_alive() and not errors,errors
        assert samples and samples[-1]['alpha']>=.995
        levels=sorted({round(v['alpha']*255)for v in samples})
        assert len(levels)>=3 and min(levels)<250,levels
        assert all(v['rect']==samples[0]['rect'] for v in samples)
        assert all(b['alpha']>=a['alpha'] for a,b in zip(samples,samples[1:])), 'Cup entrance restarted'
        assert all(len(v['rows'])==len(original['rounds']) for v in samples)
        rows={path:[] for path,_,_ in samples[0]['rows']}
        for sample in samples:
            for path,alpha,rect in sample['rows']:
                rows[path].append((alpha,rect))
        for entries in rows.values():
            assert len({tuple(v[1])for v in entries})==1
            assert entries[-1][0]>=.995
        self.cases.append({'case':'native-cup-entrance-and-staggered-rows-stable-geometry-one-entry-retry',
            'passed':True,'opacity_levels':levels,'sample_count':len(samples),
            'scope':'Actual native HUD opacity/geometry and XR callbacks; not high-speed GPU video or hardware comfort'})

    async def takeover(self):
        original=await self.layout('takeover-entry')
        await self.hover('helper.Validate.CupStart');capture=await self.capture('takeover-source')
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        back=next(t['rect'] for t in (await self.query('vr.targets'))['targets'] if t['name']=='helper.Cancel.CupBack')
        _,aim=await self.point_args('helper.Validate.CupStart')
        receipt=await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        assert receipt['status']=='input_delivered'
        legacy=await self.query('menu.get_state');assert not legacy['cup']['native']
        assert not any(n['visible'] and n['path'].endswith('/CupWorkspace') for n in await self.nodes())
        self.cases.append({'case':'actual-stick-restores-original-cup-preview','passed':True})
        fresh=await self.capture('legacy-preview')
        projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[carrier]
        x,y=quad_pixel(projection,back)
        receipt=await self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':'left','surface':{k:carrier[k]for k in ('pose','size')},'origin_mode':'hand','dwell_frames':45},'ray-restores-native-cup')
        assert receipt['status']=='input_delivered' and await self.layout('cup-restored')==original
        self.cases.append({'case':'fresh-ray-restores-native-cup-without-starting-or-changing-state','passed':True})

async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    server=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'scope':'Native Cup data, presentation and real XR non-racing callbacks; not hardware or next-round qualification'}
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=CupTest(args,session)
            try:
                if args.motion_only:await t.motion()
                elif args.takeover_only:await t.takeover()
                elif args.layout_only:await t.layout('cup-layout')
                else:await t.prove()
                result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['cases']=t.cases;(args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right')
    m=p.add_mutually_exclusive_group();m.add_argument('--takeover-only',action='store_true');m.add_argument('--layout-only',action='store_true');m.add_argument('--motion-only',action='store_true')
    asyncio.run(run(p.parse_args()))
