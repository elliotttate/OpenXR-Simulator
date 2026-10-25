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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import screenshot_pixel, ui_surface


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
        before=await self.sample()
        args=await self.aim(1050,540)
        command={**args,'stick':[0,-.65],'hold_frames':30,'command_id':uuid.uuid4().hex}
        pending=asyncio.create_task(self.tool('scroll_ui_at',command,'smooth-scroll-down'))
        samples=[]
        while not pending.done():
            samples.append(await self.sample())
            await asyncio.sleep(.03)
        result=await pending
        assert result['status']=='input_delivered',result
        # At least one row must move by a fractional row, rather than a page
        # jump or repeated digital menu-selection events.
        initial={r['name']:r['rect'][1] for r in before['rows']}
        offsets=[initial[r['name']]-r['rect'][1] for s in samples for r in s['rows'] if r['name'] in initial]
        smooth=any(1<d<90 and abs(d/92-round(d/92))>.02 for d in offsets)
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
        args=await self.aim(260,268)
        rail_before=await self.sample()
        await self.tool('scroll_ui_at',{**args,'stick':[0,-.65],'hold_frames':12},'outside-scroll-area')
        rail_after=await self.sample()
        if self.args.surface=='options':
            assert rail_after['state']['boxes'][0]['scroll']['position']==rail_before['state']['boxes'][0]['scroll']['position'], 'A ray outside the list scrolled it'
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
    asyncio.run(main(p.parse_args()))
