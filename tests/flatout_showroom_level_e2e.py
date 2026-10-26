"""Verify a level showroom base camera while real head movement and rays remain usable.

Start in an initialized non-Wreck showroom on an isolated simulator profile.
This test only hovers controls; it never buys, equips, saves or starts an event.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import numpy as np
from flatout_frontend_e2e import Frontend, ROOT, ClientSession, StdioServerParameters, stdio_client

sys.path.insert(0, str(ROOT / 'tools'))
from flatout_ui_atlas import Atlas, tracking_head_pose, validate_capture_pose


def assert_level(camera):
    basis = np.column_stack([camera[k] for k in ('x', 'y', 'z')])
    assert np.allclose(basis[:, 1], [0, 0, 1], atol=1e-5), ('Artificial showroom pitch or roll', camera)
    assert abs(basis[2, 0]) < 1e-5 and abs(basis[2, 2]) < 1e-5, camera
    assert np.allclose(basis.T @ basis, np.eye(3), atol=1e-5), ('Showroom basis lost its scale', camera)
    return basis


def assert_rendered_eyes(vr):
    camera = vr['base_camera']
    basis = assert_level(camera)
    assert len(vr['eyes']) == 2, vr
    for eye in vr['eyes']:
        tracked = eye['tracking_pose']
        position = np.array(camera['position']) + basis @ tracked['position']
        direction = -(basis @ tracked['z'])
        assert np.allclose(eye['render_position'], position, atol=.001), ('Head translation is not one-to-one', eye, position.tolist())
        assert np.allclose(eye['render_direction'], direction, atol=.001), ('Physical head rotation was flattened', eye, direction.tolist())


class ShowroomLevel(Frontend):
    async def release_hands(self):
        if not self.last_capture:
            return
        for hand in ('left', 'right'):
            result = await self.tool('release_ui_pointer', {'capture_id': self.last_capture['capture_id'], 'hand': hand}, 'release-' + hand)
            assert result['status'] == 'input_delivered' and not result['delivery']['button_down'], result

    async def run_level(self, report):
        menu = await self.query('menu.get_state')
        assert menu['current_menu'] == self.args.menu and not menu['is_3d'], menu
        assert not (await self.query('ui.get_message_box'))['open'], 'Start without a modal'
        original = await self.query('vr.get_state')
        pose = tracking_head_pose(original)
        assert_rendered_eyes(original)
        camera = original['base_camera']
        await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 2048}, 'level-showroom-both-eyes')
        report['initial'] = {'menu': menu, 'vr': original}
        self.cases.append({'case': 'level-base-and-both-rendered-eyes', 'passed': True, 'camera': camera})
        native = await self.query('vr.targets')
        back = next(t['name'] for t in native['targets'] if t['name'].startswith('helper.Cancel.'))
        other = next(t['name'] for t in native['targets'] if t['name'] != back and t.get('enabled', True))
        for hand, target in (('left', back), ('right', other)):
            previous = self.args.hand
            try:
                self.args.hand = hand
                await self.hover(target)
                assert (await self.query('vr.targets'))['hovered'] == target
                assert_rendered_eyes(await self.query('vr.get_state'))
                self.cases.append({'case': hand + '-ray-remains-aligned', 'passed': True, 'target': target})
            finally:
                self.args.hand = previous
        await self.release_hands()
        framing = Atlas(self.args, self.session, report)
        moved = {**pose, 'x': pose['x'] + .08, 'y': pose['y'] + .1, 'z': pose['z'] + .04,
                 'yaw_deg': pose['yaw_deg'] + 6, 'pitch_deg': -8, 'roll_deg': 3}
        try:
            await framing.set_head_pose(moved, 'showroom physical head movement')
            capture = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 2048}, 'physical-head-pitch-and-roll')
            validate_capture_pose(capture, moved)
            current = await self.query('vr.get_state')
            assert_rendered_eyes(current)
            assert all(np.allclose(current['base_camera'][key], camera[key], atol=1e-4)
                       for key in ('position', 'x', 'y', 'z')), ('Showroom base followed physical head movement', current)
            self.cases.append({'case': 'physical-head-rotation-and-translation-preserved', 'passed': True, 'vr': current})
        finally:
            await framing.set_head_pose(pose, 'showroom original head restore')
        assert (await self.query('menu.get_state'))['current_menu'] == menu['current_menu'], 'Hover activated navigation'
        assert (await self.query('vr.targets'))['clicks'] == native['clicks'], 'Hover activated a control'


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    args.output, args.current, args.owner_queue, args.native_timeout = args.evidence, False, None, 8
    report = {'passed': False, 'pid': args.pid, 'menu': args.menu, 'scope': 'Level non-Wreck showroom camera; both eyes, both rays, physical head movement'}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'mcp-server/openxr_simulator_mcp.py')],
                                  env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = ShowroomLevel(args, session)
            try:
                await test.run_level(report)
                report['native_errors'] = await test.query('game.errors')
                assert report['native_errors']['total'] == 0, report['native_errors']
                report['passed'] = True
            except Exception as error:
                report['error'] = repr(error)
                raise
            finally:
                report['cases'] = test.cases
                (args.evidence / 'result.json').write_text(json.dumps(report, indent=2))
                await test.release_hands()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--menu', choices=('CARS', 'GARAGE', 'UPGRADES', 'SKINS', 'DRIVERS'), required=True)
    parser.add_argument('--hand', default='right', choices=('left', 'right'))
    asyncio.run(run(parser.parse_args()))
