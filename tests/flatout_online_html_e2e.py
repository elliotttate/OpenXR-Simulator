"""Host setup fidelity and original helper semantics through real XR input."""
import argparse,asyncio,copy,json,os,sys,uuid
from pathlib import Path
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift

class OnlineTest(Frontend):
 async def setup(self):return (await self.query('menu.get_state'))['setup']
 async def stereo(self,label):
  self.last_capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},label)
  return self.last_capture
 async def layout(self,label):
  state=await self.query('menu.get_state');assert state['current_menu']=='ONLINE' and 'setup' in state
  setup=state['setup'];nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
  heading=next((n for n in nodes if n['visible'] and n['path'].endswith('/LobbySetupHeading')),None)
  assert heading,'Shared slanted native Host header is missing'
  title=await self.query('hud.get_node',{'root':'menu','name':'Menu/LobbySetup/LobbySetupHeading','presentation':True})
  assert 'FO_ALDO_THE_APACHE_DIRT.FNT' in title['presentation']['font_resource'].upper()
  geometry=json.loads((self.args.fox/'Flatout/Code/Sources/Menus/FOOnlineHtmlLayout.json').read_text())['geometry']
  targets=(await self.query('vr.targets'))['targets'];mapped={x['name']:x for x in targets}
  expected={'helper.Cancel.LobbySetupBack','helper.Validate.LobbySetupApply'}
  for row in setup['rows']:
   prefix='/LobbySetting'+str(row['index'])+'/'
   if row['visible']:
    expected|={'setup.'+str(row['index'])+'.'+side for side in ('prev','next')}
    for name,key in (('LobbySettingTitle','label'),('LobbySettingValue','value')):
     n=next(n for n in nodes if n['visible'] and prefix+name in n['path']);assert n['text']==row[key],(n,row)
    for side,key in (('prev','previous'),('next','next')):
     target=mapped['setup.'+str(row['index'])+'.'+side]
     assert target['enabled']==row[key] and target['physical_hover']==row[key],target
     l,t,r,b=target['rect'];assert abs(r-l-geometry['ArrowSize'])<.01 and abs(b-t-geometry['ArrowSize'])<.01
     assert geometry['BodyY']<=t<b<=geometry['BodyY']+geometry['BodyHeight']-15
  assert set(mapped)==expected,(set(mapped),expected)
  for name in ('helper.Cancel.LobbySetupBack','helper.Validate.LobbySetupApply'):
   l,t,r,b=mapped[name]['rect'];assert abs(t-(geometry['FooterY']-4))<.01 and b<=1056
  assert mapped['helper.Validate.LobbySetupApply']['enabled']==setup['can_continue']
  assert not any(n['effective_visible'] and n['path'].endswith(('/Header','/Helpers','/Content_Zone')) for n in nodes)
  capture=await self.stereo(label);assert_reading_distance(capture,3,4.5)
  await self.audit(label)
  self.cases.append({'case':'native-setup-values-original-font-fixed-controls-and-reading-stage','passed':True,'setup':setup})
  return setup,mapped
 async def takeover(self):
  original,targets=await self.layout('before-stick-takeover')
  _,_,aim=await self.hover('helper.Cancel.LobbySetupBack')
  source=await self.stereo('native-carrier-before-stick')
  base=max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
  await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'actual-stick-takeover')
  nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
  assert not any(n['effective_visible'] and n['path'].endswith('/LobbySetup') for n in nodes)
  assert (await self.setup())['rows']==original['rows'],'Stick takeover changed rule values'
  assert any(n['effective_visible'] and '/Lobby_Game/GAME_SETUP/' in n['path'] for n in nodes),'Original setup controls did not restore'
  self.cases.append({'case':'real-stick-restores-original-controls-without-value-mutation','passed':True})
  fresh=await self.stereo('original-controls-after-stick');projection=copy.deepcopy(fresh);projection['geometry']['surfaces']=[base]
  # A 200 ms navigation grace deliberately rejects motion concurrent with the
  # stick's release. Re-aim after neutral, as a separate input intention.
  await asyncio.sleep(.25)
  rect=targets['helper.Validate.LobbySetupApply']['rect'];x,y=quad_pixel(projection,rect)
  pending=asyncio.create_task(self.tool('hover_ui_at',{'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,'origin_mode':'hand','surface':{k:base[k] for k in ('pose','size')},'dwell_frames':45},'actual-ray-reaim-restores-native'))
  samples=[];start=asyncio.get_running_loop().time()
  try:
   while asyncio.get_running_loop().time()-start<1.3:
    nodes=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
    root=next((n for n in nodes if n['visible'] and n['path'].endswith('/LobbySetup')),None)
    if root:samples.append({'time':asyncio.get_running_loop().time()-start,'alpha':root['color']['a'],
     'rows':[{'name':n['path'],'alpha':n['color']['a'],'rect':n['rect']} for n in nodes if n['visible'] and n['path'].rsplit('/',1)[-1].startswith('LobbySetting') and n['kind']=='panel']})
    await asyncio.sleep(.01)
  finally:await pending
  self.cases.append({'case':'observed-native-entrance-fade-and-row-stagger','passed':False,'samples':samples})
  assert len({round(s['alpha']*255) for s in samples if s['alpha']<.995})>=3,'Entrance was not temporally observed'
  assert samples[-1]['alpha']>.99 and all(b['alpha']+.005>=a['alpha'] for a,b in zip(samples,samples[1:]))
  assert any(max(n['alpha'] for n in s['rows'])-min(n['alpha'] for n in s['rows'])>.05 for s in samples if s['rows']),'Row stagger was not observed'
  self.cases[-1]['passed']=True
  await self.layout('native-return-after-stick')
  assert (await self.setup())['rows']==original['rows']
  assert (await self.query('vr.targets'))['hovered']=='helper.Validate.LobbySetupApply'
  self.cases.append({'case':'real-ray-reaim-restores-native-with-original-values','passed':True})
 async def ownership(self):
  original,targets=await self.layout('ownership-entry')
  active=self.args.hand;other='left' if active=='right' else 'right'
  await self.hover('setup.9.next')
  before=await self.query('vr.targets')
  target=targets['setup.0.next']['rect'];l,t,r,b=target
  _,blank=await self.point_args('setup.0.next',((960-l)/(r-l),(40-t)/(b-t)),other)
  await self.tool('hover_ui_at',blank,'spare-ray-blank-header')
  assert (await self.query('vr.targets'))['hovered']=='setup.9.next','Blank spare ray stole normal hover'
  _,aim=await self.point_args('setup.9.next',hand=active)
  held=await self.tool('hold_ui_at',{**aim,'command_id':uuid.uuid4().hex},'hold-original-hand-trigger')
  assert held['status']=='input_delivered' and held['delivery']['button_down']
  after=await self.setup();assert after['rows'][9]['value']!=original['rows'][9]['value']
  _,spare=await self.point_args('setup.0.next',hand=other)
  await self.tool('hover_ui_at',spare,'spare-ray-different-control-during-hold')
  pointer=await self.query('vr.targets')
  assert pointer['hovered']=='setup.9.next' and pointer['clicks']==before['clicks']+1,'Spare hand stole an existing hold'
  assert (await self.setup())==after
  await self.session.call_tool('release_ui_pointer',{'capture_id':self.last_capture['capture_id'],'hand':active})
  await self.click('setup.9.prev')
  assert [r['value'] for r in (await self.setup())['rows']]==[r['value'] for r in original['rows']]
  self.cases.append({'case':'blank-spare-ray-and-held-trigger-ownership-preserved','passed':True,'active_hand':active})
 async def create_flow(self):
  setup,_=await self.layout('before-create-public-lobby')
  net=await self.query('net.get_state');assert net['alive'] and net['is_host'] and net['lan'] and net['private'] and net['connected_players']==1
  labels=(await self.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
  assert setup['can_continue'] and any(n['effective_visible'] and n['path'].endswith('/LobbySetupApply/LobbyActionLabel') and n.get('text')=='CREATE PUBLIC LOBBY' for n in labels)
  await self.click('helper.Validate.LobbySetupApply')
  for _ in range(100):
   menu=await self.query('menu.get_state');net=await self.query('net.get_state')
   if 'setup' not in menu and any(i['title'].lower()=='garage' for i in menu['items']) and net['state']=='InLobby' and not net['private']:break
   await asyncio.sleep(.1)
  else:raise AssertionError(('Create did not reach the real public lobby',menu,net))
  assert net['is_host'] and net['connected_players']==1 and net['published_status']['fo_pub']=='1'
  await self.stereo('real-public-lobby-after-one-create')
  self.cases.append({'case':'one-real-create-callback-and-idempotent-retry-reach-native-public-lobby','passed':True,'network':net})
  # Leave only our single-player LAN fixture via its native confirmation.
  for _ in range(5):
   net=await self.query('net.get_state')
   if not net['alive']:break
   assert net['connected_players']==1,'Preserve a session if another player joins'
   if (await self.query('ui.get_message_box'))['open']:
    texts=[n['text'] for n in (await self.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes'] if n.get('text')]
    assert any('quit the match' in s.lower() or 'leave' in s.lower() for s in texts),texts
    await self.click('msgbox.validate')
   else:
    targets=(await self.query('vr.targets'))['targets'];await self.click(next(x['name'] for x in targets if x['name'].startswith('helper.Cancel.')))
   await asyncio.sleep(.4)
  else:raise AssertionError('Native leave did not close the owned lobby')
  assert (await self.query('menu.get_state'))['current_menu']=='MULTIPLAYER'
  self.cases.append({'case':'native-leave-confirmation-closes-owned-lobby-and-restores-multiplayer','passed':True})
 async def prove(self):
  initial,targets=await self.layout('host-entry')
  # Exercise arrows from every row, both footer actions, and their fixed bounds.
  selected=initial['selected_row']
  for name in [x for x in targets if x.endswith('.next') or x.startswith('helper.')]:
   if not targets[name]['enabled']:continue
   pre,hovered,_=await self.hover(name);assert hovered['clicks']==pre['clicks']
   assert (await self.setup())==initial,'Hover changed the helper selection or setup'
   assert {x['name']:x['rect'] for x in hovered['targets']}=={k:v['rect'] for k,v in targets.items()}
   depth=assert_hover_lift(await self.stereo('physical-'+name));self.cases.append({'case':'fixed-physical-hover-'+name,'passed':True,'depth':depth})
  # Nondependent rules, track and ambiance must round-trip through the actual helper.
  for index in (4,5,6,7,8,9,2,3,1):
   before=await self.setup();row=next(r for r in before['rows'] if r['index']==index)
   if not row['visible'] or not row['next'] or not row['previous']:continue
   await self.click('setup.'+str(index)+'.next');after=await self.setup()
   changed=next(r for r in after['rows'] if r['index']==index);assert changed['value']!=row['value'],(row,changed)
   await self.click('setup.'+str(index)+'.prev');restored=await self.setup()
   assert [r['value'] for r in restored['rows']]==[r['value'] for r in before['rows']],(index,before,restored)
   self.cases.append({'case':'original-helper-round-trip-'+str(index),'passed':True})
  modes={initial['event_type']}
  for step in range(6):
   before=await self.setup();await self.click('setup.0.next');after=await self.setup();assert after['event_type']!=before['event_type']
   await asyncio.sleep(.6);await self.layout('mode-'+str(step))
   modes.add(after['event_type'])
   if after['event_type']==initial['event_type']:break
  else:raise AssertionError('Native event-type cycle did not return to its start')
  assert len(modes)>=2
  self.cases.append({'case':'all-native-event-types-cycle-with-dependent-rules','passed':True,'event_types':sorted(modes)})
  # Any boundary direction is shown, grey, flat and nonactivating.
  state,targets=await self.layout('after-mode-cycle')
  for name,target in targets.items():
   if target['enabled']:continue
   before=await self.query('vr.targets');_,aim=await self.point_args(name)
   await self.tool('click_ui_at',{**aim,'hold_frames':20,'command_id':uuid.uuid4().hex},'disabled-'+name)
   assert (await self.query('vr.targets'))['clicks']==before['clicks'] and (await self.setup())==state
  assert (await self.query('game.errors'))['total']==0

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=OnlineTest(a,session)
   try:
    if a.create_flow:await t.create_flow()
    elif a.ownership_only:await t.ownership()
    elif a.takeover_only:await t.takeover()
    elif a.layout_only:await t.layout('host-layout')
    else:await t.prove()
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
   except Exception as error:report['error']=repr(error);raise
   finally:
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'cases':len(report['cases'])}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--layout-only',action='store_true');p.add_argument('--takeover-only',action='store_true');p.add_argument('--ownership-only',action='store_true');p.add_argument('--create-flow',action='store_true');asyncio.run(run(p.parse_args()))
