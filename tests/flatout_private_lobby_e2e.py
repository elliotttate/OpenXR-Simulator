"""Start in a private host lobby with bots and no other human players.

Verify an unlimited private lobby offers a physically raised Ready button.
The optional --start flag starts this isolated bot race through real XR input.
"""
import argparse, asyncio, json, os, sys, uuid
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_menu_depth_e2e import MenuDepth, ROOT
from flatout_frontend_e2e import quad_pixel
from flatout_livery_e2e import screenshot_pixel, ui_surface

class PrivateLobby(MenuDepth):
    async def run(self):
        net = await self.query('net.get_state')
        assert net['private'] and net['is_host'] and net['connected_players'] == 1 and net['bots'], net
        assert net['state'] == 'InLobby' and net['lobby_timeout'] == -1 and not net['local_ready'], net
        before = await self.query('vr.targets')
        self.last_capture = await self.capture('private-lobby')
        self.cases.append({'case': 'private-bots-ready-visible', 'network': net, 'targets': before})
        ready = next((t for t in before['targets'] if t['name'].startswith('helper.Secondary.')), None)
        assert ready, 'Private lobby with bots and no countdown has no Ready ray button'
        self.args.target = [ready['name']]
        await super().run()
        if self.args.start:
            capture = await self.capture('ready-source')
            state = await self.query('vr.targets')
            target = next(t for t in state['targets'] if t['name'] == ready['name'])
            l,t,r,b = target['rect']
            if state.get('vr_frame'):
                frame = {'vr': {'frame': state['vr_frame']}}
                x,y = screenshot_pixel(frame,capture,(l+r)/2,(t+b)/2)
                surface = {'surface':ui_surface(frame)}
            else:
                x,y = quad_pixel(capture,target['rect']);surface = {}
            args = {'capture_id':capture['capture_id'],'x':float(x),'y':float(y),
                    'hand':self.args.hand,'origin_mode':'hand','hold_frames':2,
                    'dwell_frames':2,'command_id':uuid.uuid4().hex,**surface}
            clicked = await self.tool('click_ui_at',args,'ready-once')
            retry = await self.tool('click_ui_at',args,'ready-retry')
            assert retry['delivery']['sync_id'] == clicked['delivery']['sync_id']
            for _ in range(20):
                current = await self.query('net.get_state')
                if current['local_ready'] or current['state'] != 'InLobby':break
                await asyncio.sleep(.2)
            assert current['local_ready'] or current['state'] != 'InLobby', current
            self.cases.append({'case':'ready-activated-once','passed':True,'network':current})

async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    args.target=[];args.legacy_index=[];args.click=None;args.gpu_capture=None;args.reading_distance=None;args.reading_width=4.5
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'pid':args.pid}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=PrivateLobby(args,session)
            try:await test.run();result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['cases']=test.cases;(args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--start',action='store_true');asyncio.run(main(p.parse_args()))
