"""Real XR player options in a private party with a second owned LAN client.

Stage with flatout_owned_party_peer.py. No invitations, profile opening, public
matching or authority transfer. Restore mute and volume settings after testing.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift


class PeerTest(Frontend):
    room_action='party.action.0'
    async def nodes(self):return (await self.query('hud.rects',{'root':'menu','max_depth':20}))['nodes']

    async def popup(self):
        targets=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in targets}<={'party.options.close'}|{'popup.'+str(i) for i in range(7)},targets
        assert all(t['enabled'] and t['physical_hover'] for t in targets)
        nodes=await self.nodes();visible=lambda suffix:next(n for n in nodes if n['path'].endswith(suffix) and n['visible'])
        options={t['name']:visible('/PartyOption'+t['name'].split('.')[1]+'/PartyOptionLabel')['text'] for t in targets if t['name'].startswith('popup.')}
        assert len(options)==5,options
        assert any('Kick' in s for s in options.values()) and any('Volume' in s for s in options.values()),options
        await self.audit('player-popup-spacing')
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'player-options-stereo');self.last_capture=source
        assert_reading_distance(source,3.,4.5)
        return options

    async def open_peer(self):
        await self.click('player.'+str(self.remote));return await self.popup()

    async def volume(self):
        nodes=await self.nodes()
        label=next(n['text'] for n in nodes if n['path'].endswith('/PartyOptionLabel') and n['visible'] and 'Volume' in n.get('text',''))
        import re
        return int(re.search(r'(\d+)\s*%',label).group(1))

    async def hold_volume(self,name,value):
        # The row's inset track is the actual native 0..200% range.
        target=next(t for t in (await self.query('vr.targets'))['targets'] if t['name']==name)
        w=target['rect'][2]-target['rect'][0];inset=24/w
        _,args=await self.point_args(name,(inset+(1-2*inset)*value/200,.78))
        args.update(command_id=uuid.uuid4().hex,dwell_frames=12)
        result=await self.tool('hold_ui_at',args,'voice-drag-'+str(value));assert result['status']=='input_delivered',result
        return args

    async def prove(self):
        state=await self.query('game.get_state');assert state['diagnostic_save_slot']==641062
        net=await self.query('net.get_state')
        assert net['alive'] and net['lan'] and net['private'] and net['is_host'] and net['connected_players']==2,net
        self.remote=next(s['index'] for s in net['slots'] if not s['local'])
        original_muted=self.remote in net['muted'];original_all=net['mute_all']
        assert not original_muted and not original_all,'Use an untouched owned party fixture'
        options=await self.open_peer();volume_name=next(n for n,v in options.items() if 'Volume' in v);original_volume=await self.volume()
        try:
            # A fast direct grab still belongs to the slider when its bottom
            # edge is inside the adjacent Mute All button's jitter margin.
            await self.hover(next(n for n,v in options.items() if 'All' in v))
            await self.hold_volume(volume_name,40)
            now=await self.query('net.get_state')
            assert not now['mute_all'] and now['muted']==[], 'A neighboring button consumed the slider grab'
            assert await self.volume()==40
            await self.hold_volume(volume_name,original_volume)
            await self.session.call_tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand})
            assert await self.volume()==original_volume
            self.cases.append({'case':'direct-slider-grab-wins-over-neighbor-trigger-jitter','passed':True})
            for name in options:
                await self.hover(name)
                source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'popup-lift-'+name);self.last_capture=source;assert_hover_lift(source)
                assert (await self.query('net.get_state'))['muted']==[]
            # Keep the trigger down as the label and fill change. A data update
            # must not invalidate the live drag or require another click.
            await self.hover(volume_name)
            before=(await self.query('vr.targets'))['clicks']
            for value in (40,160,80):
                await self.hold_volume(volume_name,value);await asyncio.sleep(.15)
                actual=await self.volume();assert abs(actual-value)<=5,(value,actual)
            assert (await self.query('vr.targets'))['clicks']==before+1,'Volume updates retired the held drag'
            await self.session.call_tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand})
            await self.hold_volume(volume_name,original_volume)
            await self.session.call_tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand})
            assert await self.volume()==original_volume
            self.cases.append({'case':'continuous-native-volume-drag-preserves-ownership-and-restores','passed':True,'original_volume':original_volume})
            mute=next(n for n,v in options.items() if 'Mute' in v and 'All' not in v)
            await self.click(mute);assert self.remote in (await self.query('net.get_state'))['muted']
            options=await self.open_peer();await self.click(next(n for n,v in options.items() if 'Mute' in v and 'All' not in v))
            assert (await self.query('net.get_state'))['muted']==[]
            options=await self.open_peer();await self.click(next(n for n,v in options.items() if 'All' in v))
            assert (await self.query('net.get_state'))['mute_all'] and self.remote in (await self.query('net.get_state'))['muted']
            options=await self.open_peer();await self.click(next(n for n,v in options.items() if 'All' in v))
            assert not (await self.query('net.get_state'))['mute_all'] and (await self.query('net.get_state'))['muted']==[]
            await self.open_peer();await self.click('party.options.close')
            assert not (await self.query('ui.get_message_box'))['open'],'Held Close clicked Room Code underneath'
            assert any(t['name']==self.room_action for t in (await self.query('vr.targets'))['targets'])
            self.cases.append({'case':'real-player-mute-mute-all-restore-and-modal-close-no-fallthrough','passed':True})
        finally:
            # A failure must preserve its visible state for diagnosis. Do not
            # dispatch speculative cleanup into a changed popup or remote slot.
            now=await self.query('net.get_state')
            self.cases.append({'case':'settings-restored','passed':now['muted']==net['muted'] and now['mute_all']==original_all})

    async def disconnect(self,guest_pid):
        sys.path.insert(0,str(ROOT/'tools'))
        from flatout_ui_atlas import NativeGame
        guest=NativeGame(guest_pid,[],10)
        g=await asyncio.to_thread(guest.call,'game.get_state');assert g['diagnostic_save_slot']==64109901,g
        h=await self.query('net.get_state');g=await asyncio.to_thread(guest.call,'net.get_state')
        assert h['lan'] and h['private'] and h['is_host'] and h['connected_players']==2
        assert g['lan'] and g['private'] and not g['is_host'] and g['connected_players']==2 and g['room_code']==h['room_code']
        self.remote=next(s['index'] for s in h['slots'] if not s['local'])
        targets=(await self.query('vr.targets'))['targets']
        options=await self.popup() if any(t['name']=='party.options.close' for t in targets) else await self.open_peer()
        name=next(n for n,v in options.items() if 'Volume' in v)
        original=await self.volume();await self.hover(name);held=await self.hold_volume(name,original)
        clicks=(await self.query('vr.targets'))['clicks']
        if not (await asyncio.to_thread(guest.call,'ui.get_message_box'))['open']:
            await asyncio.to_thread(guest.call,'menu.back')
        text=[n.get('text','') for n in (await asyncio.to_thread(guest.call,'hud.rects',{'root':'msgbox','max_depth':20}))['nodes']]
        assert (await asyncio.to_thread(guest.call,'ui.get_message_box'))['open'] and ('Exit Lobby' in text and 'Quit the match' in text),text
        await asyncio.to_thread(guest.call,'ui.confirm_message_box',{'cancel':False})
        deadline=asyncio.get_running_loop().time()+15
        while True:
            now=await self.query('net.get_state');g=await asyncio.to_thread(guest.call,'net.get_state')
            if now['connected_players']==1 and not g['alive']:break
            assert asyncio.get_running_loop().time()<deadline,(now,g);await asyncio.sleep(.05)
        targets=await self.query('vr.targets')
        assert not any(t['name'].startswith(('popup.','party.options.')) or t['name']=='player.'+str(self.remote) for t in targets['targets']),targets
        assert targets['clicks']==clicks
        # Continue the existing held ray after its player disappears. It must
        # never become a fresh click on a room action occupying that space.
        held['command_id']=uuid.uuid4().hex
        result=await self.tool('hold_ui_at',held,'held-ray-after-peer-left');assert result['status']=='input_delivered'
        assert (await self.query('vr.targets'))['clicks']==clicks
        assert not (await self.query('ui.get_message_box'))['open']
        assert (await self.query('net.get_state'))['mic_muted']==h['mic_muted']
        self.cases.append({'case':'real-peer-disconnect-retires-popup-and-held-target-without-fallthrough','passed':True,'guest_network_closed':True})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Two owned isolated LAN clients, actual XR host; no acoustic voice qualification'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=PeerTest(a,session)
            try:
                if a.disconnect_peer:await t.disconnect(a.disconnect_peer)
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--disconnect-peer',type=int);asyncio.run(run(p.parse_args()))
