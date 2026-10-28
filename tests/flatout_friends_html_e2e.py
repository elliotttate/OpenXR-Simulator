"""Real party roster, native room actions and physical ray interaction."""
import argparse,asyncio,copy,json,os,sys,uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

BACK='helper.Cancel.PartyLeave'


class FriendsTest(Frontend):
    async def layout(self,label):
        state=await self.query('menu.get_state');net=await self.query('net.get_state')
        assert net['alive'] and net['is_host'] and net['connected_players']==1 and net['private'],net
        assert state['current_menu']=='ONLINE' and state['item_count']==5,state
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        visible=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['effective_visible'])
        # A physically promoted face hides its source subtree while the
        # compositor draws it in front. Its authored label remains inspectable.
        label_node=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['visible'])
        header=visible('/PartyHeader');roster=visible('/PartyRoster');side=visible('/PartySidePanel')
        assert side['rect'][0]-roster['rect'][2]>=24
        assert visible('/PartyCount')['text']==f"{len(net['slots'])} / {net['max_slots']}"
        targets=(await self.query('vr.targets'))['targets'];names={t['name'] for t in targets}
        assert names=={BACK}|{'party.action.'+str(i) for i in range(5)}|{'player.'+str(s['index']) for s in net['slots']},targets
        assert all(t['physical_hover'] and t['enabled'] for t in targets)
        slots={s['index']:s for s in net['slots']}
        for i in range(8):
            row=visible('/PartyPlayer'+str(i));name=visible('/PartyPlayer'+str(i)+'/PartyPlayerName')
            assert roster['rect'][0]+19<=row['rect'][0] and row['rect'][2]+19<=roster['rect'][2]
            assert roster['rect'][1]+19<=row['rect'][1] and row['rect'][3]+19<=roster['rect'][3]
            if i in slots:
                assert name['text']==slots[i]['name'],(name,slots[i])
                action=label_node('/PartyPlayer'+str(i)+'/PartyPlayerAction/PartyPlayerActionLabel')
                if slots[i]['local']:assert action['text']==('Unmute Mic' if net['mic_muted'] else 'Mute Mic')
            else:assert 'player.'+str(i) not in names
        for i,item in enumerate(state['items']):
            node=label_node('/PartyRoomAction'+str(i)+'/PartyRoomLabel');assert node['text']==item['title']
        back=next(t for t in targets if t['name']==BACK)
        assert back['rect'][1]-roster['rect'][3]>=32 and back['rect'][3]<=1056
        assert not any(n['effective_visible'] and ('/Lobby_Game/' in n['path'] or n['path'].endswith(('/Helpers','/Header'))) for n in nodes)
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/PartyWorkspace/PartyHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label+'-stereo');self.last_capture=capture
        assert_reading_distance(capture,3.,4.5)
        from flatout_header_geometry import measure_header_accent
        angles=measure_header_accent(capture,self.last_image_path,header['rect']);assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit(label)
        self.cases.append({'case':label+'-actual-eight-slot-roster-native-actions-fonts-yellow-and-stereo','passed':True,'header':angles})
        return state,net,nodes

    async def prove(self):
        state,net,_=await self.layout('entry');original=net['mic_muted'];local=net['local_slot']
        before=(await self.query('vr.targets'))['clicks']
        for name in ['player.'+str(local),'party.action.0',BACK]:
            await self.hover(name);await asyncio.sleep(.3)
            assert (await self.query('net.get_state'))['mic_muted']==original
            assert (await self.query('menu.get_state'))['selection_index']==state['selection_index']
            capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'physical-'+name);self.last_capture=capture;assert_hover_lift(capture)
        assert (await self.query('vr.targets'))['clicks']==before
        try:
            await self.click('player.'+str(local));assert (await self.query('net.get_state'))['mic_muted']!=original
            await self.layout('muted-state')
        finally:
            if (await self.query('net.get_state'))['mic_muted']!=original:await self.click('player.'+str(local))
        assert (await self.query('net.get_state'))['mic_muted']==original
        await self.click('party.action.0')
        texts=[n.get('text','') for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes']]
        assert (await self.query('ui.get_message_box'))['open'] and any(str(net['room_code']) in s for s in texts),texts
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'actual-room-code-popup');self.last_capture=capture;assert_reading_distance(capture,3.,4.5)
        await self.click('msgbox.validate');await self.layout('returned-from-room-code')
        # Browse only: never join an unrelated room or run automatic matchmaking.
        await self.click('party.action.2')
        assert any(n.get('text')=='BROWSE GAMES' for n in (await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes'])
        cancel=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(cancel);await self.layout('returned-from-browser')
        self.cases.append({'case':'hover-is-visual-mic-toggles-once-and-restores-real-code-browser-native-back','passed':True})

    async def takeover(self):
        _,_,nodes=await self.layout('before-takeover')
        bounds=next(n['rect'] for n in nodes if n['path'].endswith('/PartyRoomAction0'))
        _,_,aim=await self.hover(BACK)
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'takeover-source');self.last_capture=source
        carrier=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[1.,0],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
        assert not any(n['effective_visible'] and n['path'].endswith('/PartyWorkspace') for n in nodes)
        net=await self.query('net.get_state')
        if any(s['local'] and s.get('steam_avatar',{}).get('status')=='ready' for s in net['slots']):
            photo=next(n for n in nodes if n['effective_visible'] and n['path'].endswith('/SteamAvatar'))
            name=min((n for n in nodes if n['effective_visible'] and n['path'].endswith('/TXT_PlayerName') and n.get('text')),key=lambda n:len(n['path']))
            assert name['rect'][0]-photo['rect'][2]>=8,(name,photo)
            self.cases.append({'case':'steam-avatar-gutter-in-original-controller-layout','passed':True,'photo':photo['rect'],'name':name['rect']})
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'original-party-layout');self.last_capture=source
        projection=copy.deepcopy(source);projection['geometry']['surfaces']=[carrier];x,y=quad_pixel(projection,bounds)
        await self.tool('hover_ui_at',{'capture_id':source['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
            'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},'dwell_frames':45},'deliberate-ray-return')
        deadline=asyncio.get_running_loop().time()+3
        while True:
            nodes=(await self.query('hud.rects',{'root':'menu','max_depth':8}))['nodes']
            if any(n['effective_visible'] and n['path'].endswith('/PartyWorkspace') for n in nodes):break
            assert asyncio.get_running_loop().time()<deadline;await asyncio.sleep(.025)
        await self.layout('after-takeover')
        self.cases.append({'case':'actual-controller-original-layout-and-deliberate-ray-return','passed':True})

    async def motion(self):
        await self.layout('before-motion');await self.click('party.action.2')
        back=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        _,_,args=await self.hover(back);args.update(command_id=uuid.uuid4().hex,hold_frames=45)
        pending=asyncio.create_task(self.tool('click_ui_at',args,'party-entrance-click'));samples=[]
        try:
            deadline=asyncio.get_running_loop().time()+4
            while asyncio.get_running_loop().time()<deadline:
                nodes=(await self.query('hud.rects',{'root':'menu','max_depth':7}))['nodes']
                root=next((n for n in nodes if n['path'].endswith('/PartyWorkspace') and n['visible']),None)
                if root:
                    actions={n['path']:n['rect'] for n in nodes if n['path'].split('/')[-1].startswith('PartyRoomAction')}
                    samples.append({'alpha':root['color']['a'],'actions':actions})
                    if pending.done() and samples[-1]['alpha']>=.999:break
                await asyncio.sleep(.008)
        finally:
            await pending;(self.args.evidence/'motion-samples.json').write_text(json.dumps(samples,indent=2))
        assert samples and any(.55<=s['alpha']<.99 for s in samples),samples
        assert samples[-1]['alpha']>=.999
        assert all(b['alpha']>=a['alpha'] for a,b in zip(samples,samples[1:])),samples
        assert all(s['actions']==samples[0]['actions'] for s in samples),'Entrance moved a hit target'
        await self.tool('click_ui_at',args,'party-entrance-retry');await self.layout('after-motion')
        self.cases.append({'case':'actual-party-entrance-fades-once-with-stable-action-geometry','passed':True,'samples':len(samples)})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Owned one-player private party, real XR, native data and stereo; no messages/invitations/matchmaking/remote joins'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=FriendsTest(a,session)
            try:
                if a.layout_only:await t.layout('layout')
                elif a.takeover_only:await t.takeover()
                elif a.motion_only:await t.motion()
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true');p.add_argument('--motion-only',action='store_true');asyncio.run(run(p.parse_args()))
