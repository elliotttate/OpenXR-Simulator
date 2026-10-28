"""Verify shared native ray actions retain the game's font and visible lettering.

Creates one presentation-only Yes/No dialog; its actions have no game callback.
Input uses screenshot-derived XR rays. Staging and failure cleanup are explicit
native fixture operations, never presented as end-to-end interaction evidence.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel
from flatout_livery_e2e import world_pixel

sys.path.insert(0, str(ROOT / 'tools'))
from flatout_ui_atlas import Atlas


class ActionStyle(Frontend):
    async def check(self):
        fixture = Atlas(self.args, self.session, {'operations': []})
        assert not (await self.query('ui.get_message_box'))['open']
        await fixture.query('ui.pop_message_box', {
            'type': 'YesNoNoTitle', 'title': 'UI action style verification',
            'content': 'Presentation test only. These actions do not change game progress.'})
        try:
            await asyncio.sleep(.3)
            # Native staging can suppress rays temporarily. Deliberately move
            # the real ray, then measure the settled ray layout from a new image.
            _, aim = await self.point_args('msgbox.validate')
            aim['dwell_frames'] = 90
            await self.tool('hover_ui_at', aim, 'enter-ray-presentation')
            await self.hover('msgbox.validate')
            captured = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 2048}, 'ray-action-both-eyes')
            image = np.asarray(Image.open(self.last_image_path).convert('RGB'))
            nodes = (await self.query('hud.rects', {'root': 'msgbox', 'max_depth': 20}))['nodes']
            labels = [n for n in nodes if n['path'].endswith('/RayActionLabel') and n['visible']]
            assert len(labels) == 2, labels
            fonts = []
            for label in labels:
                info = await self.query('hud.get_node', {
                    'root': 'msgbox', 'name': label['path'].split('/', 1)[1], 'presentation': True})
                fonts.append(info['presentation']['font_resource'])
            self.cases.append({'case': 'shared-actions-original-game-font', 'fonts': fonts,
                               'passed': all(f.upper().endswith('/FO_ALDO_THE_APACHE.FNT') for f in fonts)})
            assert self.cases[-1]['passed'], self.cases[-1]
            # Test actual dark glyphs on the gold primary plate in each eye.
            # The bright red reticle cannot satisfy this luminance/chroma test.
            label = next(n for n in labels if n['text'].lower() == 'yes')
            native = await self.query('vr.targets')
            (self.args.evidence/'native-labels.json').write_text(json.dumps({'labels': labels, 'targets': native}, indent=2))
            world_frame = native.get('vr_frame')
            composition_base = any(s['size'][0] >= 2 for s in captured['geometry']['surfaces'] if s['visibility'] == 0)
            def project(x, y, eye):
                if composition_base:
                    return quad_pixel(captured, (x, y, x, y), eye)
                assert world_frame, 'Missing base carrier; a raised tile is not a full canvas'
                panel = world_frame['panel']
                point = (np.array(panel['origin']) + np.array(panel['right'])*(x/1920-.5)*world_frame['panel_width']
                         + np.array(panel['up'])*(.5-y/1080)*world_frame['panel_height'])
                return world_pixel({'vr': {'frame': world_frame}}, captured, point, eye)
            ink = []
            for eye in ('left', 'right'):
                l, t, r, b = label['rect']
                p = project(l+3, t+3, eye)
                q = project(r-3, b-3, eye)
                x0, y0 = np.floor(np.minimum(p, q)).astype(int)
                x1, y1 = np.ceil(np.maximum(p, q)).astype(int)
                crop = image[y0:y1, x0:x1]
                assert crop.size and crop.shape[0] > 5 and crop.shape[1] > 5, (eye, p, q)
                count = int(np.sum(crop.max(axis=2) < 100))
                ink.append({'eye': eye, 'dark_letter_pixels': count})
            self.cases.append({'case': 'shared-primary-lettering-in-both-rendered-eyes', 'samples': ink,
                               'passed': all(i['dark_letter_pixels'] >= 12 for i in ink)})
            assert self.cases[-1]['passed'], self.cases[-1]
            await self.click('msgbox.cancel')
            assert not (await self.query('ui.get_message_box'))['open']
        finally:
            if (await self.query('ui.get_message_box'))['open']:
                await fixture.query('ui.confirm_message_box', {'cancel': True})
            if self.last_capture:
                await self.tool('release_ui_pointer', {'capture_id': self.last_capture['capture_id'],
                                                     'hand': self.args.hand}, 'release-action-test-ray')


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    args.output = args.evidence
    args.current = False
    args.owner_queue = None
    args.native_timeout = 8
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    result = {'passed': False}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = ActionStyle(args, session)
            try:
                await test.check()
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                raise
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--hand', choices=('left', 'right'), default='right')
    asyncio.run(run(parser.parse_args()))
