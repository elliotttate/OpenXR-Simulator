"""Verify split livery panels through screenshots and real XR rays.

Start in a clean livery editor, Paint tab with tools at the top, and a neutral
head pose. Use an isolated simulator profile. --gpu additionally captures one
RenderDoc frame for flatout_livery_panels_gpu.py. No document edit, save, or
publish operation is performed. --clicks-only covers only the safe action pass.
"""
import argparse, asyncio, json, math, os, sys, uuid
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from flatout_livery_e2e import (LiveryTest, screenshot_pixel, ui_surface, quaternion_matrix,
                                assert_control_ray_hit, StdioServerParameters, stdio_client, ClientSession)
from flatout_frontend_e2e import read_game

def expected_geometry(state,capture):
    frame=state['vr']['frame'];camera=frame['camera']
    inverse=np.column_stack([camera[k] for k in ('right','up','back')]).T
    out=[]
    for p in frame['panels']:
        rect=p['canvas_rect'];panel=p['panel'];world=[]
        for x,y in ((rect[0],rect[1]),(rect[2],rect[1]),(rect[2],rect[3]),(rect[0],rect[3])):
            world.append(np.array(panel['origin'])+np.array(panel['right'])*(x/1920-.5)*p['panel_width']+
                         np.array(panel['up'])*(.5-y/1080)*p['panel_height'])
        for eye in capture['geometry']['projection_eyes']:
            points=[];depth=[]
            left,right,up,down=map(math.tan,eye['fov'])
            for point in world:
                local=quaternion_matrix(eye['pose']['orientation']).T@(inverse@(point-camera['origin'])-eye['pose']['position'])
                points.append([2*(local[0]/-local[2]-left)/(right-left)-1,2*(local[1]/-local[2]-down)/(up-down)-1])
                depth.append(float(-local[2]))
            a=np.array(points)
            out.append(dict(side=p['id'],eye=eye['eye'],ndc_bounds=[*a.min(axis=0).tolist(),*a.max(axis=0).tolist()],depth_range=[min(depth),max(depth)]))
    return out

