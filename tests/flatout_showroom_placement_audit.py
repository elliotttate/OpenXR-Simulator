"""Read-only policy check on native car-workspace atlas captures."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'tests')]
from flatout_livery_e2e import ui_surface
from flatout_ui_atlas import artifact_path


def audit(folder):
    original=(folder/'report.json').read_bytes();report=json.loads(original)
    cases=[];observed=set()
    for state in report['states']:
        if state['case'] not in ('cars','garage','upgrade','customization'):continue
        s=json.loads(artifact_path(folder,state['metadata']).read_text(encoding='utf-8'))
        frame=s['targets']['vr_frame'];surface=ui_surface({'vr':{'frame':frame}})
        import numpy as np
        from flatout_frontend_e2e import quaternion_matrix
        normal=quaternion_matrix(surface['pose']['orientation'])[:,2]
        head=np.array(s['capture']['geometry']['head']['position'])
        depth=abs(float(np.dot(np.array(surface['pose']['position'])-head,normal)))
        violations=[]
        if abs(depth-3)>.03:violations.append('workspace_plane_not_3m')
        if not np.allclose(surface['size'],[4.5,2.53125],atol=.001):violations.append('workspace_scale_differs')
        footers=[t for t in s['targets']['targets'] if t['name'].startswith('helper.')]
        if not footers:violations.append('missing_footer')
        for footer in footers:
            l,t,r,b=footer['rect']
            if abs(b-1056)>.5 or b-t<95.5:violations.append('footer_spacing_or_size:'+footer['name'])
        observed.add(state['case'])
        cases.append({'case':state['case'],'state':state['id'],'plane_depth_m':depth,
                      'surface':surface,'footers':footers,'violations':violations,'passed':not violations})
    assert observed=={'cars','garage','upgrade','customization'},observed
    assert (folder/'report.json').read_bytes()==original
    return {'passed':bool(cases) and all(c['passed'] for c in cases),'cases':cases,
            'source_report':str(folder/'report.json'),'source_unchanged':True,'runtime_access':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();assert not args.output.exists();result=audit(args.report)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'states':len(result['cases']),
                      'violations':sum(len(c['violations']) for c in result['cases'])}))
    raise SystemExit(not result['passed'])
