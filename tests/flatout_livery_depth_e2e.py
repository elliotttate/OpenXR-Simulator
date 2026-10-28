"""Verify an actual livery control moves onto a 20 mm closer stereo plane.

Start in the livery editor with an isolated profile. The game bridge is read
only; all interaction goes through screenshot-targeted simulator XR input.
"""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest, ROOT, screenshot_pixel, ui_surface, ui_frame_at, quaternion_matrix
from flatout_frontend_e2e import read_game


class LiveryDepth(LiveryTest):
    async def run(self):
        if self.args.click:
            initial = await self.state()
            assert initial[self.args.expected_key] != self.args.expected_value, 'Click must cause an observable state transition'
        for label in self.args.target:
            before = await self.state()
            control = next(c for c in before['vr']['controls'] if c['label'] == label and c['enabled'])
            await self.face_control(before, control)
            source = await self.capture('source-' + label)
            hx = control['x'] + control['width'] / 2
            hy = control['y'] + control['height'] / 2
            x, y = screenshot_pixel(before, source, hx, hy)
            base = ui_surface(before, hx, hy)
            aim = {'capture_id': source['capture_id'], 'x': float(x), 'y': float(y),
                   'origin_mode': 'hand', 'hand': self.args.hand, 'surface': base, 'dwell_frames': 45}
            receipt = await self.tool('hover_ui_at', aim, 'hover-' + label)
            assert receipt['status'] == 'input_delivered', receipt
            hovered = await self.state()
            hit = hovered['vr']['hands'][self.args.hand == 'right']
            assert hit['ui_hit'], hit
            assert control['x'] <= hit['u'] * 1920 <= control['x'] + control['width'], hit
            assert control['y'] <= hit['v'] * 1080 <= control['y'] + control['height'], hit
            current = next(c for c in hovered['vr']['controls'] if c['label'] == label)
            assert all(current[k] == control[k] for k in ('x', 'y', 'width', 'height')), 'Hover moved hit bounds'
            raised = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, 'raised-' + label)
            self.last_capture = raised
            basis = quaternion_matrix(base['pose']['orientation'])
            center = (np.array(base['pose']['position']) + basis[:, 0] * (hx / 1920 - .5) * base['size'][0]
                      + basis[:, 1] * (.5 - hy / 1080) * base['size'][1])
            candidates = []
            for tile in raised['geometry']['surfaces']:
                delta = basis.T @ (np.array(tile['pose']['position']) - center)
                if tile['visibility'] == 0 and np.linalg.norm(delta[:2]) < .01:
                    candidates.append({'tile': tile, 'offset_meters': delta.tolist()})
            passed = any(abs(t['offset_meters'][2] - HOVER_LIFT_METERS) < .001 for t in candidates)
            self.cases.append({'case': 'physical-livery-hover-' + label, 'passed': passed,
                               'control': control, 'base': base, 'candidates': candidates,
                               'submitted_surfaces': raised['geometry']['surfaces'],
                               'native_hover': hit})
            assert passed, (label, 'No matching 20mm stereo control plane', candidates)
            if self.args.gpu_capture:
                queued = await asyncio.to_thread(read_game, self.args.pid, 'renderdoc.capture',
                    {'frames': 1, 'path': str(self.args.gpu_capture.resolve()) + '-' + label})
                assert queued['success'], queued
                (self.args.evidence / ('gpu-' + label + '.json')).write_text(json.dumps(queued, indent=2))
                await self.capture('gpu-queued-' + label)
            # A hand-origin ray aimed using the old base image intersects the
            # raised face at a different UV. Check the real parallel plane,
            # rather than demanding the obsolete base-plane UV.
            frame = ui_frame_at(hovered, hx, hy)
            panel = frame['panel']
            normal = -np.array(panel['back'])
            normal /= np.linalg.norm(normal)
            plane = np.array(panel['origin']) + normal * HOVER_LIFT_METERS
            origin, direction = np.array(hit['origin']), np.array(hit['direction'])
            distance = np.dot(plane-origin, normal) / np.dot(direction, normal)
            point = origin + distance * direction
            relative = point - np.array(panel['origin'])
            expected_uv = [.5 + np.dot(relative, panel['right']) / frame['panel_width'],
                           .5 - np.dot(relative, panel['up']) / frame['panel_height']]
            assert max(abs(hit['u']-expected_uv[0])*1920, abs(hit['v']-expected_uv[1])*1080) < 1, (hit, expected_uv)
            assert np.linalg.norm(np.array(hit['end'])-point) < .002, ('Ray misses raised face', hit, point.tolist())
            self.cases.append({'case': 'raised-face-ray-intersection', 'passed': True, 'expected_uv': expected_uv})
            if self.args.raised_edge_top:
                tile = next(t['tile'] for t in candidates if abs(t['offset_meters'][2]-HOVER_LIFT_METERS) < .001)
                edge_state = copy.deepcopy(hovered)
                ui_frame_at(edge_state, hx, hy)['panel']['origin'] = plane.tolist()
                ex, ey = screenshot_pixel(edge_state, raised, hx, control['y'] + .25)
                edge_aim = {'capture_id': raised['capture_id'], 'x': float(ex), 'y': float(ey),
                            'origin_mode': 'hand', 'hand': self.args.hand, 'surface_id': tile['id'], 'dwell_frames': 45}
                edge = await self.tool('hover_ui_at', edge_aim, 'raised-edge-' + label)
                assert edge['status'] == 'input_delivered', edge
                ray = edge['target']['application_ray']
                origin, direction = np.array(ray['position']), np.array(ray['direction'])
                normal = basis[:, 2]
                distance = np.dot(np.array(base['pose']['position'])-origin, normal) / np.dot(direction, normal)
                relative = basis.T @ (origin+distance*direction-np.array(base['pose']['position']))
                base_y = (.5-relative[1]/base['size'][1])*1080
                assert base_y < control['y']-.25, ('Edge test also hits base widget', base_y, control)
                state = await self.state()
                hit = state['vr']['hands'][self.args.hand == 'right']
                assert hit['ui_hit'] and control['y'] <= hit['v']*1080 < control['y']+2, hit
                self.cases.append({'case': 'raised-edge-outside-base-hit', 'passed': True, 'base_y': base_y})
                if self.args.click == label:
                    self.edge_click_aim = edge_aim
                if self.args.drag_off:
                    initial = await self.state()
                    held = await self.tool('hold_ui_at', edge_aim, 'edge-press-before-drag-off')
                    assert held['status'] == 'input_delivered', held
                    pressed = await self.state()
                    assert pressed['tab'] == initial['tab'], 'Button activated before release'
                    ox, oy = screenshot_pixel(before, source, control['x']+control['width']+20, hy)
                    away = {**aim, 'x': float(ox), 'y': float(oy), 'dwell_frames': 30}
                    moved = await self.tool('hold_ui_at', away, 'held-drag-outside-widget')
                    assert moved['status'] == 'input_delivered', moved
                    await self.release(self.args.hand)
                    cancelled = await self.state()
                    assert all(cancelled[k] == initial[k] for k in ('tab', 'document', 'generation')), cancelled
                    self.cases.append({'case': 'genuine-drag-off-cancels-release', 'passed': True})
        if self.args.click:
            if self.args.raised_edge_top:
                assert hasattr(self, 'edge_click_aim'), '--click must be among --target for raised-edge testing'
                command = {**self.edge_click_aim, 'command_id': uuid.uuid4().hex, 'hold_frames': 45}
                click = await self.tool('click_ui_at', command, 'raised-edge-click')
                assert click['status'] == 'input_delivered', click
                after = await self.state()
                retry = await self.tool('click_ui_at', command, 'raised-edge-click-retry')
                assert retry['delivery']['sync_id'] == click['delivery']['sync_id'], retry
                repeated = await self.state()
                assert all(repeated[k] == after[k] for k in ('document', 'generation', 'tab', 'page', 'mode')), repeated
            else:
                after = await self.click(self.args.click, retry=True, hand=self.args.hand)
            assert after[self.args.expected_key] == self.args.expected_value, after
            self.cases.append({'case': 'click-result', 'passed': True,
                               'key': self.args.expected_key, 'value': after[self.args.expected_key]})
        errors = await asyncio.to_thread(read_game, self.args.pid, 'game.errors')
        assert errors['total'] == 0, errors


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
                                   env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False, 'pid': args.pid, 'hand': args.hand}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = LiveryDepth(args, session)
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
    p.add_argument('--pid', required=True, type=int)
    p.add_argument('--data-dir', required=True, type=Path)
    p.add_argument('--evidence', required=True, type=Path)
    p.add_argument('--target', action='append', required=True)
    p.add_argument('--hand', choices=('left', 'right'), default='right')
    p.add_argument('--gpu-capture', type=Path, help='Capture the native atlas draw while the control is raised')
    p.add_argument('--raised-edge-top', action='store_true')
    p.add_argument('--drag-off', action='store_true', help='Hold the raised edge, move outside and release; no activation is allowed')
    p.add_argument('--click')
    p.add_argument('--expected-key')
    p.add_argument('--expected-value', type=json.loads)
    args = p.parse_args()
    if args.click and not args.expected_key:
        p.error('--click requires --expected-key and --expected-value')
    if args.drag_off and (not args.raised_edge_top or args.click):
        p.error('--drag-off requires --raised-edge-top and excludes --click')
    asyncio.run(main(args))
