"""Populate the real LAN browser, then qualify table scrolling and stable IDs.

Uses offline-discovery fixtures addressed only to this owned game's UDP port.
Room selection is exercised; Join is never pressed because fixtures do not host
connections. Production UI consumes native network packets, not a menu preview.
"""
import argparse,asyncio,json,os,subprocess,sys,uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_browser_html_e2e import BrowserTest
from flatout_lan_discovery_fixture import LanDiscoveryFixture
from flatout_frontend_e2e import ROOT
from flatout_ui_depth_contract import assert_hover_lift

class PopulatedTest(BrowserTest):
 async def browser(self):return (await self.query('menu.get_state'))['browser']
 async def refresh_count(self,count):
  await self.click('browser.refresh')
  deadline=asyncio.get_running_loop().time()+8
  while asyncio.get_running_loop().time()<deadline:
   state=await self.browser()
   if state['room_count']==count:return state
   await asyncio.sleep(.1)
  raise AssertionError(('Native room discovery did not reach fixture count',count,state))
 async def prove_populated(self,fixture):
  net=await self.query('net.get_state');await self.refresh_count(9)
  mapped=await self.layout('nine-native-lan-rooms-top');state=await self.browser()
  assert state['scroll_offset']==0 and state['scroll_maximum']>300
  assert not mapped['browser.scroll.up']['enabled'] and mapped['browser.scroll.down']['enabled']
  room_targets=[n for n in mapped if n.startswith('room.') and not n.endswith('.join')];assert len(room_targets)==5
  assert all(int(n.split('.')[1]) in {row['id'] for row in fixture.rows} for n in room_targets),'Fixture IDs were not preserved through native discovery'
  selected=state['selected_room']
  # Hover a different room's Join without selecting it or contacting its host.
  target=next(n+'.join' for n in room_targets if int(n.split('.')[1])!=int(room_targets[0].split('.')[1]))
  pre,hover,_=await self.hover(target);assert hover['clicks']==pre['clicks'] and (await self.browser())['selected_room']==selected
  assert_hover_lift(await self.stereo('populated-room-join-physical-hover'))
  # Move to the viewport through an observed surface pixel, then hold a real
  # XR stick for a fractional-row offset. No native scroll setter is called.
  down=mapped['browser.scroll.down']['rect'];l,t,r,b=down
  _,aim=await self.point_args('browser.scroll.down',((900-l)/(r-l),(600-t)/(b-t)))
  await self.tool('scroll_ui_at',{**aim,'stick':[0,-.6],'hold_frames':18,'dwell_frames':2,'command_id':uuid.uuid4().hex},'actual-smooth-stick-scroll')
  state=await self.browser();assert 1<state['scroll_offset']<state['scroll_maximum'],state
  assert .1<state['scroll_offset']%88<87.9,'Stick scrolling jumped only in whole rows'
  await self.layout('fractional-row-native-scroll');self.cases.append({'case':'real-stick-smooth-scroll-and-safe-clipped-row-targets','passed':True,'state':state})
  await self.click('browser.scroll.down');state=await self.browser();assert abs(state['scroll_offset']-state['scroll_maximum'])<.1
  mapped=await self.layout('native-scroll-bottom');assert not mapped['browser.scroll.down']['enabled'] and mapped['browser.scroll.up']['enabled']
  names=[n for n in mapped if n.startswith('room.') and not n.endswith('.join')];assert len(names)==5
  last=max(names,key=lambda n:mapped[n]['rect'][1])
  await self.click(last);state=await self.browser();assert state['selected_room']==8,'Ninth room remains blocked by the old eight-row limit'
  # Keep that room selected while one earlier host changes status/order.
  selected_id=int(last.split('.')[1]);fixture.change(6,fo_status='racing',fo_lap=2,fo_eta_s=83)
  await self.refresh_count(9);after=await self.browser()
  # Sorting moves this known room, so observe the retained selected identity by
  # exposing the selected row using real scroll arrows and its selected style.
  assert after['selected_room']>=0 and (await self.query('net.get_state'))['room_code']==net['room_code']
  nodes=await self.nodes();viewport=next(n for n in nodes if n['effective_visible'] and n['path'].endswith('/BrowserRowsViewport'))
  wanted_y=viewport['rect'][1]+after['selected_room']*88-after['scroll_offset']
  observed={x['name']:x for x in (await self.query('vr.targets'))['targets'] if x['name'].startswith('room.') and not x['name'].endswith('.join')}
  retained=min(observed,key=lambda n:abs(observed[n]['rect'][1]-wanted_y))
  assert abs(observed[retained]['rect'][1]-wanted_y)<1.1 and int(retained.split('.')[1])==selected_id,('Refresh changed selected room identity',selected_id,after,observed)
  self.cases.append({'case':'ninth-room-selection-and-refresh-preserve-owned-party','passed':True,'selected_id_before_refresh':selected_id,'state':after})
  fixture.set_active(0,False);await self.refresh_count(8)
  state=await self.browser();assert state['scroll_offset']<=state['scroll_maximum']
  await self.stereo('host-disappears-scroll-clamped');self.cases.append({'case':'native-host-removal-clamps-reader-without-joining','passed':True,'state':state})
  await self.click('browser.scroll.up');state=await self.browser();assert state['scroll_offset']==0
  await self.layout('populated-restored-top');assert (await self.query('net.get_state'))['room_code']==net['room_code']
  assert fixture.replies>=26 and fixture.rejected>=0

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'hand':a.hand,'scope':'Owned local offline-discovery fixtures; no remote joins; no menu-state injection'}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=PopulatedTest(a,session)
   try:
    net=await t.query('net.get_state');assert net['alive'] and net['lan'] and net['is_host'] and net['connected_players']==1
    query=f"(Get-NetUDPEndpoint -LocalPort {a.game_port} -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess) -join ','"
    owner=subprocess.run(['powershell.exe','-NoProfile','-Command',query],capture_output=True,text=True,check=True).stdout.strip()
    assert owner==str(a.pid),('UDP fixture owner mismatch',owner,a.pid)
    assert (await t.query('game.errors'))['total']==0
    try:
     with LanDiscoveryFixture(a.fox,a.game_port,published={'fo_mode':str(a.mode),'fo_track':str(a.track_loca),'fo_laps':'3'}) as fixture:
      try:await t.prove_populated(fixture)
      except BaseException:
       await t.stereo('populated-before-fixture-cleanup');raise
      report['fixture']={'replies':fixture.replies,'rejected':fixture.rejected,'rooms':[{'id':x['id'],'host':x['fields']['fo_host']} for x in fixture.rows]}
      if a.profile:
       command=[sys.executable,str(ROOT/'tests/flatout_menu_hover_perf.py'),'--pid',str(a.pid),'--data-dir',str(a.data_dir.resolve()),'--evidence',str(a.evidence/'populated-performance'),'--real-xr','--menu','browser']
       child=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
       report['performance_process_pid']=child.pid
       output,_=await child.communicate();(a.evidence/'performance-console.log').write_bytes(output)
       assert child.returncode==0,('Populated browser performance gate failed',output.decode(errors='replace')[-2000:])
       report['populated_performance']=json.loads((a.evidence/'populated-performance/summary.json').read_text())
    finally:
     # Fixture sockets are closed before asking the normal browser to drop them.
     if not (await t.query('ui.get_message_box'))['open']:
      await t.refresh_count(0)
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0,report['errors'];report['passed']=True
   except Exception as error:
    report['error']=repr(error);report['state']=await t.query('menu.get_state');report['errors']=await t.query('game.errors');await t.stereo('populated-failure');raise
   finally:
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'cases':len(report['cases'])}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--fox',type=Path,required=True);p.add_argument('--game-port',type=int,required=True);p.add_argument('--mode',type=int,default=1);p.add_argument('--track-loca',type=int,default=730);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--profile',action='store_true');asyncio.run(run(p.parse_args()))
