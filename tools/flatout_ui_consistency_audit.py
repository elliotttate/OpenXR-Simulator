"""Compare immutable atlas geometry across menu families; never access a runtime.

Pass older reports first. A later report replaces the captured cases it contains,
so historical screenshots cannot be mistaken for current layout regressions.
Measurements describe submitted geometry, not lens visibility or visual style.
"""
import argparse
from collections import defaultdict
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mcp-server'));sys.path.insert(0,str(ROOT/'tests'))
from flatout_ui_atlas import artifact_path,comfort_review_targets,control_inventory
from ui_comfort import analyze_visibility


def distance(a,b):return math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))


def family(case):
    if case.startswith('dialog.'):return 'modal'
    if case in ('main','multiplayer','extra','career'):return 'ordinary spatial navigation'
    if case in ('livery','livery-modal'):return 'car-view workspace exception'
    if case in ('cars','garage','upgrade','customization','party-drivers','single-car'):return 'showroom / preview'
    return 'reading / selection'


def audit(folders):
    sources=[];selected={};coverage={}
    for folder in folders:
        folder=Path(folder).resolve();path=folder/'report.json';raw=path.read_bytes();report=json.loads(raw)
        sources.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),
                        'capture_started':report.get('started'),'native_references':report.get('native_references',[])})
        groups=defaultdict(list)
        for state in report.get('states',[]):groups[state['case']].append(state)
        for case,states in groups.items():selected[case]=(folder,states)
        for entry in report.get('source_coverage',[]):
            key=(entry['kind'],entry['id'])
            if key not in coverage or entry['status']=='captured':coverage[key]=entry
    cases=[]
    for case,(folder,states) in selected.items():
        observations=[];missing=[];errors=[]
        for state in states:
            snapshot=json.loads(artifact_path(folder,state['metadata']).read_text(encoding='utf-8'))
            capture=snapshot.get('capture',{});inventory=control_inventory(snapshot)
            targets,unmeasured=comfort_review_targets(capture,snapshot)
            missing.extend({'state':state['id'],**m} for m in unmeasured)
            if not capture.get('geometry',{}).get('supported'):
                errors.append({'state':state['id'],'reason':'No supported native geometry'});continue
            analysis=analyze_visibility(capture,targets)
            controls={c['name']:c for c in inventory['controls']}
            head=capture['geometry']['head']['position']
            for target in analysis['targets']:
                points=target['world_corners'];centre=[sum(p[i] for p in points)/4 for i in range(3)]
                a=[points[1][i]-points[0][i] for i in range(3)]
                b=[points[3][i]-points[0][i] for i in range(3)]
                normal=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
                length=math.sqrt(sum(v*v for v in normal))
                if length<=1e-9:continue
                normal=[v/length for v in normal];control=controls[target['name']]
                observations.append({'state':state['id'],'name':target['name'],'label':control.get('label'),
                    'physical_width_m':distance(points[0],points[1]),'physical_height_m':distance(points[0],points[3]),
                    'plane_depth_m':abs(sum((centre[i]-head[i])*normal[i] for i in range(3))),
                    'height_from_captured_eyes_m':centre[1]-head[1],
                    'gaze_elevation_deg':target['center_angle_deg']['elevation'],
                    'both_eye_frusta':target['fully_in_both_submitted_eye_frusta'],
                    'advisories':target['placement_advisories']})
        def span(key,items=observations):
            values=[v[key] for v in items]
            return [min(values),max(values)] if values else None
        footers=[o for o in observations if o['name'].startswith('helper.Cancel.') or o['name']=='spatial.back']
        cases.append({'case':case,'family':family(case),'source_report':str(folder/'report.json'),
            'states':len(states),'image':str(artifact_path(folder,states[0]['image'])) if states[0].get('image') else None,
            'plane_depth_m':span('plane_depth_m'),'control_height_m':span('physical_height_m'),
            'height_from_captured_eyes_m':span('height_from_captured_eyes_m'),
            'back_height_m':span('physical_height_m',footers),'measurements':observations,
            'unmeasured':missing,'errors':errors})
    assert all(hashlib.sha256(Path(s['path']).read_bytes()).hexdigest()==s['sha256'] for s in sources)
    return {'created':datetime.now(timezone.utc).isoformat(),'runtime_access':False,'source_reports_unchanged':True,
        'sources':sources,'cases':cases,'coverage':list(coverage.values()),'whole_game_complete':False,
        'limits':['Wreck Run deferred','A saved capture proves only its recorded build and state',
                  'Submitted geometry does not prove lens visibility, occlusion, readability or appearance',
                  'Colours, typography and state styling require rendered visual review',
                  'Car-view workspaces may deliberately use side or lower panels',
                  'Network, progression and in-race gaps remain explicit in source coverage']}


def write_report(result,output):
    output.mkdir(parents=True,exist_ok=False)
    (output/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    fmt=lambda v:'unmeasured' if v is None else f'{v[0]:.3f}–{v[1]:.3f}'
    lines=['# FlatOut UI consistency audit','',
        'Measured comparison of saved native states. Later reports replace earlier cases. '
        'Distances are perpendicular plane depths; heights are relative to captured eyes. '
        'Ranges include hover motion and individual controls. These are review measurements, not blanket pass/fail thresholds.','',
        '| Case | Family | Native states | Plane depth (m) | Back height (m) | Evidence |',
        '|---|---|---:|---:|---:|---|']
    for c in result['cases']:
        link=f'[Screenshot]({Path(c["image"]).as_posix()})' if c['image'] else 'No image'
        lines.append(f'| {c["case"]} | {c["family"]} | {c["states"]} | {fmt(c["plane_depth_m"])} | {fmt(c["back_height_m"])} | {link} |')
    lines+=['','## Coverage gaps','']
    lines += [f'- {e["kind"]} **{e["id"]}**: {e["status"]}. {e.get("reason","")}'
              for e in result['coverage'] if e['status']!='captured']
    lines+=['','## Limits','']+[f'- {v}' for v in result['limits']]
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,action='append',required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=audit(args.report);write_report(result,args.output)
    print(json.dumps({'cases':len(result['cases']),'states':sum(c['states'] for c in result['cases']),
                      'measured_observations':sum(len(c['measurements']) for c in result['cases']),
                      'unmeasured_observations':sum(len(c['unmeasured']) for c in result['cases']),
                      'whole_game_complete':False}))
