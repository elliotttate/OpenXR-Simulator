"""Reuse actual LAN voice/held-trigger regressions on the native race lobby."""
import argparse,asyncio,json,os,re,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_friends_peer_e2e import PeerTest
from flatout_frontend_e2e import ROOT
from flatout_ui_depth_contract import assert_reading_distance


class RacePeerTest(PeerTest):
    room_action='lobby.action.1'

    async def prove(self):
        net=await self.query('net.get_state');remaining=net['lobby_timeout']
        assert remaining<0 or remaining>70,'Start the timed peer regression immediately after the owned join'
        if remaining>=0:
            nodes=await self.nodes();text=next(n['text'] for n in nodes if n['path'].endswith('/RaceLobbyReadiness') and n['effective_visible'])
            seconds=int(re.search(r'/\s*(\d+)s$',text).group(1))
            assert abs(seconds-remaining)<3,(text,remaining)
            assert not any(n['effective_visible'] and n['path'].endswith(('/Lobby Infos','/Lobby Infos/TIME')) for n in nodes),'Duplicate legacy timer remained visible'
            self.cases.append({'case':'actual-room-countdown-visible-in-native-header','passed':True,'remaining':remaining,'label':text})
        await super().prove()
    async def popup(self):
        targets=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in targets}<={'lobby.options.close'}|{'popup.'+str(i) for i in range(7)},targets
        assert all(t['enabled'] and t['physical_hover'] for t in targets)
        nodes=await self.nodes()
        options={t['name']:next(n['text'] for n in nodes if n['visible'] and n['path'].endswith('/RaceLobbyOption'+t['name'].split('.')[1]+'/RaceLobbyOptionLabel')) for t in targets if t['name'].startswith('popup.')}
        assert 5<=len(options)<=7 and any('Kick' in s for s in options.values()) and any('Volume' in s for s in options.values()),options
        await self.audit('race-player-popup-spacing')
        source=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'race-player-options-stereo');self.last_capture=source;assert_reading_distance(source,3.,4.5)
        return options

    async def open_peer(self):
        # Both owned clients can have the same Steam display name. The actual
        # row action distinguishes the remote human from our microphone row.
        nodes=await self.nodes();row=next(n['path'].rsplit('/',2)[0].rsplit('/RaceLobbyPlayer',1)[1] for n in nodes if n['path'].endswith('/RaceLobbyPlayerActionLabel') and n['visible'] and n.get('text')=='Player options')
        await self.click('player.'+row);return await self.popup()

    async def volume(self):
        label=next(n['text'] for n in await self.nodes() if n['path'].endswith('/RaceLobbyOptionLabel') and n['visible'] and 'Volume' in n.get('text',''))
        return int(re.search(r'(\d+)\s*%',label).group(1))

    async def click(self,name,fraction=(.5,.5)):
        return await super().click('lobby.options.close' if name=='party.options.close' else name,fraction)

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Two owned LAN clients, native race-lobby player options and real XR; no acoustic voice claim'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=RacePeerTest(a,session)
            try:
                if a.prove_and_disconnect:
                    await t.prove();await t.disconnect(a.prove_and_disconnect)
                elif a.disconnect_peer:await t.disconnect(a.disconnect_peer)
                else:await t.prove()
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--disconnect-peer',type=int);p.add_argument('--prove-and-disconnect',type=int);asyncio.run(run(p.parse_args()))
