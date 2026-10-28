"""Audit actual gold-paint/text pixels in a saved native stereo capture.

This deliberately does not trust the declared caption center. It catches a
geometrically centered label that sits high inside an asymmetric paint sprite.
Use the read-only Atlas --current --button-audit capture while Welcome is open.
"""
import argparse,json,sys
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parent))
from flatout_frontend_e2e import quad_pixel


def audit(folder,node='WelcomeContinue',tolerance=2.):
    folder=Path(folder);record=snapshot=state=None
    report=json.loads((folder/'report.json').read_text(encoding='utf-8'))
    for candidate in report['states']:
        data=json.loads((folder/candidate['metadata']).read_text(encoding='utf-8'))
        found=next((r for r in data.get('button_audit',{}).get('checks',[]) if '/'+node+'/' in r['path']),None)
        if found:record,snapshot,state=found,data,candidate;break
    if record is None:raise ValueError('Capture does not contain the requested painted action: '+node)
    pixels=np.asarray(Image.open(folder/state['image']).convert('RGB'));samples=[]
    for eye in ('left','right'):
        def box(rect):
            l,t,r,b=rect;points=[quad_pixel(snapshot['capture'],(x,y,x,y),eye) for x,y in ((l,t),(r,b))]
            return [round(min(p[0] for p in points)),round(min(p[1] for p in points)),round(max(p[0] for p in points)),round(max(p[1] for p in points))]
        x0,y0,x1,y1=box(record['button']);crop=pixels[y0:y1,x0:x1]
        rgb=crop.reshape(-1,3).astype(int)
        candidates=rgb[(rgb[:,0]>190)&(rgb[:,1]>100)&(rgb[:,1]<220)&(rgb[:,2]<55)]
        if len(candidates)<100:raise ValueError('Paint is absent or insufficiently visible in '+eye)
        colors,counts=np.unique((candidates//8)*8,axis=0,return_counts=True);dominant=colors[np.argmax(counts)]
        gold=np.all(np.abs(crop.astype(int)-(dominant+3))<13,axis=2);yy,_=np.where(gold)
        paint_y=y0+float(yy.mean())
        ix0,iy0,ix1,iy1=box(record['ink']);letter_pixels=pixels[iy0:iy1,ix0:ix1]
        iy,_=np.where(letter_pixels.max(axis=2)<100)
        if len(iy)<12:raise ValueError('Caption ink is absent or clipped in '+eye)
        text_y=iy0+float(iy.min()+iy.max())*.5;delta=text_y-paint_y
        samples.append({'eye':eye,'dominant_paint':dominant.tolist(),'paint_center_y':paint_y,'actual_text_center_y':text_y,'delta_px':delta,'ink_pixels':len(iy),'passed':abs(delta)<=tolerance})
    return {'passed':all(s['passed'] for s in samples),'node':node,'tolerance_native_pixels':tolerance,'samples':samples,
            'scope':'Actual gold/text screenshot pixels; independent of declared caption center. Stable unhovered Welcome, both eyes; not a generic optical judgment for every illustration.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--atlas',type=Path,required=True);p.add_argument('--node',default='WelcomeContinue');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=audit(a.atlas,a.node);a.output.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result,indent=2));sys.exit(0 if result['passed'] else 1)
