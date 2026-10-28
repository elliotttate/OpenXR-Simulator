"""Stress real screenshot-derived ray transitions between native Career cards.

Uses the MCP server's UiAutomation backend, without an intervening screenshot
after each hover command. Every pose is derived from an observed image. A
concurrent compositor capture checks actual atlas artwork, carrier orientation,
target location and bounded physical travel, including retiring hover slots.
No clicks, native hover injection, progress edits or gameplay callbacks.
"""
import argparse
import asyncio
from io import BytesIO
import json
import math
from pathlib import Path
import sys
import time
import uuid

import numpy as np
from PIL import Image
from flatout_frontend_e2e import read_game, quad_pixel
from flatout_livery_e2e import quaternion_matrix
from flatout_ray_offset import native_pointer_offset
from flatout_ui_motion_policy import HOVER_LIFT_METERS

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mcp-server'))
from ui_targeting import UiAutomation,resolve_target


def photo_samples(capture,pixels,surface):
    # Sample the top interior of the source face, away from title/badges/rays.
    # Work in local UV so lift and perspective cannot move the comparison box.
    eye_samples={}
    projected={**capture,'geometry':{**capture['geometry'],'surfaces':[surface]}}
    for eye in ('left','right'):
        values=[]
        for v in np.linspace(.07,.32,16):
            for u in np.linspace(.09,.91,32):
                x,y=quad_pixel(projected,(u*1920,v*1080,u*1920,v*1080),eye)
                assert 1<=x<pixels.shape[1]-2 and 1<=y<pixels.shape[0]-2,(eye,x,y)
                values.append(pixels[int(round(y)),int(round(x))])
        eye_samples[eye]=np.asarray(values,dtype=float)
    return eye_samples


