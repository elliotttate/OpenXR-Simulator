"""Real XR Roof click: rotate car and wheels, keep the room and UI fixed.

Start in the livery editor with an isolated save. No game-side input injection.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest, ROOT
from flatout_livery_orientation_e2e import assert_world_upright, assert_level_tracking_space


def transform(frame):
    m = np.eye(4)
    m[:3, :3] = np.column_stack([frame[k] for k in ('right', 'up', 'back')])
    m[:3, 3] = frame['origin']
    return m


def assert_fixed_room(before, after):
    for name in ('camera', 'panel'):
        assert np.allclose(transform(before[name]), transform(after[name]), atol=.002), (name, 'Roof moved the viewpoint or UI')
    assert_world_upright(after)
    assert_level_tracking_space(after)


class RoofTest(LiveryTest):
    async def run(self):
        await self.click('STUDIO')
        await asyncio.sleep(.5)
        before = await self.state()
        await self.capture('car-upright')
        after = await self.click('Roof', hold_frames=45, retry=True)
        await self.capture('car-roof-facing-viewer')
        a, b = before['vr']['frame'], after['vr']['frame']
        assert_fixed_room(a, b)
        old, new = a['car_presentation'], b['car_presentation']
        assert not old['roof_active'] and new['roof_active'] and new['progress'] == 1
        assert np.allclose(transform(old['showroom']), transform(new['showroom']), atol=.0001), 'Room moved'
        assert len(new['parts']) == len(old['parts']) >= 5, 'Body and four wheels required'
        facing = np.array(new['parts'][0]['back'])
        facing /= np.linalg.norm(facing)
        assert np.dot(facing, b['camera']['back']) > .999, 'Roof does not face the viewer'
        delta = transform(new['parts'][0]) @ np.linalg.inv(transform(old['parts'][0]))
        for index, (p, q) in enumerate(zip(old['parts'], new['parts'])):
            assert np.allclose(transform(q), delta @ transform(p), atol=.001), ('Wheel detached', index)
        assert before['document'] == after['document'], 'View changed paint data'
        # A second real click is also harmless, separately from MCP UUID retry.
        repeated = await self.click('Roof')
        for p, q in zip(new['parts'], repeated['vr']['frame']['car_presentation']['parts']):
            assert np.allclose(transform(p), transform(q), atol=.001), 'Repeated Roof accumulated rotation'
        self.cases.append({'case': 'fixed-room-car-flip', 'passed': True, 'before': a, 'after': b})
        restored = await self.click('STUDIO', retry=True)
        final = restored['vr']['frame']['car_presentation']
        assert not final['roof_active']
        for p, q in zip(old['parts'], final['parts']):
            assert np.allclose(transform(p), transform(q), atol=.001), 'Original car pose not restored'
        self.cases.append({'case': 'restore-body-and-wheels', 'passed': True})
        if self.args.gestures:
            original = restored['document']
            await self.dual_hands(view='Roof')
            painted = await self.state()
            layer = painted['document']['layers'][painted['selected']]
            assert layer['projectionNormal'][2] > .8, ('Placement missed the roof', layer)
            undone = await self.click('UNDO')
            assert undone['document'] == original, 'Roof gesture did not restore original paint'
            await self.click('STUDIO')


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False, 'pid': args.pid}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = RoofTest(args, session)
            try:
                await test.run()
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                raise
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result, indent=2))
                if test.last_capture:
                    for hand in ('left', 'right'):
                        await session.call_tool('release_ui_pointer', {'capture_id': test.last_capture['capture_id'], 'hand': hand})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--gestures', action='store_true', help='With an unlocked selected sticker, place/move/resize it on the flipped roof and Undo')
    asyncio.run(main(parser.parse_args()))
