"""Native Garage HTML layout, real car data and screenshot-derived XR input.

Start in Garage with an isolated profile. The original Garage route regression
checks all native submenu callbacks, car-body trigger safety and both hands.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT


class GarageHtmlTest(Frontend):
    async def prove(self):
        menu = await self.query('menu.get_state')
        assert menu['current_menu'] == 'GARAGE'
        await self.tool('capture_screenshot', {'eye':'both', 'max_width':2048}, 'garage-html-both-eyes')
        nodes = (await self.query('hud.rects', {'root':'menu', 'max_depth':18}))['nodes']
        roots = [n for n in nodes if n['path'].endswith('/GarageWorkspace') and n['visible']]
        assert len(roots) == 1, 'Garage still has the old short action strip instead of the HTML workspace'
        heading = next(n for n in nodes if n['path'].endswith('/GarageWorkspace/GarageHeading'))
        original_heading = next(n for n in nodes if n['path'].endswith('/ContentZone/Label/TXT_Tips'))
        assert heading['text'] == original_heading['text'] and heading['text'].strip(), ('Garage lost its original localized title',heading,original_heading)
        manifest = json.loads(self.args.layout.read_text(encoding='utf-8'))
        g = manifest['geometry']
        targets = (await self.query('vr.targets'))['targets']
        cards = [n for n in nodes if '/GarageCard' in n['path'] and n['path'].split('/')[-1] in ('GarageCard0','GarageCard1','GarageCard2','GarageCard3') and n['visible']]
        assert len(cards) == 4, cards
        for slot, card in enumerate(sorted(cards, key=lambda n:n['rect'][0])):
            x = g['StageX'] + slot * (g['CardWidth'] + g['CardGap'])
            expected = [x,g['CardY'],x+g['CardWidth'],g['CardY']+g['CardHeight']]
            assert all(abs(a-b)<1 for a,b in zip(card['rect'],expected)), (card,expected)
        career = (await self.query('game.get_state'))['game_mode'] == 'career'
        expected_icons = ('car','tools','gear','paint') if career else ('car','gear','paint','person')
        bindings = []
        for card, icon in zip(sorted(cards,key=lambda n:n['rect'][0]), expected_icons):
            result = await self.query('hud.get_node', {'root':'menu','name':card['path'].removeprefix('MENU_DUMMY/')+'/GarageActionIcon','presentation':True})
            binding = result['presentation']
            assert all(abs(a-b)<1e-6 for a,b in zip(binding['uv'],manifest['sprites'][icon]['uv'])), (icon,binding)
            assert binding['texture_resource'].upper().endswith('/FRONTENDENHANCED/ATLAS.PTX'), binding
            bindings.append({'icon':icon,**binding})
        font_cases = (('GarageHeading','FO_ALDO_THE_APACHE_DIRT.FNT'),
                      ('GarageCard0/GarageActionLabel','FO_ALDO_THE_APACHE.FNT'),
                      ('GarageCard0/GarageActionDescription','FO_MONTSERRAT_REGULAR.FNT'))
        for name, expected in font_cases:
            result = await self.query('hud.get_node', {'root':'menu','name':'Menu/GarageWorkspace/'+name,'presentation':True})
            assert result['presentation']['font_resource'].upper().endswith('/'+expected), result
        self.cases.append({'case':'garage-original-fonts-and-native-action-icon-bindings','passed':True,'icons':bindings})
        assert len([t for t in targets if t['name'].startswith('item.')]) == menu['item_count']
        assert len([t for t in targets if t['name'].startswith('helper.Cancel.')]) == 1
        labels = [n for n in nodes if '/GarageCard' in n['path'] and n['path'].endswith('/GarageActionLabel')]
        titles = {n.get('text','').casefold() for n in labels}
        for item in menu['items']:
            if item['title'].lower() == 'go race':
                continue
            assert item['title'].casefold() in titles, (item,titles)
        gauges = [n for n in nodes if n['path'].split('/')[-1].startswith('GarageStatValue') and n['kind']=='text' and n['visible']]
        assert len(gauges) == 6, gauges
        assert all(0<=int(n['text'])<=100 for n in gauges), gauges
        preview = [g['PreviewX'],g['PreviewY'],g['PreviewX']+g['PreviewWidth'],g['PreviewY']+g['PreviewHeight']]
        # There is no replacement car image/plate in the HTML's car viewport.
        blockers = [n for n in nodes if n['kind']=='panel' and n['visible'] and n.get('color',{}).get('a',0)>.2
                    and min(n['rect'][2],preview[2])-max(n['rect'][0],preview[0])>20
                    and min(n['rect'][3],preview[3])-max(n['rect'][1],preview[1])>20]
        assert not blockers, ('A UI panel covers the live car viewport',blockers)
        self.cases.append({'case':'garage-html-native-cards-ratings-and-clear-car-viewport','passed':True,
                           'cards':cards,'ratings':gauges,'preview':preview})
        await self.garage_buttons()


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result = {'passed':False,'scope':'Native Garage HTML workspace and real XR routes'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = GarageHtmlTest(args,session)
            try:
                await test.prove()
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                raise
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
    print('Garage native HTML layout and XR routes PASS',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--layout',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right')
    asyncio.run(run(p.parse_args()))
