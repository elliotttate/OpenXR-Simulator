"""Check customization categories with real screenshot-derived XR input.

Start in Garage > Customize on the isolated simulator profile. This checks
navigation and previews without buying or equipping customization items.
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
from flatout_frontend_e2e import Frontend, ROOT
from flatout_livery_e2e import ui_surface


def check_category_planes(cases):
    observations=[c for c in cases if c['case']=='category-plane-observed']
    assert {c['category'] for c in observations}=={0,1,2,3}, 'Observe every customization category'
    first=observations[0]['surface']
    maximum=0.
    for observed in observations:
        surface=observed['surface']
        distance=float(np.linalg.norm(np.array(surface['pose']['position'])-first['pose']['position']))
        maximum=max(maximum,distance)
        assert distance<.003, ('Category change moved the action dock',observed['category'],distance)
        assert np.allclose(surface['size'],first['size'],atol=.001), 'Category change resized the action dock'
        assert abs(np.dot(surface['pose']['orientation'],first['pose']['orientation']))>.99999, 'Category change rotated the action dock'
    return {'case':'category-plane-consistency','passed':True,'observations':len(observations),'maximum_displacement_m':maximum}


class CustomizationUI(Frontend):
    async def labels(self,visible_only=False):
        return [n for n in (await self.query('hud.rects', {'root':'menu','max_depth':22}))['nodes']
                if (n['visible'] or not visible_only) and n['path'].endswith('/CustomizationCategory')]

    async def category(self):
        labels=sorted(await self.labels(),key=lambda n:n['rect'][0])
        assert len(labels)==4, ('Four readable category labels are required',labels)
        selected=[i for i,n in enumerate(labels) if n['color']['r']<.2]
        assert len(selected)==1, ('Exactly one category must be selected',labels)
        return selected[0]

    async def layout(self):
        targets=(await self.query('vr.targets'))['targets']
        names=['custo.up',*[f'custo.{i}' for i in range(4)],'custo.down']
        controls=[next((t for t in targets if t['name']==name),None) for name in names]
        await self.capture('customization-category-layout')
        assert all(controls), ('Both endpoint arrows must retain a visible button',controls)
        for i,control in enumerate(controls):
            l,t,r,b=control['rect']
            assert r-l>=79.5 and b-t>=79.5, ('Category controls need full ray targets',control)
            assert 80<=l<r<=1840 and 540<=t<b<=680, ('Keep category navigation above the cards',control)
            if i:
                assert l-controls[i-1]['rect'][2]>=19.5, ('Category controls need separate hit areas',controls[i-1],control)
        labels=await self.labels()
        assert len(labels)==4 and all(n['text'].strip() for n in labels),labels
        category=await self.category()
        nodes=(await self.query('hud.rects', {'root':'menu','max_depth':22}))['nodes']
        for control,enabled in ((controls[0],category>0),(controls[-1],category<3)):
            l,t,r,b=control['rect']
            bars=[n for n in nodes if n['kind']=='panel' and '/RayChevron/' in n['path'] and
                  l<=n['rect'][0]<=n['rect'][2]<=r and t<=n['rect'][1]<=n['rect'][3]<=b]
            assert len(bars)==2, ('Arrow artwork is missing',control,bars)
            for bar in bars:
                color=bar['color']
                correct=(color['r']>.9 and color['g']>.6 and color['b']<.1) if enabled else (
                    max(color[k] for k in ('r','g','b'))-min(color[k] for k in ('r','g','b'))<.01 and .2<color['r']<.4)
                assert correct, ('Arrow must grey out at its endpoint',control,enabled,color)
        cards=[t for t in targets if t['name'].startswith('legacy.')]
        assert cards and min(t['rect'][1] for t in cards)-max(t['rect'][3] for t in controls)>=40, ('Categories crowd the carousel',controls,cards)
        await self.audit('customization-controls-do-not-overlap')
        frame=(await self.query('vr.targets'))['vr_frame']
        self.cases.append({'case':'category-plane-observed','passed':True,'category':category,
                           'surface':ui_surface({'vr':{'frame':frame}}),'frame':frame})
        return controls

    async def inert(self,name):
        before,args=await self.point_args(name)
        category=await self.category()
        args.update(command_id=uuid.uuid4().hex,hold_frames=45,timeout=30)
        clicked=await self.tool('click_ui_at',args,'disabled-'+name)
        repeat=await self.tool('click_ui_at',args,'disabled-retry-'+name)
        assert clicked['status']=='input_delivered'
        assert clicked['delivery']['sync_id']==repeat['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks']==before['clicks'], 'Disabled arrow activated'
        assert await self.category()==category
        self.cases.append({'case':'disabled-endpoint-'+name,'passed':True})

    async def run_customization(self):
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='SKINS' and menu['is_open'], ('Start in Garage > Customize',menu)
        await self.layout()
        initial=await self.category()
        assert initial==0,'Start on Skins'
        await self.inert('custo.up')
        for index in (3,1,2,0):
            before=await self.category()
            menu=await self.query('menu.get_state')
            await self.hover(f'custo.{index}')
            assert await self.category()==before,'Hover switched a category'
            after=await self.query('menu.get_state')
            assert (after['item_count'],after['selection_index'])==(menu['item_count'],menu['selection_index']), 'Tab hover rebuilt the carousel'
            await self.click(f'custo.{index}')
            assert await self.category()==index, 'Tab did not select its category directly'
            await self.layout()
            await self.tool('capture_screenshot',{'eye':'both','max_width':2048},f'category-{index}-both-eyes')
            if index==3:
                await self.inert('custo.down')
        for index in range(1,4):
            await self.click('custo.down')
            assert await self.category()==index
        await self.inert('custo.down')
        for index in range(2,-1,-1):
            await self.click('custo.up')
            assert await self.category()==index
        await self.inert('custo.up')
        await self.orbit()
        assert await self.category()==initial
        await self.layout()
        self.cases.append(check_category_planes(self.cases))
        self.cases.append({'case':'orbit-hides-and-restores-category-navigation','passed':True})

    async def orbit(self):
        initial=await self.query('menu.get_state')
        assert initial['current_menu'] in ('SKINS','CARS','DRIVERS'),initial
        native=await self.query('vr.targets')
        orbit=next(t['name'] for t in native['targets'] if t['name'].startswith('helper.Y.'))
        await self.click(orbit)
        native=await self.query('vr.targets')
        assert len(native['targets'])==1 and native['targets'][0]['name'].startswith('helper.Cancel.'), ('Orbit must retain only its Return action',native)
        assert not await self.labels(visible_only=True), 'Category row covered orbit preview'
        await self.reading_comfort('orbit-return')
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'orbit-return-both-eyes')
        back=native['targets'][0]['name']
        await self.click(back)
        after=await self.query('menu.get_state')
        assert after['current_menu']==initial['current_menu'] and after['selection_index']==initial['selection_index'], (initial,after)
        assert len((await self.query('vr.targets'))['targets'])>1
        self.cases.append({'case':'orbit-ray-return-preserves-selection','passed':True,'menu':initial['current_menu']})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'pid':args.pid,'hand':args.hand}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=CustomizationUI(args,session)
            try:
                assert (await test.query('game.errors'))['total']==0,'Start with a clean native error queue'
                if args.orbit_only:
                    await test.orbit()
                else:
                    await test.run_customization()
                result['native_errors']=await test.query('game.errors',{'count':128})
                assert result['native_errors']['total']==0,result['native_errors']
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--orbit-only',action='store_true',help='From Cars, Drivers or Customize: check the lone ray Return action and restore the current selection')
    asyncio.run(run(p.parse_args()))
