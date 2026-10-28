"""Car workspace placement, head movement and both-hand physical ray feedback.

Uses normal menu callbacks on a disposable owned-car profile. Does not purchase,
equip an item, enter Wreck Run or start a race.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'mcp-server')]
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,quaternion_matrix
from flatout_livery_e2e import ui_surface
from flatout_ui_atlas import Atlas,comfort_review_targets
from ui_comfort import analyze_visibility


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    args.output=args.evidence;args.current=False;args.owner_queue=None
    args.native_timeout=8;args.settle=.25;args.dwell_frames=3
    args.fixture_timeout=20;args.fixture=['showroom'];args.hand='right'
    result={'passed':False,'pid':args.pid,'cases':[],'operations':[]}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            atlas=Atlas(args,session,result);atlas.active_case={'id':'showroom-consistency'}
            test=Frontend(args,session);original=None
            async def release():
                if test.last_capture:
                    for hand in ('left','right'):
                        await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand},'release-'+hand)
            try:
                initial=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'initial')
                original=initial['geometry']['head'];test.last_capture=initial
                assert (await test.query('game.errors'))['total']==0
                for height in (1.7,1.2):
                    pose={'x':0,'y':height,'z':0,'yaw_deg':0,'pitch_deg':0,'roll_deg':0}
                    await release();await atlas.main()
                    await test.observed_head_pose('entry-'+str(height),pose)
                    for alias in ('garage','cars','upgrade','customization'):
                        await release();await atlas.normal_flow(alias)
                        label=alias+'-'+str(height)
                        native=await test.query('vr.targets');menu=await test.query('menu.get_state')
                        capture=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},label)
                        test.last_capture=capture
                        surface=ui_surface({'vr':{'frame':native['vr_frame']}})
                        assert np.allclose(surface['size'],[4.5,2.53125],atol=.001),surface
                        assert np.allclose(surface['pose']['position'],[0,height-.86527778,-3],atol=.003),surface
                        basis=quaternion_matrix(surface['pose']['orientation'])
                        assert np.allclose(basis,np.eye(3),atol=.0001),surface
                        footers=[t for t in native['targets'] if t['name'].startswith('helper.')]
                        assert footers and abs(min(t['rect'][0] for t in footers)+max(t['rect'][2] for t in footers)-1920)<1,footers
                        assert all(abs(t['rect'][3]-1056)<.5 and t['rect'][3]-t['rect'][1]>=95.5 for t in footers),footers
                        snapshot={'menu':menu,'targets':native,'message':await test.query('ui.get_message_box'),'nodes':[],'capture':capture}
                        targets,missing=comfort_review_targets(capture,snapshot)
                        visibility=analyze_visibility(capture,targets)
                        assert not missing and all(t['fully_in_both_submitted_eye_frusta'] for t in visibility['targets']),visibility
                        await test.observed_head_pose(label+'-moved',{**pose,'x':.1,'y':height+.12,'z':.05,'yaw_deg':6,'pitch_deg':-4,'roll_deg':3})
                        moved=ui_surface({'vr':{'frame':(await test.query('vr.targets'))['vr_frame']}})
                        assert np.allclose(moved['pose']['position'],surface['pose']['position'],atol=.001),'UI followed head translation'
                        assert np.allclose(moved['pose']['orientation'],surface['pose']['orientation'],atol=.0001),'UI followed head rotation'
                        await test.observed_head_pose(label+'-restored',pose)
                        back=next(t for t in footers if t['name'].startswith('helper.Cancel.'))
                        l,t,r,b=back['rect'];center=np.array(surface['pose']['position'])+basis@np.array([((l+r)/3840-.5)*4.5,(.5-(t+b)/2160)*2.53125,0])
                        for hand in ('right','left'):
                            args.hand=hand
                            before,hovered,_=await test.hover(back['name'])
                            assert hovered['clicks']==before['clicks'],'Hover activated Back'
                            raised=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-'+hand+'-raised')
                            test.last_capture=raised;matches=[]
                            for tile in raised['geometry']['surfaces']:
                                delta=basis.T@(np.array(tile['pose']['position'])-center)
                                if tile['visibility']==0 and abs(delta[2]-.020)<.001 and abs(delta[0])<tile['size'][0]/2 and abs(delta[1])<tile['size'][1]/2:
                                    matches.append({'surface':tile,'offset_m':delta.tolist()})
                            assert matches,('No physical 20mm stereo Back face',label,hand,raised['geometry']['surfaces'])
                            result['cases'].append({'case':label+'-'+hand+'-physical-hover','passed':True,'matches':matches})
                            await release()
                        await test.click(back['name'])
                        after=await test.query('menu.get_state')
                        assert after['current_menu']!=menu['current_menu'] or not after['is_open'],('Back did not leave the workspace',after)
                        result['cases'].append({'case':label+'-placement-world-lock-and-back','passed':True,'surface':surface,'visibility':visibility['summary']})
                        print(label+' passed',flush=True)
                result['native_errors']=await test.query('game.errors');assert result['native_errors']['total']==0
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                await release()
                if original:
                    x,y,z=original['position']
                    await test.observed_head_pose('restore-original',{'x':x,'y':y,'z':z,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                result['ray_cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
