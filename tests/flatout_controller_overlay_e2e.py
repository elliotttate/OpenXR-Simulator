"""Visible controller-over-menu regression against a running FlatOut simulator.

Start a fresh build in Options with a level, forward-looking simulator head and
an isolated test profile. Uses real runtime poses and rendered screenshots. Only
the diagnostic overlay switch changes game state; no gameplay input is injected.
The old path must hide both controller models behind the opaque menu; the new
path must expose both, independently in each eye. Original poses/switch restored.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ui_automation_e2e import ROOT, call
from flatout_livery_e2e import quaternion_matrix


def game(pid, method, params=None):
    assert method in ('vr.controller_overlay', 'vr.get_state', 'menu.get_state', 'options.get_state', 'game.errors', 'renderdoc.capture')
    payload = json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}).encode()
    with open(r'\\.\pipe\FlatoutMCP_'+str(pid), 'r+b', buffering=0) as pipe:
        pipe.write(struct.pack('<I',len(payload))+payload)
        def read(n):
            data = b''
            while len(data)<n:
                part = pipe.read(n-len(data))
                if not part:
                    raise RuntimeError('Game pipe closed')
                data += part
            return data
        response = json.loads(read(struct.unpack('<I',read(4))[0]))
    assert 'error' not in response, response
    return response['result']


async def pose(folder, hand, values):
    target = folder/'controller_pose_command.json'
    assert not target.exists(), 'Another runtime pose command is pending'
    temporary = target.with_suffix('.overlay-test.tmp')
    temporary.write_text(json.dumps({'hand':hand, **values}))
    temporary.replace(target)
    deadline = time.monotonic()+5
    while target.exists():
        assert time.monotonic()<deadline, 'Runtime did not consume controller pose'
        await asyncio.sleep(.02)
    await asyncio.sleep(.15)


def project(capture, eye_name, point):
    eye = next(e for e in capture['geometry']['eyes'] if e['eye']==eye_name)
    local = quaternion_matrix(eye['pose']['orientation']).T @ (np.array(point)-eye['pose']['position'])
    left,right,up,down = map(math.tan,eye['fov'])
    u,v = (local[0]/-local[2]-left)/(right-left),(local[1]/-local[2]-up)/(down-up)
    ex,ey,ew,eh = eye['viewport'];cx,cy,cw,ch = capture['crop'];w,h = capture['image_size']
    return np.array([(ex+u*ew-cx)/cw*w-.5,(ey+v*eh-cy)/ch*h-.5])


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result = {'passed':False,'eyes':{}}
    original = game(args.pid,'vr.controller_overlay')
    assert original['available'], 'Build has no controller overlay resources'
    assert original.get('composition')=='stereo_quads', 'Controller overlay must use per-eye quad composition on SteamVR'
    assert game(args.pid,'options.get_state')['open'], 'Start in front-end or in-race Options'
    env = {**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())}
    params = StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env=env)
    restore = []
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                initial,_ = await call(session,'capture_screenshot',{'eye':'left'},args.evidence,'initial')
                head = initial['geometry']['head']
                assert np.max(np.abs(np.array(head['orientation'])-[0,0,0,1]))<1e-5, 'Head must face forward without tilt'
                for name in ('left','right'):
                    await session.call_tool('release_ui_pointer',{'capture_id':initial['capture_id'],'hand':name})
                await asyncio.sleep(.2)
                state = game(args.pid,'vr.get_state')
                for controller in state['controllers']:
                    p = controller['grip_pose'];z = p['z']
                    restore.append(dict(zip(('posX','posY','posZ'),np.array(p['position'])-head['position']),
                                        yaw=math.atan2(z[0],z[2]),pitch=-math.asin(z[1])))
                for hand in (0,1):
                    await pose(args.data_dir,hand,dict(posX=-.16 if hand==0 else .16,posY=0,posZ=-.65,
                                                     yaw=.2 if hand==0 else -.2,pitch=-.10))
                frames = {}
                for enabled in (False,True):
                    tag = 'on' if enabled else 'off'
                    game(args.pid,'vr.controller_overlay',{'enabled':enabled})
                    await asyncio.sleep(.6)
                    native = game(args.pid,'vr.get_state')
                    status = game(args.pid,'vr.controller_overlay')
                    assert status['rendered_eye_mask']==(3 if enabled else 0), status
                    assert status['submitted_layers']==(2 if enabled else 0), status
                    (args.evidence/(tag+'-native.json')).write_text(json.dumps({'vr':native,'overlay':status},indent=2))
                    if enabled and args.gpu_capture:
                        result['gpu_capture'] = game(args.pid,'renderdoc.capture',
                            {'path':str(args.gpu_capture.resolve()),'frames':1})
                        await asyncio.sleep(1)
                    for eye in ('left','right'):
                        capture,image = await call(session,'capture_screenshot',{'eye':eye,'max_width':1600},args.evidence,tag+'-'+eye)
                        assert len(capture['geometry']['projection_eyes'])==2
                        frames[tag,eye] = capture,image,native
                for eye in ('left','right'):
                    capture,on,native = frames['on',eye];_,off,old_native = frames['off',eye]
                    assert on.shape==off.shape
                    result['eyes'][eye] = []
                    for hand in (0,1):
                        point = native['controllers'][hand]['grip_pose']['position']
                        assert point==old_native['controllers'][hand]['grip_pose']['position'], 'A/B pose changed'
                        center = project(capture,eye,point)
                        # Bound a 24 cm neighbourhood in the same eye projection.
                        edge = project(capture,eye,np.array(point)+[.12,0,0])
                        radius = int(np.linalg.norm(edge-center))
                        x,y = map(int,center);h,w = on.shape[:2]
                        rect = (max(0,x-radius),max(0,y-radius),min(w,x+radius),min(h,y+radius))
                        l,t,r,b = rect;a = on[t:b,l:r,:3].astype(float);z = off[t:b,l:r,:3].astype(float)
                        orange = lambda p:(p[:,:,0]>65)&(p[:,:,0]>p[:,:,1]*1.45)&(p[:,:,1]>p[:,:,2]*1.2)
                        visible = orange(a)&(np.max(np.abs(a-z),axis=2)>25)
                        # Compare the same model pixels; the generous search ROI may
                        # include orange scenery through gaps between menu panels.
                        count,hidden = int(visible.sum()),int((orange(z)&visible).sum())
                        result['eyes'][eye].append({'hand':hand,'roi':rect,'visible_controller_pixels':count,'control_orange_pixels':hidden})
                        assert count>100, ('Controller did not appear above the menu',eye,hand,count)
                        assert hidden<count*.15, ('Negative control was not occluded by the menu',eye,hand,hidden,count)
                result['game_errors'] = game(args.pid,'game.errors')
                result['passed'] = True
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        for hand,values in enumerate(restore):
            await pose(args.data_dir,hand,values)
        game(args.pid,'vr.controller_overlay',{'enabled':original['enabled']})
        (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--gpu-capture',type=Path,help='Optional RenderDoc capture prefix; launch with injection')
    asyncio.run(run(parser.parse_args()))
