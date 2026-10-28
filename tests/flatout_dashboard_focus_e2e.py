"""Replay dashboard-like input-focus loss during Welcome in the simulator.

This tests the FOCUSED/VISIBLE boundary and native rendered frames. It does not
emulate the SteamVR compositor or certify the hardware dashboard artifact.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image,ImageDraw
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False);args.hand='right'
    result={'passed':False,'scope':__doc__,'parent_dialog':args.parent_dialog,'cases':[],'operations':[],'focus_transitions':[]}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Frontend(args,session);initial=None
            native=NativeGame(args.pid,result['operations'],8)
            async def focus(value):
                started=asyncio.get_running_loop().time()
                response=await test.tool('set_session_focus',{'focused':value},'focus-'+str(value))
                assert response.get('queued'),response
                deadline=asyncio.get_running_loop().time()+10
                while asyncio.get_running_loop().time()<deadline:
                    state=await session.call_tool('get_session_state',{})
                    text=' '.join(getattr(c,'text','') for c in state.content)
                    if 'Session State: '+('FOCUSED' if value else 'VISIBLE') in text:
                        result['focus_transitions'].append({'focused':value,'seconds':asyncio.get_running_loop().time()-started})
                        return
                    await asyncio.sleep(.05)
                raise AssertionError(('Focus transition not observed',value,text))
            async def stable_popup(label):
                capture=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},label)
                test.last_capture=capture
                assert (await test.query('ui.get_message_box'))['open']
                assert (await test.query('menu.get_state'))['is_open']==args.parent_dialog,'Unexpected parent-menu visibility'
                nodes=(await test.query('hud.rects',{'root':'msgbox','max_depth':48}))['nodes']
                body=next(n for n in nodes if n['visible'] and n['path'].endswith('/RayDialogBody'))
                l,t,r,b=body['rect'];sample=(l+8,t+8,l+24,b-8)
                pixels=np.asarray(Image.open(test.last_image_path).convert('RGB'))
                for eye in ('left','right'):
                    polygon=[quad_pixel(capture,(x,y,x,y),eye) for x,y in
                             ((sample[0],sample[1]),(sample[2],sample[1]),(sample[2],sample[3]),(sample[0],sample[3]))]
                    mask=Image.new('1',(pixels.shape[1],pixels.shape[0]));ImageDraw.Draw(mask).polygon(polygon,fill=1)
                    values=pixels[np.asarray(mask,dtype=bool)]
                    assert len(values)>20 and np.percentile(values.max(axis=1),90)<110,(label,eye,'Popup plate exposed the scene')
                parent=max(capture['geometry']['surfaces'],key=lambda s:np.prod(s['size']))
                result['cases'].append({'case':label,'passed':True,'capture_id':capture['capture_id'],
                                        'frame':capture['geometry']['frame_id'],'surface':parent})
                return capture,parent
            try:
                if args.parent_dialog:
                    assert not (await test.query('ui.get_message_box'))['open']
                    assert (await test.query('menu.get_state'))['current_menu']=='MAIN MENU'
                    await asyncio.to_thread(native.call,'ui.pop_message_box',{'type':'YesNoNoTitle',
                        'title':'Focus diagnostic','content':'Input focus handoff diagnostic. No original game action is attached.'})
                    await asyncio.sleep(.25)
                await test.observed_head_pose('initial-neutral',{'x':0,'y':1.7,'z':0,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                initial,parent=await stable_popup('welcome-focused')
                head=initial['geometry']['head']['position']
                for cycle in range(3):
                    # Hold on inert dialog padding; no real action should activate
                    # from losing focus or returning with a trigger still latched.
                    _,aim=await test.point_args('msgbox.validate',(.5,-2))
                    held=await test.tool('hold_ui_at',aim,'padding-held-'+str(cycle))
                    assert held['status']=='input_delivered',held
                    assert (await test.query('vr.get_state'))['controllers'][1]['trigger']>.8
                    await focus(False)
                    assert all(c['trigger']==0 for c in (await test.query('vr.get_state'))['controllers'])
                    pose={'x':head[0]+.03,'y':head[1],'z':head[2],'yaw_deg':6,'pitch_deg':-3,'roll_deg':2}
                    await test.observed_head_pose('visible-head-motion-'+str(cycle),pose)
                    capture,current=await stable_popup('welcome-visible-'+str(cycle))
                    assert capture['geometry']['frame_id']>initial['geometry']['frame_id']
                    assert np.allclose(current['pose']['position'],parent['pose']['position'],atol=.001)
                    assert np.allclose(current['pose']['orientation'],parent['pose']['orientation'],atol=.001)
                    await focus(True)
                    assert all(c['trigger']==0 for c in (await test.query('vr.get_state'))['controllers']),'Held input resumed after focus returned'
                    await test.observed_head_pose('restored-head-'+str(cycle),{'x':head[0],'y':head[1],'z':head[2],'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                    await stable_popup('welcome-refocused-'+str(cycle))
                await test.click('msgbox.cancel' if args.parent_dialog else 'msgbox.validate')
                await asyncio.sleep(.3)
                assert (await test.query('menu.get_state'))['is_open']
                assert not (await test.query('ui.get_message_box'))['open']
                result['native_errors']=await test.query('game.errors');assert result['native_errors']['total']==0
                result['cases'].append({'case':'single-modal-dismiss-after-focus-cycles' if args.parent_dialog else 'single-acknowledgement-after-focus-cycles','passed':True})
                result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if test.last_capture:
                    await focus(True)
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})
                if initial:
                    x,y,z=initial['geometry']['head']['position']
                    await test.observed_head_pose('restore-initial-head',{'x':x,'y':y,'z':z,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--parent-dialog',action='store_true',help='From Main after Welcome: test an owned diagnostic confirmation over the spatial menu')
    asyncio.run(run(p.parse_args()))
