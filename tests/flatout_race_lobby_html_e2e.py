"""Real eight-slot race lobby, native room callbacks, stereo and ray ownership.

Start in the race lobby of an owned isolated private LAN room. These checks
never create sample players or images, invite anyone, or start a race.
"""
import argparse,asyncio,copy,json,os,sys,uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

BACK='helper.Cancel.RaceLobbyLeave'


class RaceLobbyTest(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects',{'root':'menu','max_depth':22,'all':True}))['nodes']

    async def layout(self,label):
        game=await self.query('game.get_state');net=await self.query('net.get_state');state=await self.query('menu.get_state')
        assert game['diagnostic_save_slot']==641062
        assert net['alive'] and net['is_host'] and net['private'] and net['lan'],net
        assert state['items'][0]['title'].lower()=='garage',state
        nodes=await self.nodes()
        visible=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['effective_visible'])
        labels=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['visible'])
        roster=visible('/RaceLobbyRoster');side=visible('/RaceLobbySidePanel');header=visible('/RaceLobbyHeader')
        assert side['rect'][0]-roster['rect'][2]>=24
        assert visible('/RaceLobbyCount')['text']==f"{len(net['slots'])+len(net['bots'])} / {net['max_slots']}"
        names=[]
        for i in range(8):
            row=visible('/RaceLobbyPlayer'+str(i));name=visible('/RaceLobbyPlayer'+str(i)+'/RaceLobbyPlayerName')
            assert roster['rect'][0]+17<=row['rect'][0] and row['rect'][2]+17<=roster['rect'][2]
            assert roster['rect'][1]+100<=row['rect'][1] and row['rect'][3]+17<=roster['rect'][3]
            names.append(name['text'])
        assert all(s['name'] in names for s in net['slots']),(names,net['slots'])
        # LAN peers have no remote Steam identity and may also retain their
        # actual driver portrait. Prove every bot's portrait in its own row.
        for bot in net['bots']:
            parent=next(n['path'].rsplit('/',1)[0] for n in nodes if n['effective_visible'] and n['path'].endswith('/RaceLobbyPlayerName') and n.get('text')==bot['name'])
            assert any(n['effective_visible'] and n['path']==parent+'/RaceLobbyDriverPortrait' for n in nodes),bot
        targets=(await self.query('vr.targets'))['targets'];target_names={t['name'] for t in targets}
        assert {BACK,'lobby.action.0','lobby.action.1','lobby.action.2','lobby.microphone'}<=target_names,targets
        assert all(t['physical_hover'] and t['enabled'] for t in targets)
        assert not any(t['name'].startswith('player.') and names[int(t['name'].split('.')[1])] not in {s['name'] for s in net['slots']} for t in targets),'Bot row became an actionable human'
        # Native readiness includes configured bots; a one-human room with bots
        # may start normally. Never click Ready in this non-racing UI fixture.
        if len(net['slots'])+len(net['bots'])<=1:
            assert 'helper.Secondary.RaceLobbyReady' not in target_names
        for n in targets:
            if n['name'].startswith(('helper.Cancel.','lobby.microphone')) or n['name'] in ('lobby.action.1','lobby.action.2'):
                assert n['rect'][1]-roster['rect'][3]>=32 and n['rect'][3]<=1056
        photos=[n for n in nodes if n['effective_visible'] and n['path'].endswith('/SteamAvatar')]
        assert len(photos)==sum(s['steam_avatar']['status']=='ready' for s in net['slots']),photos
        for photo in photos:
            name=visible(photo['path'].split('/RaceLobbyWorkspace')[-1].rsplit('/',1)[0]+'/RaceLobbyPlayerName')
            assert name['rect'][0]-photo['rect'][2]>=13,(name,photo)
        assert visible('/RaceLobbyTrackName')['text'], 'Missing real configured track name'
        photo=visible('/RaceLobbyTrackPhoto');assert photo['rect'][2]<=side['rect'][2]-17
        heading=await self.query('hud.get_node',{'root':'menu','name':'Menu/RaceLobbyWorkspace/RaceLobbyHeading','presentation':True})
        assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in heading['presentation']['font_resource'].upper()
        assert np.allclose([heading['color'][k] for k in ('r','g','b')],[1,.659,0],atol=.004)
        assert not any(n['effective_visible'] and ('/Lobby_Game/' in n['path'] or n['path'].endswith(('/Helpers','/Header'))) for n in nodes)
        assert not any(n['effective_visible'] and n['path'].endswith(('/Lobby Infos','/Lobby Infos/TIME')) for n in nodes),'Duplicate legacy countdown outside the ray panel'
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label+'-stereo');self.last_capture=capture
        assert_reading_distance(capture,3.,4.5)
        from flatout_header_geometry import measure_header_accent
        angles=measure_header_accent(capture,self.last_image_path,header['rect']);assert all(7.5<v['degrees']<12.5 for v in angles.values()),angles
        await self.audit(label)
        self.cases.append({'case':label+'-eight-real-rows-track-summary-fonts-yellow-stereo','passed':True,'names':names,'header':angles})
        return state,net,nodes

    async def prove(self):
        state,net,_=await self.layout('entry');original=net['mic_muted'];before=(await self.query('vr.targets'))['clicks']
        nodes=await self.nodes()
        local_row=next(int(n['path'].rsplit('/',2)[0].rsplit('/RaceLobbyPlayer',1)[1]) for n in nodes if n['path'].endswith('/RaceLobbyPlayerActionLabel') and n.get('text') in ('Mute Mic','Unmute Mic') and n['visible'])
        for name in ('player.'+str(local_row),'lobby.action.0','lobby.microphone',BACK):
            await self.hover(name)
            capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'physical-'+name);self.last_capture=capture;assert_hover_lift(capture)
            assert (await self.query('net.get_state'))['mic_muted']==original
        assert (await self.query('vr.targets'))['clicks']==before
        try:
            await self.click('player.'+str(local_row));assert (await self.query('net.get_state'))['mic_muted']!=original
            await self.click('lobby.microphone');assert (await self.query('net.get_state'))['mic_muted']==original
        finally:
            if (await self.query('net.get_state'))['mic_muted']!=original:await self.click('lobby.microphone')
        await self.click('lobby.action.1')
        texts=[n.get('text','') for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes']]
        assert (await self.query('ui.get_message_box'))['open'] and any(str(net['room_code']) in s for s in texts),texts
        await self.click('msgbox.validate');await self.layout('after-room-code')
        await self.click('lobby.action.2')
        assert (await self.query('menu.get_state'))['current_menu']=='OPTIONS'
        capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'native-options-from-lobby');self.last_capture=capture;assert_reading_distance(capture,3.,4.5)
        back=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        await self.click(back);await self.layout('after-options')
        self.cases.append({'case':'visual-only-hover-single-click-retry-real-mic-room-code-options-return','passed':True})

    async def takeover(self):
        _,_,nodes=await self.layout('before-takeover')
        # Resume rays with deliberate wrist movement to a different control;
        # leaving the wrist where the stick was used correctly stays suppressed.
        bounds=next(n['rect'] for n in nodes if n['path'].endswith('/RaceLobbySideAction0'))
        _,_,aim=await self.hover(BACK)
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'takeover-source');self.last_capture=source
        carrier=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[1.,0],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
        nodes=await self.nodes();assert not any(n['effective_visible'] and n['path'].endswith('/RaceLobbyWorkspace') for n in nodes)
        assert any(n['effective_visible'] and '/Lobby_Game/' in n['path'] for n in nodes)
        if (await self.query('net.get_state'))['lobby_timeout']>=0:
            assert any(n['effective_visible'] and n['path'].endswith('/Lobby Infos/TIME') for n in nodes),'Original controller countdown did not return'
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'original-lobby-layout');self.last_capture=source
        projection=copy.deepcopy(source);projection['geometry']['surfaces']=[carrier];x,y=quad_pixel(projection,bounds)
        await self.tool('hover_ui_at',{'capture_id':source['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','surface':{k:carrier[k] for k in ('pose','size')},'dwell_frames':45},'deliberate-ray-return')
        deadline=asyncio.get_running_loop().time()+3
        while True:
            if any(n['effective_visible'] and n['path'].endswith('/RaceLobbyWorkspace') for n in await self.nodes()):break
            assert asyncio.get_running_loop().time()<deadline;await asyncio.sleep(.025)
        await self.layout('after-takeover')
        self.cases.append({'case':'actual-controller-original-layout-and-ray-return','passed':True})

    async def motion(self):
        await self.layout('before-motion');await self.click('lobby.action.2')
        back=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.Cancel.'))
        _,_,args=await self.hover(back);args.update(command_id=uuid.uuid4().hex,hold_frames=45)
        pending=asyncio.create_task(self.tool('click_ui_at',args,'lobby-entrance-click'));samples=[]
        try:
            deadline=asyncio.get_running_loop().time()+4
            while asyncio.get_running_loop().time()<deadline:
                nodes=await self.nodes();root=next((n for n in nodes if n['path'].endswith('/RaceLobbyWorkspace') and n['visible']),None)
                if root:
                    actions={n['path']:n['rect'] for n in nodes if n['path'].split('/')[-1].startswith(('RaceLobbyFooter','RaceLobbySideAction'))}
                    samples.append({'alpha':root['color']['a'],'actions':actions})
                    if pending.done() and samples[-1]['alpha']>=.999:break
                await asyncio.sleep(.008)
        finally:
            await pending;(self.args.evidence/'motion-samples.json').write_text(json.dumps(samples,indent=2))
        assert samples and any(.55<=s['alpha']<.99 for s in samples),samples
        assert samples[-1]['alpha']>=.999
        assert all(b['alpha']>=a['alpha'] for a,b in zip(samples,samples[1:])),samples
        assert all(s['actions']==samples[0]['actions'] for s in samples),'Entrance moved hit geometry'
        await self.tool('click_ui_at',args,'lobby-entrance-retry');await self.layout('after-motion')
        self.cases.append({'case':'actual-lobby-entrance-fade-with-stable-action-geometry','passed':True,'samples':len(samples)})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Owned private LAN race lobby, actual XR; no race launch or external invitations'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=RaceLobbyTest(a,session)
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
