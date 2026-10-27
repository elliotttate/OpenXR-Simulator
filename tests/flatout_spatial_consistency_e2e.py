"""Native spatial navigation: seated/standing placement, world lock and ray lift.

Requires a disposable profile with an owned Career car, an unobstructed frontend
and the attached simulator IPC directory. Never enters Wreck Run or starts a race.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'mcp-server'))
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend
from flatout_spatial_placement_audit import placement_metrics
from flatout_ui_atlas import Atlas,comfort_review_targets
from ui_comfort import analyze_visibility


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    args.output=args.evidence;args.current=False;args.owner_queue=None
    args.native_timeout=8;args.settle=.25;args.dwell_frames=3
    args.fixture_timeout=20;args.fixture=['showroom'];args.hand='right'
    args.skip_menu_index=[2];args.require_physical_lift=True;args.spatial_click_index=None
    result={'passed':False,'pid':args.pid,'cases':[],'operations':[]}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            atlas=Atlas(args,session,result);atlas.active_case={'id':'spatial-consistency'}
            test=Frontend(args,session);original=None
            async def release():
                if test.last_capture:
                    for hand in ('left','right'):
                        await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand},'release-'+hand)
            try:
                await session.initialize()
                initial=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'initial')
                test.last_capture=initial;original=initial['geometry']['head']
                for height in (1.7,1.2):
                    pose={'x':0,'y':height,'z':0,'yaw_deg':0,'pitch_deg':0,'roll_deg':0}
                    await test.observed_head_pose('entry-height-'+str(height),pose)
                    for alias in ('main','multiplayer','extra','career'):
                        await release()
                        if alias=='main':
                            await atlas.normal_flow('extra');await atlas.main()
                        else:await atlas.normal_flow(alias)
                        label=alias+'-'+str(height)
                        capture=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},label+'-both-eyes')
                        test.last_capture=capture
                        snapshot={'menu':await test.query('menu.get_state'),'targets':await test.query('vr.targets'),
                                  'message':await test.query('ui.get_message_box'),'nodes':[],'capture':capture}
                        (args.evidence/(label+'-snapshot.json')).write_text(json.dumps(snapshot,indent=2),encoding='utf-8')
                        placement=placement_metrics(snapshot)
                        targets,missing=comfort_review_targets(capture,snapshot)
                        analysis=analyze_visibility(capture,targets)
                        assert not missing and not placement['violations'],(label,placement,missing)
                        assert all(t['preferred_gaze_zone'] and t['fully_in_both_submitted_eye_frusta'] for t in analysis['targets']),analysis
                        result['cases'].append({'case':label+'-placement','passed':True,'placement':placement,'visibility':analysis['summary']})
                        # Physical head movement must not drag the menu or reintroduce pitch/roll.
                        await test.observed_head_pose(label+'-head-motion',{**pose,'x':.1,'y':height+.12,'z':.05,'yaw_deg':6,'pitch_deg':-4,'roll_deg':3})
                        moved=await test.query('menu.get_state')
                        assert moved['spatial_layout']==snapshot['menu']['spatial_layout']
                        assert [i['rest_position'] for i in moved['items']]==[i['rest_position'] for i in snapshot['menu']['items']]
                        result['cases'].append({'case':label+'-world-lock','passed':True})
                        await test.observed_head_pose(label+'-head-restored',pose)
                        # Every enabled label, both hands; the deferred Wreck Run item is omitted.
                        args.skip_menu_index=[2] if alias=='main' else []
                        for hand in ('right','left'):
                            args.hand=hand
                            await test.spatial_hover();await release()
                        print(label+': placement, world lock and both-hand ray lift passed',flush=True)
                result['native_errors']=await test.query('game.errors')
                assert result['native_errors']['total']==0,result['native_errors']
                result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                await release()
                if original:
                    x,y,z=original['position']
                    await test.observed_head_pose('restore-original-height',{'x':x,'y':y,'z':z,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                result['ray_cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
