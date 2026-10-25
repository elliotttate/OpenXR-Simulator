"""Screenshot -> real hand hover -> 15 mm submitted face -> one click/retry.

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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel
from flatout_livery_e2e import ui_surface, quaternion_matrix
from flatout_ui_depth_contract import assert_reading_distance


class MenuDepth(Frontend):
    async def run(self):
        initial = await self.query('vr.targets')
        assert 'WRECK' not in initial.get('menu', '').upper(), 'Wreck Run is deferred'
        names = self.args.target or [t['name'] for t in initial['targets'] if t.get('enabled', True)]
        for name in names:
            before = await self.query('vr.targets')
            target = next(t for t in before['targets'] if t['name'] == name)
            assert target.get('enabled', True), ('Disabled control requested', name)
            source = await self.capture('source-' + name)
            frame = before.get('vr_frame')
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
            assert current['hovered'] == name and current['clicks'] == before['clicks'], current
            assert next(t['rect'] for t in current['targets'] if t['name'] == name) == target['rect'], 'Hover moved hit bounds'
            raised = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 4096}, 'raised-' + name)
            self.last_capture = raised
            tiles = [s for s in raised['geometry']['surfaces'] if s['id'] != base['id'] and s['visibility'] == 0]
            normal = quaternion_matrix(base['pose']['orientation'])[:, 2]
            lifts = [float(np.dot(np.array(s['pose']['position']) - base['pose']['position'], normal)) for s in tiles]
            assert any(abs(lift - .015) < .001 for lift in lifts), (name, 'No 15mm physical lift', lifts)
            assert len(tiles) <= 2, ('Unbounded transient layers', tiles)
            self.cases.append({'case': 'physical-hover-' + name, 'passed': True, 'lift_meters': lifts,
                               'source_rect': target['rect'], 'base': base, 'tiles': tiles})
        if self.args.click:
            await self.click(self.args.click)
            after = await self.query('menu.get_state')
            assert after['current_menu'] == self.args.expected_menu, after
            self.cases.append({'case': 'click-result', 'passed': True, 'menu': after['current_menu']})
        errors = await self.query('game.errors')
        self.cases.append({'case': 'game-error-diagnostics', 'diagnostics': errors})


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
    p.add_argument('--target', action='append')
    p.add_argument('--click')
    p.add_argument('--expected-menu')
    p.add_argument('--reading-distance', type=float)
    p.add_argument('--reading-width', type=float, default=4.5)
    args = p.parse_args()
    if args.click and not args.expected_menu:
        p.error('--click requires --expected-menu')
    asyncio.run(main(args))
