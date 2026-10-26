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


def assert_world_upright(frame):
    panel = frame['panel']
    assert np.allclose(panel['up'], [0, 0, 1], atol=.001), ('Showroom pitch or roll remained in panel', panel['up'])
    assert abs(panel['right'][2]) < .001 and abs(panel['back'][2]) < .001, panel


def assert_level_tracking_space(frame):
    camera = frame['camera']
    assert np.allclose(camera['up'], [0, 0, 1], atol=.001), ('Authored view tilted VR tracking space', camera)
    assert abs(camera['right'][2]) < .001 and abs(camera['back'][2]) < .001, camera


class ViewOrientation(LiveryTest):
    async def run(self):
        for label in ('Roof', 'Left', 'Right', 'Front', 'Rear', 'STUDIO'):
            before = await self.state()
            after = await self.click(label, retry=True)
            assert after['camera_view'] != before['camera_view'], (label, 'View did not change')
            assert_world_upright(after['vr']['frame'])
            assert_level_tracking_space(after['vr']['frame'])
            self.cases.append({'case': 'world-upright-' + label, 'passed': True, 'frame': after['vr']['frame']})
        errors = await asyncio.to_thread(read_game, self.args.pid, 'game.errors')
        assert errors['total'] == 0, errors


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
        self.cases.append({'case': 'world-upright-after-tilted-entry', 'frame': frame})
        assert_world_upright(frame)
        assert_level_tracking_space(frame)
        heading = np.array(head['back']) * [1, 0, 1]
        heading /= np.linalg.norm(heading)
        world_heading = (camera @ heading) * [1, 1, 0]
        world_heading /= np.linalg.norm(world_heading)
        assert np.dot(-np.array(panel['back']), world_heading) > .999, 'Opening horizontal heading changed'
        opening_eye = np.array(frame['camera']['origin']) + camera @ head['origin']
        assert np.allclose(np.array(panel['origin']) - opening_eye,
                           -world_heading * frame['panel_distance'], atol=.001)
        for label, pose in [('straighten', {'x': 0, 'y': 1.7, 'z': 0, 'yaw_deg': 0, 'pitch_deg': 0, 'roll_deg': 0}),
                            ('face-opening-heading', {'x': 0, 'y': 1.7, 'z': 0, 'yaw_deg': float(np.degrees(np.arctan2(heading[0], heading[2]))), 'pitch_deg': 0, 'roll_deg': 0}),
                            ('lean-and-turn', {'x': .12, 'y': 1.75, 'z': .05, 'yaw_deg': 10, 'pitch_deg': 10, 'roll_deg': -12})]:
            delivery = await self.session.call_tool('set_head_pose', pose)
            assert not delivery.isError, delivery
            await asyncio.sleep(.3)
            self.last_capture = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, label)
            moved = (await self.state())['vr']['frame']
            assert_world_upright(moved)
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
    if args.saved_state:
        state = json.loads(args.saved_state.read_text())
        report = {'passed': False, 'source': str(args.saved_state), 'frame': state['vr']['frame']}
        try:
            assert_world_upright(state['vr']['frame'])
            report['passed'] = True
        except AssertionError as error:
            report['error'] = repr(error)
            raise
        finally:
            (args.evidence/'result.json').write_text(json.dumps(report, indent=2))
        return
    assert args.pid and args.data_dir, 'Live test requires --pid and --data-dir'
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
                                   env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False, 'pid': args.pid}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = (ViewOrientation if args.views_only else Orientation)(args, session)
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
    parser.add_argument('--pid', type=int)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--saved-state', type=Path, help='Check a saved native livery state without input')
    parser.add_argument('--views-only', action='store_true', help='Click every showroom view and require world-vertical UI')
    parser.add_argument('--evidence', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
