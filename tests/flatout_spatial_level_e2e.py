"""Verify level spatial menu artwork and real XR hover, preserving authored layout."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'pid':args.pid,'hand':args.hand}
    args.skip_menu_index=args.skip_menu_index or []
    args.spatial_click_index=None
    args.require_physical_lift=True
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Frontend(args,session)
            try:
                menu=await test.query('menu.get_state')
                assert menu['is_3d'] and menu['current_menu']==args.menu,menu
                layout=menu['spatial_layout']
                (args.evidence/'layout.json').write_text(json.dumps(layout,indent=2))
                await test.capture('spatial-level-source')
                i,j,k=[np.array(layout[a]) for a in ('right','up','back')]
                unit=[v/np.linalg.norm(v) for v in (i,j,k)]
                assert np.allclose(unit[1],[0,0,1],atol=1e-5),('Menu leans away from world up',unit[1].tolist())
                assert abs(unit[0][2])<1e-5 and abs(unit[2][2])<1e-5,('Menu has pitch or roll',layout)
                assert max(abs(np.dot(unit[a],unit[b])) for a,b in ((0,1),(0,2),(1,2)))<1e-5
                positions=[np.array(item['rest_position']) for item in menu['items']]
                assert all(np.linalg.norm((p-positions[0])[:2])<1e-4 for p in positions),'Rows do not stack vertically'
                if args.reference_layout:
                    original=json.loads(args.reference_layout.read_text(encoding='utf-8'))
                    assert np.allclose(layout['origin'],original['origin'],atol=1e-4),'Leveling moved the authored anchor'
                    for axis in ('right','up','back'):
                        assert abs(np.linalg.norm(layout[axis])-np.linalg.norm(original[axis]))<1e-5,('Leveling changed authored scale',axis)
                    old_yaw=np.array(original['back'])[:2];old_yaw/=np.linalg.norm(old_yaw)
                    new_yaw=k[:2]/np.linalg.norm(k[:2])
                    assert np.allclose(old_yaw,new_yaw,atol=1e-5),'Leveling changed authored yaw'
                    old_right=np.array(original['right'])[:2];old_right/=np.linalg.norm(old_right)
                    new_right=i[:2]/np.linalg.norm(i[:2])
                    assert np.dot(old_right,new_right)>.999,'Leveling mirrored the horizontal artwork basis'
                test.cases.append({'case':'world-up-and-preserved-authored-layout','passed':True,'layout':layout})
                await test.spatial_hover()
                await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'level-menu-both-eyes')
                result['native_errors']=await test.query('game.errors')
                assert result['native_errors']['total']==0,result['native_errors']
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--menu',default='FLATOUT')
    p.add_argument('--reference-layout',type=Path)
    p.add_argument('--skip-menu-index',type=int,action='append')
    p.add_argument('--hand',choices=('left','right'),default='right')
    asyncio.run(run(p.parse_args()))
