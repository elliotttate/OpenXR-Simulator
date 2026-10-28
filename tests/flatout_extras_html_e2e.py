"""Native Extras: rendered layout, physical ray feedback and real destinations."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

NAMES=('extras.statistics','extras.credits','helper.Cancel.ExtrasBack')


class ExtrasTest(Frontend):
    async def layout(self,label):
        state=await self.query('menu.get_state')
        assert state['current_menu']=='EXTRAS' and state['item_count']==2,state
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert any(n['effective_visible'] and n['path'].endswith('/ExtrasWorkspace') for n in nodes)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label+'-stereo')
        self.last_capture=capture
        assert_reading_distance(capture,3.,4.5)
        controls=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in controls}==set(NAMES),controls
        assert all(t['enabled'] and t['physical_hover'] for t in controls)
        rects={t['name']:t['rect'] for t in controls}
        assert all(20<=r[0]<r[2]<=1900 and 20<=r[1]<r[3]<=1056 for r in rects.values())
        assert rects[NAMES[1]][0]-rects[NAMES[0]][2]>=20
        assert rects[NAMES[2]][1]-max(rects[n][3] for n in NAMES[:2])>=32
        assert all(r[3]-r[1]>=103.999 for r in rects.values())
        for i,item in enumerate(state['items']):
            title=next(n for n in nodes if n['path'].endswith('/ExtrasCard'+str(i)+'/ExtrasCardTitle'))
            assert title['text']==item['title'],(title,item)
            assert not item['rendered_bounds']['visible'],'Old spatial labels draw behind the cards'
            photo=await self.query('hud.get_node',{'root':'menu','name':'Menu/ExtrasWorkspace/ExtrasCard'+str(i)+'/ExtrasPhoto','presentation':True})
            assert 'LEVELS_' in photo['presentation']['texture_resource'].upper(),photo
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/ExtrasWorkspace/ExtrasHeading','presentation':True})
        assert heading['text'].strip() and heading['text'].strip().upper()!='NOT FOUND',heading
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        assert not any(n['effective_visible'] and n['path'].endswith(('/Header','/Helpers','/Content_Zone')) for n in nodes)
        from flatout_header_geometry import measure_header_accent
        header=next(n for n in nodes if n['path'].endswith('/ExtrasHeader'))
        angles=measure_header_accent(capture,self.last_image_path,header['rect'])
        assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit(label)
        self.cases.append({'case':label+'-original-data-fonts-yellow-header-and-stereo-layout','passed':True,'header':angles})
        return state,rects

    async def takeover(self):
        original,bounds=await self.layout('before-takeover')
        _,_,aim=await self.hover(NAMES[2])
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'takeover-source')
        self.last_capture=source
        carrier=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],
            'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert not any(n['effective_visible'] and n['path'].endswith('/ExtrasWorkspace') for n in nodes)
        state=await self.query('menu.get_state')
        assert [i['title'] for i in state['items']]==[i['title'] for i in original['items']]
        assert all(i['rendered_bounds']['visible'] for i in state['items'])
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'original-spatial-layout')
        self.last_capture=capture
        projection=copy.deepcopy(capture);projection['geometry']['surfaces']=[carrier]
        x,y=quad_pixel(projection,bounds[NAMES[0]])
        clicks=(await self.query('vr.targets'))['clicks']
        await self.tool('hover_ui_at',{'capture_id':capture['capture_id'],'x':float(x),'y':float(y),
            'hand':self.args.hand,'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},
            'dwell_frames':45},'deliberate-ray-return')
        await asyncio.sleep(.45);await self.layout('after-takeover')
        assert (await self.query('vr.targets'))['clicks']==clicks
        self.cases.append({'case':'actual-stick-restores-original-and-ray-restores-native-without-click','passed':True})

    async def prove(self):
        state,bounds=await self.layout('entry')
        selected=state['selection_index']
        for name in NAMES:
            await self.hover(name);await asyncio.sleep(.4)
            assert (await self.query('menu.get_state'))['selection_index']==selected
            assert {t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}==bounds
            capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'lift-'+name)
            self.last_capture=capture;assert_hover_lift(capture)
            self.cases.append({'case':'visual-only-physical-hover-'+name,'passed':True})
        for action,menu in zip(NAMES[:2],('PLAYER STATS','CREDITS')):
            await self.click(action)
            assert (await self.query('menu.get_state'))['current_menu']==menu
            assert not (await self.query('ui.get_message_box'))['open']
            cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
            await self.click(cancel);await asyncio.sleep(.4)
            await self.layout('returned-from-'+menu)
        await self.click(NAMES[2])
        state=await self.query('menu.get_state')
        assert state['current_menu']=='MAIN MENU' and state['is_3d']
        assert not (await self.query('vr.targets'))['targets']
        self.cases.append({'case':'real-statistics-credits-return-and-preserved-main-with-one-shot-retries','passed':True})

    async def motion(self):
        await self.layout('motion-source')
        await self.click(NAMES[0])
        assert (await self.query('menu.get_state'))['current_menu']=='PLAYER STATS'
        cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        samples=[]
        pending=asyncio.create_task(self.click(cancel))
        try:
            deadline=asyncio.get_running_loop().time()+4
            while asyncio.get_running_loop().time()<deadline:
                nodes=(await self.query('hud.rects',{'root':'menu','max_depth':6}))['nodes']
                root=next((n for n in nodes if n['path'].endswith('/ExtrasWorkspace') and n['effective_visible']),None)
                if root:
                    cards=[next(n for n in nodes if n['path'].endswith('/ExtrasCard'+str(i))) for i in range(2)]
                    samples.append({'time':asyncio.get_running_loop().time(),'root_alpha':root['color']['a'],
                        'card_alpha':[c['color']['a'] for c in cards],'rects':[c['rect'] for c in cards]})
                if pending.done() and samples and samples[-1]['root_alpha']>=.999:break
                await asyncio.sleep(.005)
        finally:
            await pending
            (self.args.evidence/'motion-samples.json').write_text(json.dumps(samples,indent=2))
        assert samples and any(.55<=s['root_alpha']<.99 for s in samples),samples
        assert all(s['rects']==samples[0]['rects'] for s in samples),'Entrance moved the ray hit rectangles'
        assert all(b['root_alpha']>=a['root_alpha'] for a,b in zip(samples,samples[1:]))
        assert samples[-1]['root_alpha']>=.999
        self.cases.append({'case':'actual-ray-return-fades-native-board-with-fixed-control-bounds','passed':True,
            'samples':len(samples),'scope':'Native HUD opacity and stereo final frame; hardware and GPU timeline pending'})
        await self.layout('motion-return')


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'hand':a.hand,'scope':'Owned simulator, real XR actions and stereo capture; not hardware qualification'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=ExtrasTest(a,session)
            try:
                if a.motion_only:await t.motion()
                elif a.takeover_only:await t.takeover()
                elif a.layout_only:await t.layout('layout')
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0
                report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true')
    p.add_argument('--motion-only',action='store_true')
    asyncio.run(run(p.parse_args()))
