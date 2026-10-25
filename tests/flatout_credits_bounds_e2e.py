"""Credits rewind must be visibly grey and inert at the beginning of the roll.

Start in Credits. Only screenshot-derived XR actions move the reader; the
native bridge supplies read-only state and rendered widget diagnostics.
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


class CreditsBounds(Frontend):
    async def reader(self):
        state=await self.query('menu.get_state')
        assert state['current_menu']=='CREDITS',state
        return state['credits']

    async def run_bounds(self):
        state=await self.reader()
        if not state['paused']:await self.click('helper.Y.0')
        for _ in range(8):
            state=await self.reader()
            if abs(state['position'])<.5:break
            _,aim=await self.point_args('helper.LB.1')
            result=await self.tool('hold_ui_at',{**aim,'dwell_frames':60,'timeout':30},'rewind-to-start')
            assert result['status']=='input_delivered',result
            await self.tool('release_ui_pointer',{'capture_id':aim['capture_id'],'hand':self.args.hand},'rewind-release')
        else:raise AssertionError('Could not reach the beginning of Credits')
        top=await self.reader();assert top['paused'] and abs(top['position'])<.5,top
        targets=await self.query('vr.targets')
        inactive=not any(t['name']=='helper.LB.1' for t in targets['targets'])
        self.cases.append(dict(case='rewind-disabled-at-start',passed=inactive,reader=top,targets=targets))
        assert inactive,'Rewind must be disabled at the beginning of Credits'
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':14}))['nodes']
        label=next(n for n in nodes if n.get('text','').strip().upper()=='REWIND' and n['visible'])
        color=[label['color'][c] for c in ('r','g','b')]
        assert max(color)<.8 and max(color)-min(color)<.15,('Unavailable rewind must be grey',label)
        capture=await self.capture('credits-rewind-grey-at-start')
        l,t,r,b=label['rect'];px,py=(l+r)/2,(t+b)/2
        aim={'capture_id':capture['capture_id'],'hand':self.args.hand,'origin_mode':'hand','ray_offset':RAY_OFFSET,'dwell_frames':12}
        if capture['geometry']['surfaces']:x,y=quad_pixel(capture,(px,py,px,py))
        else:
            native={'vr':{'frame':targets['vr_frame']}}
            x,y=screenshot_pixel(native,capture,px,py);aim['surface']=ui_surface(native)
        aim.update(x=float(x),y=float(y))
        clicks=targets['clicks'];audio=await self.query('audio.ui_feedback')
        await self.tool('hover_ui_at',aim,'disabled-rewind-hover')
        command={**aim,'command_id':uuid.uuid4().hex,'hold_frames':45,'timeout':30}
        delivered=await self.tool('click_ui_at',command,'disabled-rewind-held')
        assert delivered['status']=='input_delivered',delivered
        retried=await self.tool('click_ui_at',command,'disabled-rewind-retry')
        assert delivered['delivery']['sync_id']==retried['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks']==clicks
        assert (await self.query('audio.ui_feedback'))['click']==audio['click']
        assert abs((await self.reader())['position'])<.5
        self.cases.append(dict(case='grey-rewind-hold-and-retry-inert',passed=True,label=label))
        _,forward=await self.point_args('helper.RB.2')
        await self.tool('hold_ui_at',{**forward,'dwell_frames':20,'timeout':30},'forward-away-from-start')
        await self.tool('release_ui_pointer',{'capture_id':forward['capture_id'],'hand':self.args.hand},'forward-release')
        away=await self.reader();assert away['position']<-1 and away['paused'],away
        assert any(t['name']=='helper.LB.1' for t in (await self.query('vr.targets'))['targets'])
        self.cases.append(dict(case='rewind-reenables-after-forward-scroll',passed=True,reader=away))


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report=dict(passed=False,pid=args.pid,hand=args.hand,scope='Credits grey rewind endpoint and ray-only recovery')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=CreditsBounds(args,session)
            try:
                await test.run_bounds();report['passed']=True
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                report['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand},'final-release-'+hand)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=['left','right'],default='right')
    asyncio.run(main(p.parse_args()))
