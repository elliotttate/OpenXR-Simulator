"""Verify real carousel geometry and ray endpoints through screenshot-based XR input."""
import argparse, asyncio, copy, json, os, sys, uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT
from flatout_livery_e2e import world_pixel, ui_surface

class WorldArrows(Frontend):
    async def run(self):
        before=await self.query('vr.targets')
        assert 'WRECK' not in before.get('menu','').upper()
        arrow=next(a for a in before['world_arrows'] if a['side']==self.args.side)
        rest=arrow['rest']; center=np.array(rest['origin'])
        normal=np.array(rest['normal']);normal/=np.linalg.norm(normal)
        vr=await self.query('vr.get_state')
        eye=np.mean([e['render_position'] for e in vr['eyes']],axis=0)
        if np.dot(normal,eye-center)<0:normal=-normal
        frame=copy.deepcopy(before['vr_frame']);right=np.array(rest['right']);up=np.array(rest['up'])
        frame.update(panel_width=float(2*np.linalg.norm(right)),panel_height=float(2*np.linalg.norm(up)))
        frame['panel']={'origin':center.tolist(),'right':(right/np.linalg.norm(right)).tolist(),
                        'up':(up/np.linalg.norm(up)).tolist(),'back':(-normal).tolist()}
        state={'vr':{'frame':frame}}
        source=await self.capture('source-arrow')
        x,y=world_pixel(state,source,center,'left')
        assert 0<=x<source['image_size'][0] and 0<=y<source['image_size'][1],(x,y)
        aim={'capture_id':source['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
             'origin_mode':'hand','surface':ui_surface(state),'dwell_frames':45}
        result=await self.tool('hover_ui_at',aim,'hover-arrow');assert result['status']=='input_delivered',result
        current=await self.query('vr.targets');actual=next(a for a in current['world_arrows'] if a['side']==self.args.side)
        expected=.015 if arrow['enabled'] else 0
        self.check_geometry(arrow,actual,normal,expected,current,'hover')
        raised=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},'stereo-raised-arrow');self.last_capture=raised
        if self.args.press:
            selection=(await self.query('menu.get_state'))['selection_index']
            held=await self.tool('hold_ui_at',aim,'held-arrow');assert held['status']=='input_delivered',held
            pressed=await self.query('vr.targets');p=next(a for a in pressed['world_arrows'] if a['side']==self.args.side)
            # Navigation can reach the endpoint and disable this arrow immediately.
            self.check_geometry(arrow,p,normal,.003 if p['enabled'] else 0,pressed,'pressed')
            selected=(await self.query('menu.get_state'))['selection_index']
            if arrow['enabled']:assert selected!=selection,'Enabled arrow did not navigate'
            else:assert selected==selection,'Disabled arrow navigated'
            await self.tool('hold_ui_at',aim,'still-held-arrow')
            assert (await self.query('menu.get_state'))['selection_index']==selected,'Holding repeated navigation'
            await self.tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand},'release-arrow')
            self.cases.append({'case':'one-navigation-or-disabled-inert','passed':True,'before':selection,'after':selected})
        errors=await self.query('game.errors');assert errors['total']==0,errors

    def check_geometry(self,base,actual,normal,expected,state,phase):
        delta=np.array(actual['draw']['origin'])-base['rest']['origin']
        assert np.linalg.norm(delta-normal*expected)<.0015,(phase,delta.tolist(),expected)
        for key in ('right','up','normal'):
            assert np.allclose(actual['draw'][key],base['rest'][key],atol=1e-5),(phase,key)
        for old,new in zip(base['rest_bars'],actual['bars']):
            assert np.linalg.norm(np.array(new['origin'])-old['origin']-delta)<.0015,(phase,'bar displacement')
        assert actual['rest']==base['rest'],'Interaction moved fixed source geometry'
        if actual['enabled']:
            ray=next(r for r in state['world_arrow_rays'] if r['hand']==int(self.args.hand=='right'))
            endpoint=np.array(ray['end']);relative=endpoint-np.array(actual['draw']['origin'])
            assert abs(np.dot(relative,normal))<.002,(phase,'ray endpoint off visible face',ray,actual)
            for axis in ('right','up'):
                v=np.array(actual['draw'][axis]);extent=np.linalg.norm(v)
                assert abs(np.dot(relative,v/extent))<=extent+.002,(phase,'ray outside arrow',axis)
        self.cases.append({'case':phase+'-actual-world-arrow','passed':True,'expected_lift':expected,'base':base,'actual':actual,'rays':state['world_arrow_rays']})

async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report={'passed':False,'pid':args.pid,'hand':args.hand,'side':args.side}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=WorldArrows(args,session)
            try:await test.run();report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=test.cases;(args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--side',type=int,choices=(-1,1),required=True);p.add_argument('--press',action='store_true')
    asyncio.run(main(p.parse_args()))
