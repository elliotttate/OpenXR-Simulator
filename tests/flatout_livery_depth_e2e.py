"""Verify an actual livery control moves onto a 15 mm closer stereo plane.

Start in the livery editor with an isolated profile. The game bridge is read
only; all interaction goes through screenshot-targeted simulator XR input.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest, ROOT, screenshot_pixel, ui_surface, quaternion_matrix
from flatout_frontend_e2e import read_game


class LiveryDepth(LiveryTest):
    async def run(self):
        for label in self.args.target:
            before = await self.state()
            control = next(c for c in before['vr']['controls'] if c['label'] == label and c['enabled'])
            source = await self.capture('source-' + label)
            hx = control['x'] + control['width'] / 2
            hy = control['y'] + control['height'] / 2
            x, y = screenshot_pixel(before, source, hx, hy)
            base = ui_surface(before)
            aim = {'capture_id': source['capture_id'], 'x': float(x), 'y': float(y),
                   'origin_mode': 'hand', 'hand': self.args.hand, 'surface': base, 'dwell_frames': 45}
            receipt = await self.tool('hover_ui_at', aim, 'hover-' + label)
            assert receipt['status'] == 'input_delivered', receipt
            hovered = await self.state()
            hit = hovered['vr']['hands'][self.args.hand == 'right']
            assert hit['ui_hit'] and max(abs(hit['u'] * 1920 - hx), abs(hit['v'] * 1080 - hy)) < 1, hit
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
            passed = any(abs(t['offset_meters'][2] - .015) < .001 for t in candidates)
            self.cases.append({'case': 'physical-livery-hover-' + label, 'passed': passed,
                               'control': control, 'base': base, 'candidates': candidates,
                               'submitted_surfaces': raised['geometry']['surfaces'],
                               'native_hover': hit})
            assert passed, (label, 'No matching 15mm stereo control plane', candidates)
        if self.args.click:
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
    p.add_argument('--click')
    p.add_argument('--expected-key')
    p.add_argument('--expected-value', type=json.loads)
    args = p.parse_args()
    if args.click and not args.expected_key:
        p.error('--click requires --expected-key and --expected-value')
    asyncio.run(main(args))
