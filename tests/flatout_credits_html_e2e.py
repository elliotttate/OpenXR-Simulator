"""Live native Credits reader, original content and actual XR transport."""
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

NAMES=('helper.Y.0','helper.LB.1','helper.RB.2','helper.Cancel.3')


class CreditsTest(Frontend):
    async def credits_layout(self):
        menu=await self.query('menu.get_state');assert menu['current_menu']=='CREDITS'
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18,'all':True}))['nodes']
        visible=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['effective_visible'])
        panel=visible('/CreditsReadingPanel');viewport=visible('/CreditsViewport');header=visible('/CreditsHeader')
        l,t,r,b=panel['rect'];vl,vt,vr,vb=viewport['rect']
        assert 0<l<vl<vr<r<1920 and 0<t<vt<vb<b<1080
        text=[n for n in nodes if '/CreditsText/' in n['path'] and n.get('text')]
        assert len(text)>50,'Authored credits content is missing'
        assert any('Flat2VR' in n['text'] for n in text),text[:3]
        assert not any(n['effective_visible'] and n['kind']=='panel' and n['color']['a']>.2 and n['rect'][2]-n['rect'][0]>=1900 and n['rect'][3]-n['rect'][1]>=1070 for n in nodes),'A full black backdrop covers the scene'
        actions=(await self.query('vr.targets'))['targets'];assert {a['name'] for a in actions}==set(NAMES)
        assert all(a['physical_hover'] for a in actions)
        assert all(a['rect'][1]-b>=32 and a['rect'][3]<=1056 and a['rect'][2]-a['rect'][0]>=400 and a['rect'][3]-a['rect'][1]>=104 for a in actions),actions
        ordered=sorted(actions,key=lambda a:a['rect'][0]);assert all(b['rect'][0]-a['rect'][2]>=15 for a,b in zip(ordered,ordered[1:]))
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/CreditsWorkspace/CreditsHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},'credits-layout-stereo');self.last_capture=capture
        assert_reading_distance(capture,3.,4.5)
        from flatout_header_geometry import measure_header_accent
        angles=measure_header_accent(capture,self.last_image_path,header['rect']);assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit('credits-controls')
        self.cases.append({'case':'credits-original-content-theme-bounded-reader-and-stereo','passed':True,'authored_text_nodes':len(text),'header':angles})

    async def prove(self):
        await self.credits_layout()
        await self.click(NAMES[0]);assert (await self.query('menu.get_state'))['credits']['paused']
        for name in NAMES:
            before=(await self.query('menu.get_state'))['credits']['position']
            await self.hover(name);await asyncio.sleep(.3)
            assert abs((await self.query('menu.get_state'))['credits']['position']-before)<1
            capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'physical-hover-'+name);self.last_capture=capture
            assert_hover_lift(capture)
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':12}))['nodes']
        assert any(n['effective_visible'] and n['path'].endswith('/CreditsResumeIcon') for n in nodes)
        assert not any(n['effective_visible'] and n['path'].endswith(('/CreditsPauseBar0','/CreditsPauseBar1')) for n in nodes)
        self.cases.append({'case':'paused-action-shows-resume-icon-with-resume-label','passed':True})
        await self.credits()

    async def takeover(self):
        await self.credits_layout();await self.click(NAMES[0])
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        bounds=next(n['rect'] for n in nodes if n['path'].endswith('/CreditsAction0'))
        _,_,aim=await self.hover(NAMES[3])
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'takeover-source');self.last_capture=capture
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        # Vertical stick input over this reader belongs to smooth scrolling.
        # Horizontal navigation deliberately returns to the controller layout.
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[1.,0],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert not any(n['effective_visible'] and n['path'].endswith('/CreditsWorkspace') for n in nodes)
        assert any(n['effective_visible'] and n['path'].endswith('/CreditsText') for n in nodes),'Original credits roll did not restore'
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'original-credits-roll');self.last_capture=capture
        projection=copy.deepcopy(capture);projection['geometry']['surfaces']=[carrier];x,y=quad_pixel(projection,bounds)
        await self.tool('hover_ui_at',{'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
            'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},'dwell_frames':45},'deliberate-ray-return')
        deadline=asyncio.get_running_loop().time()+3
        while True:
            nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
            if any(n['effective_visible'] and n['path'].endswith('/CreditsWorkspace') for n in nodes):break
            assert asyncio.get_running_loop().time()<deadline
            await asyncio.sleep(.025)
        await self.credits_layout()
        self.cases.append({'case':'original-credits-roll-on-stick-and-native-on-deliberate-ray-return','passed':True})

    async def motion(self):
        await self.click(NAMES[3]);assert (await self.query('menu.get_state'))['current_menu']=='EXTRAS'
        pending=asyncio.create_task(self.click('extras.credits'));samples=[]
        try:
            deadline=asyncio.get_running_loop().time()+5
            while asyncio.get_running_loop().time()<deadline:
                nodes=(await self.query('hud.rects',{'root':'menu','max_depth':6}))['nodes']
                root=next((n for n in nodes if n['path'].endswith('/CreditsWorkspace') and n['effective_visible']),None)
                if root:
                    actions=[n['rect'] for n in nodes if n['path'].split('/')[-1] in ['CreditsAction'+str(i) for i in range(4)]]
                    samples.append({'alpha':root['color']['a'],'actions':actions})
                    if pending.done() and samples[-1]['alpha']>=.999:break
                await asyncio.sleep(.008)
        finally:
            await pending;(self.args.evidence/'motion-samples.json').write_text(json.dumps(samples,indent=2))
        assert samples and any(.55<=s['alpha']<.99 for s in samples),samples
        assert all(s['actions']==samples[0]['actions'] for s in samples)
        assert samples[-1]['alpha']>=.999
        await self.credits_layout()
        self.cases.append({'case':'actual-ray-entry-fade-and-fixed-controls','passed':True,'samples':len(samples)})

    async def smooth(self):
        await self.credits_layout()
        if not (await self.query('menu.get_state'))['credits']['paused']:await self.click(NAMES[0])
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':12}))['nodes']
        viewport=next(n['rect'] for n in nodes if n['path'].endswith('/CreditsViewport'))
        controls=(await self.query('vr.targets'))['targets'];rect=next(t['rect'] for t in controls if t['name']==NAMES[0])
        x=(viewport[0]+viewport[2])/2;y=(viewport[1]+viewport[3])/2
        _,args=await self.point_args(NAMES[0],((x-rect[0])/(rect[2]-rect[0]),(y-rect[1])/(rect[3]-rect[1])))
        before=(await self.query('menu.get_state'))['credits']['position']
        pending=asyncio.create_task(self.tool('scroll_ui_at',{**args,'stick':[0,-.65],'hold_frames':45,'command_id':uuid.uuid4().hex},'actual-smooth-reader-scroll'))
        samples=[]
        try:
            while not pending.done():
                samples.append((await self.query('menu.get_state'))['credits']['position']);await asyncio.sleep(.02)
        finally:await pending
        after=(await self.query('menu.get_state'))['credits']
        assert after['paused'] and 20<before-after['position']<500,(before,after)
        assert len(set(round(p,1) for p in samples))>=3,samples
        assert all(b<=a+.1 for a,b in zip(samples,samples[1:])),samples
        assert any(n['effective_visible'] and n['path'].endswith('/CreditsWorkspace') for n in (await self.query('hud.rects',{'root':'menu','max_depth':6}))['nodes'])
        self.cases.append({'case':'vertical-stick-smooth-scroll-keeps-native-reader-and-pause','passed':True,'samples':samples})

    async def finish(self):
        await self.credits_layout()
        if (await self.query('menu.get_state'))['credits']['paused']:await self.click(NAMES[0])
        before,args=await self.point_args(NAMES[2]);await self.tool('hold_ui_at',args,'full-roll-hold')
        start=asyncio.get_running_loop().time();refresh=start;samples=[];fades=[]
        try:
            while True:
                current=await self.query('menu.get_state');now=asyncio.get_running_loop().time()
                if current['current_menu']!='CREDITS':assert current['current_menu']=='EXTRAS';break
                assert now-start<100,'Forward never finished the authored roll'
                nodes=(await self.query('hud.rects',{'root':'menu','max_depth':6}))['nodes']
                root=next(n for n in nodes if n['path'].endswith('/CreditsWorkspace'))
                if root['color']['a']<.99:fades.append(root['color']['a'])
                samples.append(current['credits']['position'])
                if now-refresh>4 and abs(current['credits']['speed'])>1:
                    _,args=await self.point_args(NAMES[2]);await self.tool('hold_ui_at',args,'full-roll-refresh');refresh=now
                await asyncio.sleep(.025)
        finally:
            capture=await self.capture('full-roll-release-source');await self.tool('release_ui_pointer',{'capture_id':capture['capture_id'],'hand':self.args.hand},'full-roll-release')
        after=await self.query('vr.targets');assert after['clicks']==before['clicks']+1
        assert {t['name'] for t in after['targets']}=={'extras.statistics','extras.credits','helper.Cancel.ExtrasBack'},after
        assert fades and all(0<=a<.99 for a in fades)
        self.cases.append({'case':'complete-authored-roll-held-forward-fade-and-clean-native-extras-return','passed':True,'seconds':now-start,'frames_sampled':len(samples),'fade':fades})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'hand':a.hand,'scope':'Owned simulator, native authored credits and real XR; hardware qualification pending'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=CreditsTest(a,session)
            try:
                if a.layout_only:await t.credits_layout()
                elif a.takeover_only:await t.takeover()
                elif a.motion_only:await t.motion()
                elif a.smooth_only:await t.smooth()
                elif a.finish_only:await t.finish()
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true')
    p.add_argument('--motion-only',action='store_true');p.add_argument('--smooth-only',action='store_true');p.add_argument('--finish-only',action='store_true')
    asyncio.run(run(p.parse_args()))