async def main(args):
    args.evidence.mkdir(exist_ok=False,parents=True)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    report=dict(passed=False,cases=[])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=LiveryTest(args,session)
            initial=await test.state()
            assert not initial['dirty'], 'Use a clean disposable livery fixture'
            assert initial['tab']==0, 'Start on the Paint tab'
            assert not next(c['enabled'] for c in initial['vr']['controls'] if c['label']=='Tools up'), 'Start at the top of the tool list'
            assert np.allclose(initial['vr']['frame']['left_eye']['back'],[0,0,1],atol=1e-4), 'Start with a neutral head pose'
            head=(np.array(initial['vr']['frame']['left_eye']['origin'])+initial['vr']['frame']['right_eye']['origin'])/2
            try:
                assert initial['vr']['frame']['layout']=='side_panels'
                assert initial['vr']['ui_volume']['mode_mm']==0
                if args.gpu:
                    await session.call_tool('set_head_pose',dict(x=float(head[0]),y=float(head[1]),z=float(head[2]),yaw_deg=0,pitch_deg=0,roll_deg=0))
                    await test.wait_head_pose(0)
                    state=await test.state()
                    composed=await test.tool('capture_screenshot',dict(eye='both',max_width=4096),'neutral-both')
                    test.last_capture=composed
                    (args.evidence/'neutral-state.json').write_text(json.dumps(state,indent=2))
                    queued=await asyncio.to_thread(read_game,args.pid,'renderdoc.capture',dict(frames=1,path=str((args.evidence/'neutral').resolve())))
                    assert queued['success'],queued
                    await asyncio.sleep(2)
                    files=list(args.evidence.glob('neutral_frame*.rdc'))
                    assert len(files)==1,files
                    job=dict(capture=str(files[0].resolve()),result=str((args.evidence/'neutral-gpu.json').resolve()),expected=expected_geometry(state,composed))
                    (args.evidence/'neutral-job.json').write_text(json.dumps(job,indent=2))
                for side,label in (('left','STICKERS'),('right','Tools down')):
                    if args.clicks_only:
                        continue
                    for hand in ('left','right'):
                        state=await test.state()
                        control=next(c for c in state['vr']['controls'] if c['label']==label and c['enabled'])
                        await test.face_control(state,control)
                        state=await test.state()
                        source=await test.capture(side+'-'+hand+'-source')
                        hx,hy=control['x']+control['width']/2,control['y']+control['height']/2
                        x,y=screenshot_pixel(state,source,hx,hy)
                        assert 0<=x<source['image_size'][0] and 0<=y<source['image_size'][1]
                        base=ui_surface(state,hx,hy)
                        aim=dict(capture_id=source['capture_id'],x=float(x),y=float(y),surface=base,hand=hand,
                                 origin_mode='hand',dwell_frames=45)
                        receipt=await test.tool('hover_ui_at',aim,side+'-'+hand+'-hover')
                        assert receipt['status']=='input_delivered'
                        state=await test.state();hit=assert_control_ray_hit(state,control,hand)
                        composed=await test.tool('capture_screenshot',dict(eye='both',max_width=4096),side+'-'+hand+'-both')
                        test.last_capture=composed
                        basis=quaternion_matrix(base['pose']['orientation'])
                        center=np.array(base['pose']['position'])+basis[:,0]*((hx/1920-.5)*base['size'][0])+basis[:,1]*((.5-hy/1080)*base['size'][1])
                        offsets=[(basis.T@(np.array(t['pose']['position'])-center)).tolist() for t in composed['geometry']['surfaces']
                                 if np.linalg.norm(quaternion_matrix(t['pose']['orientation'])-basis)<1e-4]
                        assert any(np.linalg.norm(d[:2])<.01 and abs(d[2]-.015)<.001 for d in offsets),offsets
                        stem=side+'-'+hand
                        (args.evidence/(stem+'-state.json')).write_text(json.dumps(state,indent=2))
                        report['cases'].append(dict(case=stem,passed=True,hit=hit,offsets=offsets))
                        await test.release(hand)
                # Real ray clicks, including command retry, must activate once.
                before=await test.state()
                assert before['tab']==0, 'Start this fixture on Paint for the tab transition check'
                after=await test.click('Tools down',retry=True,hold_frames=45,hand='right')
                enabled=lambda s,label: next(c['enabled'] for c in s['vr']['controls'] if c['label']==label)
                assert enabled(before,'Tools down') and not enabled(before,'Tools up')
                assert not enabled(after,'Tools down') and enabled(after,'Tools up')
                assert after['document']==before['document'] and after['generation']==before['generation']
                controls=lambda s: [(c['id'],c['y']) for c in s['vr']['controls'] if c['x']>1400]
                assert controls(before)!=controls(after), 'Scrolling must move the actual rendered tool controls'
                await test.click('Tools up',hand='right')
                after=await test.click('STICKERS',retry=True,hold_frames=45,hand='left')
                assert after['tab']==1 and after['document']==before['document']
                restored=await test.click('PAINT',hand='left')
                assert restored['document']==initial['document']
                errors=await asyncio.to_thread(read_game,args.pid,'game.errors')
                assert errors['total']==0,errors
                report['cases'].append(dict(case='both-panel-clicks-and-restoration',passed=True))
                report['passed']=True
            except BaseException as e:
                report['error']=repr(e)
                raise
            finally:
                report['cases']+=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',dict(capture_id=test.last_capture['capture_id'],hand=hand))
                await session.call_tool('set_head_pose',dict(x=float(head[0]),y=float(head[1]),z=float(head[2]),yaw_deg=0,pitch_deg=0,roll_deg=0))

p=argparse.ArgumentParser()
p.add_argument('--pid',type=int,required=True)
p.add_argument('--data-dir',type=Path,required=True)
p.add_argument('--evidence',type=Path,required=True)
p.add_argument('--gpu',action='store_true')
p.add_argument('--clicks-only',action='store_true')
asyncio.run(main(p.parse_args()))