def card_surface(base,rect):
    l,t,r,b=rect
    axes=quaternion_matrix(base['pose']['orientation'])
    center=np.asarray(base['pose']['position'])+axes@np.array([
        ((l+r)/3840-.5)*base['size'][0],(.5-(t+b)/2160)*base['size'][1],0])
    return {'id':-1,'visibility':0,'pose':{'position':center.tolist(),
        'orientation':base['pose']['orientation']},
        'size':[(r-l)/1920*base['size'][0],(b-t)/1080*base['size'][1]]}


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    ui=UiAutomation(a.data_dir)
    native=lambda method:asyncio.to_thread(read_game,a.pid,method)
    report={'passed':False,'hand':a.hand,'moves':[],'frames':[],
        'scope':'Actual XR action states and both-eye composed pixels; simulator only'}
    capture=None
    stop=False
    sampler=None
    failure=None
    async def take(label):
        c,data=await ui.capture('both',timeout=15,max_width=4096)
        (a.evidence/(label+'.json')).write_text(json.dumps(c,indent=2))
        (a.evidence/(label+'.jpg')).write_bytes(data)
        return c,np.asarray(Image.open(BytesIO(data)).convert('RGB'))
    try:
        state=await native('menu.get_state')
        assert state['current_menu']=='FLATOUT_MODE' and state['career']['native_catalog'],state
        original=state['career']
        targets=await native('vr.targets');clicks=targets['clicks']
        cards={t['name']:t['rect'] for t in targets['targets'] if t['name'].startswith('tile.')}
        assert len(cards)==4,cards
        # Release inherited test leases and settle every face into the base.
        capture,_=await take('initial')
        for hand in range(2):
            await ui.command({'action':'release','session_id':capture['geometry']['session_id'],'hand':hand},5)
        await asyncio.sleep(.4)
        capture,pixels=await take('unhovered-baseline')
        base=max((s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:math.prod(s['size']))
        assert len([s for s in capture['geometry']['surfaces'] if s['visibility']==0])==1
        expected={name:photo_samples(capture,pixels,card_surface(base,rect)) for name,rect in cards.items()}
        calibration=native_pointer_offset(await native('vr.get_state'),a.hand)
        poses={}
        for name,rect in cards.items():
            l,t,r,b=rect;x=l+(r-l)*.5;y=t+(b-t)*.75
            px,py=quad_pixel(capture,(x,y,x,y))
            poses[name]=resolve_target(capture,float(px),float(py),hand=a.hand,
                surface_id=base['id'],ray_offset=calibration)

        async def sample():
            frame=0
            while not stop:
                c,p=await take('transition-'+str(frame).zfill(3));frame+=1
                surfaces=[s for s in c['geometry']['surfaces'] if s['visibility']==0]
                current_base=max(surfaces,key=lambda s:math.prod(s['size']))
                record={'frame':c['geometry']['frame_id'],'tiles':[]}
                for tile in surfaces:
                    if tile['id']==current_base['id']:continue
                    axes=quaternion_matrix(current_base['pose']['orientation'])
                    dot=abs(float(np.dot(tile['pose']['orientation'],current_base['pose']['orientation'])))
                    local=axes.T@(np.asarray(tile['pose']['position'])-current_base['pose']['position'])
                    assert dot>1-1e-6,('Hover tile tilted away from its carrier',tile,current_base)
                    assert -.0001<=local[2]<=HOVER_LIFT_METERS+.001,('Unbounded physical travel',local.tolist())
                    candidates=[]
                    for name,rect in cards.items():
                        ref=card_surface(current_base,rect)
                        lateral=np.linalg.norm((np.asarray(tile['pose']['position'])-ref['pose']['position'])@axes[:,:2])
                        size_error=max(abs(np.asarray(tile['size'])-ref['size']))
                        if lateral<.005 and size_error<.005:candidates.append(name)
                    assert len(candidates)==1,('Tile does not belong to exactly one observed card',tile,candidates)
                    name=candidates[0]
                    actual=photo_samples(c,p,tile)
                    scores={eye:{other:float(np.abs(actual[eye]-expected[other][eye]).mean()) for other in cards} for eye in actual}
                    for eye,score in scores.items():
                        assert min(score,key=score.get)==name,('Stale artwork from another card',eye,name,score)
                        assert score[name]<25,('Raised card pixels differ from its native source',eye,name,score)
                    record['tiles'].append({'target':name,'lift':float(local[2]),'scores':scores})
                assert len(record['tiles'])<=2,record
                report['frames'].append(record)

        sampler=asyncio.create_task(sample())
        names=list(cards)
        order=(names+names[-2::-1])*a.cycles
        for index,name in enumerate(order):
            if sampler.done():await sampler
            # Keep source observations fresh while retaining fixed hit geometry.
            if time.time()-capture['created_at']>20:
                capture,_=await take('renewed-source-'+str(index))
            pose=poses[name]['aim_pose']
            payload={'action':'hover','session_id':capture['geometry']['session_id'],
                'capture_id':capture['capture_id'],'hand':0 if a.hand=='left' else 1,
                'button':'trigger','dwell_frames':a.dwell_frames,'hold_frames':1,
                'surface_id':base['id'],'lease_ms':10000,'command_id':uuid.uuid4().hex}
            payload.update(dict(zip(('px','py','pz'),pose['position'])))
            payload.update(dict(zip(('qx','qy','qz','qw'),pose['orientation'])))
            delivery=await ui.command(payload,10)
            assert delivery['success'],delivery
            report['moves'].append({'target':name,'delivery':delivery,'time':time.monotonic()})
        await asyncio.sleep(.15);stop=True;await sampler
        current=await native('vr.targets');state=await native('menu.get_state')
        assert current['clicks']==clicks and state['career']['selected_event_id']==original['selected_event_id']
        assert state['career']['page']==original['page'] and state['career']['catalog']==original['catalog']
        observed={t['target'] for f in report['frames'] for t in f['tiles']}
        assert observed==set(cards),('Capture failed to observe all hovered cards',observed)
        assert len(report['frames'])>=8 and len({f['frame'] for f in report['frames']})==len(report['frames'])
        report['native_errors']=await native('game.errors');assert report['native_errors']['total']==0
        report['passed']=True
    except BaseException as error:
        failure=error;report['error']=repr(error)
    finally:
        stop=True
        if sampler:
            try:await sampler
            except BaseException as error:
                if failure is None:failure=error;report['error']=repr(error);report['passed']=False
        if capture:
            for hand in range(2):await ui.command({'action':'release','session_id':capture['geometry']['session_id'],'hand':hand},5)
        (a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':report['passed'],'moves':len(report['moves']),'frames':len(report['frames']),'error':report.get('error')}))
    if failure:raise failure


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--cycles',type=int,default=8);p.add_argument('--dwell-frames',type=int,choices=range(1,10),default=1)
    asyncio.run(run(p.parse_args()))
