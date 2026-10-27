"""Native career classes: original art/data, ray focus and one-shot callbacks."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT
from flatout_ui_depth_contract import assert_reading_distance


class ClassesTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def layout(self,label):
        state=await self.query('menu.get_state');targets=(await self.query('vr.targets'))['targets']
        assert state['current_menu']=='FLATOUT' and state['item_count']==3
        assert not state['is_3d']
        assert {t['name'] for t in targets}=={'item.0','item.1','item.2','helper.Cancel.ClassBack','helper.Validate.ClassContinue'}
        nodes=await self.nodes()
        root=next(n for n in nodes if n['path'].endswith('/ClassWorkspace'))
        assert root['visible']
        for n in nodes:
            if n['path'].endswith(('/Content_Zone','/Helpers','/Header')):assert not n['visible'],n['path']
        assert all(20<=t['rect'][0]<t['rect'][2]<=1900 and 20<=t['rect'][1]<t['rect'][3]<=1060 for t in targets)
        assert all(t['rect'][3]-t['rect'][1]>=104 for t in targets)
        cards=[next(t for t in targets if t['name']=='item.'+str(i))['rect'] for i in range(3)]
        assert cards[1][0]-cards[0][2]>=20 and cards[2][0]-cards[1][2]>=20
        def text(card,name):return next(n['text'] for n in nodes if n['path'].endswith('/ClassCard'+str(card)+'/'+name))
        for i,item in enumerate(state['items']):
            assert text(i,'ClassName')==item['title']
            assert text(i,'ClassDescription')
            assert text(i,'ClassCups') and text(i,'ClassEvents')
            assert text(i,'ClassProgress')==str(self.class_percent[i])+'%'
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/ClassWorkspace/ClassHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        packs=('LEVELS_DESERT','LEVELS_FACTORY','LEVELS_FOREST');skins=('Ghost Town','Water Canal 2','Forest')
        for i in range(3):
            node=await self.query('hud.get_node',{'root':'menu','name':'Menu/ClassWorkspace/ClassCard'+str(i)+'/ClassTrackPhoto','presentation':True})
            source=self.args.fox/'Flatout/Data/Assets/GUI_FlatOut/Textures/Menus'/(packs[i]+'.PSK.XML')
            skin=next(n for n in ET.parse(source).iter('skin') if n.attrib['Name']==skins[i])
            lo=list(map(float,skin.attrib['uvs'].split(';')));uv=node['presentation']['uv']
            assert lo[0]-1e-5<=uv[0]<uv[2]<=lo[2]+1e-5 and lo[1]-1e-5<=uv[1]<uv[3]<=lo[3]+1e-5
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-both-eyes')
        assert_reading_distance(capture,3.,4.5)
        await self.audit(label)
        self.cases.append({'case':'native-class-layout-original-data-and-art','passed':True,'targets':targets})
        return state

    async def cancel(self):
        name=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(name)

    async def prove(self):
        # Compare against the independently exposed native career progress.
        await self.click('helper.Cancel.ClassBack')
        hub=(await self.query('menu.get_state'))['career_hub']
        self.class_percent=hub['class_percent']
        await self.click('career.hero')
        original=await self.layout('classes-entry');selected=original['selection_index']
        bounds={t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}
        for name in bounds:
            await self.hover(name,(.5,.8))
            assert (await self.query('menu.get_state'))['selection_index']==selected,'Hover committed a class'
            assert {t['name']:t['rect'] for t in (await self.query('vr.targets'))['targets']}==bounds
            self.cases.append({'case':'visual-only-stable-hover-'+name,'passed':True})
        for action in ('item.'+str(selected),'helper.Validate.ClassContinue'):
            await self.click(action)
            grid=await self.query('menu.get_state')
            assert 'career' in grid and grid['career']['tiles']
            assert not (await self.query('game.get_state'))['is_loading']
            await self.cancel()
            await self.layout('classes-return-'+action.replace('.','-'))
        if self.args.wrong_class_flow:
            assert self.args.fixture_slot==641062,'Wrong-class/shop flow is restricted to the isolated starter profile'
            self.cases.append({'case':'isolated-profile-preflight','context':hub,'selection':selected})
            assert selected==0 and hub['credits']==0 and hub['car'].casefold()=='grosse pointe'
            for index in (1,2):
                await self.click('item.'+str(index))
                assert (await self.query('ui.get_message_box'))['open']
                assert {t['name'] for t in (await self.query('vr.targets'))['targets']}=={'msgbox.validate'}
                await self.click('msgbox.validate')
                cars=await self.query('menu.get_state')
                assert cars['current_menu']=='CARS' and cars['items']
                expected=('classic','allstars')[index-1]
                assert all(item['car_class']==expected for item in cars['items']),cars
                await self.cancel()
                await self.layout('classes-return-from-forced-shop-'+str(index))
            # Restore the native Derby class via its ordinary callback.
            await self.click('item.0');await self.cancel()
            self.cases.append({'case':'both-wrong-car-class-warnings-and-native-shop-return-without-purchase','passed':True})
        await self.click('helper.Cancel.ClassBack')
        after=(await self.query('menu.get_state'))['career_hub']
        assert all(after[k]==hub[k] for k in ('credits','wins','owned_cars','progress_percent','class_percent','car'))
        assert (await self.query('game.errors'))['total']==0
        self.cases.append({'case':'original-career-history-and-profile-preserved','passed':True})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'scope':'Native class selection and screenshot-derived XR input'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=ClassesTest(args,session)
            try:await t.prove();result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['cases']=t.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True)
    p.add_argument('--hand',default='right',choices=('left','right'))
    p.add_argument('--wrong-class-flow',action='store_true');p.add_argument('--fixture-slot',type=int)
    asyncio.run(run(p.parse_args()))
