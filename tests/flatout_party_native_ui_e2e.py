"""Qualify real Party count selection, ordered stunt playlist and first driver entry.

Only fresh normal Party callbacks stage the fixture. Controller actions make
every selection, page, toggle, clear and continuation. This never finishes driver
selection or starts a race, and does not inject playlist/player data.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_filter_ui_e2e import Filters
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas
from flatout_button_audit import inspect

class Party(Filters):
    async def park(self):
        active=self.args.hand
        try:
            for hand in ('left','right'):self.args.hand=hand;await self.ray_mode()
        finally:self.args.hand=active
        await asyncio.sleep(.25)

    async def entrance(self,name):
        samples=[];deadline=asyncio.get_running_loop().time()+15
        while asyncio.get_running_loop().time()<deadline:
            root=next((n for n in await self.nodes() if n['path'].endswith('/'+name) and n.get('effective_visible',n['visible'])),None)
            if root:
                samples.append(root['color']['a'])
                if len(samples)>=5 and samples[-1]>=.999:break
            await asyncio.sleep(.015)
        assert samples and samples[-1]>=.999 and any(.3<=v<.99 for v in samples),samples
        self.cases.append({'case':'observed-native-entry-fade','passed':True,'hand':self.args.hand,'root':name,'alpha':samples})

    async def layout_party(self):
        await self.park();nodes=await self.nodes();g=self.layouts['Party'];native=await self.query('party.get_state')
        assert native['open'] and not native['stunt_picker_open'] and not native['playlist'],native
        for n in range(2,9):
            plate=next(v for v in nodes if v['path'].endswith('/PartyCount'+str(n)))
            expected=[g['CountsX']+(n-2)*(g['CountWidth']+g['CountGap']),g['CountsY']]
            assert np.allclose(plate['rect'][:2],expected,atol=1),plate
            number=next(v for v in nodes if v['path']==plate['path']+'/PartyNumberBand/PartyCountNumber')
            assert number['text']==str(n) and abs(number['presentation']['font_scale']-.8)<.02,number
        await self.audit('PartyWorkspace',16)

    async def audit(self,root,minimum):
        nodes=await self.nodes();a=await inspect({'nodes':nodes,'message':await self.query('ui.get_message_box')},self.query,self.args.fox_root)
        assert a['passed'] and len(a['checks'])>=minimum,a
        text=[n for n in nodes if '/'+root+'/' in n['path'] and n.get('effective_visible',n['visible']) and n.get('text')]
        assert all('/FO_' in n['presentation']['font_resource'].upper() for n in text),text
        self.cases.append({'case':'native-layout-original-fonts-and-glyph-padding','passed':True,'hand':self.args.hand,'root':root,'audit':a})

    async def page(self,first):
        for _ in range(5):
            current=(await self.query('party.get_state'))['visible'][0]
            if current==first:return
            await self.click('stunts.page.next' if current<first else 'stunts.page.previous')
        raise AssertionError(('Page never reached',first))

    async def layout_stunts(self):
        await self.park();nodes=await self.nodes();p=await self.query('party.get_state');menu=await self.query('menu.get_state');g=self.layouts['Stunts']
        assert p['stunt_picker_open'] and p['players'] in (2,8),p
        first,last=p['visible'];expected=list(range(first,last+1))
        targets={t['name']:t for t in (await self.query('vr.targets'))['targets']}
        assert sorted(int(n.split('.')[1])for n in targets if n.startswith('stunt.'))==expected
        for slot,i in enumerate(expected):
            plate=next(n for n in nodes if n['path'].endswith('/StuntCard'+str(i)))
            title=next(n for n in nodes if n['path']==plate['path']+'/StuntName')
            photo=next(n for n in nodes if n['path']==plate['path']+'/StuntArtwork')
            assert title['text']==menu['items'][i]['title'] and title['text'].strip(),(title,menu['items'][i])
            assert np.allclose(plate['rect'][:2],[g['StageX']+slot*(g['CardWidth']+g['CardGap']),g['CardY']],atol=1),plate
            assert photo['rect'][0]>=plate['rect'][0]+7 and photo['rect'][2]<=plate['rect'][2]-7,photo
            selected=[v['index']for v in p['playlist']];badge=next((n for n in nodes if n['path']==plate['path']+'/StuntOrderBadge'),None)
            assert bool(badge and badge['visible'])==(i in selected),(badge,p)
            if i in selected:
                number=next(n for n in nodes if n['path']==badge['path']+'/StuntOrder')
                assert int(number['text'])==selected.index(i)+1,(number,p)
        assert targets['stunts.page.previous']['enabled']==(first>0)
        assert targets['stunts.page.next']['enabled']==(last+1<menu['item_count'])
        assert targets['helper.Validate.StuntsStart']['enabled']==bool(p['playlist'])
        assert targets['helper.Y.StuntsClear']['enabled']==bool(p['playlist'])
        await self.audit('StuntsWorkspace',3)

    async def disabled(self,name):
        before,aim=await self.point_args(name);target=next(t for t in before['targets']if t['name']==name)
        assert not target['enabled'],target
        party=await self.query('party.get_state');aim.update(command_id=uuid.uuid4().hex,hold_frames=30)
        await self.tool('click_ui_at',aim,'disabled-'+name);await self.tool('click_ui_at',aim,'disabled-retry-'+name)
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert (await self.query('party.get_state'))==party and not (await self.query('ui.get_message_box'))['open']
        self.cases.append({'case':'disabled-control-held-click-and-retry-inert','passed':True,'hand':self.args.hand,'target':name})

    async def takeover_party(self,root,back):
        before,aim=await self.point_args(back);playlist=(await self.query('party.get_state'))['playlist']
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'hold_frames':8,'dwell_frames':2,'command_id':uuid.uuid4().hex},'native-controller-takeover')
        assert not any(n['path'].endswith('/'+root) and n.get('effective_visible',n['visible'])for n in await self.nodes())
        await self.ray_mode()
        assert (await self.query('vr.targets'))['clicks']==before['clicks'] and (await self.query('party.get_state'))['playlist']==playlist
        self.cases.append({'case':'original-controller-layout-and-fresh-ray-restoration','passed':True,'hand':self.args.hand,'root':root})

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);result={'passed':False,'pid':a.pid,'scope':__doc__}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Party(a,session)
            t.layouts={k:json.loads((a.fox_root/('Flatout/Code/Sources/Menus/FO'+k+'HtmlLayout.json')).read_text(encoding='utf-8'))['geometry']for k in ('Party','Stunts')}
            sa=SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand='right',ray_presentation=True)
            stage=Atlas(sa,session,{'operations':[]});stage.active_case={'id':'native-party-qualification'};t.query_lock=stage.lock
            try:
                for hand,players in (('left',2),('right',8)):
                    a.hand=hand;observer=asyncio.create_task(t.entrance('PartyWorkspace'))
                    try:await stage.normal_flow('party_game');await t.ray_mode()
                    finally:await observer
                    await t.layout_party();initial=await t.query('party.get_state')
                    for count in range(2,9):
                        before=await t.query('party.get_state');await t.hover('players.'+str(count));assert (await t.query('party.get_state'))==before
                        await t.click('players.'+str(count));p=await t.query('party.get_state')
                        assert p['open'] and p['selection_index']==count-2 and p['players']==initial['players'] and not p['playlist'],p
                    await t.depth('players.8');await t.takeover_party('PartyWorkspace','helper.Cancel.PartyBack');await t.click('players.'+str(players))
                    observer=asyncio.create_task(t.entrance('StuntsWorkspace'))
                    try:await t.click('helper.Validate.PartyContinue')
                    finally:await observer
                    assert (await t.query('party.get_state'))['players']==players
                    await t.page(0);await t.layout_stunts()
                    for name in ('stunts.page.previous','helper.Y.StuntsClear','helper.Validate.StuntsStart'):await t.disabled(name)
                    await t.depth('stunt.0');expected=[]
                    for page in (0,4,8):
                        await t.page(page)
                        for i in range(page,page+4):
                            await t.click('stunt.'+str(i));expected.append(i)
                            assert [v['index']for v in (await t.query('party.get_state'))['playlist']]==expected
                        await t.layout_stunts()
                    await t.disabled('stunts.page.next');await t.page(0);await t.click('stunt.1');expected.remove(1)
                    await t.click('stunt.1');expected.append(1)
                    assert [v['index']for v in (await t.query('party.get_state'))['playlist']]==expected
                    await t.layout_stunts();await t.takeover_party('StuntsWorkspace','helper.Cancel.StuntsBack');await t.depth('helper.Y.StuntsClear')
                    await t.click('helper.Y.StuntsClear');assert not (await t.query('party.get_state'))['playlist'];await t.layout_stunts()
                    await t.page(0);await t.click('stunt.2');await t.click('stunt.0');await t.depth('helper.Validate.StuntsStart')
                    # The native GameConfig retains the current driver's choice;
                    # opening the picker must not confirm or replace that choice.
                    drivers_before=(await t.query('party.get_state'))['driver_choices']
                    await t.click('helper.Validate.StuntsStart');p=await t.query('party.get_state')
                    assert (await t.query('menu.get_state'))['current_menu']=='DRIVERS' and p['players']==players and [v['index']for v in p['playlist']]==[0],p
                    assert (await t.query('game.get_state'))['is_main_menu'] and p['driver_choices']==drivers_before,p
                    t.cases.append({'case':'one-start-opens-real-driver-selection-with-ordered-queue','passed':True,'hand':hand,'players':players,'remaining_queue':p['playlist']})
                    await t.click('helper.Cancel.4');assert (await t.query('party.get_state'))['stunt_picker_open']
                    await t.click('helper.Cancel.StuntsBack');p=await t.query('party.get_state')
                    assert p['open'] and not p['stunt_picker_open'],p
                    await t.click('helper.Cancel.PartyBack')
                    assert not (await t.query('party.get_state'))['open']
                    assert (await t.query('menu.get_state'))['current_menu']=='MULTIPLAYER'
                    if t.last_capture:
                        for h in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':h})
                    await stage.main()
                result['errors']=await t.query('game.errors');assert result['errors']['total']==0,result['errors'];result['passed']=True
            except Exception as e:result['error']=repr(e);raise
            finally:
                result['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
