"""Verify native driver pages and Party choices using screenshot-derived XR rays.

Start in a fresh, isolated Party Drivers fixture. Never confirm the
Start Race dialog. The native fixture stages the menu; all tested input is XR.
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


class DriversTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def inert(self,name):
        before,_,args=await self.hover(name)
        args.update(command_id=uuid.uuid4().hex,hold_frames=45)
        await self.tool('click_ui_at',args,'disabled-'+name)
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        await self.tool('click_ui_at',args,'disabled-retry-'+name)
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        self.cases.append({'case':'disabled-held-click-and-retry','target':name,'passed':True})

    async def check(self,label):
        menu=await self.query('menu.get_state')
        assert menu['current_menu']=='DRIVERS'
        nodes=await self.nodes()
        assert any(n['path'].endswith('/DriversWorkspace') and n['visible'] for n in nodes),'Missing native DriversWorkspace'
        targets=(await self.query('vr.targets'))['targets']
        assert not any(t['name'].startswith('legacy.') for t in targets),targets
        start=menu['selection_index']//6*6
        indices=list(range(start,min(start+6,menu['item_count'])))
        assert {t['name'] for t in targets if t['name'].startswith('drivers.item.')}=={'drivers.item.'+str(i) for i in indices}
        for slot,i in enumerate(indices):
            card=next(n for n in nodes if n['path'].endswith('/DriversCard'+str(slot)))
            x=self.g['RailX']+slot*(self.g['CardWidth']+self.g['CardGap'])
            assert all(abs(a-b)<1 for a,b in zip(card['rect'],[x,self.g['CardY'],x+self.g['CardWidth'],self.g['CardY']+self.g['CardHeight']])),card
            name=next(n for n in nodes if n['path'].endswith('/DriversCard'+str(slot)+'/DriversCardName'))
            assert name['text']==menu['items'][i]['title'] and name['text'],(name,menu['items'][i])
            path='Menu/DriversWorkspace/DriversCard'+str(slot)+'/DriversPortrait'
            actual=await self.query('hud.get_node',{'root':'menu','name':path,'presentation':True})
            item=menu['items'][i];skin=item['skin_name']
            pack='HUD_OPPONENTS2' if skin.startswith('CRASHTESTDUMMY') or skin=='JACK_BENTON' else 'HUD_OPPONENTS'
            xml=self.args.fox/'Flatout/Data/Assets/GUI_FlatOut/Textures/HUD'/(pack+'.PSK.XML')
            authored=next(n for n in ET.parse(xml).iter('skin') if n.attrib['Name']==skin)
            assert all(abs(a-b)<1e-5 for a,b in zip(map(float,authored.attrib['uvs'].split(';')),actual['presentation']['uv']))
            badge=next((n for n in nodes if n['path'].endswith('/DriversCard'+str(slot)+'/DriversLock')),None)
            assert bool(badge and badge['visible'])==item['locked'],(item,badge)
            portrait=next(n for n in nodes if n['path'].endswith('/DriversCard'+str(slot)+'/DriversPortrait'))
            assert portrait['color']['r']<.7 if item['locked'] else portrait['color']['r']>.99
        lineup=next(n for n in nodes if n['path'].endswith('/DriversLineup'))
        rows=[n for n in nodes if '/DriversLineup/DriversPlayer' in n['path'] and n['visible']]
        assert rows and min(lineup['rect'][3]-n['rect'][3] for n in rows)>=20,'Lineup bottom padding'
        protected=[self.g['PreviewX'],self.g['PreviewY'],self.g['PreviewX']+self.g['PreviewWidth'],self.g['PreviewY']+self.g['PreviewHeight']]
        assert not [n for n in nodes if '/DriversWorkspace/' in n['path'] and n['kind']=='panel' and n['visible'] and n.get('color',{}).get('a',0)>.2
            and min(n['rect'][2],protected[2])-max(n['rect'][0],protected[0])>20 and min(n['rect'][3],protected[3])-max(n['rect'][1],protected[1])>20]
        assert all(t['rect'][2]-t['rect'][0]>=79 and t['rect'][3]-t['rect'][1]>=79 for t in targets)
        await self.audit(label)
        self.cases.append({'case':'native-portraits-page-and-clear-preview','indices':indices,'passed':True})

    async def prove(self):
        self.g=json.loads(self.args.layout.read_text())['geometry']
        assert (await self.query('party.get_state'))['players']==self.args.players,'Use the matching fresh Party fixture'
        errors=await self.query('game.errors')
        await self.check('drivers-first-page')
        font=await self.query('hud.get_node',{'root':'menu','name':'Menu/DriversWorkspace/DriversHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in font['presentation']['font_resource'].upper(),font
        before=await self.query('menu.get_state')
        await self.hover('drivers.item.1');await asyncio.sleep(.4)
        assert (await self.query('menu.get_state'))['selection_index']==before['selection_index'],'Hover swapped the driver'
        await self.click('drivers.item.1')
        assert (await self.query('menu.get_state'))['selection_index']==1
        assert next(n for n in await self.nodes() if n['path'].endswith('/DriversFooter2/DriversFooterLabel'))['text']=='Locked'
        await self.inert('helper.Validate.3')
        await self.inert('drivers.prev')
        seen=set(range(6))
        menu=await self.query('menu.get_state')
        while (menu['selection_index']//6+1)*6<menu['item_count']:
            await self.click('drivers.next');await self.check('drivers-page')
            menu=await self.query('menu.get_state');first=menu['selection_index']//6*6;seen.update(range(first,min(first+6,menu['item_count'])))
        assert seen==set(range(menu['item_count']))
        await self.inert('drivers.next')
        while (await self.query('menu.get_state'))['selection_index']//6>0:
            await self.click('drivers.prev')
        await self.click('drivers.item.0')
        await self.click('helper.Y.0')
        names={t['name'] for t in (await self.query('vr.targets'))['targets']}
        assert names=={'helper.Cancel.0'},names
        await self.click('helper.Cancel.0');await self.check('drivers-after-orbit')
        await self.click('helper.Validate.3')
        assert not (await self.query('ui.get_message_box'))['open']
        nodes=await self.nodes();first=next(n for n in nodes if n['path'].endswith('/DriversPlayer0'))
        assert (await self.query('menu.get_state'))['items'][0]['title'] in first['text'],first
        player=next(n for n in nodes if n['path'].endswith('/DriversCurrentPlayer'))
        assert '2' in player['text'],player
        await self.click('helper.Cancel.4')
        assert '1' in next(n for n in await self.nodes() if n['path'].endswith('/DriversCurrentPlayer'))['text']
        for i in range(self.args.players):
            await self.click('helper.Validate.3')
            assert (await self.query('ui.get_message_box'))['open']==(i==self.args.players-1)
        assert (await self.query('ui.get_message_box'))['open'],'Native final confirmation did not open'
        await self.click('msgbox.cancel')
        assert not (await self.query('ui.get_message_box'))['open']
        assert str(self.args.players) in next(n for n in await self.nodes() if n['path'].endswith('/DriversCurrentPlayer'))['text']
        assert (await self.query('game.errors'))['total']==errors['total']==0
        await asyncio.sleep(.8)
        resting=await self.nodes()
        assert next(n for n in resting if n['path'].endswith('/DriversWorkspace'))['color']['a']>.99
        assert all(n['color']['a']>.99 for n in resting if '/DriversWorkspace/DriversCard' in n['path'] and n['path'].count('/')==3 and n['visible'])
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'drivers-native-flow-verified')
        self.cases.append({'case':'native-party-lineup-backtracking-orbit-and-safe-final-cancel','passed':True})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'players':args.players,'scope':'Isolated Party fixture; simulator XR, no Start Race confirmation'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=DriversTest(args,session)
            try:
                await t.prove();result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=t.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',required=True,type=int);p.add_argument('--data-dir',required=True,type=Path)
    p.add_argument('--evidence',required=True,type=Path);p.add_argument('--layout',required=True,type=Path)
    p.add_argument('--fox',required=True,type=Path);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--players',type=int,default=2,choices=range(2,9))
    asyncio.run(run(p.parse_args()))
