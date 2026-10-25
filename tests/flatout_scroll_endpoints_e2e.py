"""Reach both ends of native menus through screenshot-derived XR ray input."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel
from flatout_livery_e2e import screenshot_pixel, ui_surface


SURFACES = {
    'statistics': ('statistics.get_state', 'stats.scroll.up', 'stats.scroll.down',
                   ((1620, 68, 1700, 140), (1740, 68, 1820, 140))),
    'party': ('party.get_state', 'stunts.page.previous', 'stunts.page.next',
              ((1620, 56, 1700, 128), (1740, 56, 1820, 128))),
    'challenge': ('menu.get_state', 'challenge.page.prev', 'challenge.page.next',
                  ((1424, 92, 1520, 188), (1696, 92, 1792, 188))),
}


class ScrollEndpoints(Frontend):
    async def grey(self, side):
        method, previous, following, rects = SURFACES[self.args.surface]
        name = (previous, following)[side]
        native = await self.query('vr.targets')
        assert name not in [t['name'] for t in native['targets']], native
        # Keep the reticle clear of the disabled icon for the pixel oracle.
        park = next(t['name'] for t in native['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.hover(park)
        before = await self.query(method)
        capture = await self.capture('grey-' + name)
        rect = rects[side]
        if capture['geometry']['surfaces']:
            project = lambda x, y: quad_pixel(capture, (x, y, x, y))
            surface = None
        else:
            frame = {'vr': {'frame': native['vr_frame']}}
            project = lambda x, y: screenshot_pixel(frame, capture, x, y)
            surface = ui_surface(frame)
        (x0, y0), (x1, y1) = project(*rect[:2]), project(*rect[2:])
        pixels = np.asarray(Image.open(self.last_image_path).convert('RGB')).astype(float)
        patch = pixels[int(min(y0, y1)):int(max(y0, y1))+1,
                       int(min(x0, x1)):int(max(x0, x1))+1]
        grey = (np.min(patch, axis=2) > 58) & (np.max(patch, axis=2) < 155) & (np.ptp(patch, axis=2) < 28)
        assert int(np.sum(grey)) >= 8, ('Disabled arrow is not visibly grey', name)
        px, py = project((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        aim = dict(capture_id=capture['capture_id'], x=float(px), y=float(py),
                   hand=self.args.hand, origin_mode='hand', dwell_frames=12, timeout=30)
        if surface:
            aim['surface'] = surface
        await self.tool('hover_ui_at', aim, 'disabled-hover')
        clicks = (await self.query('vr.targets'))['clicks']
        command = {**aim, 'hold_frames': 45, 'command_id': uuid.uuid4().hex}
        receipt = await self.tool('click_ui_at', command, 'disabled-held')
        retry = await self.tool('click_ui_at', command, 'disabled-retry')
        assert receipt['delivery']['sync_id'] == retry['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks'] == clicks
        assert await self.query(method) == before, ('Disabled arrow changed menu state', name)
        self.cases.append(dict(case='visible-grey-inert-' + name, passed=True,
                               grey_pixels=int(np.sum(grey)), state=before))

    async def run_endpoints(self):
        method, previous, following, _ = SURFACES[self.args.surface]
        for _ in range(100):
            if previous not in [t['name'] for t in (await self.query('vr.targets'))['targets']]:
                break
            await self.click(previous)
        else:
            raise AssertionError('Could not reach first page')
        await self.grey(0)
        pages = [await self.query(method)]
        for _ in range(100):
            if following not in [t['name'] for t in (await self.query('vr.targets'))['targets']]:
                break
            await self.click(following)
            current = await self.query(method)
            assert current != pages[-1], ('Paging did not advance', following)
            pages.append(current)
        else:
            raise AssertionError('Could not reach last page')
        await self.grey(1)
        self.cases.append(dict(case='both-ray-paging-endpoints', passed=True, pages=pages))


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    report = dict(passed=False, pid=args.pid, hand=args.hand, surface=args.surface,
                  scope='Visible grey inert endpoint buttons through real XR rays')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = ScrollEndpoints(args, session)
            try:
                await test.run_endpoints()
                report['passed'] = True
            except Exception as error:
                report['error'] = repr(error)
                raise
            finally:
                report['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(report, indent=2))
                if test.last_capture:
                    for hand in ('left', 'right'):
                        await session.call_tool('release_ui_pointer',
                            {'capture_id': test.last_capture['capture_id'], 'hand': hand})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--hand', choices=['left', 'right'], default='right')
    parser.add_argument('--surface', choices=SURFACES, required=True)
    asyncio.run(main(parser.parse_args()))
