"""Apply or remove one real ray-selected sticker in an owned isolated profile.

Start in Livery. The diagnostic slot must match --fixture-slot (>=1000).
Normal UI performs the edit/save/return; no native document mutation or Workshop.
Use --remove-only after profiling the saved design to restore an empty fixture.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_livery_e2e import LiveryTest,ROOT,screenshot_pixel,ui_surface
from flatout_frontend_e2e import read_game,Frontend


async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False}
 p=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
  env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(p) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=LiveryTest(a,session);front=Frontend(a,session)
   try:
    game=await asyncio.to_thread(read_game,a.pid,'game.get_state')
    assert a.fixture_slot>=1000 and game['diagnostic_save_slot']==a.fixture_slot,game
    state=await t.state();report['initial']=state
    assert not state['dirty'] and not state['document']['painted'],state
    if a.remove_only:
     assert len(state['document']['layers'])==1 and state['selected']==0,state
     if state['tab']!=1:await t.click('STICKERS')
     # Reopening starts in the layer list; selecting its real card exposes tools.
     controls=(await t.state())['vr']['controls']
     if not any(c['label']=='DELETE' for c in controls):
      layer=next(c for c in controls if c['window']=='Livery layers' and c['enabled'] and c['label'] not in ('Layer page previous','Layer page next'))
      await t.click(layer['label'],control_window='Livery layers')
     for _ in range(8):
      controls=(await t.state())['vr']['controls']
      if any(c['label']=='DELETE' and c['enabled'] for c in controls):break
      assert any(c['label']=='Tools down' and c['enabled'] for c in controls),controls
      await t.click('Tools down')
     else:raise AssertionError('Delete was not reachable through the tool scroll buttons')
     state=await t.click('DELETE')
     assert not state['document']['layers'],state
    else:
     assert not state['document']['layers'],'This fixture test requires an empty original livery'
     if state['tab']!=1:await t.click('STICKERS')
     await t.click('ADD STICKER')
     state=await t.click('ADD STICKER',hold_frames=30,retry=True)
     assert len(state['document']['layers'])==1,state
     if any(c['label']=='DONE PLACING' for c in state['vr']['controls']):await t.click('DONE PLACING')
    saved=await t.click('SAVE AND APPLY',retry=True)
    assert not saved['dirty'] and 'saved' in saved['status'].lower(),saved
    assert saved['atlas_generation']==saved['generation'] or a.remove_only,saved
    report['saved']=saved
    await t.capture('applied-livery')
    # Leave through real Back. Page-specific Back returns to the layer list first.
    if saved['page']!=0:
     labels={c['label'] for c in saved['vr']['controls']}
     await t.click('BACK TO LAYERS' if 'BACK TO LAYERS' in labels else 'BACK')
    state=await t.state();assert state['page']==0 and not state['dirty'],state
    control=next(c for c in state['vr']['controls'] if c['label']=='BACK')
    await t.face_control(state,control);capture=await t.capture('leave-source')
    x=control['x']+control['width']/2;y=control['y']+control['height']/2
    px,py=screenshot_pixel(state,capture,x,y)
    receipt=await t.tool('click_ui_at',{'capture_id':capture['capture_id'],'x':float(px),'y':float(py),
     'hand':a.hand,'surface':ui_surface(state,x,y),'origin_mode':'hand','dwell_frames':30,
     'hold_frames':2,'command_id':uuid.uuid4().hex},'actual-back-to-garage')
    assert receipt['status']=='input_delivered',receipt
    await asyncio.sleep(.5)
    menu=await front.query('menu.get_state');assert menu['current_menu']=='GARAGE',menu
    # The side-column glance must not become a misleading off-axis perf fixture.
    await front.observed_head_pose('neutral-garage-restored',{'x':0,'y':1.7,'z':0,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
    report['errors']=await front.query('game.errors');assert report['errors']['total']==0
    report['passed']=True
   except Exception as error:report['error']=repr(error);raise
   finally:
    report['cases']=t.cases
    (a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    capture=front.last_capture or t.last_capture
    if capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'remove_only':a.remove_only,'cases':len(t.cases)}))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True)
 p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
 p.add_argument('--fixture-slot',type=int,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
 p.add_argument('--remove-only',action='store_true');asyncio.run(run(p.parse_args()))
