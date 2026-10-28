"""Qualify the native room browser with real screenshot-derived XR input.

Never joins a remote room. Start in Browse Games with an owned one-player LAN
party; optionally exercise Host and return through the original callbacks.
"""
import argparse,asyncio,copy,json,os,sys,uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

class BrowserTest(Frontend):
 async def nodes(self):return (await self.query('hud.rects',{'root':'menu','max_depth':20}))['nodes']
 async def stereo(self,label):
  self.last_capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label)
  return self.last_capture
 async def layout(self,label):
  nodes=await self.nodes();visible=[n for n in nodes if n['effective_visible']]
  assert any(n['path'].endswith('/RoomBrowser/BrowserHeading') and n.get('text')=='BROWSE GAMES' for n in visible),'Native browser heading missing'
  title=await self.query('hud.get_node',{'root':'menu','name':'Menu/RoomBrowser/BrowserHeading','presentation':True})
  assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in title['presentation']['font_resource'].upper()
  assert not any(('/Lobby_Game/LOBBY_GAME/' in n['path'] or n['path'].endswith(('/Helpers','/Header'))) and n['kind'] in ('panel','text','flash') for n in visible),'Legacy browser artwork still visible'
  t=await self.query('vr.targets');mapped={x['name']:x for x in t['targets']}
  required={'helper.Cancel.BrowserBack','browser.refresh','browser.code','browser.host','browser.scroll.up','browser.scroll.down'}
  assert required<=set(mapped),(required,set(mapped))
  for name in required:
   l,top,r,b=mapped[name]['rect'];assert 80<l<r<1840 and 60<top<b<1056,(name,mapped[name])
  for name in ('helper.Cancel.BrowserBack','browser.host'):
   assert abs(mapped[name]['rect'][1]-916)<.02
  viewport=next(n for n in visible if n['path'].endswith('/BrowserRowsViewport'))
  empty=next((n for n in visible if n['path'].endswith('/BrowserEmpty')),None)
  if empty:
   assert 'NO PUBLIC GAMES FOUND' in empty['text'].upper() or 'SEARCHING' in empty['text'].upper(),empty
   assert not any(x.startswith('room.') for x in mapped),'Empty browser has live room targets'
   for name in ('browser.scroll.up','browser.scroll.down'):
    assert not mapped[name]['enabled'] and not mapped[name]['physical_hover']
  for name,target in mapped.items():
   if name.startswith('room.'):
    l,top,r,b=target['rect'];vl,vt,vr,vb=viewport['rect'];assert vl<=l<r<=vr and vt<=top<b<=vb+.1
  capture=await self.stereo(label);assert_reading_distance(capture,3.,4.5)
  await self.audit('browser-target-spacing')
  self.cases.append({'case':'native-browser-original-font-reading-stage-and-empty-or-live-data','passed':True,'targets':t,'empty':empty})
  return mapped
 async def takeover(self):
  targets=await self.layout('native-before-stick')
  _,_,aim=await self.hover('helper.Cancel.BrowserBack');source=await self.stereo('browser-native-carrier')
  base=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
  await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[1,0],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'browser-real-stick-takeover')
  nodes=await self.nodes();assert not any(n['effective_visible'] and n['path'].endswith('/RoomBrowser') for n in nodes)
  assert any(n['effective_visible'] and '/Lobby_Game/' in n['path'] for n in nodes),'Original browser did not restore on navigation'
  fresh=await self.stereo('browser-original-after-stick');projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[base]
  await asyncio.sleep(.25)
  x,y=quad_pixel(projection,targets['browser.code']['rect'])
  task=asyncio.create_task(self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','surface':{k:base[k] for k in ('pose','size')},'dwell_frames':45},'browser-ray-return'))
  samples=[];start=asyncio.get_running_loop().time()
  try:
   while asyncio.get_running_loop().time()-start<1.3:
    nodes=await self.nodes();root=next((n for n in nodes if n['effective_visible'] and n['path'].endswith('/RoomBrowser')),None)
    if root:samples.append({'time':asyncio.get_running_loop().time()-start,'alpha':root['color']['a']})
    await asyncio.sleep(.015)
  finally:await task
  self.cases.append({'case':'real-horizontal-stick-layout-and-ray-return-with-observed-entrance','passed':False,'samples':samples})
  assert len({round(s['alpha']*255) for s in samples if s['alpha']<.995})>=3,'Entrance not observed'
  assert samples[-1]['alpha']>.99 and all(b['alpha']+.005>=a['alpha'] for a,b in zip(samples,samples[1:]))
  await self.layout('browser-restored-native')
  # Mark the temporal case by identity rather than relying on added layout cases.
  next(c for c in self.cases if c['case']=='real-horizontal-stick-layout-and-ray-return-with-observed-entrance')['passed']=True
 async def prove(self):
  targets=await self.layout('browser-native-entry');net=await self.query('net.get_state')
  assert net['alive'] and net['lan'] and net['is_host'] and net['connected_players']==1,'Only an owned one-player LAN fixture is supported'
  for name in ('helper.Cancel.BrowserBack','browser.refresh','browser.code','browser.host'):
   # Automatic refresh can briefly disable Refresh; wait for the real scan.
   if name=='browser.refresh':
    for _ in range(80):
     if next(t for t in (await self.query('vr.targets'))['targets'] if t['name']==name)['enabled']:break
     await asyncio.sleep(.1)
   pre,hovered,_=await self.hover(name)
   assert hovered['clicks']==pre['clicks'] and (await self.query('net.get_state'))['room_code']==net['room_code']
   assert {x['name']:x['rect'] for x in hovered['targets'] if x['name'] in targets}=={k:v['rect'] for k,v in targets.items()}
   depth=assert_hover_lift(await self.stereo('browser-physical-'+name));self.cases.append({'case':'fixed-physical-hover-'+name,'passed':True,'depth':depth})
  await self.click('browser.refresh')
  await asyncio.sleep(.5)
  assert (await self.query('net.get_state'))['room_code']==net['room_code'],'Refresh changed party room'
  await self.layout('browser-after-real-refresh')
  await self.takeover()
  await self.click('browser.code')
  assert all(t['name'].startswith('code.key.') for t in (await self.query('vr.targets'))['targets']),'Browser controls leaked through keypad'
  await self.click('code.key.1');await self.click('code.key.2');await self.click('code.key.10');await self.click('code.key.12')
  await self.layout('browser-after-keypad-close')
  assert (await self.query('net.get_state'))['room_code']==net['room_code'],'Typing changed party room'
  self.cases.append({'case':'native-room-code-entry-delete-and-close-preserve-party','passed':True})
  if self.args.host_flow:
   await self.click('browser.host')
   for _ in range(100):
    if 'setup' in (await self.query('menu.get_state')):break
    await asyncio.sleep(.1)
   else:raise AssertionError('Host did not reach real setup')
   await self.stereo('browser-host-native-setup')
   assert (await self.query('net.get_state'))['room_code']==net['room_code']
   await self.click('helper.Cancel.LobbySetupBack')
   self.cases.append({'case':'host-opens-real-setup-without-publishing-or-joining','passed':True})

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Real owned LAN browser; never joins remote rooms'}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=BrowserTest(a,session)
   try:
    assert (await t.query('game.errors'))['total']==0
    if a.layout_only:await t.layout('browser-native-layout')
    elif a.takeover_only:await t.takeover()
    else:await t.prove()
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0,report['errors'];report['passed']=True
   except Exception as error:
    report['error']=repr(error);report['state']=await t.query('menu.get_state');report['network']=await t.query('net.get_state');report['errors']=await t.query('game.errors')
    await t.stereo('browser-failure');raise
   finally:
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'cases':len(report['cases'])}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true');p.add_argument('--host-flow',action='store_true');asyncio.run(run(p.parse_args()))

