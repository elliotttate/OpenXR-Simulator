"""Check that returning from Orbit cannot strand native cards mid-entrance."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report={'passed':False,'menu':args.menu}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Frontend(args,session)
            try:
                assert (await t.query('menu.get_state'))['current_menu']==args.menu.upper()
                await asyncio.sleep(2)
                # Rebind a page after the clock has advanced, then return to an
                # available first item. The old bug kept this late page timestamp
                # while resetting the clock when Orbit hid the workspace.
                for name in (('class.1','class.0') if args.menu=='cars' else ('drivers.next','drivers.prev')):
                    await t.click(name)
                await t.click('helper.Y.0')
                targets=(await t.query('vr.targets'))['targets']
                assert len(targets)==1 and targets[0]['name']=='helper.Cancel.0',targets
                await t.click('helper.Cancel.0')
                stem='Cars' if args.menu=='cars' else 'Drivers'
                deadline=asyncio.get_running_loop().time()+1.2
                while True:
                    nodes=(await t.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
                    cards=[n for n in nodes if '/'+stem+'Workspace/'+stem+'Card' in n['path'] and n['path'].count('/')==3 and n['visible']]
                    root=next(n for n in nodes if n['path'].endswith('/'+stem+'Workspace'))
                    if root['color']['a']>.99 and cards and all(n['color']['a']>.99 for n in cards):break
                    assert asyncio.get_running_loop().time()<deadline,{'root':root,'cards':cards}
                    await asyncio.sleep(.05)
                report.update(passed=True,cards=[{'path':n['path'],'alpha':n['color']['a']} for n in cards])
                await t.tool('capture_screenshot',{'eye':'both','max_width':2048},'restored-opaque-workspace')
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',required=True,type=int);p.add_argument('--menu',choices=('cars','drivers'),required=True)
    p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',default='right',choices=('right','left'))
    asyncio.run(run(p.parse_args()))
