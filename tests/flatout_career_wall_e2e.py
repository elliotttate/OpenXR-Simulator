"""Original white wall choices and the separate native Career reading panel.

Uses screenshot-derived real rays, actual stick takeover and a cancel-only
confirmation. No profile, race, score or unlock mutation is staged.
"""
import argparse,asyncio,json,math,os,sys
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_career_html_e2e import CareerTest,ROOT
from flatout_ui_depth_contract import assert_hover_lift,assert_reading_distance

class WallTest(CareerTest):
 async def capture(self,label):
  self.last_capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label)
  return self.last_capture

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=WallTest(a,session);head=None
   try:
    state=await t.query('menu.get_state');assert state['current_menu']=='MAIN MENU' and state['is_3d']
    source=await t.capture('white-main-unchanged');head=source['geometry']['head']
    await t.enter_white_career('actual-white-career-ray-entry');await asyncio.sleep(.7)
    hub=await t.layout('native-career-still-on-reading-stage')
    before=await t.capture('native-career-stage-before-hover');base=max((s for s in before['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
    native=await t.query('vr.targets')
    for target in native['targets']:
     pre,hovered,_=await t.hover(target['name']);assert hovered['clicks']==pre['clicks']
     assert next(x['rect'] for x in hovered['targets'] if x['name']==target['name'])==target['rect']
     assert (await t.query('menu.get_state'))['career_hub']==hub
     report.setdefault('physical_depth',{})[target['name']]=assert_hover_lift(await t.capture('physical-'+target['name']))
    await t.takeover()
    assert (await t.query('menu.get_state'))['career_hub']==hub
    assert_reading_distance(await t.capture('native-board-restored-at-original-size'),3,4.5)
    if hub['active_cup']:
     await t.click('career.cancel-cup');assert (await t.query('ui.get_message_box'))['open']
     modal=await t.capture('native-modal-stays-on-reading-stage');assert_reading_distance(modal,3,4.5)
     carrier=max((s for s in modal['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
     assert np.allclose(carrier['pose']['position'],base['pose']['position'],atol=.001)
     assert np.allclose(carrier['pose']['orientation'],base['pose']['orientation'],atol=1e-5)
     await t.click('msgbox.cancel');assert (await t.query('menu.get_state'))['career_hub']==hub
    await t.click('helper.Cancel.CareerBack');assert (await t.query('menu.get_state'))['current_menu']=='MAIN MENU'
    await t.capture('white-main-still-in-prior-position')
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0
    report['passed']=True
   except Exception as error:report['error']=repr(error);raise
   finally:
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    if head:
     x,y,z=head['position'];await t.observed_head_pose('restore-neutral-head',{'x':x,'y':y,'z':z,'yaw_deg':0,'pitch_deg':0,'roll_deg':0})
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
 print(json.dumps({'passed':report['passed'],'hand':a.hand}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');asyncio.run(run(p.parse_args()))
