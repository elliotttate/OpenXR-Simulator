"""Measure native authored entry/hover timing without screenshot-serialization gaps.

Start at Main or the FlatOut event browser with an isolated simulator profile.
All navigation uses screenshot-derived XR input. The sampler owns a separate
read-only NativeGame pipe client and never writes game memory or material state.
UI material uniforms are not exposed by the current bridge: shader-only photo
and sheen durations are explicitly unqualified, not inferred from screenshots.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,RAY_OFFSET
from flatout_feedback_layout_e2e import (challenge_nodes,default_layout_manifest,
    load_layout_manifest,settle_challenge_baseline)
from flatout_livery_e2e import ui_surface,world_pixel
from flatout_ray_offset import native_pointer_offset
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame


def require(ok,message,detail=None):
    if not ok:raise AssertionError((message,detail) if detail is not None else message)


def extract_timeline(hud,stamp):
    nodes=hud['nodes']
    heading=challenge_nodes(nodes,'ChallengeHeading')
    cards=challenge_nodes(nodes,'ChallengeCard')
    footer=challenge_nodes(nodes,'ChallengeActionFrame')
    labels=challenge_nodes(nodes,'RayActionLabel')
    icons=challenge_nodes(nodes,'ChallengeActionIcon')
    if len(heading)!=1 or any(len(group)!=3 for group in (cards,footer,labels,icons)):
        return {'time':stamp,'active':False}
    return {'time':stamp,'active':True,'header_alpha':heading[0]['color']['a'],
            'header_rect':heading[0]['rect'],'card_alphas':[n['color']['a'] for n in cards],
            'card_rects':[n['rect'] for n in cards],'footer_alphas':[n['color']['a'] for n in footer],
            'footer_rects':[n['rect'] for n in footer],
            'footer_colors':[n['color'] for n in icons],
            'footer_label_colors':[n['color'] for n in labels]}


class NativeTimelineSampler:
    def __init__(self,pid,duration,native=None):
        self.operations=[]
        self.native=native if native is not None else NativeGame(pid,self.operations,timeout=3)
        self.duration=duration;self.samples=[];self.targets=[];self.error=None
        self.ready=threading.Event();self.stop=threading.Event();self.lock=threading.Lock()
        self.thread=threading.Thread(target=self.collect,name='native-authored-timeline',daemon=True)

    def collect(self):
        deadline=time.monotonic()+self.duration
        try:
            while not self.stop.is_set() and time.monotonic()<deadline:
                started=time.monotonic()
                hud=self.native.call('hud.rects',{'root':'menu','max_depth':5})
                ended=time.monotonic()
                sample=extract_timeline(hud,(started+ended)/2)
                sample['query_seconds']=ended-started
                with self.lock:
                    self.samples.append(sample);count=len(self.samples)
                self.ready.set()
                if count%8==0:
                    native=self.native.call('vr.targets')
                    target={'time':time.monotonic(),'clicks':native['clicks'],
                            'hovered':native['hovered'],'rects':{t['name']:t['rect'] for t in native['targets']}}
                    with self.lock:self.targets.append(target)
                self.stop.wait(.002)
        except Exception as error:self.error=error
        finally:self.ready.set()

    async def finish(self):
        self.stop.set();await asyncio.to_thread(self.thread.join,8)
        require(not self.thread.is_alive(),'Native timeline thread did not stop within8seconds')
        if self.error:raise RuntimeError('Native timeline sampler failed: '+repr(self.error)) from self.error

    def evidence(self):
        with self.lock:return {'samples':list(self.samples),'targets':list(self.targets),'operations':list(self.operations)}


def cubic(t,a,b):return 3*(1-t)**2*t*a+3*(1-t)*t*t*b+t**3


def normalized_age(value,authored=True):
    """Independent high-precision inversion of CSS y(t), then evaluate x(t)."""
    require(0<=value<=1,'Opacity outside normalized range',value)
    low,high=0.,1.
    for _ in range(52):
        mid=(low+high)/2
        if cubic(mid,.75 if authored else .1,1)<value:low=mid
        else:high=mid
    return cubic((low+high)/2,.2 if authored else .25,.2 if authored else .25)


def fit_track(samples,read,expected,label,authored=True):
    points=[(s['time'],float(read(s))) for s in samples if s['active']]
    require(points,label+': no native timeline observations')
    tolerance=1.1/255
    require(all(-tolerance<=v<=1+tolerance for _,v in points),label+': values out of bounds')
    require(all(b[1]+tolerance>=a[1] for a,b in zip(points,points[1:])),label+': reversed or restarted')
    intermediate=[(t,v) for t,v in points if .05<v<.95]
    levels={round(v*255) for _,v in intermediate}
    require(len(intermediate)>=5 and len(levels)>=5,label+': insufficient intermediate native frames',len(intermediate))
    require(points[-1][1]>=.995,label+': never settled',points[-1])
    # t = onset + duration * inverse_ease(value). Quantized near-end samples
    # are excluded; fitting cannot turn a static completion frame into timing.
    origin=points[0][0]
    ages=np.array([normalized_age(v,authored) for _,v in intermediate])
    times=np.array([t-origin for t,_ in intermediate])
    design=np.column_stack([np.ones(len(ages)),ages])
    onset,duration=np.linalg.lstsq(design,times,rcond=None)[0]
    residual=float(np.sqrt(np.mean((times-design@np.array([onset,duration]))**2)))
    uncertainties=[s.get('query_seconds',0) for s in samples
                   if s['active'] and .05<float(read(s))<.95]
    longest_query=max(uncertainties,default=0)
    # With only a few early/late points, one long queued RPC gives its midpoint
    # excessive leverage. The state was read somewhere inside that interval;
    # treating it as an exact timestamp can manufacture a shorter duration.
    # Keep the raw estimate and fail inconclusively rather than widening the
    # authored-time tolerance or changing production motion for acquisition.
    require(not (len(intermediate)<8 and longest_query>.1*expected),
            label+': inconclusive native duration; sparse midrange samples contain a long RPC interval',
            {'estimated_seconds':float(duration),'expected_seconds':expected,
             'intermediate_samples':len(intermediate),'longest_query_seconds':longest_query,
             'residual_seconds':residual,'limit':'HUD observation timestamp lies within the request/response interval, not exactly at its midpoint'})
    allowed=max(.05,.18*expected)
    require(abs(float(duration)-expected)<=allowed,label+': native duration differs from authored timing',
            {'estimated':float(duration),'expected':expected,'tolerance':allowed})
    require(residual<=.04,label+': native samples do not follow one authored timeline',residual)
    return {'track':label,'expected_seconds':expected,'estimated_seconds':float(duration),
            'estimated_onset':origin+float(onset),'residual_seconds':residual,
            'intermediate_samples':len(intermediate),'distinct_intermediate_levels':len(levels),
            'tolerance_seconds':allowed,'limit':'Native own-color/geometry timeline, not GPU presentation or material-uniform proof'}


def qualify_entry(trace,layout,main_entry=False):
    samples=trace['samples'];tracks=[]
    trace['prepaint_exclusions']=[]
    active_indices=[i for i,s in enumerate(samples) if s['active']]
    if main_entry and len(active_indices)>=2:
        first_index,next_index=active_indices[:2]
        first,following=samples[first_index],samples[next_index]
        # Native HUD reads can run between Manage() opening/resetting the
        # header/cards and target collection repainting the reused footer.
        # Only this exact first authoring-state boundary is excluded: a prior
        # inactive Main sample, zero new header/cards, retained fully opaque
        # footer, then the next active row with progressing header/card0 and
        # footer reset to zero. All raw rows remain preserved in the report.
        prepaint=(first_index>0 and not samples[first_index-1]['active'] and
            first['header_alpha']==0 and all(a==0 for a in first['card_alphas']) and
            all(a>=.995 for a in first['footer_alphas']) and
            following['header_alpha']>0 and following['card_alphas'][0]>0 and
            all(a==0 for a in following['footer_alphas']))
        if prepaint:
            trace['prepaint_exclusions']=[{'sample_index':first_index,'raw_sample':first,
                'reason':'First Main-to-Challenge authoring row precedes footer repaint: header/cards reset to0 while reused footer retains1; next row resets footer0',
                'limit':'This HUD authoring-state row does not establish a rendered flash; framebuffer evidence is required'}]
            samples=[s for i,s in enumerate(samples) if i!=first_index]
    tracks.append(fit_track(samples,lambda s:s['header_alpha'],.9,'header900ms'))
    for slot in range(3):tracks.append(fit_track(samples,lambda s,i=slot:s['card_alphas'][i],.85,'card'+str(slot)+'850ms'))
    for slot in range(3):tracks.append(fit_track(samples,lambda s,i=slot:s['footer_alphas'][i],1.,'footer'+str(slot)+'1000ms'))
    base=tracks[0]['estimated_onset']
    for track,delay in zip(tracks,[0,0,.085,.170,.350,.350,.350]):
        observed=track['estimated_onset']-base
        require(abs(observed-delay)<=.06,'Authored stagger/delay mismatch',{'track':track['track'],'observed':observed,'expected':delay})
        track['relative_delay_seconds']=observed;track['expected_delay_seconds']=delay
    for sample in samples:
        if not sample['active']:continue
        require(abs(sample['header_rect'][1]-(layout['rects']['heading']['y']-17*(1-sample['header_alpha'])))<=1.6,
                'Header offset did not follow its opacity timeline')
        for slot in range(3):
            expected=layout['cards'][slot]['card']['y']+25*(1-sample['card_alphas'][slot])
            require(abs(sample['card_rects'][slot][1]-expected)<=1.6,'Card visual entry offset mismatch',slot)
            expected=layout['visual_bounds']['actions'][slot]['y']+15*(1-sample['footer_alphas'][slot])
            require(expected-5.6<=sample['footer_rects'][slot][1]<=expected+1.6,
                    'Footer entry offset exceeds authored reveal plus optional4px hover',slot)
    return tracks


def qualify_footer_hover(trace,baseline):
    active=[s for s in trace['samples'] if s['active']]
    require(active,'No footer hover observations')
    idle=baseline['footer_colors'][0]['b'];settled=float(np.median([s['footer_colors'][0]['b'] for s in active[-5:]]))
    require(abs(idle-settled)>.5,'Footer icon blue-channel feedback is not observable',[idle,settled])
    track=fit_track(active,lambda s:(s['footer_colors'][0]['b']-idle)/(settled-idle),.35,'footer-icon-hover350ms')
    for sample in trace['targets']:
        require(sample['rects']==baseline['target_rects'],'Hover changed immutable ray hit rectangles')
        require(sample['clicks']==baseline['clicks'],'Hover activated an action')
    return track


class Timeline(Frontend):
    def __init__(self,args,session):
        super().__init__(args,session)
        self.layout=load_layout_manifest(args.layout_manifest);self.timelines={};self.collector=None

    async def tool(self,name,args,label):
        value=await super().tool(name,args,label)
        capture=value if 'geometry' in value else value.get('capture')
        if capture:
            self.last_capture=capture
            session=capture['geometry'].get('session_id','');match=re.fullmatch(r'(\d+)-\d+-\d+',session)
            require(match is not None and int(match.group(1))==self.args.pid,'Capture process mismatch',session)
        return value

    async def calibrated(self,aim):
        aim={**aim,'timeout':30,'command_id':uuid.uuid4().hex}
        offset=native_pointer_offset(await self.query('vr.get_state'),self.args.hand)
        require(offset is not None,'Native controller pointer pose unavailable')
        aim['ray_offset']=offset;self.calibrated_commands[aim['command_id']]=dict(aim)
        return aim

    async def observe(self,label,name,aim,duration):
        self.collector=NativeTimelineSampler(self.args.pid,duration)
        started=time.monotonic();self.collector.thread.start()
        require(await asyncio.to_thread(self.collector.ready.wait,4),'Timeline sampler did not become ready')
        if self.collector.error:raise RuntimeError(repr(self.collector.error))
        try:
            receipt=await self.tool(name,aim,label+'-input')
            require(receipt['status']=='input_delivered','XR input was not delivered',receipt)
            await asyncio.sleep(max(0,started+duration-time.monotonic()))
        finally:
            await self.collector.finish();self.timelines[label]=self.collector.evidence();self.collector=None
        return receipt,self.timelines[label]

    async def main_aim(self):
        menu=await self.query('menu.get_state');native=await self.query('vr.targets')
        require(menu['current_menu']=='MAIN MENU' and menu['is_3d'],'Native entry must use the real spatial main-menu callback')
        item=next(i for i in menu['items'] if i['index']==1)
        require('FLATOUT' in item['title'].upper(),'Main index1 is not FlatOut Mode',item['title'])
        capture=await self.capture('main-flatout-source');point=item['rendered_position']
        frame={**native['vr_frame'],'panel':{**menu['spatial_layout'],'origin':point},'panel_width':1.,'panel_height':1.}
        state={'vr':{'frame':frame}};x,y=world_pixel(state,capture,point,'left')
        require(0<=x<capture['image_size'][0] and 0<=y<capture['image_size'][1],'Main FlatOut button is outside the screenshot')
        return {'capture_id':capture['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
                'origin_mode':'hand','surface':ui_surface(state),'ray_offset':RAY_OFFSET,'dwell_frames':1}

    async def run(self):
        require((await self.query('game.errors'))['total']==0,'Start with no native game errors')
        require(not (await self.query('ui.get_message_box'))['open'],'Dismiss existing modal before this test')
        menu=await self.query('menu.get_state')
        if menu['current_menu']=='INSANITY':
            await settle_challenge_baseline(self);await self.click('helper.Cancel.0')
        aim=await self.main_aim()
        require((await self.tool('hover_ui_at',aim,'main-flatout-hover'))['status']=='input_delivered','Main hover failed')
        aim=await self.calibrated({**(await self.main_aim()),'hold_frames':2})
        clicks=(await self.query('vr.targets'))['clicks']
        receipt,entry=await self.observe('entry','click_ui_at',aim,2.6)
        require((await self.query('menu.get_state'))['current_menu']=='INSANITY','Entry did not open the event browser')
        after_entry=(await self.query('vr.targets'))['clicks']
        # The spatial-menu path calls m_p3DMenu->Validate() directly and does
        # not increment s_iDebugClicks. Listed quad callbacks do increment it.
        # Require the spatial path's unchanged counter and no unintended quad
        # activation; do not mistake incomplete counter coverage for a repeat.
        require(after_entry==clicks,'Spatial entry unexpectedly activated a counted quad action',[clicks,after_entry])
        require(all(s['clicks']==clicks for s in entry['targets']),'An entry-frame ray activated a counted quad action')
        retry=await self.tool('click_ui_at',aim,'entry-same-id-retry')
        require(all(retry['delivery'][key]==receipt['delivery'][key] for key in ('sync_id','applied_frame')) and
                (await self.query('vr.targets'))['clicks']==clicks and
                (await self.query('menu.get_state'))['current_menu']=='INSANITY','Entry retry redelivered input')
        self.cases.append({'case':'spatial-entry-delivery-and-idempotent-retry','passed':True,
            'debug_clicks_before':clicks,'debug_clicks_after':after_entry,
            'limit':'Spatial Validate dispatch is not counted by vr.targets.clicks; this proves delivered one-click request, stable destination/counter, and same-delivery retry, not an independent callback-count measurement'})
        tracks=qualify_entry(entry,self.layout,main_entry=True)
        self.cases.append({'case':'native-authored-header-card-footer-entry-timing','passed':True,'tracks':tracks,
                           'prepaint_exclusions':entry['prepaint_exclusions']})
        await settle_challenge_baseline(self)
        native=await self.query('vr.targets')
        baseline=extract_timeline(await self.query('hud.rects',{'root':'menu','max_depth':5}),time.monotonic())
        baseline['clicks']=native['clicks'];baseline['target_rects']={t['name']:t['rect'] for t in native['targets']}
        _,aim=await self.point_args('helper.Cancel.0');aim['dwell_frames']=1
        aim=await self.calibrated(aim)
        _,hover=await self.observe('footer-hover','hover_ui_at',aim,1.4)
        track=qualify_footer_hover(hover,baseline)
        current=await self.query('vr.targets')
        require(current['hovered']=='helper.Cancel.0' and current['clicks']==baseline['clicks'],'Footer hover activated or missed Back')
        require({t['name']:t['rect'] for t in current['targets']}==baseline['target_rects'],'Footer hover changed final target bounds')
        self.cases.append({'case':'native-footer-hover-icon-timing-and-inert-ray','passed':True,'track':track,
                           'probe':'ChallengeActionIcon own RGB; secondary footer label intentionally stays white'})
        await settle_challenge_baseline(self)
        require((await self.query('game.errors'))['total']==0,'Timeline test produced a native error')


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'pid':args.pid,'scope':'Native own-color/geometry entry and footer hover timing',
            'material_uniform_queries_supported':False,
            'not_qualified':['shader-only700ms footer sheen duration','shader-only1050ms photo sheen duration',
                'shader-only1000ms photo zoom/700ms grade duration','GPU presentation/frame time','headset comfort'],
            'capability_evidence':'hud.rects returns rect/color/text; hud.get_node returns source coords/color/children, with no UI material vectors'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Timeline(args,session)
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
            require(result['passed'],'Cleanup failed',result.get('cleanup_errors'))
    print('Native authored entry and observable hover timing PASS',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True);parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--hand',choices=('left','right'),default='right')
    parser.add_argument('--layout-manifest',type=Path,default=default_layout_manifest())
    asyncio.run(main(parser.parse_args()))
