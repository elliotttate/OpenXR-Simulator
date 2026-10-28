"""Read native material timelines while real screenshot-derived rays hover UI.

Requires the optional hud.get_node material_vectors API. Starts on FlatOut
event0, page0, no modal, motion enabled. Never clicks an event/action or writes
material state. Uniform timelines are not pixel, GPU-cost or headset proof.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_feedback_layout_e2e import challenge_nodes,default_layout_manifest,settle_challenge_baseline
from flatout_challenge_timeline_e2e import Timeline,NativeTimelineSampler,fit_track,require


def material_vector(node,name):
    material=node.get('material',{})
    require(material.get('available') is True,'Native UI material is unavailable',node.get('name'))
    require(name not in material.get('missing',[]) and name in material.get('vectors',{}),
            'Requested native material vector is unavailable',name)
    vector=material['vectors'][name]
    require(len(vector)==4 and all(np.isfinite(v) for v in vector),'Invalid native material vector',vector)
    return vector


class MaterialSampler(NativeTimelineSampler):
    def __init__(self,pid,duration,path,vector,native=None):
        super().__init__(pid,duration,native)
        self.path=path;self.vector=vector

    def collect(self):
        deadline=time.monotonic()+self.duration
        try:
            while not self.stop.is_set() and time.monotonic()<deadline:
                started=time.monotonic()
                node=self.native.call('hud.get_node',{'root':'menu','name':self.path,'material_vectors':[self.vector]})
                ended=time.monotonic()
                sample={'time':(started+ended)/2,'query_seconds':ended-started,'active':True,
                        'vector':material_vector(node,self.vector),'node':node['name']}
                with self.lock:self.samples.append(sample)
                self.ready.set()
                # Keep early curve acquisition dedicated to the one tiny node.
                if ended>deadline-self.duration+.5 and len(self.samples)%12==0:
                    native=self.native.call('vr.targets')
                    with self.lock:self.targets.append({'time':time.monotonic(),'clicks':native['clicks'],
                        'hovered':native['hovered'],'rects':{t['name']:t['rect'] for t in native['targets']}})
                self.stop.wait(.002)
        except Exception as error:self.error=error
        finally:self.ready.set()


def qualify_photo(trace):
    specs=[('photo-zoom1000ms',0,1.005,.05,1.,True),
           ('photo-brightness700ms',1,.91,.17,.7,False),
           ('photo-saturation700ms',2,.86,.24,.7,False),
           ('photo-sheen1050ms',3,0.,1.,1.05,True)]
    result=[]
    for label,component,origin,span,duration,authored in specs:
        track=fit_track(trace['samples'],lambda s,i=component,a=origin,b=span:(s['vector'][i]-a)/b,
                        duration,label,authored)
        track['uniform']='g_PhotoDynamics';track['component']=component
        result.append(track)
    normalized=[[float((s['vector'][1]-.91)/.17),float((s['vector'][2]-.86)/.24)] for s in trace['samples']]
    require(all(abs(a-b)<.0001 for a,b in normalized),'Brightness and saturation filter tracks desynchronized')
    return result


def qualify_footer(trace):
    samples=trace['samples']
    mix=fit_track(samples,lambda s:s['vector'][0],.35,'footer-style-mix350ms',True)
    sheen=fit_track(samples,lambda s:s['vector'][1],.7,'footer-sheen700ms',True)
    sizes=np.array([s['vector'][2:] for s in samples])
    require(np.max(np.abs(sizes-sizes[0]))<.001,'Footer style shader paint dimensions changed during hover')
    return [mix,sheen]


class MaterialTimeline(Timeline):
    async def observe_material(self,label,target,path,vector,duration):
        before=await self.query('vr.targets')
        baseline={t['name']:t['rect'] for t in before['targets']}
        _,aim=await self.point_args(target);aim['dwell_frames']=1
        aim=await self.calibrated(aim)
        self.collector=MaterialSampler(self.args.pid,duration,path,vector)
        started=time.monotonic();self.collector.thread.start()
        require(await asyncio.to_thread(self.collector.ready.wait,4),'Native material sampler did not become ready')
        if self.collector.error:raise RuntimeError(repr(self.collector.error))
        try:
            receipt=await self.tool('hover_ui_at',aim,label+'-ray-hover')
            require(receipt['status']=='input_delivered','Material hover was not delivered',receipt)
            await asyncio.sleep(max(0,started+duration-time.monotonic()))
        finally:
            await self.collector.finish();trace=self.collector.evidence()
            trace.update(path=path,material_vector=vector);self.timelines[label]=trace;self.collector=None
        after=await self.query('vr.targets')
        require(after['hovered']==target and after['clicks']==before['clicks'],'Material hover activated or missed UI',after)
        require({t['name']:t['rect'] for t in after['targets']}==baseline,'Material hover changed immutable final ray targets')
        require(trace['targets'],'No native target observation during material hover')
        for observation in trace['targets']:
            require(observation['rects']==baseline and observation['clicks']==before['clicks'],
                    'Material hover changed ray hit bounds or activated an action',observation)
        return trace

    async def run(self):
        menu=await self.query('menu.get_state')
        require(menu['current_menu']=='INSANITY' and menu['selection_index']==0,'Start at FlatOut event0, page0')
        require(not (await self.query('ui.get_message_box'))['open'],'Close modal before material timeline test')
        require((await self.query('game.errors'))['total']==0,'Start without native game errors')
        await settle_challenge_baseline(self)
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':5}))['nodes']
        paths={}
        for suffix,vector in [('ChallengeTrackArt','g_PhotoDynamics'),('ChallengeActionBody','g_StyleState')]:
            candidates=challenge_nodes(nodes,suffix)
            require(len(candidates)==3,'Need three native material nodes',suffix)
            # HUD paths include MENU_DUMMY; hud.get_node starts at that root.
            # Existing repeated-name semantics select the first constructed
            # card/footer (slot0), which is also the leftmost queried node.
            path=candidates[0]['path'].split('/',1)[1]
            node=await self.query('hud.get_node',{'root':'menu','name':path,'material_vectors':[vector]})
            require(node['name']==suffix,'Material query resolved the wrong native node',node.get('name'))
            value=material_vector(node,vector);paths[suffix]=path
            if suffix=='ChallengeTrackArt':
                require(np.allclose(value,[1.005,.91,.86,0],rtol=0,atol=.0001),
                        'Photo must be at idle material state with motion enabled',value)
            else:require(np.allclose(value[:2],[0,0],atol=.0001),'Footer must be at idle style state',value)
        photo=await self.observe_material('photo','challenge.event.0',paths['ChallengeTrackArt'],'g_PhotoDynamics',1.7)
        self.cases.append({'case':'native-photo-material-timelines','passed':True,'tracks':qualify_photo(photo)})
        await settle_challenge_baseline(self)
        footer=await self.observe_material('footer','helper.Cancel.0',paths['ChallengeActionBody'],'g_StyleState',1.3)
        self.cases.append({'case':'native-footer-material-mix-and700ms-sheen','passed':True,'tracks':qualify_footer(footer)})
        await settle_challenge_baseline(self)
        require((await self.query('game.errors'))['total']==0,'Native material test produced a game error')


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'pid':args.pid,'scope':'Read-only native material-vector timelines during actual ray hover',
            'not_qualified':['GPU consumes these uniform values','700ms pixel sweep visibility','GPU frame cost','headset comfort'],
            'input':'Hover only, no event/action activation or native state writes'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=MaterialTimeline(args,session)
            try:await test.run();result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                cleanup=[]
                if test.collector:
                    try:
                        await test.collector.finish();test.timelines['interrupted']=test.collector.evidence()
                    except Exception as error:cleanup.append(repr(error))
                if test.last_capture:
                    for hand in ('left','right'):
                        try:await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand},'cleanup-'+hand)
                        except Exception as error:cleanup.append(repr(error))
                result.update(cases=test.cases,timelines=test.timelines,cleanup_errors=cleanup)
                if cleanup:result['passed']=False
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
            require(result['passed'],'Material timeline cleanup failed',result.get('cleanup_errors'))
    print('Native material timeline and inert ray-hover regression PASS',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True);parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--hand',choices=('left','right'),default='right')
    parser.add_argument('--layout-manifest',type=Path,default=default_layout_manifest())
    asyncio.run(main(parser.parse_args()))
