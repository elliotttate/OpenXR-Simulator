"""Exercise the real leaderboard's first/last ray scrolling endpoints."""
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
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel, RAY_OFFSET


class LeaderboardScroll(Frontend):
    async def layout(self):
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        legacy=[n for n in nodes if n['visible'] and
                (n['path'].endswith(('/ArrowUp','/ArrowDown')) or '/Header/Bloc_Nav' in n['path'])]
        assert not legacy,('Legacy leaderboard arrows/header remain visible',legacy)
        native=await self.query('vr.targets')
        arrows=[t for t in native['targets'] if t['name'].startswith('page.')]
        assert arrows and all(t['rect'][2]-t['rect'][0]>=80 and
                              t['rect'][3]-t['rect'][1]>=72 for t in arrows),arrows
        self.cases.append(dict(case='matched-header-arrows-without-legacy-controls',passed=True,targets=arrows))

    async def settled(self):
        for _ in range(120):
            state=await self.query('leaderboard.get_state')
            if not state['busy'] and not (await self.query('ui.get_message_box'))['open']:
                errors=await self.query('game.errors',{'count':128})
                if errors['total']:
                    (self.args.evidence/'native-errors.json').write_text(json.dumps(errors,indent=2))
                assert errors['total']==0,('Native error while browsing leaderboard',errors)
                await asyncio.sleep(.1)
                return state
            await asyncio.sleep(.25)
        raise AssertionError('Leaderboard did not finish loading')

    async def grey(self, direction):
        before=await self.settled()
        native=await self.query('vr.targets')
        assert 'page.'+direction not in [t['name'] for t in native['targets']],native
        capture=await self.capture('grey-'+direction)
        assert capture['geometry']['surfaces'],'Leaderboard should use the readable head-height quad'
        x=1660 if direction=='up' else 1780
        corners=[quad_pixel(capture,(a,b,a,b)) for a,b in ((x-40,96),(x+40,168))]
        (x0,y0),(x1,y1)=corners
        pixels=np.asarray(Image.open(self.last_image_path).convert('RGB')).astype(float)
        patch=pixels[int(min(y0,y1)):int(max(y0,y1))+1,int(min(x0,x1)):int(max(x0,x1))+1]
        grey=(np.min(patch,axis=2)>58)&(np.max(patch,axis=2)<155)&(np.ptp(patch,axis=2)<28)
        assert int(np.sum(grey))>=8,('Unavailable direction has no visible grey arrow',direction)
        px,py=quad_pixel(capture,(x,132,x,132))
        aim=dict(capture_id=capture['capture_id'],x=float(px),y=float(py),hand=self.args.hand,
                 origin_mode='hand',ray_offset=RAY_OFFSET,dwell_frames=12,timeout=30)
        await self.tool('hover_ui_at',aim,'grey-hover-'+direction)
        command={**aim,'command_id':uuid.uuid4().hex,'hold_frames':45}
        clicks=(await self.query('vr.targets'))['clicks']
        receipt=await self.tool('click_ui_at',command,'grey-held-'+direction)
        repeat=await self.tool('click_ui_at',command,'grey-retry-'+direction)
        assert receipt['delivery']['sync_id']==repeat['delivery']['sync_id']
        after=await self.settled()
        assert (await self.query('vr.targets'))['clicks']==clicks
        assert after==before,('Disabled arrow changed leaderboard data',before,after)
        self.cases.append(dict(case='visible-grey-inert-'+direction,passed=True,state=after,grey_pixels=int(np.sum(grey))))

    async def run_scroll(self):
        assert (await self.query('menu.get_state'))['current_menu']=='LEADERBOARD'
        initial=await self.settled()
        assert initial['entries']>0 and initial['first_rank']==1,initial
        await self.layout()
        await self.grey('up')
        pages=[initial]
        for _ in range(200):
            native=await self.query('vr.targets')
            if 'page.down' not in [t['name'] for t in native['targets']]:break
            old=pages[-1]
            await self.click('page.down')
            after=await self.settled()
            assert after['first_rank']>old['first_rank'],('Page down failed to advance',old,after)
            pages.append(after)
            print('Leaderboard page',len(pages),'rank',after['first_rank'],'/',after['total'],flush=True)
        else:raise AssertionError('Leaderboard did not reach its end within 200 pages')
        final=pages[-1]
        assert final['first_rank']+final['entries']-1>=final['total'],final
        await self.grey('down')
        await self.layout()
        self.cases.append(dict(case='all-real-leaderboard-pages-reachable',passed=True,pages=pages))


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report=dict(passed=False,pid=args.pid,hand=args.hand,scope='Actual Steam leaderboard pages and visible grey ray endpoints')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=LeaderboardScroll(args,session)
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
    asyncio.run(main(p.parse_args()))
