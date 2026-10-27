"""Native HTML-inspired vehicle browser; input uses screenshot-derived XR rays.

Start in Cars on an isolated profile. Native fixtures only stage the initial
screen. The test never confirms a purchase or launches a race.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT


class CarsHtmlTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects', {'root':'menu', 'max_depth':18}))['nodes']

    async def inactive_click(self, name):
        before, _, args = await self.hover(name)
        args.update(command_id=uuid.uuid4().hex, hold_frames=45)
        delivered = await self.tool('click_ui_at', args, 'inactive-'+name)
        assert delivered['status'] == 'input_delivered', delivered
        after = await self.query('vr.targets')
        assert after['clicks'] == before['clicks'], (name, before, after)
        await self.tool('click_ui_at', args, 'inactive-retry-'+name)
        assert (await self.query('vr.targets'))['clicks'] == before['clicks']
        assert not (await self.query('ui.get_message_box'))['open']
        self.cases.append({'case':'inactive-control-stays-inert', 'target':name, 'passed':True})

    async def check_page(self, label):
        menu = await self.query('menu.get_state')
        targets = (await self.query('vr.targets'))['targets']
        assert not any(t['name'].startswith('legacy.') for t in targets), targets
        assert {t['name'] for t in targets if t['name'].startswith('class.')} == {'class.0','class.1','class.2'}
        first = menu['selection_index']//5*5
        expected = list(range(first, min(first+5, menu['item_count'])))
        assert {t['name'] for t in targets if t['name'].startswith('cars.item.')} == {'cars.item.'+str(i) for i in expected}
        nodes = await self.nodes()
        manifest = json.loads(self.args.layout.read_text())
        g = manifest['geometry']
        for slot, index in enumerate(expected):
            card = next(n for n in nodes if n['path'].endswith('/CarsCard'+str(slot)))
            x = g['RailX']+slot*(g['CardWidth']+g['CardGap'])
            assert all(abs(a-b)<1 for a,b in zip(card['rect'],[x,g['CardY'],x+g['CardWidth'],g['CardY']+g['CardHeight']])), card
            name = next(n for n in nodes if n['path'].endswith('/CarsCard'+str(slot)+'/CarsCardName'))
            assert name['text'] == menu['items'][index]['title'], (name,menu['items'][index])
            photo = next(n for n in nodes if n['path'].endswith('/CarsCard'+str(slot)+'/CarsThumbnail'))
            binding = await self.query('hud.get_node', {'root':'menu','name':photo['path'].removeprefix('MENU_DUMMY/'),'presentation':True})
            item = menu['items'][index]
            skin = 'Skin_20' if item['locked'] else item['skin_name']
            pack = 'DATA:/GUI_FLATOUT/TEXTURES/MENUS/CARS.PSK' if item['locked'] else item['skin_pack']
            path = self.args.fox/'Flatout/Data/Assets'/pack.removeprefix('DATA:/')
            skin_node = next(n for n in ET.parse(str(path)+'.XML').iter('skin') if n.attrib['Name']==skin)
            uv = list(map(float, skin_node.attrib['uvs'].split(';')))
            assert all(abs(a-b)<1e-5 for a,b in zip(uv,binding['presentation']['uv'])), (item,binding,uv)
            # Fitting contains the authored image rather than stretching its car.
            l,t,r,b = photo['rect']
            assert r-l <= g['CardWidth']-27 and b-t <= 87 and l >= card['rect'][0]+13 and t >= g['CardY']+7
        preview = [g['PreviewX'],g['PreviewY'],g['PreviewX']+g['PreviewWidth'],g['PreviewY']+g['PreviewHeight']]
        blockers = [n for n in nodes if '/CarsWorkspace/' in n['path'] and n['kind']=='panel' and n['visible']
                    and n.get('color',{}).get('a',0)>.2 and min(n['rect'][2],preview[2])-max(n['rect'][0],preview[0])>20
                    and min(n['rect'][3],preview[3])-max(n['rect'][1],preview[1])>20]
        assert not blockers, ('A UI panel covers the native car preview',blockers)
        assert all(t['rect'][2]-t['rect'][0]>=79 and t['rect'][3]-t['rect'][1]>=79 for t in targets), targets
        await self.audit(label)
        self.cases.append({'case':'car-page-native-artwork-layout-and-clear-preview','passed':True,'label':label,'indices':expected})

    async def prove(self):
        errors = await self.query('game.errors')
        menu = await self.query('menu.get_state')
        assert menu['current_menu'] == 'CARS', menu
        mode = (await self.query('game.get_state'))['game_mode']
        assert mode in ('career','quick_events'), 'Use an isolated Career or Quick Events fixture; no race will be launched'
        await self.tool('capture_screenshot', {'eye':'both', 'max_width':2048}, 'cars-html-both-eyes')
        nodes = (await self.query('hud.rects', {'root':'menu', 'max_depth':18}))['nodes']
        roots = [n for n in nodes if n['path'].endswith('/CarsWorkspace') and n['visible']]
        assert len(roots) == 1, 'Cars still uses the old carousel instead of the native HTML workspace'
        self.cases.append({'case':'cars-native-html-workspace', 'passed':True})
        for name, font in [('CarsHeading','FO_ALDO_THE_APACHE_DIRT.FNT'),('CarsCard0/CarsCardName','FO_ALDO_THE_APACHE.FNT'),('CarsCard0/CarsCardStatus','FO_MONTSERRAT_REGULAR.FNT')]:
            r = await self.query('hud.get_node', {'root':'menu','name':'Menu/CarsWorkspace/'+name,'presentation':True})
            assert r['presentation']['font_resource'].upper().endswith('/'+font), r
        self.cases.append({'case':'original-native-fonts','passed':True})
        await self.check_page('cars-first-page')
        # Passing over any thumbnail does not commit focus or swap the model's
        # named preview. Each tile remains independently hoverable by the ray.
        selection = menu['selection_index']
        title = next(n['text'] for n in await self.nodes() if n['path'].endswith('/CarsName'))
        for t in (await self.query('vr.targets'))['targets']:
            if not t['name'].startswith('cars.item.'): continue
            await self.hover(t['name'])
            assert (await self.query('menu.get_state'))['selection_index'] == selection
            assert next(n['text'] for n in await self.nodes() if n['path'].endswith('/CarsName')) == title
        self.cases.append({'case':'thumbnail-hover-does-not-commit-or-change-preview','passed':True})
        # Native prices and purchase/money confirmation remain authoritative.
        first = (await self.query('menu.get_state'))['items'][0]
        await self.click('cars.item.1')
        focused = await self.query('menu.get_state')
        assert focused['current_menu']=='CARS' and focused['selection_index']==1
        assert not (await self.query('ui.get_message_box'))['open']
        await asyncio.sleep(.65)
        nodes = await self.nodes()
        assert next(n['text'] for n in nodes if n['path'].endswith('/CarsName')) == focused['items'][1]['title']
        if mode == 'career':
            status = next(n['text'] for n in nodes if n['path'].endswith('/CarsStatus'))
            if 'Price' in status:
                assert int(''.join(c for c in status if c.isdigit())) == focused['items'][1]['price']
                await self.click('helper.Validate.3')
                assert (await self.query('ui.get_message_box'))['open']
                text = [n.get('text','') for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes']]
                names = {t['name'] for t in (await self.query('vr.targets'))['targets']}
                if 'msgbox.cancel' in names:
                    assert any('purchase' in x.lower() for x in text), text
                    await self.click('msgbox.cancel')
                else:
                    assert any('money' in x.lower() or 'cash' in x.lower() for x in text), text
                    await self.click('msgbox.validate')
                assert (await self.query('menu.get_state'))['current_menu']=='CARS'
                self.cases.append({'case':'native-buy-confirmation-or-insufficient-cash-and-safe-dismiss','passed':True,'texts':text})
        locked = next((i['index'] for i in focused['items'] if i['locked'] and i['index']<5), None)
        if locked is not None:
            await self.click('cars.item.'+str(locked))
            assert (await self.query('menu.get_state'))['selection_index']==locked
            await asyncio.sleep(.2)
            nodes = await self.nodes()
            assert not any(n['visible'] and n['path'].split('/')[-1].startswith('CarsStatValue') for n in nodes)
            await self.inactive_click('helper.Validate.3')
            self.cases.append({'case':'locked-car-readable-with-no-stale-ratings-or-activation','passed':True})
        # Traverse every roster entry and both pages in all native classes.
        for cls, expected_class in enumerate(('derby','classic','allstars')):
            await self.click('class.'+str(cls))
            state = await self.query('menu.get_state')
            assert state['items'] and all(i['car_class']==expected_class for i in state['items']), state
            # Move to the start with real arrow clicks; endpoints remain inert.
            for _ in range(state['selection_index']): await self.click('cars.prev')
            await self.inactive_click('cars.prev')
            await self.check_page('cars-class-'+str(cls)+'-first')
            count = (await self.query('menu.get_state'))['item_count']
            for index in range(1,count):
                await self.click('cars.next')
                assert (await self.query('menu.get_state'))['selection_index']==index
                if index%5==0: await self.check_page('cars-class-'+str(cls)+'-page-'+str(index//5))
            await self.inactive_click('cars.next')
            await self.tool('capture_screenshot', {'eye':'both','max_width':2048}, 'cars-class-'+str(cls)+'-last')
        await self.click('class.0')
        state = await self.query('menu.get_state')
        for _ in range(state['selection_index']): await self.click('cars.prev')
        assert (await self.query('menu.get_state'))['selection_index']==0
        await self.click('helper.Y.0')
        targets = (await self.query('vr.targets'))['targets']
        assert len(targets)==1 and targets[0]['name'].startswith('helper.Cancel.'), targets
        await self.click(targets[0]['name'])
        assert any(n['visible'] and n['path'].endswith('/CarsWorkspace') for n in await self.nodes())
        self.cases.append({'case':'orbit-return-restores-native-browser','passed':True})
        # Select the original owned ride, proving the real callback result, then
        # return through Garage. Never confirm a car purchase or start a race.
        assert not first['locked']
        await self.click('helper.Validate.3')
        assert (await self.query('menu.get_state'))['current_menu']=='GARAGE'
        await self.click('item.'+str(0 if mode=='career' else 1))
        final = await self.query('menu.get_state')
        assert final['current_menu']=='CARS' and final['selection_index']==0
        self.cases.append({'case':'native-select-callback-and-garage-round-trip','passed':True})
        assert (await self.query('game.errors'))['total']==errors['total']
        await self.tool('capture_screenshot', {'eye':'both','max_width':2048}, 'cars-native-flow-verified')


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result = {'passed':False, 'scope':'Native car browser, original content and screenshot-derived XR input'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = CarsHtmlTest(args, session)
            try:
                await test.prove()
                result['passed'] = True
            except Exception as error:
                result['error'] = repr(error)
                raise
            finally:
                result['cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', required=True, type=int)
    p.add_argument('--data-dir', required=True, type=Path)
    p.add_argument('--evidence', required=True, type=Path)
    p.add_argument('--layout', required=True, type=Path)
    p.add_argument('--fox', required=True, type=Path)
    p.add_argument('--hand', choices=('left','right'), default='right')
    asyncio.run(run(p.parse_args()))
