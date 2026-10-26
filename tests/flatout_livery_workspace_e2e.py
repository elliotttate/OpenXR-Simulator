"""Verify a clear car workspace and a separately aimed livery tool console.

Start the Release game in Livery with an isolated profile and an unlocked layer.
All interaction uses captured pixels and real XR input. Edits are undone.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import ROOT, LiveryTest, world_pixel


def clearance(state):
    frame = state['vr']['frame']
    camera, panel = frame['camera'], frame['panel']
    basis = np.column_stack([camera[k] for k in ('right', 'up', 'back')])
    assert np.allclose(panel['up'], [0, 0, 1], atol=1e-5), panel
    angles = []
    for name in ('left_eye', 'right_eye'):
        eye = np.array(camera['origin']) + basis @ frame[name]['origin']
        inner = np.array(panel['origin']) + np.array(panel['right'])*frame['panel_width']/2 - eye
        angle = math.degrees(math.atan2(inner @ camera['right'], -inner @ camera['back']))
        assert angle < -35, ('Editor still covers the car-facing workspace', name, angle)
        angles.append(angle)
    return angles


class WorkspaceTest(LiveryTest):
    async def look(self, yaw, label):
        result = await self.session.call_tool('set_head_pose', dict(x=0, y=1.7, z=0, yaw_deg=yaw,
                                                                  pitch_deg=0, roll_deg=0))
        assert not result.isError, result
        (self.args.evidence/(label+'-head.json')).write_text(json.dumps({'yaw_degrees':yaw,'result':[c.text for c in result.content if c.type=='text']}))
        await asyncio.sleep(.35)

    async def stereo(self, label):
        for eye in ('left', 'right'):
            await self.tool('capture_screenshot', {'eye':eye, 'max_width':1280}, label+'-'+eye)

    async def car_point(self, hand, x, y, hold=True):
        state = await self.state()
        already_grabbed = state['vr']['hands'][hand=='right']['grabbed']
        capture = await self.capture('car-source')
        camera = state['vr']['frame']['camera']
        # A car-facing work plane, independent of the relocated menu canvas.
        basis = np.column_stack([camera[k] for k in ('right', 'up', 'back')])
        point = np.array(camera['origin']) + basis @ [x, 1.7+y, -4]
        px, py = world_pixel(state, capture, point, 'left')
        assert 0 <= px < capture['image_size'][0] and 0 <= py < capture['image_size'][1]
        args = {'capture_id':capture['capture_id'], 'x':float(px), 'y':float(py),
                'origin_mode':'hand', 'hand':hand, 'dwell_frames':15,
                'surface':{'pose':{'position':[0, 1.7, -4], 'orientation':[0,0,0,1]}, 'size':[20,20]}}
        receipt = await self.tool('hold_ui_at' if hold else 'hover_ui_at', args, 'car-'+hand)
        assert receipt['status']=='input_delivered', receipt
        after = await self.state()
        hit = after['vr']['hands'][hand=='right']
        assert hit['valid'] and not hit['ui_hit'], hit
        # Starting a gesture must hit the car. Once captured, a drag may cross
        # the silhouette without dropping the sticker or transferring to UI.
        if not already_grabbed:
            assert hit['car_hit'], hit
        if hold:
            assert hit['grabbed'], hit
        return after

    async def run_workspace(self):
        from flatout_frontend_e2e import read_game
        native = await asyncio.to_thread(read_game,self.args.pid,'vr.get_state')
        assert 'Simulator' in native.get('runtime',''), 'Use an isolated simulator launch'
        original = await self.state()
        self.cases.append({'case':'car-corridor-clearance', 'inner_edge_degrees':clearance(original), 'passed':True})
        await self.look(0, 'face-car')
        await self.stereo('unobstructed-car')
        await self.look(65, 'face-console')
        await self.stereo('console')
        await self.click('PAINT', hand='left', hold_frames=45, retry=True)
        paint = await self.state()
        await self.click('Matte')
        restored = await self.click('UNDO', hand='left')
        assert restored['document']==paint['document']
        await self.click('STICKERS')
        await self.click('EDIT LAYER', hand='left')
        await self.click('SCALE / SKEW')
        await self.click('MOVE / ROTATE', hand='left')
        await self.click('PLACE ON CAR')
        before = await self.state()
        await self.look(0, 'paint-workspace')
        await self.car_point('left', .115, .115)
        joined = await self.car_point('right', .63, .115)
        assert joined['vr']['grab_mask']==3
        await self.car_point('left', .01, .16)
        changed = await self.car_point('right', .80, .06)
        selected = changed['selected']
        assert changed['document']['layers'][selected]['scale'] != joined['document']['layers'][selected]['scale']
        await self.release('left')
        final = await self.release('right')
        assert final['vr']['grab_mask']==0
        await self.stereo('painted-car')
        await self.look(65, 'return-console')
        restored = await self.click('UNDO', hand='left')
        assert restored['document']==before['document'], 'One Undo must restore the entire car gesture'
        await self.click('DONE PLACING')
        await self.click('Roof')
        await self.look(0, 'face-roof')
        roof = await self.state()
        self.cases.append({'case':'roof-console-clearance', 'inner_edge_degrees':clearance(roof), 'passed':True})
        await self.stereo('unobstructed-roof')
        await self.look(65, 'restore-studio')
        await self.click('STUDIO')
        await self.click('BACK TO LAYERS')
        await self.look(0, 'finished')
        assert (await self.state())['document']==original['document']
        self.cases.append({'case':'side-console-and-dual-car-ray', 'passed':True})


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
                                   env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result = {'passed':False}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = WorkspaceTest(args, session)
            try:
                await test.run_workspace()
                result['passed'] = True
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':True, 'cases':len(test.cases)}))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
