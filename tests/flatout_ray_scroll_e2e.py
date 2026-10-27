"""Verify that a screenshot-aimed thumbstick scroll is continuous and local.

Start in Options, at the top of General. Input goes through actual OpenXR
thumbstick action snapshots; the native bridge is read-only.
"""
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
from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import screenshot_pixel, ui_surface


def statistics_body_ink(capture,image_path):
    pixels=np.asarray(Image.open(image_path).convert('RGB')).astype(float)
    (x0,y0),(x1,y1)=[quad_pixel(capture,(x,y,x,y)) for x,y in ((115,186),(1760,898))]
    patch=pixels[int(min(y0,y1)):int(max(y0,y1))+1,int(min(x0,x1)):int(max(x0,x1))+1]
    ink=(np.min(patch,axis=2)>155)&(np.ptp(patch,axis=2)<35)
    count=int(np.sum(ink))
    assert count>=100,('Statistics descriptions and values must render legibly',count)
    return count


def statistics_body_glyphs(capture,image_path):
    pixels=np.asarray(Image.open(image_path).convert('RGB')).astype(float)
    heights=[]
    for row in (2,3,4,5):
        top=186+row*51+7
        (x0,y0),(x1,y1)=[quad_pixel(capture,(x,y,x,y)) for x,y in ((135,top),(1200,top+38))]
        patch=pixels[int(y0):int(y1)+1,int(x0):int(x1)+1]
        ink=(patch.min(2)>155)&(np.ptp(patch,axis=2)<35)
        rows=np.where(ink.sum(1)>=3)[0]
        if len(rows):
            heights.append(int(rows[-1]-rows[0]+1))
    assert heights and float(np.median(heights))>=6,('Statistics body glyphs are too small in the actual eye image',heights)
    return heights


class RayScroll(Frontend):
    async def aim(self, x, y):
        native = await self.query('vr.targets')
        capture = await self.capture('scroll-source')
        args = dict(capture_id=capture['capture_id'], hand=self.args.hand,
                    origin_mode='hand', ray_offset=RAY_OFFSET, dwell_frames=8, timeout=30)
        if capture['geometry']['surfaces']:
            x,y = quad_pixel(capture,(x,y,x,y))
        else:
            frame={'vr':{'frame':native['vr_frame']}}
            x,y = screenshot_pixel(frame,capture,x,y)
            args['surface']=ui_surface(frame)
        return {**args,'x':float(x),'y':float(y)}

    async def sample(self):
        state=await self.query('statistics.get_state' if self.args.surface=='statistics' else 'options.get_state')
        targets=await self.query('vr.targets')
        rows=[t for t in targets['targets'] if t['name'].startswith('line.') or
              (t['name'].startswith('box0.row') and '.' not in t['name'][5:])]
        return dict(state=state, rows=rows, clicks=targets['clicks'])

    async def run_scroll(self):
        assert (await self.query('menu.get_state'))['current_menu']==('PLAYER STATS' if self.args.surface=='statistics' else 'OPTIONS')
        if self.args.surface=='options':
            await self.click('settings.category.0')
            while any(t['name']=='settings.scroll.up' for t in (await self.query('vr.targets'))['targets']):
                await self.click('settings.scroll.up')
        before=await self.sample()
        args=await self.aim(1050,540)
        if self.args.surface=='statistics':
            count=statistics_body_ink(self.last_capture,self.last_image_path)
            self.cases.append(dict(case='statistics-body-text-readable',passed=True,bright_pixels=count))
            if before['state']['scroll_position']<.5:
                heights=statistics_body_glyphs(self.last_capture,self.last_image_path)
                self.cases.append(dict(case='statistics-body-glyph-size',passed=True,ink_heights=heights))
        command={**args,'stick':[0,-.65],'hold_frames':self.args.hold_frames,'command_id':uuid.uuid4().hex}
        samples=[]
        for _ in range(3):
            args=await self.aim(1050,540)
            command={**args,'stick':[0,-.65],'hold_frames':self.args.hold_frames,'command_id':uuid.uuid4().hex}
            result=await self.tool('scroll_ui_at',command,'smooth-scroll-down')
            assert result['status']=='input_delivered',result
            samples.append(await self.sample())
        # At least one row must move by a fractional row, rather than a page
        # jump or repeated digital menu-selection events.
        initial={r['name']:r['rect'][1] for r in before['rows']}
        offsets=[initial[r['name']]-r['rect'][1] for s in samples for r in s['rows'] if r['name'] in initial]
        tops=sorted({r['rect'][1] for r in before['rows']})
        assert len(tops)>=2, 'Need adjacent visible rows to measure the native scroll stride'
        row_height=float(np.median(np.diff(tops)))
        smooth=any(d>1 and abs(d/row_height-round(d/row_height))>.02 for d in offsets)
        self.cases.append(dict(case='fractional-row-motion-from-native-stick',passed=smooth,samples=samples))
        assert smooth, 'Stick over settings did not produce continuous row motion'
        after=await self.sample()
        assert after['clicks']==before['clicks'],'Scrolling must not click a setting'
        values=lambda s:[[r.get('value') for r in b['rows']] for b in s['boxes']]
        if self.args.surface=='options':
            assert values(after['state'])==values(before['state']),'Scrolling changed an option'
        retry=await self.tool('scroll_ui_at',command,'scroll-idempotent-retry')
        assert retry['delivery']['sync_id']==result['delivery']['sync_id']
        await asyncio.sleep(.2)
        assert (await self.sample())['rows']==after['rows'],'Neutral stick must stop immediately'
        # Hover the category rail with the same stick: it must not scroll or
        # change categories, even if the spare hand rests over the list.
        args=await self.aim(260,150 if self.args.surface=='statistics' else 268)
        rail_before=await self.sample()
        await self.tool('scroll_ui_at',{**args,'stick':[0,-.65],'hold_frames':12},'outside-scroll-area')
        rail_after=await self.sample()
        if self.args.surface=='options':
            assert rail_after['state']['boxes'][0]['scroll']['position']==rail_before['state']['boxes'][0]['scroll']['position'], 'A ray outside the list scrolled it'
        else:
            assert rail_after['state']==rail_before['state'],'A ray outside Statistics scrolled or navigated it'
        self.cases.append(dict(case='outside-region-and-neutral-inert',passed=True))


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report=dict(passed=False,pid=args.pid,hand=args.hand,scope='Native ray-local proportional thumbstick scrolling')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=RayScroll(args,session)
            try:
                await test.run_scroll();report['passed']=True
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                report['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=['left','right'],default='right')
    p.add_argument('--surface',choices=['options','statistics'],default='options')
    p.add_argument('--hold-frames',type=int,choices=range(1,91),default=12,
                   help='Action-sync cycles per pulse; allow the native UI update to observe the axis')
    asyncio.run(main(p.parse_args()))
