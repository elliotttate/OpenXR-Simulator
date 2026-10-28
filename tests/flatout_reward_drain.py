"""Finish an open reward-preview queue with real ray clicks on Continue (no state injection)."""
import argparse,asyncio,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_reward_native_ui_e2e import Rewards,ACTION
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Rewards(a,session)
            stage=Atlas(SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand=a.hand,ray_presentation=True),session,{'operations':[]})
            t.query_lock=stage.lock;await t.ray_mode()
            for _ in range(8):
                s=await t.query('ui.reward_state')
                if not s['open']:break
                await t.wait_phase(s['presentation'],('ready',));await t.click(ACTION)
                await asyncio.sleep(.6)
            print(json.dumps(await t.query('ui.reward_state')))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
