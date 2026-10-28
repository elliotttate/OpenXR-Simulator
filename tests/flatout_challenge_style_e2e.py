"""Native FlatOut photo/material/shine proof through screenshot-derived XR rays.

Start on the first FlatOut page with event0 selected and no modal. This test
does not launch the game, buy anything, start a race or enter Wreck Run.
It waits for entrance, measures both eyes, hovers without activating, then
clicks Next once (including its idempotent retry) and returns to page0.

Missing or unsampled animation is a failure, not a successful skipped check.
Pixel evidence proves visible response, not shader implementation or GPU cost.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel
from flatout_feedback_layout_e2e import default_layout_manifest, load_layout_manifest, rect_edges
from flatout_livery_e2e import quaternion_matrix
from flatout_ui_depth_contract import assert_hover_lift, ui_planes
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from flatout_ray_offset import native_pointer_offset

LUMA=np.array([.2126,.7152,.0722])
SOURCE_ZOOM=1.055/1.005


def grid(box,nx=64,ny=32):
    """Sample cell centers, not JPEG/shape borders."""
    l,t,r,b=rect_edges(box) if isinstance(box,dict) else box
    x,y=np.meshgrid(np.linspace(l,r,nx,endpoint=False)+(r-l)/(2*nx),
                    np.linspace(t,b,ny,endpoint=False)+(b-t)/(2*ny))
    return np.column_stack([x.ravel(),y.ravel()])


def outside(points,boxes,padding=4):
    keep=np.ones(len(points),dtype=bool)
    for box in boxes:
        l,t,r,b=rect_edges(box)
        keep &= ~((points[:,0]>=l-padding)&(points[:,0]<=r+padding)&
                  (points[:,1]>=t-padding)&(points[:,1]<=b+padding))
    return keep


def project(capture,world,eye):
    e=next(e for e in capture['geometry']['eyes'] if e['eye']==eye)
    local=(world-np.array(e['pose']['position']))@quaternion_matrix(e['pose']['orientation'])
    assert np.all(local[:,2]<0),'Sampling geometry is behind an eye'
    left,right,up,down=map(math.tan,e['fov'])
    u=(local[:,0]/-local[:,2]-left)/(right-left)
    v=(local[:,1]/-local[:,2]-up)/(down-up)
    ex,ey,ew,eh=e['viewport'];cx,cy,cw,ch=capture['crop'];w,h=capture['image_size']
    return np.column_stack([((ex+u*ew-cx)/cw)*w-.5,((ey+v*eh-cy)/ch)*h-.5])


def bilinear(image,xy):
    h,w=image.shape[:2];x=xy[:,0];y=xy[:,1]
    assert np.all((x>=0)&(x<w-1)&(y>=0)&(y<h-1)), 'ROI clips an image edge'
    ix=x.astype(int);iy=y.astype(int);fx=(x-ix)[:,None];fy=(y-iy)[:,None]
    return ((1-fx)*(1-fy)*image[iy,ix]+fx*(1-fy)*image[iy,ix+1]+
            (1-fx)*fy*image[iy+1,ix]+fx*fy*image[iy+1,ix+1])


class Frame:
    def __init__(self,capture,image_path,stamp,request_start=None):
        self.capture=capture;self.path=Path(image_path);self.time=stamp
        self.request_start=stamp if request_start is None else request_start
        # Retain8-bit pixels; normalize only sampled ROI values. Expanding an
        # entire stereo frame to float64 costs ~150MB and stalls acquisition.
        self.image=np.asarray(Image.open(self.path).convert('RGB'),dtype=np.uint8)
        assert list(self.image.shape[1::-1])==capture['image_size']
        self.base=ui_planes(capture)[0]
        self.basis=quaternion_matrix(self.base['pose']['orientation'])

    def samples(self,points,eye,lift=0):
        local=np.column_stack([(points[:,0]/1920-.5)*self.base['size'][0],
                               (.5-points[:,1]/1080)*self.base['size'][1],np.full(len(points),lift)])
        world=local@self.basis.T+np.array(self.base['pose']['position'])
        xy=project(self.capture,world,eye)
        keep=np.ones(len(points),dtype=bool)
        # Exclude actual rendered beam ribbons, rather than treating their
        # yellow/cyan pixels as photo color or glow. Controller images are not rays.
        for ray in self.capture['geometry'].get('decorations',[]):
            if ray.get('role')!='one_texel_ribbon':continue
            axis=quaternion_matrix(ray['pose']['orientation'])[:,1]*ray['size'][1]/2
            ends=np.array([np.array(ray['pose']['position'])-axis,np.array(ray['pose']['position'])+axis])
            if np.any((ends-np.array(next(e for e in self.capture['geometry']['eyes'] if e['eye']==eye)['pose']['position']))[:,2]>=0):
                continue
            segment=project(self.capture,ends,eye);a,b=segment;delta=b-a
            u=np.clip((xy-a)@delta/max(float(delta@delta),1e-10),0,1)
            keep &= np.linalg.norm(xy-a-u[:,None]*delta,axis=1)>3
        return bilinear(self.image,xy)/255,keep

    def lift_at(self,rect):
        l,t,r,b=rect
        center=np.array(self.base['pose']['position'])+self.basis@np.array([
            ((l+r)/3840-.5)*self.base['size'][0],(.5-(t+b)/2160)*self.base['size'][1],0])
        candidates=[]
        for tile in self.capture['geometry']['surfaces']:
            if tile['visibility']!=0 or tile['id']==self.base['id']:continue
            delta=self.basis.T@(np.array(tile['pose']['position'])-center)
            if abs(delta[0])<=tile['size'][0]/2 and abs(delta[1])<=tile['size'][1]/2:
                candidates.append(float(delta[2]))
        return max(candidates,default=0)


def noise_limit(values):
    """An observed repeated-rest noise bound with one8-bit-code resolution floor.

    Spatial block means avoid pretending thousands of adjacent JPEG pixels are
    independent statistical samples. This is a conservative observed bound,
    not a statistical confidence interval or a renderer-tuned target value.
    """
    blocks=[float(np.mean(v)) for v in np.array_split(np.asarray(values),min(32,len(values))) if len(v)]
    return max(1/255,float(np.percentile(np.abs(blocks),95)))


def directional_metric(rest,repeat,changed,direction):
    limit=noise_limit(repeat-rest)
    delta=float(np.mean(changed-repeat))
    return {'mean_delta':delta,'repeated_rest_noise_limit':limit,
            'passed':bool(direction*delta>limit)}


def gold_ratio(rgb):
    """Black source-over shadow preserves this tone-normalized chromaticity."""
    return (rgb[:,0]-rgb[:,2])/np.maximum(rgb.sum(axis=1),1/255)


def source_effect_prediction(rest,source_rest,source_hover):
    """Predict CSS-reference RGBA change over the measured stationary background.

    Outside-card FX texels must be translucent. This does not fit a coefficient
    to the observed hover: only the independent reference states and rest image
    define the prediction. Tone mapping can limit magnitude agreement.
    """
    assert np.all(source_rest[:,3]<.95),'Reference ROI is not exterior translucent FX'
    background=(rest-source_rest[:,:3]*source_rest[:,3:4])/(1-source_rest[:,3:4])
    return source_hover[:,:3]*source_hover[:,3:4]+background*(1-source_hover[:,3:4])


def source_effect_metrics(rest,repeat,changed,source_rest,source_hover):
    predicted=source_effect_prediction(repeat,source_rest,source_hover)
    expected_delta=predicted-repeat;actual_delta=changed-repeat
    expected=expected_delta.mean(axis=0);actual=actual_delta.mean(axis=0)
    source_error=float(np.linalg.norm(actual-expected))
    unchanged_error=float(np.linalg.norm(actual))
    rgb_noise=max(noise_limit((repeat-rest)[:,i]) for i in range(3))
    expected_gold=float(np.mean(gold_ratio(predicted)-gold_ratio(repeat)))
    actual_gold=float(np.mean(gold_ratio(changed)-gold_ratio(repeat)))
    # A normalized multi-channel ratio has no fixed1/255 output-code step.
    # Use repeated-rest block noise and floating point resolution, not the raw
    # RGB floor that wrongly rejected sub-code averaged chromaticity changes.
    block_noise=[float(np.mean(v)) for v in np.array_split(gold_ratio(repeat)-gold_ratio(rest),32) if len(v)]
    chroma_noise=max(np.finfo(float).eps*64,float(np.percentile(np.abs(block_noise),95)))
    chroma_pass=bool(abs(expected_gold)>chroma_noise and np.sign(expected_gold)*actual_gold>chroma_noise)
    return {'passed':bool(unchanged_error>rgb_noise and source_error<unchanged_error and chroma_pass),
            'predicted_delta_rgb':expected.tolist(),'actual_delta_rgb':actual.tolist(),
            'distance_from_source_prediction':source_error,'distance_from_unchanged':unchanged_error,
            'repeated_rest_rgb_noise_limit':rgb_noise,'predicted_normalized_gold_delta':expected_gold,
            'actual_normalized_gold_delta':actual_gold,'repeated_rest_chroma_noise':chroma_noise,
            'chromatic_response_passed':chroma_pass,
            'limit':'Source-over reference predicts direction/combined response; native HDR/color-space/JPEG can change magnitude. This cannot certify exact blur sigma or reconstruct alpha.'}


def reference_chroma_signal(rest,repeat,source_rest,source_hover):
    """ROI selection uses reference/rest only, never observed hover pixels."""
    predicted=source_effect_prediction(repeat,source_rest,source_hover)
    expected=float(np.mean(gold_ratio(predicted)-gold_ratio(repeat)))
    blocks=[float(np.mean(v)) for v in np.array_split(gold_ratio(repeat)-gold_ratio(rest),32) if len(v)]
    noise=max(np.finfo(float).eps*64,float(np.percentile(np.abs(blocks),95)))
    return {'predicted_chroma_delta':expected,'repeated_rest_chroma_noise':noise,
            'selection_score':abs(expected)-noise}


def saturation(rgb):
    high=np.max(rgb,axis=1);low=np.min(rgb,axis=1)
    return (high-low)/np.maximum(high,1/255)


def normalized_error(a,b):
    aa=a@LUMA;bb=b@LUMA
    assert np.std(aa)>1/255 and np.std(bb)>1/255,'Photo ROI lacks measurable texture'
    return float(np.mean(((aa-aa.mean())/aa.std()-(bb-bb.mean())/bb.std())**2))


def shine_metrics(profiles,noise,positions=None,window_seconds=.7):
    """Require a resolved transient sweep inside the actual acquisition window.

    Profiles must subtract a repeated SETTLED-HOVER reference at the same
    physical lift and registered visual position. A new hover fill is not a
    sheen. Elapsed values are conservative capture-completion upper bounds.
    """
    assert profiles,'No shine frames acquired'
    limit=max(1/255,3*float(np.percentile(np.abs(noise),95)))
    positions=np.linspace(0,1,len(profiles[0][1])) if positions is None else np.asarray(positions)
    rows=[]
    for elapsed,profile in profiles:
        residual=profile-np.median(profile)
        positive=np.maximum(residual-limit,0)
        power=float(positive.sum())
        support=int(np.count_nonzero(positive))
        center=float(np.dot(positive,positions)/power) if power else None
        rows.append({'elapsed_s':elapsed,'peak':float(np.max(residual)),
                     'pulse_power':power,'pulse_center_x':center,'support_columns':support})
    seen=[r for r in rows if r['pulse_power']>0 and r['support_columns']>=3 and 0<=r['elapsed_s']<=window_seconds]
    step=max(.2,2/max(1,len(profiles[0][1])-1))
    moved=any(b['pulse_center_x']-a['pulse_center_x']>step for i,a in enumerate(seen) for b in seen[i+1:])
    faded=bool(seen) and rows[-1]['elapsed_s']>=window_seconds and rows[-1]['pulse_power']<=.15*max(r['pulse_power'] for r in seen)
    return {'passed':len(seen)>=2 and moved and faded,'noise_limit':limit,'samples':rows,
            'resolved_forward_motion':moved,'minimum_forward_travel':step,'faded':faded,
            'resolved_early_pulses':len(seen),'source_window_seconds':window_seconds,
            'qualification':'observed' if len(seen)>=2 and moved and faded else 'inconclusive-or-unobserved',
            'limit':'Requires two resolved forward positions captured within700ms plus a late fade against same-pose settled hover; missed timing is inconclusive and never a pass. Exact shader duration is not certified.'}


def footer_edge_profile(rgb,mask,nx=64,ny=16):
    """Edge gradients locate the footer independently of its interior sheen."""
    value=np.where(mask,rgb@LUMA,np.nan).reshape(2,ny,nx)
    counts=np.sum(np.isfinite(value),axis=2)
    rows=np.nansum(value,axis=2)/np.maximum(counts,1)
    assert np.all(counts>=nx//3),'Rays obscure footer pose-registration edges'
    gradient=np.diff(rows,axis=1).ravel()
    length=float(np.linalg.norm(gradient))
    assert length>1/255,'Footer boundary lacks measurable contrast for pose registration'
    return gradient/length


def register_footer_y(frame,reference,action,eye,target):
    """Locate the known0..-4px hover displacement using top/bottom rim edges."""
    x,y,w,h=(action[k] for k in ('x','y','width','height'))
    edges=np.concatenate([grid([x+24,y-6,x+w-48,y+6],64,16),
                          grid([x+24,y+h-6,x+w-48,y+h+6],64,16)])
    settled,settled_mask=reference.samples(edges+[0,-4],eye,reference.lift_at(target))
    expected=footer_edge_profile(settled,settled_mask)
    candidates=[]
    for offset in np.linspace(-4,0,17):
        rgb,mask=frame.samples(edges+[0,offset],eye,frame.lift_at(target))
        observed=footer_edge_profile(rgb,mask&settled_mask)
        candidates.append({'offset_y':float(offset),'edge_error':float(np.mean((observed-expected)**2))})
    best=min(candidates,key=lambda c:c['edge_error'])
    assert best['edge_error']<=.02,('Footer pose registration is inconclusive',best)
    return best,candidates


class ChallengeStyle(Frontend):
    def visual_phase(self,name,operation):
        """Keep independent visual failures while continuing safe acquisition."""
        try:
            operation()
        except Exception as error:
            self.cases.append({'case':name+'-analysis-error','passed':False,'error':repr(error)})

    async def frame(self,label,width=None):
        width=width or self.args.capture_width
        requested=time.perf_counter()
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':width,'timeout':15},label)
        self.last_capture=capture
        frame=Frame(capture,self.last_image_path,time.perf_counter(),requested)
        self.frames.append(frame)
        return frame

    async def capture(self,label):
        return (await self.frame(label)).capture

    async def park(self):
        source=await self.frame('park-source',2048)
        x,y=quad_pixel(source.capture,(10,10,10,10))
        for hand in ('left','right'):
            receipt=await self.tool('hover_ui_at',{'capture_id':source.capture['capture_id'],
                'x':float(x),'y':float(y),'hand':hand,'origin_mode':'hand','surface_id':source.base['id'],
                'dwell_frames':1},'park-'+hand)
            assert receipt['status']=='input_delivered'
        await asyncio.sleep(.5)
        assert not (await self.query('vr.targets'))['hovered'],'Parking left a control focused'

    async def prove(self):
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='INSANITY' and menu['selection_index']==0,'Start on FlatOut event0/page0'
        assert not (await self.query('ui.get_message_box'))['open']
        assert (await self.query('game.errors'))['total']==0
        await self.park();await asyncio.sleep(1.5)
        before=await self.query('vr.targets')
        rects={t['name']:t['rect'] for t in before['targets']}
        assert all(r[2]-r[0]>=79.5 and r[3]-r[1]>=79.5 for r in rects.values()),rects
        rest=await self.frame('settled-both-eyes-rest');await asyncio.sleep(.15)
        repeat=await self.frame('settled-both-eyes-rest-repeat')
        up=rest.basis[:,1]
        depth=abs(float(np.dot(np.array(rest.base['pose']['position'])-
                               np.array(rest.capture['geometry']['head']['position']),rest.basis[:,2])))
        assert np.allclose(up,[0,1,0],atol=.0001),'Carrier gained pitch or roll'
        assert abs(depth-3)<.01 and np.allclose(rest.base['size'],[4.5,2.53125],atol=.001),'Carrier distance/scale changed'
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':48}))['nodes']
        full=[n for n in nodes if n.get('visible') and n.get('kind')=='panel' and n['rect'][2]-n['rect'][0]>=1900 and n['rect'][3]-n['rect'][1]>=1060 and n.get('color',{}).get('a',0)>.1]
        assert not full,('A full-canvas drawable backdrop was introduced',full)
        self.cases.append({'case':'upright3m-carrier-and-no-full-canvas-plate','passed':True,'depth_m':depth,'up':up.tolist(),
                           'limit':'Native visible panel inventory, not a framebuffer-alpha reconstruction.'})
        await self.hover('challenge.event.0');await asyncio.sleep(1.2)
        hovered=await self.frame('settled-both-eyes-card-hover')
        after=await self.query('vr.targets')
        assert after['hovered']=='challenge.event.0' and after['clicks']==before['clicks'],'Hover activated an event'
        assert {t['name']:t['rect'] for t in after['targets']}==rects,'Hover changed source targets'
        lift=assert_hover_lift(hovered.capture)
        measured_lift=hovered.lift_at(rects['challenge.event.0'])
        assert abs(measured_lift-HOVER_LIFT_METERS)<.001,measured_lift
        self.cases.append({'case':'stable-targets-and-real20mm-both-eye-face','passed':True,**lift})
        self.visual_phase('photo',lambda:self.photo(rest,repeat,hovered,measured_lift))
        self.visual_phase('effects',lambda:self.effects(rest,repeat,hovered))
        await self.park();await asyncio.sleep(.7)
        native=await self.query('vr.targets')
        primary=next(t for t in native['targets'] if t['name'].startswith('helper.Validate.'))
        assert primary.get('enabled',True),'Primary footer must be enabled'
        # Compare the pulse with the SAME completed hover fill, four-pixel
        # visual offset and physical20mm face. Rest/unhover is a different
        # image state and cannot isolate a transient sheen.
        await self.hover(primary['name']);await asyncio.sleep(1.1)
        footer_hover=await self.frame('footer-settled-hover');await asyncio.sleep(.15)
        footer_repeat=await self.frame('footer-settled-hover-repeat')
        assert abs(footer_hover.lift_at(primary['rect'])-HOVER_LIFT_METERS)<.001
        assert abs(footer_repeat.lift_at(primary['rect'])-HOVER_LIFT_METERS)<.001
        await self.park();await asyncio.sleep(.7)
        # Prepare all queries/calibration before the short animation window.
        _,aim=await self.point_args(primary['name'])
        aim.update(dwell_frames=1,timeout=30,command_id=uuid.uuid4().hex)
        calibration=native_pointer_offset(await self.query('vr.get_state'),self.args.hand)
        assert calibration is not None,'Need a native pointer pose before temporal acquisition'
        aim['ray_offset']=calibration;self.calibrated_commands[aim['command_id']]=dict(aim)
        start=time.perf_counter()
        response=await self.tool('hover_ui_at',aim,'footer-entry-one-frame')
        assert response['status']=='input_delivered'
        self.last_capture=response['capture']
        entry=Frame(response['capture'],self.last_image_path,time.perf_counter(),start);self.frames.append(entry)
        samples=[entry]
        # Capture as quickly as the backend permits rather than pretending
        # overdue requested deadlines were sampled. Late frames remain useful
        # for fade/noise checks, but never qualify the700ms moving window.
        while time.perf_counter()<start+.7 and len(samples)<8:
            samples.append(await self.frame('footer-shine-fast-'+str(len(samples))))
        await asyncio.sleep(max(0,start+1.1-time.perf_counter()))
        samples.append(await self.frame('footer-shine-late-settled'))
        current=await self.query('vr.targets')
        assert current['hovered']==primary['name'] and current['clicks']==before['clicks'],'Footer hover activated an action'
        assert {t['name']:t['rect'] for t in current['targets']}==rects,'Shine acquisition changed immutable hit targets'
        self.visual_phase('footer-shine',lambda:self.shine(footer_hover,footer_repeat,samples,start,primary['rect'],response['delivery']))
        await self.park()
        await self.click('challenge.page.next')
        page=await self.query('menu.get_state');assert page['selection_index']//3==1,page
        await self.click('challenge.page.prev')
        page=await self.query('menu.get_state');assert page['selection_index']//3==0,page
        self.cases.append({'case':'one-page-click-retry-and-return','passed':True})
        assert all(c.get('passed',False) for c in self.cases),('Visual qualification failed; independent phases and page cleanup were still acquired',[c['case'] for c in self.cases if not c.get('passed',False)])

    def photo(self,rest,repeat,hovered,lift):
        photo=self.layout['cards'][0]['photo'];card=self.layout['cards'][0]
        x,y,w,h=(photo[k] for k in ('x','y','width','height'))
        points=grid([x+.08*w,y+.16*h,x+.92*w,y+.60*h])
        excluded=[card['badge_extension'],card['recommendation']]
        center=np.array([x+w/2,y+h/2])
        expected_points=center+(points-center)/SOURCE_ZOOM
        keep=outside(points,excluded)&outside(expected_points,excluded)
        points=points[keep];expected_points=expected_points[keep]
        observations=[]
        for eye in ('left','right'):
            a,am=rest.samples(expected_points,eye)
            b,bm=repeat.samples(expected_points,eye)
            c,cm=hovered.samples(points,eye,lift)
            valid=am&bm&cm
            assert np.count_nonzero(valid)>=128,'Photo ROI obscured by artwork or rays'
            a,b,c=a[valid],b[valid],c[valid]
            brightness=directional_metric(a@LUMA,b@LUMA,c@LUMA,1)
            # The SAME reference weight suppresses unstable dark-pixel HSV in
            # every phase. Recomputing weights after brightening could falsely
            # report saturation gain from brightness alone.
            weight=np.sqrt(np.maximum(a@LUMA,0))
            color=directional_metric(saturation(a)*weight,saturation(b)*weight,saturation(c)*weight,1)
            errors=[]
            for scale in np.linspace(1,1.10,21):
                coordinates=center+(points-center)/scale
                values,mask=repeat.samples(coordinates,eye)
                good=valid&mask&outside(coordinates,excluded)
                errors.append({'relative_scale':float(scale),'normalized_error':normalized_error(values[good],hovered.samples(points,eye,lift)[0][good])})
            best=min(errors,key=lambda q:q['normalized_error'])
            same=errors[0]['normalized_error']
            expected=min(errors,key=lambda q:abs(q['relative_scale']-SOURCE_ZOOM))['normalized_error']
            zoom_noise=normalized_error(a,b)
            zoom_ok=best['relative_scale']>1+(SOURCE_ZOOM-1)/2 and expected+zoom_noise<same
            observation={'eye':eye,'sample_count':len(c),'source_filter':{'brightness':[.91,1.08],'saturation':[.86,1.1],'relative_zoom':SOURCE_ZOOM},
                'brightness':brightness,'weighted_saturation':color,'zoom':{'passed':bool(zoom_ok),'best':best,'source_zoom_error':expected,'unzoomed_error':same,'repeated_rest_shape_noise':zoom_noise,'fit':errors}}
            observations.append(observation)
        self.cases.append({'case':'both-eye-photo-grade-and-source-zoom','passed':all(o['brightness']['passed'] and o['weighted_saturation']['passed'] and o['zoom']['passed'] for o in observations),'observations':observations,
                           'limit':'Directional radiometric response and source-scale feature agreement; JPEG/tone mapping prevent an exact CSS filter-coefficient or shader-cost proof.'})

    def effects(self,rest,repeat,hovered):
        card=self.layout['cards'][0]['card'];x,y,w,h=(card[k] for k in ('x','y','width','height'))
        material=next(m for m in self.styles['materials'] if m['name']=='CardSelectedHoverFx')
        assert material['draw_offset'][0]<=-52 and material['draw_offset'][1]<=-65,'Styles manifest lacks required outer margins'
        # Candidate bands are fixed from the CSS9px/16px hot rings and26px/60px
        # diffuse halos. Select using independent source RGBA and rest frames,
        # not by looking for whichever observed hover pixels happen to pass.
        glow_candidates={
            'top-hot-ring':grid([x+.2*w,y-24,x+.8*w,y-9],64,12),
            'left-hot-ring':grid([x-24,y+.15*h,x-9,y+.85*h],12,64),
            'left-diffuse-halo':grid([x-52,y+.2*h,x-28,y+.8*h],16,48)}
        # Below the card, left of the first footer silhouette; no text/icons.
        shadow=grid([x+40,y+h+35,min(x+140,self.layout['visual_bounds']['actions'][0]['x']-8),y+h+65],32,16)
        observations=[]
        distance=HOVER_LIFT_METERS if self.args.effects_plane=='lifted' else 0
        atlas=np.asarray(Image.open(self.args.styles_manifest.with_name('Styles.png')).convert('RGBA'),dtype=float)/255
        def reference(name,points):
            m=next(m for m in self.styles['materials'] if m['name']==name)
            origin=np.array([m['reference']['x'],m['reference']['y']])+m['draw_offset']
            uv=(points-origin)/m['draw_size'];ax,ay,aw,ah=m['atlas_rect']
            assert np.all((uv>=0)&(uv<=1)),('Reference FX sample escaped manifest',name)
            return bilinear(atlas,np.array([ax,ay])+uv*np.array([aw-1,ah-1]))
        for eye in ('left','right'):
            row={'eye':eye}
            candidates=[]
            for region,points in glow_candidates.items():
                a,am=rest.samples(points,eye);b,bm=repeat.samples(points,eye);mask=am&bm
                assert np.count_nonzero(mask)>=64,('Reference glow ROI obscured',eye,region)
                signal=reference_chroma_signal(a[mask],b[mask],reference('CardSelectedFx',points)[mask],reference('CardSelectedHoverFx',points)[mask])
                candidates.append({'region':region,**signal})
            chosen=max(candidates,key=lambda c:c['selection_score'])
            assert chosen['selection_score']>0,('No reference glow region exceeds repeated-rest noise',eye,candidates)
            glow=glow_candidates[chosen['region']]
            for name,points,metric,direction in [('gold_glow',glow,lambda rgb:rgb[:,0]-rgb[:,2],1),('downward_shadow',shadow,lambda rgb:rgb@LUMA,-1)]:
                a,am=rest.samples(points,eye);b,bm=repeat.samples(points,eye);c,cm=hovered.samples(points,eye,distance)
                valid=am&bm&cm;assert np.count_nonzero(valid)>=64,(name,eye)
                if name=='gold_glow':
                    row[name]=source_effect_metrics(a[valid],b[valid],c[valid],reference('CardSelectedFx',points)[valid],reference('CardSelectedHoverFx',points)[valid])
                    row[name]['raw_gold_r_minus_b_delta']=float(np.mean(metric(c[valid])-metric(b[valid])))
                    row[name]['region']=chosen['region'];row[name]['reference_only_region_candidates']=candidates
                else:row[name]=directional_metric(metric(a[valid]),metric(b[valid]),metric(c[valid]),direction)
                row[name]['sample_count']=int(np.count_nonzero(valid))
            observations.append(row)
        self.cases.append({'case':'both-eye-outer-gold-and-downward-shadow-response','passed':all(o[n]['passed'] for o in observations for n in ('gold_glow','downward_shadow')),
                           'effects_plane':self.args.effects_plane,'observations':observations,'reference':'CSS-authored straight-alpha selected/selected-hover FX; stronger shadow can lower raw(R-B) on an orange scene.',
                           'limit':'Reference-delta agreement plus normalized chromaticity and observed shadow response, not reconstruction of compositor alpha or CSS blur sigma.'})

    def shine(self,rest,repeat,frames,start,target,delivery):
        action=self.layout['actions'][2];x,y,w,h=(action[k] for k in ('x','y','width','height'))
        points=grid([x+16,y+8,x+w-16,y+h-8],96,12)
        excludes=list(self.layout['action_contents'][2].values())
        valid=outside(points,excludes,padding=10)
        frame_ids=[int(f.capture['geometry']['frame_id']) for f in frames]
        assert all(b>a for a,b in zip(frame_ids,frame_ids[1:])),'Shine frames did not advance native frame IDs'
        assert frame_ids[0]>=delivery['applied_frame'],'Shine frame predates delivered hover'
        observations=[]
        for eye in ('left','right'):
            a,am=rest.samples(points+[0,-4],eye,rest.lift_at(target))
            b,bm=repeat.samples(points+[0,-4],eye,repeat.lift_at(target))
            keep=valid&am&bm
            def profile(rgb,mask):
                # Grid is row-major; keep the same96 horizontal columns.
                values=np.where(mask,(rgb@LUMA),np.nan).reshape(12,96)
                count=np.sum(np.isfinite(values),axis=0)
                result=np.nansum(values,axis=0)/np.maximum(count,1)
                return np.where(count>=2,result,np.nan)
            baseline=profile(b,keep);noise=profile(b-a,keep)
            samples=[];registration=[]
            for frame in frames:
                pose,candidates=register_footer_y(frame,repeat,action,eye,target)
                rgb,mask=frame.samples(points+[0,pose['offset_y']],eye,frame.lift_at(target))
                diff=profile(rgb,keep&mask)-baseline
                samples.append((frame.time-start,diff))
                registration.append({'capture_id':frame.capture['capture_id'],
                    'frame_id':frame.capture['geometry']['frame_id'],'sync_id':frame.capture['geometry']['sync_id'],
                    'capture_time_bracket_s':[frame.request_start-start,frame.time-start],
                    'server_created_at':frame.capture.get('created_at'),'physical_lift_m':frame.lift_at(target),
                    'visual_offset_registration':pose,'pose_candidates':candidates})
            columns=np.isfinite(baseline)&np.isfinite(noise)
            for _,values in samples:columns &= np.isfinite(values)
            assert np.count_nonzero(columns)>=32,'Footer text/rays obscure the shine sample'
            result=shine_metrics([(t,v[columns]) for t,v in samples],noise[columns],
                                 positions=((np.arange(96)+.5)/96)[columns])
            observations.append({'eye':eye,**result,'registration':registration,
                'reference':'Repeated settled hover, same20mm face and−4px visual position',
                'timing_method':'Native frame/sync IDs plus local request/response brackets; elapsed is capture-completion upper bound, not a requested deadline'})
        self.cases.append({'case':'both-eye-moving-footer-shine-observed','passed':all(o['passed'] for o in observations),'observations':observations,
            'limit':'Requires two moving pulse positions conservatively captured inside700ms and a late fade. Exact shader duration is not certified; a missed acquisition window remains an explicit inconclusive failure.'})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'pid':args.pid,'scope':'FlatOut selected-first-card material/photo hover and primary-footer shine; no race/Wreck Run',
            'limits':['Not headset GPU/performance proof','Not browser playback','Not exact compositor alpha/blur/shader-coefficient reconstruction','A missed short shine is an explicit inconclusive failure'],
            'requested_pixel_capture_width':args.capture_width,
            'manifest_sha256':hashlib.sha256(args.layout_manifest.read_bytes()).hexdigest(),
            'styles_manifest_sha256':hashlib.sha256(args.styles_manifest.read_bytes()).hexdigest()}
    result['styles_image_sha256']=hashlib.sha256(args.styles_manifest.with_name('Styles.png').read_bytes()).hexdigest()
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=ChallengeStyle(args,session);test.frames=[]
            test.layout=load_layout_manifest(args.layout_manifest)
            test.styles=json.loads(args.styles_manifest.read_text());assert test.styles['schema']=='fox.challenge-css-materials.v1'
            try:
                await test.prove()
                result['native_errors']=await test.query('game.errors');assert result['native_errors']['total']==0
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                cleanup_errors=[]
                if test.last_capture:
                    for hand in ('left','right'):
                        try:
                            cleanup=await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})
                            if cleanup.isError:cleanup_errors.append({'hand':hand,'error':str(cleanup)})
                        except Exception as error:
                            cleanup_errors.append({'hand':hand,'error':repr(error)})
                result['cleanup_errors']=cleanup_errors
                success_before_cleanup=result['passed']
                if cleanup_errors:result['passed']=False
                result['cases']=test.cases
                result['captures']=[{'path':str(f.path),'sha256':hashlib.sha256(f.path.read_bytes()).hexdigest(),
                    'capture_id':f.capture['capture_id'],'time':f.time,'request_start':f.request_start,
                    'server_created_at':f.capture.get('created_at'),'frame_id':f.capture['geometry'].get('frame_id'),
                    'sync_id':f.capture['geometry'].get('sync_id')} for f in test.frames]
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if cleanup_errors and success_before_cleanup:
                    raise AssertionError(('Pointer cleanup failed',cleanup_errors))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--layout-manifest',type=Path,default=default_layout_manifest())
    p.add_argument('--styles-manifest',type=Path,required=True)
    p.add_argument('--effects-plane',choices=('base','lifted'),required=True,help='Actual placement contract of the separate FX sprites; do not guess from pixels')
    p.add_argument('--capture-width',type=int,choices=(2048,4096),default=4096,help='4096 preserves the full native pixel frame (3456 wide on the current simulator); lower width is explicit and may be inconclusive')
    asyncio.run(run(p.parse_args()))
