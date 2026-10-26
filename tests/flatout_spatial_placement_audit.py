"""Audit actual native atlas frames against the ordinary-menu placement policy.

This re-measures saved geometry; it never launches a game or sends input.
The preferred region is a project review policy, not a human-comfort certificate.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'mcp-server'))
from flatout_ui_atlas import artifact_path, comfort_review_targets
from ui_comfort import analyze_visibility


def placement_metrics(snapshot):
    """Measure rest geometry, so a legitimate 15 mm hover is not drift."""
    menu=snapshot['menu'];layout=menu['spatial_layout']
    camera=snapshot['targets']['vr_frame']['camera']
    head=snapshot['capture']['geometry']['head']['position']
    eye=[camera['origin'][i]+sum(camera[k][i]*head[j]
         for j,k in enumerate(('right','up','back'))) for i in range(3)]
    length=lambda v:math.sqrt(sum(x*x for x in v))
    normal=layout['back'];normal=[v/length(normal) for v in normal]
    positions=[item['rest_position'] for item in menu['items']]
    if menu.get('spatial_back'):positions.append(menu['spatial_back']['position'])
    depths=[abs(sum((p[i]-eye[i])*normal[i] for i in range(3))) for p in positions]
    heights=[]
    for item in menu['items']:
        corners=item['rendered_bounds']['corners']
        heights.append(length([corners[0][i]-corners[3][i] for i in range(3)]))
    centre=(min(p[2] for p in positions)+max(p[2] for p in positions))*.5-eye[2]
    up=layout['up'];upright=math.degrees(math.acos(min(1,max(-1,up[2]/length(up)))))
    violations=[]
    if any(abs(d-3)>.025 for d in depths):violations.append('navigation_plane_not_3m')
    if abs(centre+1)>.03:violations.append('column_centre_not_1m_below_opening_eyes')
    if abs(layout['scale']-.08)>.0001:violations.append('navigation_type_scale_differs')
    if upright>.01:violations.append('navigation_not_upright')
    return {'plane_depth_m':depths,'column_centre_below_eyes_m':-centre,
            'label_height_m':heights,'scale':layout['scale'],'tilt_deg':upright,
            'violations':violations}


def audit(source):
    folder=Path(source);report_file=folder/'report.json'
    original=report_file.read_bytes();report=json.loads(original)
    expected={'MAIN MENU','MULTIPLAYER','EXTRAS','FLATOUT'}
    observed=set();cases=[]
    for state in report['states']:
        snapshot=json.loads(artifact_path(folder,state['metadata']).read_text(encoding='utf-8'))
        menu=snapshot['menu']
        if menu.get('current_menu') not in expected or not menu.get('is_3d'):continue
        assert not snapshot.get('message',{}).get('open'),'Audit needs the actual unobstructed menu'
        capture=snapshot['capture'];assert capture['geometry']['supported']
        targets,missing=comfort_review_targets(capture,snapshot)
        assert not missing and targets,missing
        analysis=analyze_visibility(capture,targets)
        placement=placement_metrics(snapshot)
        outside=[t['name'] for t in analysis['targets'] if not t['preferred_gaze_zone']]
        clipped=[t['name'] for t in analysis['targets'] if not t['fully_in_both_submitted_eye_frusta']]
        observed.add(menu['current_menu'])
        cases.append({'menu':menu['current_menu'],'state':state['id'],'passed':not outside and not clipped and not placement['violations'],
            'placement':placement,
            'capture_id':capture['capture_id'],'session':capture['geometry']['session_id'],
            'frame':capture['geometry']['frame_id'],'target_count':len(targets),
            'outside_preferred':outside,'clipped':clipped,
            'elevation_bounds':{t['name']:t['angular_bounds_deg']['elevation'] for t in analysis['targets']}})
    assert observed==expected,('Missing ordinary spatial menus',expected-observed)
    assert report_file.read_bytes()==original
    return {'passed':bool(cases) and all(c['passed'] for c in cases),
        'scope':'Main, Multiplayer, Extras and Career native label-layout/Back placement; no collider/ink inference',
        'source_report':str(report_file),'source_report_sha256':hashlib.sha256(original).hexdigest(),
        'runtime_access':False,'cases':cases,'source_unchanged':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    assert not args.output.exists(),'Preserve existing audit receipts'
    result=audit(args.report);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'states':len(result['cases']),
        'outside_preferred_observations':sum(len(c['outside_preferred']) for c in result['cases']),
        'consistency_violations':sum(len(c['placement']['violations']) for c in result['cases']),
        'clipped_observations':sum(len(c['clipped']) for c in result['cases'])}))
    raise SystemExit(0 if result['passed'] else 1)
