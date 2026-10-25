"""Options footer padding and complete ray paging; no game-side input injection.

Start in Options. Every category/page/row is reached through real screenshot
coordinates and XR actions. Native diagnostics are read-only layout/state oracles.
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
import numpy as np
from PIL import Image

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET, quad_pixel
from flatout_livery_e2e import screenshot_pixel, ui_surface


class FooterTest(Frontend):
    async def footer(self):
        state=await self.query('options.get_state')
        assert state['open']
        targets=(await self.query('vr.targets'))['targets']
        controls=[t for t in targets if t['name'].startswith(('helper.','settings.scroll.'))]
        capture=await self.capture('options-footer-padding')
        # Hover growth is centred on the same row; compare centres rather than
        # rejecting the deliberately larger hovered button.
        centres={round((t['rect'][1]+t['rect'][3])/2,1) for t in controls}
        aligned=len(centres)==1
        self.cases.append(dict(case='paging-and-footer-actions-share-one-row',passed=aligned,controls=controls))
        assert aligned,'Paging and Back/Reset need one aligned action row with consistent bottom padding'
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':14}))['nodes']
        body=next(n for n in nodes if n['path'].endswith('/OptionsSettings') and n['visible'])
        assert not any(n['path'].endswith('/OptionsFooter') and n['visible'] for n in nodes),'Use separate buttons, without a full footer background'
        pixels=np.asarray(Image.open(self.last_image_path).convert('RGB'))
        observations=[]
        for control in controls:
            a,c,d,e=control['rect']
            assert a>=24 and d<=1896 and e<=1056,('Footer needs24px outer margins',control)
            assert c>=body['rect'][3]+23.5,('Paging must stay below settings with a clear gap',control,body)
            assert e-c>=72,('Ray target too short',control)
            assert d-a>=160,('Up/down and footer buttons need spacious ray targets',control)
            if capture['geometry']['surfaces']:
                p=quad_pixel(capture,(a,c,a,c));q=quad_pixel(capture,(d,e,d,e))
            else:
                frame=(await self.query('vr.targets')).get('vr_frame') or state['vr_frame']
                native={'vr':{'frame':frame}}
                p=screenshot_pixel(native,capture,a,c);q=screenshot_pixel(native,capture,d,e)
            x0,x1=sorted((round(p[0]),round(q[0])));y0,y1=sorted((round(p[1]),round(q[1])))
            crop=pixels[max(0,y0):min(len(pixels),y1),max(0,x0):min(pixels.shape[1],x1)].astype(float)
            assert crop.size,control
            white=int(((crop.min(2)>155)&(crop.max(2)-crop.min(2)<35)).sum())
            dark=float((crop.mean(2)<80).mean())
            gold=float(((crop[:,:,0]>185)&(crop[:,:,1]>115)&(crop[:,:,2]<65)).mean())
            dark_ink=int((crop.max(2)<70).sum())
            observations.append(dict(target=control['name'],white_text_pixels=white,dark_button_fraction=dark,
                                     gold_button_fraction=gold,dark_ink_pixels=dark_ink))
            visible=(white>=8 and dark>.4) or (control['name'].startswith('helper.Validate.') and gold>.4 and dark_ink>=8)
            assert visible,('Clickable button must actually render its label and plate',observations[-1])
        rows=[t for t in targets if '.row' in t['name']]
        assert all(row['rect'][3]<=body['rect'][3]-23.5 for row in rows),('Settings need bottom padding',rows,body)
        box=state['boxes'][state['selection_index']]
        first,last,total=(box['scroll'][k] for k in ('first','last','total'))
        expected={'settings.scroll.up':first>0,'settings.scroll.down':last+1<total}
        for name,enabled in expected.items():
            assert any(c['name']==name for c in controls)==enabled,(name,enabled,controls)
        self.cases.append(dict(case='separate-buttons-visible-and-spaced',passed=True,settings=body,scroll=box['scroll'],pixels=observations))
        if self.args.check_only:return
        # A disabled arrow remains visible at the end of a scrollable category,
        # but cannot activate another control beneath it or change any setting.
        if total<=7:return
        for name,node_name in [('settings.scroll.up','OptionsPageUp'),('settings.scroll.down','OptionsPageDown')]:
            if expected[name]:continue
            node=next(n for n in nodes if n['path'].endswith('/'+node_name) and n['visible'])
            labels=[n for n in nodes if n['path'].startswith(node['path']+'/') and n['kind']=='text' and n['visible']]
            assert labels,('Disabled direction must remain visible',node)
            for label in labels:
                color=[label['color'][channel] for channel in ('r','g','b')]
                assert max(color)<.8 and max(color)-min(color)<.15,('Unavailable direction must be grey',label)
            capture=await self.capture('disabled-'+node_name)
            a,c,d,e=node['rect'];px,py=(a+d)/2,(c+e)/2
            aim={'capture_id':capture['capture_id'],'hand':self.args.hand,'origin_mode':'hand','ray_offset':RAY_OFFSET,'dwell_frames':12}
            if capture['geometry']['surfaces']:
                x,y=quad_pixel(capture,(px,py,px,py))
            else:
                frame=(await self.query('vr.targets')).get('vr_frame') or state['vr_frame']
                native={'vr':{'frame':frame}}
                x,y=screenshot_pixel(native,capture,px,py);aim['surface']=ui_surface(native)
            aim.update(x=float(x),y=float(y))
            before=await self.query('options.get_state');clicks=(await self.query('vr.targets'))['clicks']
            await self.tool('hover_ui_at',aim,'disabled-arrow-hover')
            command={**aim,'hold_frames':45,'command_id':uuid.uuid4().hex,'timeout':30}
            delivered=await self.tool('click_ui_at',command,'disabled-arrow-held')
            assert delivered['status']=='input_delivered',delivered
            retried=await self.tool('click_ui_at',command,'disabled-arrow-retry')
            assert delivered['delivery']['sync_id']==retried['delivery']['sync_id']
            after=await self.query('options.get_state')
            assert after['selection_index']==before['selection_index']
            assert after['boxes'][after['selection_index']]['scroll']==before['boxes'][before['selection_index']]['scroll']
            assert [[r['value'] for r in b['rows']] for b in after['boxes']]==[[r['value'] for r in b['rows']] for b in before['boxes']]
            assert (await self.query('vr.targets'))['clicks']==clicks
            self.cases.append(dict(case='disabled-arrow-cannot-click-through',passed=True,target=name,hand=self.args.hand))

    async def options_redesign_layout(self):
        await super().options_redesign_layout()
        await self.footer()

    async def paging(self):
        original=await self.query('options.get_state')
        values=[[r['value'] for r in b['rows']] for b in original['boxes']]
        for index in range(len(original['boxes'])):
            await self.click('settings.category.'+str(index))
            for _ in range(10):
                box=(await self.query('options.get_state'))['boxes'][index]
                if box['scroll']['first']==0:break
                await self.click('settings.scroll.up')
            else:raise AssertionError('Cannot reach first settings page')
            seen=set()
            for _ in range(10):
                box=(await self.query('options.get_state'))['boxes'][index]
                await self.options_redesign_layout()
                first,last,total=(box['scroll'][k] for k in ('first','last','total'))
                visible=[r['index'] for r in box['rows'] if r['visible']]
                seen.update(visible)
                if visible:
                    await self.hover(f'box{index}.row{visible[0]}',(.1,.5))
                    if len(visible)>1:await self.hover(f'box{index}.row{visible[-1]}',(.1,.5))
                if last+1>=total:break
                await self.click('settings.scroll.down')
                after=(await self.query('options.get_state'))['boxes'][index]
                assert after['scroll']['first']==last+1,after
            else:raise AssertionError('Cannot reach last settings page')
            present={r['index'] for r in box['rows'] if r['present']}
            assert seen==present,(index,seen,present)
            after=await self.query('options.get_state')
            assert [[r['value'] for r in b['rows']] for b in after['boxes']]==values
            self.cases.append(dict(case='all-pages-and-grey-endpoints',passed=True,category=index,seen=sorted(seen),hand=self.args.hand))
            print(box['title'],len(seen),'settings paged without edits PASS',flush=True)
        await self.click('settings.category.0')


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result=dict(passed=False,pid=args.pid,hand=args.hand,scope='Options padded footer, every category/page and disabled arrow through actual XR rays')
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=FooterTest(args,session)
            try:
                if args.check_only:await test.footer()
                elif args.paging_only:await test.paging()
                else:await test.options()
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand},'final-release-'+hand)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--hand',choices=['left','right'],default='right')
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--paging-only',action='store_true',help='Check all pages/endpoints and representative row targeting with the other hand')
    asyncio.run(main(parser.parse_args()))
