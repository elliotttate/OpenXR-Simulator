"""Screenshot -> real hand hover -> 20 mm submitted face -> one click/retry.

Start in the desired non-Wreck menu. No game-side input injection is used.
The existing game pipe supplies read-only target and presentation diagnostics.
"""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import numpy as np
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel
from flatout_livery_e2e import ui_surface, quaternion_matrix
from flatout_ui_depth_contract import assert_reading_distance


class MenuDepth(Frontend):
    async def run(self):
        initial = await self.query('vr.targets')
        assert 'WRECK' not in initial.get('menu', '').upper(), 'Wreck Run is deferred'
        names = (self.args.target or []) + ['legacy.' + str(i) for i in self.args.legacy_index]
        names = names or [t['name'] for t in initial['targets'] if t.get('enabled', True)]
        for name in names:
            before = await self.query('vr.targets')
            legacy_index = int(name.split('.')[1]) if name.startswith('legacy.') else None
            if legacy_index is not None:
                legacy_selection = (await self.query('menu.get_state'))['selection_index']
                mapping = await self.query('ui.vr_button_map')
                button = next(b for b in mapping['buttons'] if b['index'] == legacy_index)
                assert button['hits'] and button['visible'], button
                target = {'name': name, 'rect': button['top_left'] + button['bottom_right']}
            else:
                target = next(t for t in before['targets'] if t['name'] == name)
            assert target.get('enabled', True), ('Disabled control requested', name)
            source = await self.capture('source-' + name)
            frame = before.get('vr_frame')
            if frame and (await self.query('menu.get_state'))['is_3d']:
                frame = None
            # An RTT menu has no native base quad, even when its hover tile exists.
            if frame:
                base = ui_surface({'vr': {'frame': frame}})
                base['id'] = 'world-space-carrier'
                carrier_args = {'surface': {k: base[k] for k in ('pose', 'size')}}
            else:
                base = max(source['geometry']['surfaces'], key=lambda s: np.prod(s['size']))
                carrier_args = {'surface_id': base['id']}
                if self.args.reading_distance:
                    check = assert_reading_distance(source, self.args.reading_distance, self.args.reading_width)
                    self.cases.append({'case': 'reading-distance', 'passed': True, **check})
            projection = copy.deepcopy(source)
            projection['geometry']['surfaces'] = [{**base, 'visibility': 0}]
            x, y = quad_pixel(projection, target['rect'])
            aim = {'capture_id': source['capture_id'], 'x': float(x), 'y': float(y),
                   'hand': self.args.hand, 'origin_mode': 'hand', 'dwell_frames': 45, **carrier_args}
            hovered = await self.tool('hover_ui_at', aim, 'hover-' + name)
            assert hovered['status'] == 'input_delivered', hovered
            current = await self.query('vr.targets')
            assert current['clicks'] == before['clicks'], current
            if legacy_index is not None:
                if any(t['name'] == name for t in current['targets']):
                    # Carousels preview on hover and commit selection on click.
                    assert current['hovered'] == name, current
                    assert (await self.query('menu.get_state'))['selection_index'] == legacy_selection, 'Hover committed carousel selection'
                    assert next(t['rect'] for t in current['targets'] if t['name'] == name) == target['rect'], 'Hover moved hit bounds'
                else:
                    menu = await self.query('menu.get_state')
                    assert menu['selection_index'] == legacy_index, menu
            else:
                assert current['hovered'] == name, current
                assert next(t['rect'] for t in current['targets'] if t['name'] == name) == target['rect'], 'Hover moved hit bounds'
            raised = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, 'raised-' + name)
            self.last_capture = raised
            tiles = [s for s in raised['geometry']['surfaces'] if s['id'] != base['id'] and s['visibility'] == 0]
            basis = quaternion_matrix(base['pose']['orientation'])
            normal = basis[:, 2]
            lifts = [float(np.dot(np.array(s['pose']['position']) - base['pose']['position'], normal)) for s in tiles]
            l, t, r, b = target['rect']
            point = (np.array(base['pose']['position']) + basis[:, 0] * ((l+r)/3840-.5) * base['size'][0]
                     + basis[:, 1] * (.5-(t+b)/2160) * base['size'][1])
            matching = []
            for tile, lift in zip(tiles, lifts):
                local = quaternion_matrix(tile['pose']['orientation']).T @ (point-np.array(tile['pose']['position']))
                if abs(lift-HOVER_LIFT_METERS) < .001 and all(abs(local[i]) <= tile['size'][i]/2+.002 for i in (0, 1)):
                    matching.append(tile)
            passed = bool(matching)
            if not passed:
                self.cases.append({'case': 'physical-hover-' + name, 'passed': False,
                                   'lift_meters': lifts, 'source_rect': target['rect'], 'base': base, 'tiles': tiles})
            assert passed, (name, 'No 20mm physical lift', lifts)
            assert len(tiles) <= 2, ('Unbounded transient layers', tiles)
            if self.args.gpu_capture:
                queued = await self.query('renderdoc.capture', {'frames': 1,
                    'path': str(self.args.gpu_capture.resolve()) + '-' + name.replace('.', '-')})
                assert queued['success'], queued
                (self.args.evidence / ('gpu-' + name + '.json')).write_text(json.dumps(queued, indent=2))
                await self.capture('gpu-queued-' + name)
            self.cases.append({'case': 'physical-hover-' + name, 'passed': True, 'lift_meters': lifts,
                               'source_rect': target['rect'], 'base': base, 'tiles': tiles})
            if name == getattr(self.args, 'inert_target', None):
                menu_before = await self.query('menu.get_state')
                message_before = await self.query('ui.get_message_box')
                held = await self.tool('hold_ui_at', aim, 'locked-hold-' + name)
                assert held['status'] == 'input_delivered', held
                after = await self.query('vr.targets')
                assert after['clicks'] == before['clicks'], 'Locked preview activated'
                held_tiles = {s['id']: s for s in held['capture']['geometry']['surfaces']}
                for tile in matching:
                    assert tile['id'] in held_tiles, 'Locked highlight disappeared on press'
                    assert np.allclose(held_tiles[tile['id']]['pose']['position'], tile['pose']['position'], atol=.001), 'Locked preview used active press travel'
                await self.tool('release_ui_pointer', {'capture_id': held['capture']['capture_id'], 'hand': self.args.hand}, 'locked-release-' + name)
                menu_after = await self.query('menu.get_state')
                assert menu_after['current_menu'] == menu_before['current_menu']
                assert menu_after['selection_index'] == menu_before['selection_index'], 'Locked preview committed selection'
                assert (await self.query('ui.get_message_box')) == message_before, 'Locked preview opened a dialog'
                self.cases.append({'case': 'locked-preview-stays-inert-' + name, 'passed': True})
        if self.args.click:
            await self.click(self.args.click)
            after = await self.query('menu.get_state')
            assert after['current_menu'] == self.args.expected_menu, after
            self.cases.append({'case': 'click-result', 'passed': True, 'menu': after['current_menu']})
        errors = await self.query('game.errors')
        self.cases.append({'case': 'game-error-diagnostics', 'diagnostics': errors})
        assert errors['total'] == 0, errors


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False, 'pid': args.pid, 'hand': args.hand}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = MenuDepth(args, session)
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--hand', choices=('left', 'right'), default='right')
    p.add_argument('--gpu-capture', type=Path, help='Capture the native atlas draw while the control is raised')
    p.add_argument('--target', action='append')
    p.add_argument('--legacy-index', action='append', type=int, default=[])
    p.add_argument('--inert-target', help='Also hold/release this locked preview and require no activation or press travel')
    p.add_argument('--click')
    p.add_argument('--expected-menu')
    p.add_argument('--reading-distance', type=float)
    p.add_argument('--reading-width', type=float, default=4.5)
    args = p.parse_args()
    if args.click and not args.expected_menu:
        p.error('--click requires --expected-menu')
    asyncio.run(main(args))
