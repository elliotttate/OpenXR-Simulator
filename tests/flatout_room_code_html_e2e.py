"""Real-input room-code keypad styling, spacing, motion and validation.

Uses only an owned one-player LAN party. Tests own-room rejection and an all-zero
code; it never searches for or joins somebody else's room.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift
from flatout_header_geometry import measure_header_accent

class CodeTest(Frontend):
 async def nodes(self):return (await self.query('hud.rects',{'root':'menu','max_depth':22}))['nodes']
 async def state(self):return (await self.query('menu.get_state'))['browser']
 async def stereo(self,label):
  self.last_capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label);return self.last_capture
 async def layout(self,label):
  nodes=await self.nodes();shown=[n for n in nodes if n['effective_visible']]
  assert any(n['path'].endswith('/RoomCodePad/RoomCodeHeading') for n in shown),'Shared room-code header is missing'
  title=await self.query('hud.get_node',{'root':'menu','name':'Menu/RoomCodePad/RoomCodeHeading','presentation':True})
  assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in title['presentation']['font_resource'].upper()
  assert not any(n['path'].endswith('/RoomBrowser') for n in shown),'Browser is visible behind keypad'
  assert not any(n['kind']=='panel' and n['rect'][0]<=0 and n['rect'][1]<=0 and n['rect'][2]>=1920 and n['rect'][3]>=1080 for n in shown),'Full-screen backdrop leaked'
  targets=(await self.query('vr.targets'))['targets'];mapped={t['name']:t for t in targets}
  assert set(mapped)=={'code.key.'+str(i) for i in range(13)}
  for i in range(12):
   l,t,r,b=mapped['code.key.'+str(i)]['rect'];assert r-l>=200 and b-t>=100 and 620<=l<r<=1300 and 480<=t<b<=976
  close=mapped['code.key.12']['rect'];assert close[2]-close[0]>=80 and close[3]-close[1]>=80
  for a in targets:
   for b in targets:
    if a['name']>=b['name']:continue
    l,t,r,bot=a['rect'];x,y,z,w=b['rect'];assert min(r,z)-max(l,x)<=1 or min(bot,w)-max(t,y)<=1,(a,b)
  header=next(n for n in shown if n['path'].endswith('/RoomCodePad/RoomCodeHeader'))
  body=next(n for n in shown if n['path'].endswith('/RoomCodePad/RoomCodePanel'))
  field=next(n for n in shown if n['path'].endswith('/RoomCodePad/RoomCodeField'))
  assert body['rect'][1]-header['rect'][3]>=19,'Header and body need a visible gap'
  assert field['rect'][0]-body['rect'][0]>=47 and body['rect'][2]-field['rect'][2]>=47,'Code field needs larger body margins'
  assert field['rect'][1]-body['rect'][1]>=47
  for i in range(12):
   l,t,r,b=mapped['code.key.'+str(i)]['rect']
   assert l-body['rect'][0]>=47 and body['rect'][2]-r>=47 and body['rect'][3]-b>=63,'Keys need generous body margins'
  state=await self.state();assert state['keypad_open']
  capture=await self.stereo(label);assert_reading_distance(capture,3.,4.5)
  header=next(n for n in shown if n['path'].endswith('/RoomCodePad/RoomCodeHeader'))
  angles=measure_header_accent(capture,self.last_image_path,header['rect'])
  self.cases.append({'case':'rendered-header-accent-retains-ten-degree-slant-in-both-eyes','passed':False,'measurements':angles})
  assert all(8<=v['degrees']<=12 and v['rms_canvas_pixels']<1.3 for v in angles.values()),angles
  self.cases[-1]['passed']=True
  self.cases.append({'case':'keypad-original-font-readable-fixed-keys-and-exclusive-reading-surface','passed':True,'state':state,'targets':targets})
  return state,mapped
 async def disabled(self,key):
  name='code.key.'+str(key);pre,aim=await self.point_args(name);before=await self.state()
  target=next(t for t in pre['targets'] if t['name']==name);assert not target['enabled'] and not target['physical_hover']
  await self.tool('click_ui_at',{**aim,'command_id':uuid.uuid4().hex,'hold_frames':45},'disabled-'+name)
  assert (await self.query('vr.targets'))['clicks']==pre['clicks'];assert (await self.state())['code']==before['code']
  self.cases.append({'case':'disabled-key-is-visible-and-inert-'+str(key),'passed':True})
 async def open(self,observe=True):
  task=asyncio.create_task(self.click('browser.code'));samples=[];start=asyncio.get_running_loop().time()
  try:
   while asyncio.get_running_loop().time()-start<9:
    root=next((n for n in await self.nodes() if n['effective_visible'] and n['path'].endswith('/RoomCodePad')),None)
    if root:samples.append({'time':asyncio.get_running_loop().time()-start,'alpha':root['color']['a']})
    if task.done() and root and root['color']['a']>.99:break
    if task.done() and not root and not observe:break
    await asyncio.sleep(.015)
  finally:await task
  if observe:
   self.cases.append({'case':'observed-keypad-entrance-fade','passed':False,'samples':samples})
   assert len({round(x['alpha']*255) for x in samples if x['alpha']<.995})>=3,'Keypad entrance not observed'
   assert samples[-1]['alpha']>.99 and all(b['alpha']+.005>=a['alpha'] for a,b in zip(samples,samples[1:]));self.cases[-1]['passed']=True
 async def prove(self):
  net=await self.query('net.get_state');assert net['alive'] and net['lan'] and net['is_host'] and net['connected_players']==1
  await self.open();initial,targets=await self.layout('keypad-native-entry')
  assert initial['code']=='' and not targets['code.key.10']['enabled'] and not targets['code.key.11']['enabled']
  for i in list(range(10))+[12]:
   pre,hover,_=await self.hover('code.key.'+str(i));assert hover['clicks']==pre['clicks'] and (await self.state())['code']==''
   depth=assert_hover_lift(await self.stereo('keypad-hover-'+str(i)));self.cases.append({'case':'fixed-composed-physical-key-hover-'+str(i),'passed':True,'depth':depth})
  await self.disabled(10);await self.disabled(11)
  # Vertical stick must navigate keys, not be consumed by the browser's reader.
  _,_,aim=await self.hover('code.key.1')
  await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,-1],'hold_frames':8,'dwell_frames':2,'command_id':uuid.uuid4().hex},'keypad-real-stick-navigation')
  assert (await self.state())['code']=='' and (await self.state())['keypad_open']
  # The heading is white with a gold last word, so compare with the shared accent (VRMenuPalette::Accent).
  nodes=await self.nodes();gold={'r':1.,'g':.659,'b':0.}
  focus=next(n for n in nodes if n['effective_visible'] and n['path'].endswith('/RoomCodeKey4/RoomCodeKeyLabel'))
  assert all(abs(focus['color'][c]-gold[c])<.01 for c in ('r','g','b')),'Vertical stick did not move keypad focus from 1 to 4'
  await self.hover('code.key.9');assert (await self.state())['code']==''
  self.cases.append({'case':'keypad-stick-navigation-and-deliberate-ray-return','passed':True})
  own=str(net['room_code']).zfill(5);assert len(own)==5
  for index,digit in enumerate(own):
   await self.click('code.key.'+digit);assert (await self.state())['code']==own[:index+1]
  state,mapped=await self.layout('keypad-valid-code');assert mapped['code.key.11']['enabled'] and not mapped['code.key.1']['enabled']
  await self.disabled(1);await self.hover('code.key.11');assert_hover_lift(await self.stereo('keypad-ready-join-physical'))
  await self.click('code.key.11');state=await self.state();assert 'own room' in state['code_status'].lower(),state
  assert (await self.query('net.get_state'))['room_code']==net['room_code']
  await self.stereo('keypad-own-room-error');self.cases.append({'case':'real-own-room-code-rejection-keeps-party','passed':True,'state':state})
  for _ in own:await self.click('code.key.10')
  for _ in range(5):await self.click('code.key.0')
  assert (await self.state())['code']=='00000';await self.disabled(11)
  await self.stereo('keypad-zero-code-inert');await self.click('code.key.12')
  assert not (await self.state())['keypad_open'] and (await self.query('net.get_state'))['room_code']==net['room_code']
  await self.stereo('keypad-closed-browser-restored');self.cases.append({'case':'all-zero-code-inert-and-close-restores-browser','passed':True})

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=CodeTest(a,session)
   try:
    assert (await t.query('game.errors'))['total']==0
    if a.layout_only:await t.open(observe=False);await t.layout('keypad-layout')
    else:await t.prove()
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
   except Exception as error:
    report['error']=repr(error);report['state']=await t.query('menu.get_state');report['errors']=await t.query('game.errors');await t.stereo('keypad-failure');raise
   finally:
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'cases':len(report['cases'])}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');asyncio.run(run(p.parse_args()))
