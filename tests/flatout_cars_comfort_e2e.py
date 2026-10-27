"""Check native showroom controls against actual stereo frusta and gaze guidance."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT
from flatout_livery_e2e import ui_surface


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result={'passed':False,'scope':'Actual submitted eye coverage and project advisory control envelope; not lens or headset qualification'}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            t=Frontend(args,session)
            try:
                assert (await t.query('menu.get_state'))['current_menu']==args.menu.upper()
                capture=await t.tool('capture_screenshot',{'eye':'both','max_width':2048},'comfort-source')
                head=capture['geometry']['head']
                original=dict(zip(('x','y','z'),head['position']))
                assert all(abs(a-b)<1e-5 for a,b in zip(head['orientation'],[0,0,0,1])), 'Start with neutral rotation'
                result['postures']=[]
                try:
                    for posture,height in (('standing',1.7),('seated',1.2)):
                        capture=await t.observed_head_pose('cars-'+posture,{**original,'y':height,
                            'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                        native=await t.query('vr.targets')
                        surface=ui_surface({'vr':{'frame':native['vr_frame']}}) if native.get('vr_frame') else max(
                            (s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
                        surface={k:surface[k] for k in ('pose','size')}
                        review=await t.tool('analyze_ui_visibility',{'capture_id':capture['capture_id'],'reference_mode':'level_head',
                            'include_overlay':True,'targets':[{'name':x['name'],'role':'control','surface':surface,
                                'rect':x['rect'],'canvas_size':[1920,1080]} for x in native['targets']]},'comfort-'+posture+'-analysis')
                        violations=[{'name':x['name'],'center':x['center_angle_deg'],'advisories':x['placement_advisories']}
                            for x in review['targets'] if not x['fully_in_both_submitted_eye_frusta'] or
                            any(a in x['placement_advisories'] for a in ('outside_outer_review_envelope','small_angular_control_target'))]
                        result['postures'].append({'name':posture,'head_height':height,'summary':review['summary'],'violations':violations})
                        assert not violations, (posture,violations)
                finally:
                    await t.observed_head_pose('cars-head-restored',{**original,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
                result['passed']=True
            except Exception as error:
                result['error']=repr(error)
                raise
            finally:
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',required=True,type=int)
    p.add_argument('--data-dir',required=True,type=Path)
    p.add_argument('--evidence',required=True,type=Path)
    p.add_argument('--hand',default='right',choices=('left','right'))
    p.add_argument('--menu',default='cars',choices=('cars','drivers','upgrades'))
    asyncio.run(run(p.parse_args()))
