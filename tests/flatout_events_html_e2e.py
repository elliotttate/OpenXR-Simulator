"""Native Career events: real factory data, four-card paging and XR callbacks."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame


def factory_entries(fox,car_class):
    """Read source values as data; never execute game configuration scripts."""
    result={}
    for series in range(3):
        source=fox/'Flatout/Data/Common/Tuning/Factories'/('Events'+car_class+'Level'+str(series)+'.cfg')
        text='\n'.join(line.split('--',1)[0] for line in source.read_text(encoding='latin1').splitlines())
        starts=list(re.finditer(r'^\s*Event\d+\s*=\s*\{',text,re.M))
        for i,start in enumerate(starts):
            block=text[start.end():starts[i+1].start() if i+1<len(starts) else len(text)]
            name=re.search(r'\bIndex\s*=\s*"([^"]+)"',block).group(1)
            prizes=[int(re.search(r'\bCredits'+medal+r'\s*=\s*(\d+)',block).group(1)) for medal in ('Gold','Silver','Bronze')]
            unlock=re.search(r'\bUnlock\s*=\s*"([^"]*)"',block)
            result[name]={'series':series,'prizes':prizes,'rounds':len(re.findall(r'^\s*TrackName\s*=',block,re.M)),
                          'unlock':unlock.group(1) if unlock else ''}
    return result


class EventsTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':20}))['nodes']

    async def grid(self):
        state=await self.query('menu.get_state')
        assert state['current_menu']=='FLATOUT_MODE' and not state['is_3d'],state
        assert state['career']['native_catalog'],state
        return state,state['career']

    async def cancel(self):
        name=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(name)

    async def takeover(self,keyboard=False):
        _,original=await self.layout('takeover-entry')
        assert original['page']==0,'Use the first page for the original-series crossover check'
        target=original['tiles'][0]['target']
        await self.hover(target,(.5,.75))
        capture=await self.capture('takeover-native-carrier')
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        _,aim=await self.point_args(target,(.5,.75))
        if keyboard:
            # The test never synthesizes OS input. Its caller must press one
            # navigation key in the owned game window through Computer Use.
            print('AWAITING_EXTERNAL_KEYBOARD: send one Down key to the owned game window',flush=True)
            deadline=time.monotonic()+60
            while (await self.query('menu.get_state'))['career']['native_catalog']:
                assert time.monotonic()<deadline,'No actual keyboard takeover observed'
                await asyncio.sleep(.05)
            await asyncio.sleep(.15)
        else:
            receipt=await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,
                'command_id':uuid.uuid4().hex},'real-left-stick-takeover')
            assert receipt['status']=='input_delivered',receipt
        fallback,legacy=await self.grid_legacy()
        expected_selection=next(t['event_id'] for t in legacy['tiles'] if t['line']==legacy['selected_line'] and t['index']==fallback['selection_index'])
        (self.args.evidence/'takeover-legacy-state.json').write_text(json.dumps(fallback,indent=2))
        assert not any(n['visible'] and n['path'].endswith('/EventWorkspace') for n in await self.nodes())
        assert legacy['page_count']==3 and legacy['tiles']
        self.cases.append({'case':'actual-keyboard-restores-original-three-series-layout' if keyboard else 'real-controller-input-restores-original-three-series-layout','passed':True})
        fresh=await self.capture('takeover-original-layout')
        rect=next(t['rect'] for t in (await self.query('vr.targets'))['targets'] if t['name']==target)
        # Reaim using a fresh observation and the independently observed native
        # reading plane. The ray enters the new presentation without a click.
        projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[{**carrier,'visibility':0}]
        l,t,r,b=rect
        # The old card lies on the same carrier but at a different screen box.
        # Choose its own current rectangle so the fallback has a real UI hit.
        x,y=quad_pixel(projection,(l+(r-l)*.65,t+(b-t)*.7,l+(r-l)*.65,t+(b-t)*.7))
        receipt=await self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':'left',
            'surface':{k:carrier[k] for k in ('pose','size')},'origin_mode':'hand','dwell_frames':45},'real-ray-restores-native-catalog')
        assert receipt['status']=='input_delivered',receipt
        _,restored=await self.layout('takeover-native-restored')
        assert restored['catalog']==original['catalog'] and restored['selected_event_id']==expected_selection
        self.cases.append({'case':'real-ray-reaim-restores-four-page-catalog-without-activation','passed':True})

    async def grid_legacy(self):
        state=await self.query('menu.get_state')
        assert state['current_menu']=='FLATOUT_MODE' and not state['career']['native_catalog'],state
        return state,state['career']

    async def binding(self):
        """A reused arrow on another page must retire the old presentation.

        Audio diagnostics record the exact owner/generation/action identity used
        by the real pointer, rather than a test-only copy of the menu counter.
        Both pages keep the same enabled page.next action and HUD owner.
        """
        _,initial=await self.grid()
        assert initial['page']==0 and initial['page_count']>=3
        async def arrow_identity():
            await self.hover('events.garage')
            await self.hover('page.next')
            events=(await self.query('audio.ui_feedback'))['events']
            return next(e['target'] for e in reversed(events)
                        if e['cue']=='hover' and e['target'].endswith(':page.next'))
        first=await arrow_identity()
        stable=await arrow_identity()
        assert stable==first,'Ordinary hover recreated the presentation'
        try:
            await self.click('page.next')
            _,paged=await self.grid()
            assert paged['page']==1
            replaced=await arrow_identity()
        finally:
            if (await self.grid())[1]['page']==1:await self.click('page.prev')
        self.cases.append({'case':'page-replacement-invalidates-reused-action-identity',
            'passed':replaced!=first,'before':first,'stable':stable,'replacement':replaced})
        assert replaced!=first,('New page inherited the previous physical presentation',first,replaced)
        assert (await self.query('game.errors'))['total']==0

    async def motion(self):
        _,before=await self.layout('motion-entry')
        assert before['page']==0
        nodes=await self.nodes()
        depth=max(n['depth'] for n in nodes if n['path'].endswith(('/EventWorkspace','/EventCard0','/EventCard1','/EventCard2','/EventCard3')))
        _,aim=await self.point_args('page.next')
        aim.update(command_id=uuid.uuid4().hex,dwell_frames=1)
        native=NativeGame(self.args.pid,[],timeout=3)
        samples=[];errors=[];ready=threading.Event();stop=threading.Event()
        def collect():
            try:
                deadline=time.monotonic()+4
                while not stop.is_set() and time.monotonic()<deadline and len(samples)<1000:
                    nodes=native.call('hud.rects',{'root':'menu','max_depth':depth})['nodes']
                    root=next(n for n in nodes if n['path'].endswith('/EventWorkspace'))
                    cards=[next(n for n in nodes if n['path'].endswith('/EventCard'+str(i))) for i in range(4)]
                    samples.append({'time':time.monotonic(),'root_alpha':root['color']['a'],'root_rect':root['rect'],
                        'card_alphas':[c['color']['a'] for c in cards],'card_rects':[c['rect'] for c in cards]})
                    ready.set();time.sleep(.005)
            except Exception as error:errors.append(repr(error));ready.set()
        thread=threading.Thread(target=collect,name='native-event-motion-sampler',daemon=True);thread.start()
        try:
            assert await asyncio.to_thread(ready.wait,3)
            assert not errors,errors
            original_clicks=(await self.query('vr.targets'))['clicks']
            held=await self.tool('hold_ui_at',aim,'motion-page-next-held')
            assert held['status']=='input_delivered',held
            await asyncio.sleep(.8)
            after=await self.query('vr.targets');_,state=await self.grid()
            assert after['clicks']==original_clicks+1 and state['page']==1
            await self.tool('hold_ui_at',aim,'motion-same-command-retry')
            assert (await self.query('vr.targets'))['clicks']==after['clicks']
        finally:
            stop.set();await asyncio.to_thread(thread.join,4)
            if self.last_capture:
                await self.tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand},'motion-release')
            (self.args.evidence/'motion-samples.json').write_text(json.dumps({'samples':samples,'errors':errors},indent=2))
        assert not thread.is_alive() and not errors,errors
        assert samples and all(s['root_rect']==samples[0]['root_rect'] and s['card_rects']==samples[0]['card_rects'] for s in samples)
        assert min(s['root_alpha'] for s in samples)>=.995,'Paging replayed the whole header/footer entrance'
        levels=sorted({round(a*255) for s in samples for a in s['card_alphas']})
        assert len(levels)>=3 and min(levels)<240 and all(a>=.995 for a in samples[-1]['card_alphas']),levels
        self.cases.append({'case':'stable-reader-and-card-geometry-with-content-only-page-fade-and-one-held-activation',
            'passed':True,'sample_count':len(samples),'card_alpha_levels':levels,
            'scope':'Native HUD opacity samples and XR callbacks; not GPU pixel video or headset comfort'})
        await self.click('page.prev')

    async def layout(self,label):
        state,grid=await self.grid();nodes=await self.nodes();native=await self.query('vr.targets')
        assert any(n['visible'] and n['path'].endswith('/EventWorkspace') for n in nodes)
        tiles=grid['tiles'];assert tiles==grid['catalog'][grid['page']*4:(grid['page']+1)*4]
        targets={t['name']:t['rect'] for t in native['targets']}
        assert set(targets)=={t['target'] for t in tiles}|{'page.prev','page.next','helper.Cancel.EventsBack','events.garage'}
        assert len({t['target'] for t in tiles})==len(tiles),'Ambiguous identities on a mixed-series page'
        states={t['name']:t for t in native['targets']}
        for tile in tiles:
            control=states[tile['target']]
            assert control['enabled']==(not tile['locked'] and tile['installed']),control
            assert control['physical_hover'],'Inspectable cards must retain physical hover'
        for name,enabled in (('page.prev',grid['page']>0),('page.next',grid['page']+1<grid['page_count'])):
            assert states[name]['enabled']==enabled and states[name]['physical_hover']==enabled,states[name]
        assert all(20<=r[0]<r[2]<=1900 and 20<=r[1]<r[3]<=1060 for r in targets.values())
        # Native float rectangles can report the authored 104 px arrow as
        # 103.999992 px after transforming its fractional canvas position.
        assert all(r[3]-r[1]>=104-.001 for r in targets.values())
        rects=[targets[t['target']] for t in tiles]
        assert all(b[0]-a[2]>=20 for a,b in zip(rects,rects[1:]))
        def node(slot,name):return next(n for n in nodes if n['path'].endswith('/EventCard'+str(slot)+'/EventContent/'+name))
        for slot,tile in enumerate(tiles):
            assert node(slot,'EventTitle')['text']==tile['title']
            assert node(slot,'EventSeries')['text']==tile['series_title']
            assert int(''.join(filter(str.isdigit,node(slot,'EventRoundCount')['text'])))==tile['rounds']
            for medal in range(3):
                assert int(''.join(filter(str.isdigit,node(slot,'EventPrize'+str(medal))['text'])))==tile['prizes'][medal]
            if tile['locked']:
                assert tile['unlock_title'] and tile['unlock_title'] in node(slot,'EventUnlock')['text']
            assert len(tile['images'])==tile['rounds'] and tile['installed']
            for index,image in enumerate(tile['images']):
                photo=await self.query('hud.get_node',{'root':'menu','name':'Menu/EventWorkspace/EventCard'+str(slot)+'/EventContent/EventRoundPhoto'+str(index),'presentation':True})
                path=self.args.fox/'Flatout/Data/Assets'/image['pack'].removeprefix('DATA:/')
                skin=next(x for x in ET.parse(str(path)+'.XML').iter('skin') if x.attrib['Name']==image['skin'])
                expected=list(map(float,skin.attrib['uvs'].split(';')));actual=photo['presentation']['uv']
                assert expected[0]-1e-5<=actual[0]<actual[2]<=expected[2]+1e-5
                assert expected[1]-1e-5<=actual[1]<actual[3]<=expected[3]+1e-5
        for slot,enabled in ((4,grid['page']>0),(5,grid['page']+1<grid['page_count'])):
            arrow=next(n for n in nodes if n['path'].endswith('/EventWorkspace/EventAction'+str(slot)))
            assert arrow['visible']
            if not enabled:assert arrow['color']['a']<=.31,arrow
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/EventWorkspace/EventHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-both-eyes')
        assert_reading_distance(capture,3.,4.5)
        await self.audit(label)
        self.cases.append({'case':label+'-native-data-art-fonts-layout','passed':True,'page':grid['page'],'events':[t['event_id'] for t in tiles]})
        return state,grid

    async def inert(self,name,label,fraction=(.5,.5)):
        before,aim=await self.point_args(name,fraction)
        state,grid=await self.grid()
        receipt=await self.tool('click_ui_at',{**aim,'command_id':uuid.uuid4().hex,'hold_frames':20},label)
        assert receipt['status']=='input_delivered',receipt
        after=await self.query('vr.targets');_,current=await self.grid()
        assert after['clicks']==before['clicks'],(name,before,after)
        assert (current['page'],current['selected_event_id'])==(grid['page'],grid['selected_event_id'])
        assert not (await self.query('ui.get_message_box'))['open']
        assert not (await self.query('game.get_state'))['is_loading']
        self.cases.append({'case':label,'passed':True})

    async def prove(self):
        _,initial=await self.grid();catalog=initial['catalog']
        prefix=next(c for c in ('Derby','Classic','AllStars') if catalog[0]['source_index'].startswith(c))
        expected=factory_entries(self.args.fox,prefix)
        assert len(catalog)==len(expected)==16 and len({e['event_id'] for e in catalog})==16
        assert initial['page_count']==(len(catalog)+3)//4
        lookup={e['source_index']:e['event_id'] for e in catalog}
        assert set(lookup)==set(expected)
        for entry in catalog:
            source=expected[entry['source_index']]
            assert all(entry[k]==source[k] for k in ('series','prizes','rounds')),(entry,source)
            assert entry['unlock_event_id']==lookup.get(source['unlock'],0),(entry,source)
        self.cases.append({'case':'all-native-factory-events-prizes-rounds-and-dependencies','passed':True,'catalog':catalog})
        while (await self.grid())[1]['page']>0:await self.click('page.prev')
        seen=[]
        for page in range(initial['page_count']):
            state,grid=await self.layout('page-'+str(page))
            before_selection=(state['selection_index'],grid['selected_event_id'])
            rects={t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}
            for tile in grid['tiles']:
                await self.hover(tile['target'],(.5,.75))
                current,native=await self.grid()
                assert (current['selection_index'],native['selected_event_id'])==before_selection,'Hover committed an event'
                assert {t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}==rects
                if tile['locked']:await self.inert(tile['target'],'locked-inert-'+str(tile['event_id']))
                seen.append(tile['event_id'])
            if page+1<initial['page_count']:await self.click('page.next')
        assert seen==[e['event_id'] for e in catalog]
        await self.inert('page.next','last-page-does-not-wrap')
        for _ in range(initial['page_count']-1):await self.click('page.prev')
        await self.inert('page.prev','first-page-does-not-wrap')
        await self.inert('helper.Cancel.EventsBack','blank-footer-does-not-validate',(2.,.5))
        await self.click('page.next');await self.click('page.next')
        await self.click('events.garage');assert (await self.query('menu.get_state'))['current_menu']=='GARAGE'
        await self.cancel();_,returned=await self.layout('garage-return')
        assert returned['page']==2,'Garage return lost the catalog page'
        await self.click('page.prev');await self.click('page.prev')
        _,grid=await self.grid();cup=next(t for t in grid['tiles'] if not t['locked'] and t['rounds']>1)
        await self.click(cup['target'])
        preview=await self.query('menu.get_state')
        # The native preview and grid share the localized FLATOUT_MODE title.
        # Only the grid exposes the career catalog diagnostic.
        assert preview['item_count']==1 and 'career' not in preview
        assert not (await self.query('game.get_state'))['is_loading']
        await self.cancel();_,returned=await self.layout('cup-preview-return')
        assert returned['selected_event_id']==cup['event_id'] and returned['page']==0
        assert returned['catalog']==catalog
        assert (await self.query('game.errors'))['total']==0
        self.cases.append({'case':'all-pages-locks-garage-history-and-native-cup-preview-return','passed':True})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'scope':'Native event catalog data, paging, screenshot-derived XR input and non-racing callbacks'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=EventsTest(args,session)
            try:
                if args.binding_only:await t.binding()
                elif args.motion_only:await t.motion()
                elif args.keyboard_takeover:await t.takeover(keyboard=True)
                elif args.takeover_only:await t.takeover()
                else:await t.prove()
                result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['cases']=t.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right')
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--takeover-only',action='store_true');mode.add_argument('--motion-only',action='store_true')
    mode.add_argument('--keyboard-takeover',action='store_true',help='Wait for real external keyboard input in the owned game window, then verify XR return')
    mode.add_argument('--binding-only',action='store_true',help='Verify page replacement retires the real pointer presentation identity')
    asyncio.run(run(p.parse_args()))
