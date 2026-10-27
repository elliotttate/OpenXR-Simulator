"""Check real modal/parent orientation after turning the tracked head.

Run in an idle front-end menu, without an existing modal. The default opens a
diagnostic Yes/No template with no original callback. --real-quit instead opens
Main's normal Quit dialog and selects No; neither mode exits the game.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import math

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT
from flatout_livery_e2e import quaternion_matrix
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas, tracking_head_pose, validate_capture_pose


def spatial_basis(menu, vr):
    layout=menu['spatial_layout']
    camera=vr['base_camera']
    inverse=np.column_stack([camera[k] for k in ('x','y','z')]).T
    basis=inverse @ np.column_stack([layout['right'],layout['up'],-np.array(layout['back'])])
    # Authored menu transforms can include scale; compare orientation only.
    return basis / np.linalg.norm(basis,axis=0)


class ModalAlignment(Frontend):
    async def check(self,report):
        assert not (await self.query('ui.get_message_box'))['open'],'Start without a real modal'
        menu=await self.query('menu.get_state')
        original=await self.query('vr.get_state')
        pose=tracking_head_pose(original)
        baseline=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'parent-before-modal')
        if menu.get('is_3d'):
            expected=spatial_basis(menu,original)
        else:
            parent=max((s for s in baseline['geometry']['surfaces'] if s.get('visibility',0)==0),key=lambda s:np.prod(s['size']))
            expected=quaternion_matrix(parent['pose']['orientation'])
        framing=Atlas(self.args,self.session,report)
        moved={**pose,'yaw_deg':pose['yaw_deg']+33,'pitch_deg':-8,'roll_deg':5}
        owned=False
        try:
            await framing.set_head_pose(moved,'modal alignment tracked head turn')
            if self.args.real_quit:
                assert menu['current_menu']=='MAIN MENU','Real Quit test must start at Main'
                index=next(i['index'] for i in menu['items'] if i['title']=='QUIT')
                await framing.select(index)
                await asyncio.sleep(.25)
                text=[n['text'] for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes'] if n.get('text')]
                assert any('Windows' in t or 'QUIT' in t.upper() for t in text),('Unexpected real confirmation',text)
            else:
                await framing.query('ui.pop_message_box',{'type':'YesNoNoTitle','title':'VR modal alignment diagnostic',
                    'content':'Test-only dialog. No original game action is attached.'})
            owned=True
            await asyncio.sleep(.25)
            capture=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'modal-after-head-turn')
            validate_capture_pose(capture,moved)
            parent=max((s for s in capture['geometry']['surfaces'] if s.get('visibility',0)==0),key=lambda s:np.prod(s['size']))
            observed=quaternion_matrix(parent['pose']['orientation'])
            angles={axis:math.degrees(math.acos(float(np.clip(np.dot(expected[:,i],observed[:,i]),-1,1))))
                    for i,axis in enumerate(('right','up','normal'))}
            case={'case':'modal-inherits-parent-orientation','menu':menu['current_menu'],'real_quit':self.args.real_quit,
                  'passed':max(angles.values())<.1,'axis_error_degrees':angles,
                  'expected_basis':expected.tolist(),'submitted_basis':observed.tolist(),
                  'capture_id':capture['capture_id'],'session':capture['geometry']['session_id'],
                  'frame':capture['geometry']['frame_id']}
            self.cases.append(case)
            assert case['passed'],case
            second_pose={**moved,'yaw_deg':moved['yaw_deg']+12,'pitch_deg':3,'roll_deg':-4}
            await framing.set_head_pose(second_pose,'modal remains fixed during another head turn')
            followup=await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'modal-retains-parent-frame')
            validate_capture_pose(followup,second_pose)
            retained=max((s for s in followup['geometry']['surfaces'] if s.get('visibility',0)==0),key=lambda s:np.prod(s['size']))
            assert np.allclose(parent['pose']['position'],retained['pose']['position'],atol=.001),'Modal followed head translation/heading'
            assert np.allclose(quaternion_matrix(retained['pose']['orientation']),expected,atol=.001),'Modal stopped matching its parent'
            assert np.allclose(parent['size'],retained['size'],atol=.001),'Modal scale changed during head movement'
            self.cases.append({'case':'modal-stays-world-locked-and-parallel','passed':True})
            # The modal stays with its parent while the person looks away.
            # Face the parent again before selecting its screenshot-visible button.
            await framing.set_head_pose(pose,'face parent again for visible modal cancel target')
            target=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if 'cancel' in t['name'].lower() or t['name']=='msgbox.no')
            await self.click(target)
            assert not (await self.query('ui.get_message_box'))['open'],'Ray did not dismiss the tested modal'
            owned=False
            self.cases.append({'case':'parent-facing-ray-cancel-after-turn','passed':True,'target':target})
        finally:
            if owned and (await self.query('ui.get_message_box'))['open']:
                await framing.query('ui.confirm_message_box',{'cancel':True})
            if self.last_capture:
                await self.tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':self.args.hand},'release-owned-modal-ray')
            await framing.set_head_pose(pose,'modal alignment original head restore')
        assert (await self.query('game.errors'))['total']==0


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    args.output=args.evidence;args.current=False;args.owner_queue=None;args.native_timeout=8
    report={'passed':False,'pid':args.pid,'scope':'Actual stereo modal orientation and ray Cancel after head turn'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=ModalAlignment(args,session)
            try:
                await test.check(report);report['passed']=True
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                report['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',required=True,type=int)
    parser.add_argument('--data-dir',required=True,type=Path)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--hand',default='right',choices=('left','right'))
    parser.add_argument('--dwell-frames',type=int,default=3)
    parser.add_argument('--real-quit',action='store_true',help='Open Main menu Quit normally and select No by ray; never exit')
    args=parser.parse_args();asyncio.run(run(args))
