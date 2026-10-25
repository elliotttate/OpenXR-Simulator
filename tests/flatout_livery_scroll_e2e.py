"""Ray scroll buttons, disabled endpoints and proportional thumbstick input.

Start in the livery editor. This test browses without changing its document.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest, ROOT, screenshot_pixel, ui_surface, game_read


class LiveryScroll(LiveryTest):
    async def press(self, label):
        return await self.click(label,hold_frames=45,retry=True,hand=self.args.hand)

    @staticmethod
    def control(state,label):
        return next(c for c in state['vr']['controls'] if c['label']==label)

    async def disabled(self,label):
        before=await self.state();control=self.control(before,label)
        assert control['enabled'] is False,(label,control)
        park=next(c for c in before['vr']['controls'] if c['label'] in ('BACK TO LAYERS','BACK'))
        source=await self.capture('park-before-grey')
        px,py=screenshot_pixel(before,source,park['x']+park['width']/2,park['y']+park['height']/2)
        await self.tool('hover_ui_at',dict(capture_id=source['capture_id'],x=float(px),y=float(py),
            hand=self.args.hand,origin_mode='hand',surface=ui_surface(before),dwell_frames=8),'park-reticle-away-from-grey-icon')
        capture=await self.capture('grey-'+label.replace(' ','-'))
        x0,y0=screenshot_pixel(before,capture,control['x'],control['y'])
        x1,y1=screenshot_pixel(before,capture,control['x']+control['width'],control['y']+control['height'])
        patch=self.last_pixels[int(min(y0,y1)):int(max(y0,y1))+1,int(min(x0,x1)):int(max(x0,x1))+1].astype(float)
        grey=(np.min(patch,axis=2)>48)&(np.max(patch,axis=2)<150)&(np.ptp(patch,axis=2)<28)
        assert int(np.sum(grey))>=4,('Disabled button needs visible grey ink',label)
        after=await self.press(label)
        for key in ('document','generation','category','catalog_selection','selected','library_scroll','categories_scroll'):
            assert after[key]==before[key],('Disabled direction changed state',label,key)
        self.cases.append(dict(case='grey-and-inert-'+label,passed=True,control=control,grey_pixels=int(np.sum(grey))))

    async def smooth(self):
        before=await self.state()
        control=next(c for c in before['vr']['controls'] if c['label'].startswith('Sticker ') and c['height']>60)
        hx,hy=control['x']+control['width']/2,control['y']+control['height']/2
        capture=await self.capture('stick-scroll-source')
        x,y=screenshot_pixel(before,capture,hx,hy)
        command=dict(capture_id=capture['capture_id'],x=float(x),y=float(y),hand=self.args.hand,
            origin_mode='hand',surface=ui_surface(before),dwell_frames=8,hold_frames=30,
            stick=[0,-.65],timeout=30,command_id=uuid.uuid4().hex)
        samples=[]
        for _ in range(3):
            capture=await self.capture('stick-scroll-step-source')
            command={**command,'capture_id':capture['capture_id'],'hold_frames':4,'command_id':uuid.uuid4().hex}
            receipt=await self.tool('scroll_ui_at',command,'library-thumbstick')
            assert receipt['status']=='input_delivered',receipt
            samples.append((await self.state())['library_scroll']['position'])
        after=await self.state();position=after['library_scroll']['position']
        assert position>before['library_scroll']['position']+1,('Stick did not scroll its hovered grid',samples)
        assert len(set(samples))>=3,('Scrolling jumped rather than moving continuously',samples)
        for key in ('document','generation','category','catalog_selection','selected'):
            assert after[key]==before[key],('Stick also navigated or edited the livery',key)
        repeated=await self.tool('scroll_ui_at',command,'library-scroll-retry')
        assert repeated['delivery']['sync_id']==receipt['delivery']['sync_id']
        await asyncio.sleep(.25)
        assert (await self.state())['library_scroll']['position']==position,'Released stick kept scrolling'
        self.cases.append(dict(case='ray-local-smooth-scroll-'+self.args.hand,passed=True,positions=samples))

    async def run_scroll(self):
        original=await self.state()
        async with self.query_lock:
            catalog=await asyncio.to_thread(game_read,self.args.pid,'catalog')
        if original['tab']!=1:await self.press('STICKERS')
        if (await self.state())['page']!=1:await self.press('ADD STICKER')
        while self.control(await self.state(),'Categories up')['enabled']:await self.press('Categories up')
        if (await self.state())['category']!=0:await self.press('Category: Basic shapes')
        for _ in range(12):
            state=await self.state()
            if not self.control(state,'Stickers up')['enabled']:break
            await self.press('Stickers up')
        await self.disabled('Stickers up');await self.disabled('Categories up')
        state=await self.state();seen=set()
        for _ in range(30):
            state=await self.state()
            seen.update(int(c['label'][8:]) for c in state['vr']['controls'] if c['label'].startswith('Sticker '))
            if not self.control(state,'Stickers down')['enabled']:break
            old=state['library_scroll']['position'];after=await self.press('Stickers down')
            assert after['library_scroll']['position']>old
        else:raise AssertionError('Sticker grid did not reach its lower bound')
        assert seen==set(range(state['filtered_stickers'])),('Some stickers cannot be reached through the arrows',seen)
        await self.disabled('Stickers down')
        self.cases.append(dict(case='all-stickers-reachable-by-ray',passed=True,count=len(seen)))
        while self.control(await self.state(),'Stickers up')['enabled']:await self.press('Stickers up')
        await self.smooth()
        while self.control(await self.state(),'Stickers up')['enabled']:await self.press('Stickers up')
        categories=set()
        category=(await self.state())['category']
        for _ in range(100):
            state=await self.state()
            categories.update(c['label'] for c in state['vr']['controls'] if c['label'].startswith('Category: '))
            assert state['category']==category,'Category paging must not change the selection'
            if not self.control(state,'Categories down')['enabled']:break
            await self.press('Categories down')
        else:raise AssertionError('Category navigation wrapped past its last category')
        await self.disabled('Categories down')
        assert len(categories)==catalog['categories'],('Some categories cannot be reached through the arrows',categories,catalog['categories'])
        self.cases.append(dict(case='category-viewport-bounds-preserve-selection',passed=True,seen=sorted(categories)))
        # Return through the visible list rather than resetting state through MCP.
        while self.control(await self.state(),'Categories up')['enabled']:await self.press('Categories up')
        await self.press('BACK TO LAYERS')
        after=await self.state()
        assert after['document']==original['document'] and after['generation']==original['generation']


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report=dict(passed=False,pid=args.pid,hand=args.hand,scope='Livery bounded ray scrolling without document edits')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=LiveryScroll(args,session)
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
