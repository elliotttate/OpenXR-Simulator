"""Leave an in-race fixture through the real pause Main Menu row (ray clicks only)."""
import argparse,asyncio,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_pause_native_ui_e2e import Pause
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Pause(a,session)
            stage=Atlas(SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand=a.hand,ray_presentation=True),session,{'operations':[]})
            t.query_lock=stage.lock
            if not (await t.query('hud.pause_menu'))['is_open']:await t.press_menu()
            await t.ray_mode();items=(await t.query('hud.pause_menu'))['items']
            row=next(i for i,title in enumerate(items) if title.strip().lower()=='main menu')
            await t.click('pause.'+str(row))
            if (await t.query('ui.get_message_box')).get('open'):await t.click('msgbox.validate')
            await t.until(lambda:t._main(),'Main-menu row did not return to the frontend',240)
            print(json.dumps({'items':items,'errors':await t.query('game.errors')}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
