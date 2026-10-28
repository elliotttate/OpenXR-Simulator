"""Native upgrade workspace checks through screenshot-derived XR rays.

Start in Upgrades on an isolated Career profile. No purchase or race is confirmed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT


class UpgradeTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def prove(self):
        initial=await self.query('menu.get_state')
        assert initial['current_menu']=='UPGRADES'
        assert (await self.query('game.get_state'))['game_mode']=='career'
        errors=await self.query('game.errors')
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'upgrades-both-eyes')
        assert any(n['path'].endswith('/UpgradesWorkspace') and n['visible'] for n in await self.nodes()),'Missing native UpgradesWorkspace'
        await self.check('upgrades-initial')
        before=await self.query('menu.get_state')
        await self.hover('upgrades.item.1');await asyncio.sleep(.4)
        after=await self.query('menu.get_state')
        assert before['selection_index']==after['selection_index'],'Hover committed a preview'
        assert before['upgrade_context']==after['upgrade_context'],'Hover changed ratings or credits'
        for item in initial['items']:
            await self.click('upgrades.item.'+str(item['index']))
            state=await self.query('menu.get_state');assert state['selection_index']==item['index']
            await self.check('upgrades-part-'+str(item['index']))
            await self.click('helper.Validate.UpgradeBuy')
            assert (await self.query('ui.get_message_box'))['open'],'Original Buy callback did not open its modal'
            text=[n.get('text','') for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':18}))['nodes']]
            targets={t['name'] for t in (await self.query('vr.targets'))['targets']}
            if 'msgbox.cancel' in targets:
                await self.click('msgbox.cancel')
            else:
                assert any('money' in x.lower() or 'credits' in x.lower() or 'not enough cash'==x.lower() for x in text),text
                await self.click('msgbox.validate')
            returned=await self.query('menu.get_state')
            assert returned['upgrade_context']['credits']==state['upgrade_context']['credits']
            assert [x['upgrade_levels_installed'] for x in returned['items']]==[x['upgrade_levels_installed'] for x in initial['items']]
        await self.click('upgrades.item.0')
        await asyncio.sleep(.8)
        assert all(n['color']['a']>.99 for n in await self.nodes() if '/UpgradesWorkspace/UpgradesCard' in n['path'] and n['path'].count('/')==3 and n['visible'])
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'upgrades-native-flow-verified')
        await self.click('helper.Cancel.UpgradeBack')
        assert (await self.query('menu.get_state'))['current_menu']=='GARAGE'
        await self.click('item.1')
        assert (await self.query('menu.get_state'))['current_menu']=='UPGRADES'
        await self.check('upgrades-garage-round-trip')
        assert (await self.query('game.errors'))['total']==errors['total']==0
        self.cases.append({'case':'native-buy-dialogs-safely-dismissed-and-garage-round-trip','passed':True})

    async def check(self,label):
        state=await self.query('menu.get_state');nodes=await self.nodes()
        assert state['item_count']==6,'This fixture needs all six parts'
        targets=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in targets}=={'upgrades.item.'+str(i) for i in range(6)}|{'helper.Validate.UpgradeBuy','helper.Cancel.UpgradeBack'}
        layout=json.loads((self.args.fox/'Flatout/Code/Sources/Menus/FOUpgradesHtmlLayout.json').read_text())['geometry']
        names=('CP_Engine','CP_GearBox','CP_Exhaust','CP_Nitro','CP_BrakesTyres','CP_Chassis')
        skins=ET.parse(self.args.fox/'Flatout/Data/Assets/GUI_FlatOut/Textures/Menus/CARPARTS.PSK.XML')
        for item in state['items']:
            i=item['index'];card=next(n for n in nodes if n['path'].endswith('/UpgradesCard'+str(i)))
            x=layout['RailX']+i*(layout['CardWidth']+layout['CardGap'])
            assert all(abs(a-b)<1 for a,b in zip(card['rect'],[x,layout['CardY'],x+layout['CardWidth'],layout['CardY']+layout['CardHeight']])),card
            def child(name):return next(n for n in nodes if n['path'].endswith('/UpgradesCard'+str(i)+'/'+name))
            assert child('UpgradesCardName')['text']==item['title'] and item['title']
            assert child('UpgradesCardLevel')['text']==str(item['upgrade_levels_installed'])+' / '+str(item['upgrade_max_levels'])
            assert int(child('UpgradesCardPrice')['text'].replace(',','').replace(' ',''))==item['price']
            actual=await self.query('hud.get_node',{'root':'menu','name':'Menu/UpgradesWorkspace/UpgradesCard'+str(i)+'/UpgradesPartIcon','presentation':True})
            authored=next(n for n in skins.iter('skin') if n.attrib['Name']==names[item['upgrade_category']])
            assert all(abs(a-b)<1e-5 for a,b in zip(actual['presentation']['uv'],map(float,authored.attrib['uvs'].split(';'))))
        for i,stat in enumerate(state['upgrade_context']['ratings']):
            actual=next(n for n in nodes if n['path'].endswith('/UpgradesStatValue'+str(i)))
            delta=stat['preview']-stat['current'];expected=str(stat['current'])+((' ('+('+' if delta>0 else '')+str(delta)+')') if delta else '')
            assert actual['text']==expected,(actual,stat)
        header=await self.query('hud.get_node',{'root':'menu','name':'Menu/UpgradesWorkspace/UpgradesHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in header['presentation']['font_resource'].upper(),header
        box=[layout['PreviewX'],layout['PreviewY'],layout['PreviewX']+layout['PreviewWidth'],layout['PreviewY']+layout['PreviewHeight']]
        assert not [n for n in nodes if '/UpgradesWorkspace/' in n['path'] and n['kind']=='panel' and n['visible'] and n.get('color',{}).get('a',0)>.2
            and min(n['rect'][2],box[2])-max(n['rect'][0],box[0])>20 and min(n['rect'][3],box[3])-max(n['rect'][1],box[1])>20]
        assert all(t['rect'][2]-t['rect'][0]>=79 and t['rect'][3]-t['rect'][1]>=79 for t in targets)
        await self.audit(label)
        self.cases.append({'case':'native-part-artwork-prices-levels-preview-and-original-font','passed':True,'label':label})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=UpgradeTest(args,session)
            try:
                await t.prove();result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=t.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--fox',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
