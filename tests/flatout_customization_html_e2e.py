"""Exercise native Customization through screenshot-derived XR input."""
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


class CustomTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']

    async def prove(self):
        assert (await self.query('menu.get_state'))['current_menu']=='SKINS'
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'customization-both-eyes')
        assert any(n['path'].endswith('/CustomizationWorkspace') and n['visible'] for n in await self.nodes()),'Missing native CustomizationWorkspace'
        original=(await self.query('menu.get_state'))['customization_context']
        errors=await self.query('game.errors')
        try:
            for category,key in enumerate(('skin','boost','horn','weapon')):
                await self.click('custo.'+str(category))
                state=await self.query('menu.get_state');assert state['customization_context']['category']==category
                expected_ids={i['index'] for i in state['items']};seen=set()
                while (await self.query('menu.get_state'))['selection_index']//5>0:await self.click('custom.prev')
                await self.inert('custom.prev')
                while True:
                    page=await self.check('category-'+str(category)+'-page')
                    seen.update(page)
                    state=await self.query('menu.get_state')
                    if (state['selection_index']//5+1)*5>=state['item_count']:break
                    await self.click('custom.next')
                assert seen==expected_ids,(category,seen,expected_ids)
                await self.inert('custom.next')
                while (await self.query('menu.get_state'))['selection_index']//5>0:await self.click('custom.prev')
                state=await self.query('menu.get_state')
                available=[x for x in state['items'] if not x['locked'] and x['installed']]
                alternate=next((x for x in available if not x['equipped']),None)
                # A newly created garage can retain the native BoostFX sentinel
                # 0, which has no selectable card or restore callback. Never
                # commit away from a value the real UI cannot restore.
                restorable=next((x for x in available if x['customization_id']==original[key]),None)
                if alternate and restorable:
                    await self.focus(alternate['index'])
                    await self.click('helper.Validate.CustomSelect')
                    equipped=await self.query('menu.get_state')
                    assert equipped['customization_context'][key]==alternate['customization_id']
                    assert next(x for x in equipped['items'] if x['customization_id']==alternate['customization_id'])['equipped']
                    self.cases.append({'case':'native-equip-and-profile-state','category':category,'passed':True})
                if restorable:
                    await self.restore(category,key,original[key])
                else:
                    assert (await self.query('menu.get_state'))['customization_context'][key]==original[key]
                    self.cases.append({'case':'native-value-without-selectable-card-preserved','category':category,
                        'value':original[key],'passed':True,'equip_tested':False})
                state=await self.query('menu.get_state')
                locked=next((x for x in state['items'] if x['locked'] or not x['installed']),None)
                if locked:
                    await self.focus(locked['index'])
                    await self.inert('helper.Validate.CustomSelect')
                    assert (await self.query('menu.get_state'))['customization_context'][key]==original[key]
            await self.restore(0,'skin',original['skin'])
            await self.click('helper.Y.CustomOrbit')
            targets=(await self.query('vr.targets'))['targets']
            assert len(targets)==1 and targets[0]['name']=='helper.Cancel.0',targets
            await self.click('helper.Cancel.0');await asyncio.sleep(.8)
            nodes=await self.nodes()
            assert next(n for n in nodes if n['path'].endswith('/CustomizationWorkspace'))['color']['a']>.99
            assert all(n['color']['a']>.99 for n in nodes if '/CustomizationWorkspace/CustomizationCard' in n['path'] and n['path'].count('/')==3 and n['visible'])
            await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'custom-native-flow-verified')
            await self.click('helper.Cancel.CustomBack')
            assert (await self.query('menu.get_state'))['current_menu']=='GARAGE'
            await self.click('item.2')
            assert (await self.query('menu.get_state'))['current_menu']=='SKINS'
            final=(await self.query('menu.get_state'))['customization_context']
            assert all(final[k]==original[k] for k in ('skin','boost','horn','weapon')),(original,final)
            assert (await self.query('game.errors'))['total']==errors['total']==0
            self.cases.append({'case':'native-callbacks-all-categories-restored-orbit-and-garage-roundtrip','passed':True})
        finally:
            # Weapon choice is shared through the game's native preference store.
            # Restore it through real UI even if a later assertion fails.
            state=await self.query('menu.get_state')
            if state['current_menu']=='SKINS' and state['customization_context']['weapon']!=original['weapon']:
                await self.restore(3,'weapon',original['weapon'])

    async def restore(self,category,key,value):
        await self.click('custo.'+str(category))
        state=await self.query('menu.get_state')
        item=next(x for x in state['items'] if x['customization_id']==value and not x['locked'])
        await self.focus(item['index'])
        if state['customization_context'][key]!=value:await self.click('helper.Validate.CustomSelect')

    async def focus(self,index):
        state=await self.query('menu.get_state')
        while state['selection_index']//5!=index//5:
            await self.click('custom.next' if state['selection_index']//5<index//5 else 'custom.prev')
            state=await self.query('menu.get_state')
        before=await self.query('menu.get_state')
        await self.hover('custom.item.'+str(index));await asyncio.sleep(.3)
        after=await self.query('menu.get_state')
        assert before['selection_index']==after['selection_index']
        assert before['customization_context']==after['customization_context'],'Hover changed equipped state'
        await self.click('custom.item.'+str(index))
        assert (await self.query('menu.get_state'))['selection_index']==index

    async def inert(self,name):
        before,_,args=await self.hover(name);args.update(command_id=uuid.uuid4().hex,hold_frames=45)
        state=await self.query('menu.get_state')
        await self.tool('click_ui_at',args,'disabled-'+name)
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        await self.tool('click_ui_at',args,'disabled-retry-'+name)
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert (await self.query('menu.get_state'))['customization_context']==state['customization_context']
        self.cases.append({'case':'disabled-hold-and-retry','target':name,'passed':True})

    async def check(self,label):
        state=await self.query('menu.get_state');nodes=await self.nodes();targets=(await self.query('vr.targets'))['targets']
        assert not any(t['name'].startswith(('legacy.','custo.up','custo.down')) for t in targets),targets
        g=json.loads((self.args.fox/'Flatout/Code/Sources/Menus/FOCustomizationHtmlLayout.json').read_text())['geometry']
        first=state['selection_index']//5*5;indices=list(range(first,min(first+5,state['item_count'])))
        assert {t['name'] for t in targets if t['name'].startswith('custom.item.')}=={'custom.item.'+str(i) for i in indices}
        assert {t['name'] for t in targets if t['name'].startswith('custo.')}=={'custo.'+str(i) for i in range(4)}
        for slot,i in enumerate(indices):
            item=state['items'][i];prefix='/CustomizationCard'+str(slot)
            card=next(n for n in nodes if n['path'].endswith(prefix));x=g['RailX']+slot*(g['CardWidth']+g['CardGap'])
            assert all(abs(a-b)<1 for a,b in zip(card['rect'],[x,g['CardY'],x+g['CardWidth'],g['CardY']+g['CardHeight']])),card
            name=next(n for n in nodes if n['path'].endswith(prefix+'/CustomizationCardName'))
            assert name['text']==item['title'].removeprefix('> ') and name['text'],(name,item)
            for child,visible in (('CustomizationLock',item['locked'] or not item['installed']),('CustomizationEquipped',item['equipped'] and not item['locked'])):
                node=next((n for n in nodes if n['path'].endswith(prefix+'/'+child)),None)
                assert bool(node and node['visible'])==visible,(node,item)
            actual=await self.query('hud.get_node',{'root':'menu','name':'Menu/CustomizationWorkspace/CustomizationCard'+str(slot)+'/CustomizationArt','presentation':True})
            path=self.args.fox/'Flatout/Data/Assets'/item['skin_pack'].removeprefix('DATA:/')
            xml=ET.parse(str(path)+'.XML');authored=next(n for n in xml.iter('skin') if n.attrib['Name']==item['skin_name'])
            assert all(abs(a-b)<1e-5 for a,b in zip(map(float,authored.attrib['uvs'].split(';')),actual['presentation']['uv']))
        header=await self.query('hud.get_node',{'root':'menu','name':'Menu/CustomizationWorkspace/CustomizationHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in header['presentation']['font_resource'].upper()
        preview=[g['PreviewX'],g['PreviewY'],g['PreviewX']+g['PreviewWidth'],g['PreviewY']+g['PreviewHeight']]
        assert not [n for n in nodes if '/CustomizationWorkspace/' in n['path'] and n['kind']=='panel' and n['visible'] and n.get('color',{}).get('a',0)>.2
            and min(n['rect'][2],preview[2])-max(n['rect'][0],preview[0])>20 and min(n['rect'][3],preview[3])-max(n['rect'][1],preview[1])>20]
        assert all(t['rect'][2]-t['rect'][0]>=79 and t['rect'][3]-t['rect'][1]>=79 for t in targets)
        assert all(20<=t['rect'][0]<t['rect'][2]<=1900 and 20<=t['rect'][1]<t['rect'][3]<=1060 for t in targets), 'Controls require canvas-edge clearance'
        await self.audit(label);return indices


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=CustomTest(args,session)
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
