"""Native Statistics: all real values, bounded pages, XR controls and stereo."""
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

PREV='statistics.page.previous'
NEXT='statistics.page.next'
BACK='helper.Cancel.StatisticsBack'


class StatisticsTest(Frontend):
    async def layout(self,label):
        state=await self.query('statistics.get_state');native=state['native_table']
        assert state['open'] and native['active'],state
        assert len(native['rows'])==30 and native['page_count']==3,native
        assert len({r['source'] for r in native['rows']})==30
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        visible=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['effective_visible'])
        root=visible('/StatisticsWorkspace');header=visible('/StatisticsHeader')
        table=visible('/StatisticsTable');summary=visible('/StatisticsSummary')
        assert 24<=table['rect'][1]-summary['rect'][3]<=40
        assert root['rect']==[0,0,1920,1080]
        rendered=[]
        for i,row in enumerate(native['rows'][native['page']*10:(native['page']+1)*10]):
            a=visible('/StatisticsRow'+str(i)+'/StatisticsLabel');b=visible('/StatisticsRow'+str(i)+'/StatisticsValue')
            assert a['text']==row['label'] and b['text']==row['value'],(row,a,b)
            assert a['rect'][2]+20<=b['rect'][0],(a,b)
            assert a['rect'][1]>=table['rect'][1] and b['rect'][3]<=table['rect'][3]
            rendered.append(row)
        actions=(await self.query('vr.targets'))['targets']
        names={a['name'] for a in actions}
        assert names=={BACK}|({PREV} if native['page'] else set())|({NEXT} if native['page']+1<3 else set()),actions
        assert all(a['physical_hover'] and a['enabled'] for a in actions)
        back=next(a for a in actions if a['name']==BACK)
        assert back['rect'][1]-table['rect'][3]>=32 and back['rect'][3]<=1056
        for i in range(3):
            text=visible('/StatisticsSummaryTitle'+str(i))['text'];value=visible('/StatisticsSummaryValue'+str(i))['text']
            assert any(r['label']==text and r['value']==value for r in native['rows']),(text,value)
        # FlashRenderer exposes a passive container even when its draw tree is
        # hidden. Check the rendered descendants, not that bookkeeping dummy.
        assert not any(n['effective_visible'] and ('/Lobby_Game/' in n['path'] or n['path'].endswith(('/Helpers','/Header'))) for n in nodes)
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/StatisticsWorkspace/StatisticsHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label+'-stereo');self.last_capture=capture
        assert_reading_distance(capture,3.,4.5)
        from flatout_header_geometry import measure_header_accent
        angles=measure_header_accent(capture,self.last_image_path,header['rect'])
        assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit(label)
        self.cases.append({'case':label+'-native-values-fonts-yellow-layout-and-stereo','passed':True,'rows':rendered,'header':angles})
        return state,nodes

    async def disabled(self,index):
        state,nodes=await self.layout('disabled-'+str(index))
        action=next(n for n in nodes if n['path'].endswith('/StatisticsAction'+str(index)))
        assert max(action['color'][k] for k in ('r','g','b'))<.45,action
        capture=self.last_capture
        x,y=quad_pixel(capture,action['rect'])
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        args={'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
              'surface_id':carrier['id'],'origin_mode':'hand','dwell_frames':30,'hold_frames':30,'command_id':uuid.uuid4().hex}
        before=(await self.query('vr.targets'))['clicks']
        await self.tool('click_ui_at',args,'disabled-page-click-'+str(index))
        after=await self.query('statistics.get_state')
        assert after['native_table']['page']==state['native_table']['page']
        assert (await self.query('vr.targets'))['clicks']==before
        self.cases.append({'case':'disabled-page-is-grey-and-inert-'+str(index),'passed':True})

    async def prove(self):
        seen=[]
        while (await self.query('statistics.get_state'))['native_table']['page']:
            await self.click(PREV)
        await self.disabled(0)
        for page in range(3):
            state,_=await self.layout('page-'+str(page));assert state['native_table']['page']==page
            seen.extend(r['source'] for r in state['native_table']['rows'][page*10:(page+1)*10])
            controls=await self.query('vr.targets');catalog=state['native_table']['rows']
            for target in controls['targets']:
                await self.hover(target['name']);await asyncio.sleep(.3)
                assert (await self.query('statistics.get_state'))['native_table']['rows']==catalog
                capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'hover-'+str(page)+'-'+target['name']);self.last_capture=capture
                assert_hover_lift(capture)
            if page<2:await self.click(NEXT)
        assert len(seen)==len(set(seen))==30,seen
        await self.disabled(1)
        await self.click(PREV);await self.click(PREV)
        await self.click(BACK)
        assert (await self.query('menu.get_state'))['current_menu']=='EXTRAS'
        self.cases.append({'case':'all-thirty-values-reached-once-bounded-paging-held-trigger-and-retries','passed':True})

    async def takeover(self):
        state,nodes=await self.layout('before-takeover')
        # Move from Back to the header; leaving the ray stationary after a
        # stick takeover must not steal input back from the controller.
        bounds=next(n['rect'] for n in nodes if n['path'].endswith('/StatisticsAction1'))
        _,_,aim=await self.hover(BACK)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'takeover-source');self.last_capture=capture
        carrier=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.], 'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        after=await self.query('statistics.get_state');assert not after['native_table']['active']
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert any(n['effective_visible'] and '/Lobby_Game/' in n['path'] for n in nodes)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'original-keyboard-layout');self.last_capture=capture
        projection=copy.deepcopy(capture);projection['geometry']['surfaces']=[carrier]
        x,y=quad_pixel(projection,bounds)
        await self.tool('hover_ui_at',{'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
            'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},'dwell_frames':45},'deliberate-ray-return')
        deadline=asyncio.get_running_loop().time()+3
        while not (await self.query('statistics.get_state'))['native_table']['active']:
            assert asyncio.get_running_loop().time()<deadline,'Deliberate ray did not restore the native reader'
            await asyncio.sleep(.025)
        await self.layout('after-takeover')
        self.cases.append({'case':'actual-stick-original-layout-and-deliberate-ray-return','passed':True})

    async def motion(self):
        while (await self.query('statistics.get_state'))['native_table']['page']:
            await self.click(PREV)
        await self.layout('motion-source')
        _,_,args=await self.hover(NEXT)
        args.update(command_id=uuid.uuid4().hex,hold_frames=45)
        pending=asyncio.create_task(self.tool('click_ui_at',args,'motion-page-click'));samples=[]
        try:
            deadline=asyncio.get_running_loop().time()+4
            while asyncio.get_running_loop().time()<deadline:
                state=await self.query('statistics.get_state')
                if state['native_table']['page']==1:
                    nodes=(await self.query('hud.rects',{'root':'menu','max_depth':6}))['nodes']
                    table=next(n for n in nodes if n['path'].endswith('/StatisticsTable'))
                    root=next(n for n in nodes if n['path'].endswith('/StatisticsWorkspace'))
                    actions=[n['rect'] for n in nodes if '/StatisticsAction' in n['path'] and n['kind']=='panel']
                    samples.append({'table_alpha':table['color']['a'],'root_alpha':root['color']['a'],'actions':actions})
                    if pending.done() and samples[-1]['table_alpha']>=.999:break
                await asyncio.sleep(.008)
        finally:
            await pending;(self.args.evidence/'motion-samples.json').write_text(json.dumps(samples,indent=2))
        assert samples and any(.55<=s['table_alpha']<.99 for s in samples),samples
        assert all(s['root_alpha']>=.999 for s in samples),'Paging faded the stable header/action dock'
        assert all(s['actions']==samples[0]['actions'] for s in samples),'Paging moved a hit target'
        assert samples[-1]['table_alpha']>=.999
        await self.tool('click_ui_at',args,'motion-page-retry')
        assert (await self.query('statistics.get_state'))['native_table']['page']==1
        self.cases.append({'case':'page-content-fades-with-stable-header-and-actions','passed':True,'samples':len(samples)})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'hand':a.hand,'scope':'Owned isolated simulator; actual XR, native model and composed stereo, not hardware qualification'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=StatisticsTest(a,session)
            try:
                if a.layout_only:await t.layout('layout')
                elif a.takeover_only:await t.takeover()
                elif a.motion_only:await t.motion()
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true')
    p.add_argument('--motion-only',action='store_true')
    asyncio.run(run(p.parse_args()))
