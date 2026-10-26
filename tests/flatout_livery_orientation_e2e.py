"""Native regression for opening livery with a tilted head.

Open Garage -> Livery with a nonzero head pitch/roll before running. The game
bridge only reads geometry; simulator commands move the actual XR head.
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
from flatout_frontend_e2e import read_game


class Orientation(LiveryTest):
    async def run(self):
        state = await self.state()
        frame = state['vr']['frame']
        await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, 'opening-orientation')
        self.last_capture = await self.tool('capture_screenshot', {'eye': 'left'}, 'source')
        head = frame['head_reference']
        assert np.linalg.norm(np.array(head['up']) - [0, 1, 0]) > .1, 'Open livery with a tilted head first'
        camera = np.column_stack([frame['camera'][k] for k in ('right', 'up', 'back')])
        panel = frame['panel']
        local_up = camera.T @ panel['up']
        local_right = camera.T @ panel['right']
        local_back = -(camera.T @ panel['back'])
        self.cases.append({'case': 'upright-after-tilted-entry', 'frame': frame,
                           'local_up': local_up.tolist(), 'local_back': local_back.tolist()})
        assert np.allclose(local_up, [0, 1, 0], atol=.001), ('Captured head tilt remained in panel', local_up)
        assert abs(local_right[1]) < .001 and abs(local_back[1]) < .001
        heading = np.array(head['back']) * [1, 0, 1]
        heading /= np.linalg.norm(heading)
        assert np.dot(local_back, heading) > .999, 'Opening heading changed'
        opening_eye = np.array(frame['camera']['origin']) + camera @ head['origin']
        assert np.allclose(np.array(panel['origin']) - opening_eye,
                           camera @ (-heading * frame['panel_distance']), atol=.001)
        for label, pose in [('straighten', {'x': 0, 'y': 1.7, 'z': 0, 'yaw_deg': 0, 'pitch_deg': 0, 'roll_deg': 0}),
                            ('face-opening-heading', {'x': 0, 'y': 1.7, 'z': 0, 'yaw_deg': float(np.degrees(np.arctan2(heading[0], heading[2]))), 'pitch_deg': 0, 'roll_deg': 0}),
                            ('lean-and-turn', {'x': .12, 'y': 1.75, 'z': .05, 'yaw_deg': 10, 'pitch_deg': 10, 'roll_deg': -12})]:
            delivery = await self.session.call_tool('set_head_pose', pose)
            assert not delivery.isError, delivery
            await asyncio.sleep(.3)
            self.last_capture = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, label)
            moved = (await self.state())['vr']['frame']
            eye_center = (np.array(moved['left_eye']['origin']) + moved['right_eye']['origin']) / 2
            assert np.allclose(eye_center, [pose['x'], pose['y'], pose['z']], atol=.001), 'Head movement was not rendered'
            if label == 'straighten':
                assert np.allclose(moved['left_eye']['up'], [0, 1, 0], atol=.001), 'Head tilt was not cleared'
            for axis in ('origin', 'right', 'up', 'back'):
                assert np.allclose(moved['panel'][axis], panel[axis], atol=.001), (label, 'Panel followed head', axis)
            assert moved['head_reference'] == head, 'Reference was recaptured during movement'
            self.cases.append({'case': label + '-panel-stays-fixed', 'passed': True, 'frame': moved})
        errors = await asyncio.to_thread(read_game, self.args.pid, 'game.errors')
        assert errors['total'] == 0, errors


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
                                   env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False, 'pid': args.pid}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = Orientation(args, session)
            try:
                await test.run()
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                raise
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result, indent=2))
                await session.call_tool('set_head_pose', {'x': 0, 'y': 1.7, 'z': 0, 'yaw_deg': 0, 'pitch_deg': 0, 'roll_deg': 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
