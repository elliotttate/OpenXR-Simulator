"""Stationary real-ray toggle hover must not blink in frontend or paused Options.

Retains consecutive composed frames plus native hit identity and values. Hover
never edits a setting. Start in Options, or use --open-pause-options from a race.
"""
import argparse,asyncio,copy,json,os,sys
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_hover_lift

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'target':a.target,'hand':a.hand}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=Frontend(a,session)
   try:
    if a.open_pause_options:
     g=await t.query('game.get_state');assert not g['is_main_menu'] and not g['is_loading']
     pause=await t.query('hud.pause_menu')
     if not pause['is_open']:await t.menu_button()
     pause=await t.query('hud.pause_menu');index=next(i for i,label in enumerate(pause['items']) if label.upper()=='OPTIONS')
     targets=(await t.query('vr.targets'))['targets'];name='button.'+str(index);assert any(x['name']==name for x in targets)
     await t.click(name)
    assert (await t.query('options.get_state'))['open']
    if (await t.query('options.get_state'))['selection_index']!=0:await t.click('settings.category.0')
    for _ in range(3):
     targets=(await t.query('vr.targets'))['targets']
     if any(x['name']==a.target for x in targets):break
     assert any(x['name']=='settings.scroll.down' for x in targets)
     await t.click('settings.scroll.down');await asyncio.sleep(.4)
    else:raise AssertionError('Requested General toggle was not reachable')
    await asyncio.sleep(.7)
    nodes=(await t.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
    report['animation_nodes']=[n for n in nodes if n['path'].endswith(('/OptionsFocused','/OptionsRowsViewport','/OptionsRow'))]
    assert report['animation_nodes'],'Options animation tree is missing'
    assert all(n['color']['a']>.99 for n in report['animation_nodes']),('Options entrance stopped before fully visible',report['animation_nodes'])
    original=await t.query('options.get_state');before=await t.query('vr.targets')
    assert next(x for x in before['targets'] if x['name']==a.target)['physical_hover'],'Masked switch did not become eligible for physical hover'
    _,hovered,aim=await t.hover(a.target);await asyncio.sleep(.5)
    source=await t.tool('capture_screenshot',{'eye':'both','max_width':2048},'stationary-toggle-source');t.last_capture=source
    target=next(x for x in hovered['targets'] if x['name']==a.target)
    report['original_options']=original;report['target_rect']=target['rect'];report['hover_start']=hovered
    burst=await t.tool('capture_flicker_window',{'timeout':15,'max_frames':12},'consecutive-toggle-frames');report['burst']=burst
    samples=[]
    for _ in range(20):
     samples.append(await t.query('vr.targets'));await asyncio.sleep(.015)
    report['pointer_samples']=samples
    assert all(next(t for t in x['targets'] if t['name']==a.target)['rect']==report['target_rect'] for x in samples),'Hover moved the switch hit bounds'
    assert all(x['hovered']==a.target and x['clicks']==before['clicks'] for x in samples),'Stationary hover lost its toggle or activated it'
    state=await t.query('options.get_state');assert [[r['value'] for r in b['rows']] for b in state['boxes']]==[[r['value'] for r in b['rows']] for b in original['boxes']],'Hover edited values'
    # The continuous detector intentionally retains only 320-wide frames.
    # A 61 px switch is subpixel there; retain that burst for continuity and
    # use full-resolution captures for the small-control pixel assertion.
    frames=[]
    for i in range(12):
     if i%3==0:await t.hover(a.target)
     c=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},'toggle-full-resolution-'+str(i));t.last_capture=c
     current=await t.query('vr.targets');assert current['hovered']==a.target and current['clicks']==before['clicks'],current
     frames.append((c,t.last_image_path));await asyncio.sleep(.007*(i%3))
    report['physical_depth']=assert_hover_lift(frames[-1][0])
    patches={e:[] for e in ('left','right')};report['rendered_frames']=[{'frame_id':c['geometry']['frame_id'],'file':str(f)} for c,f in frames]
    assert len({c['geometry']['frame_id'] for c,f in frames})==12
    for c,f in frames:
     pixels=np.asarray(Image.open(f).convert('RGB'))
     for eye in patches:
      l,top,r,b=target['rect'];points=[quad_pixel(c,(x,y,x,y),eye) for x,y in ((l,top),(r,b))]
      x0,y0=map(lambda x:int(round(x)),points[0]);x1,y1=map(lambda x:int(round(x)),points[1]);patch=pixels[min(y0,y1):max(y0,y1),min(x0,x1):max(x0,x1)]
      assert patch.size and min(patch.shape[:2])>=5,(eye,points,pixels.shape)
      patches[eye].append(patch)
    report['temporal']={}
    sheet=Image.new('RGB',(12*180,2*100),(16,16,16))
    for row,(eye,frames) in enumerate(patches.items()):
     stack=np.stack(frames).astype(float);delta=np.abs(np.diff(stack,axis=0));metrics={'maximum_mean_delta':float(delta.mean(axis=(1,2,3)).max()),'maximum_changed_fraction':float((delta.max(axis=3)>40).mean(axis=(1,2)).max()),'frame_luminance':[float(x.mean()) for x in stack]};report['temporal'][eye]=metrics
     for i,frame in enumerate(frames):sheet.paste(Image.fromarray(frame).resize((180,100)),(i*180,row*100))
    sheet.save(a.evidence/'toggle-temporal.png')
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0,report['errors']
    assert all(m['maximum_changed_fraction']<.15 for m in report['temporal'].values()),('Stationary toggle visibly flickered',report['temporal'])
    report['passed']=True
   except Exception as error:report['error']=repr(error);raise
   finally:
    (a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'temporal':report.get('temporal')}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--target',default='box0.row9.toggle');p.add_argument('--open-pause-options',action='store_true');asyncio.run(run(p.parse_args()))
