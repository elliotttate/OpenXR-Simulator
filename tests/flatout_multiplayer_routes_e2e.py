"""Exercise real Multiplayer entry routes without joining a remote game.

Start in the Multiplayer hub. A route must reach its native destination through
the asynchronous network state machine. --keep leaves it open for visual work.
"""
import argparse,asyncio,json,os,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT

async def run(a):
 a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'route':a.route}
 params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize();t=Frontend(a,session)
   try:
    assert (await t.query('menu.get_state'))['current_menu']=='MULTIPLAYER'
    assert not (await t.query('net.get_state'))['alive'],'Do not disturb an existing network session'
    assert (await t.query('game.errors'))['total']==0
    await t.click({'browse':'multiplayer.online','host':'multiplayer.host','party-lobby':'multiplayer.friends'}[a.route])
    trace=[];deadline=asyncio.get_running_loop().time()+35
    while asyncio.get_running_loop().time()<deadline:
     state=await t.query('menu.get_state');net=await t.query('net.get_state')
     nodes=(await t.query('hud.rects',{'root':'menu','max_depth':16}))['nodes']
     texts=[n['text'] for n in nodes if n['visible'] and n.get('text')]
     trace.append({'menu':state,'net':net,'texts':texts})
     ready=(a.route=='host' and any('CREATE A PUBLIC LOBBY' in s for s in texts)) or (a.route=='browse' and 'BROWSE GAMES' in texts) or (a.route=='party-lobby' and net['alive'] and any('Browse Games'==i.get('title') for i in state['items']))
     if ready:break
     assert not (await t.query('ui.get_message_box'))['open'],('Unexpected modal during native routing',texts)
     await asyncio.sleep(.2)
    else:raise AssertionError(('Native route did not reach its destination',trace[-1]))
    report['trace']=trace;report['destination']=trace[-1]
    assert net['alive'] and net['is_host'] and net['connected_players']==1,net
    assert (await t.query('game.get_state'))['game_mode']=='online'
    report['capture']=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},a.route+'-actual-native-destination');t.last_capture=report['capture']
    if not a.keep:
     # Return through real Back and the known leave-own-party confirmation.
     for _ in range(4):
      state=await t.query('menu.get_state');net=await t.query('net.get_state')
      if not net['alive'] and state['current_menu']=='MULTIPLAYER':break
      assert net['connected_players']==1,'Another player arrived; preserve their session'
      if (await t.query('ui.get_message_box'))['open']:
       texts=[n['text'] for n in (await t.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes'] if n.get('text')]
       assert any('leave' in s.lower() or 'quit the match' in s.lower() for s in texts),texts
       await t.click('msgbox.validate')
      else:
       targets=(await t.query('vr.targets'))['targets'];back=next(x['name'] for x in targets if x['name'].startswith('helper.Cancel.'))
       await t.click(back)
      await asyncio.sleep(.4)
     else:raise AssertionError('Own party did not close through native Back')
    report['errors']=await t.query('game.errors');assert report['errors']['total']==0,report['errors']
    report['passed']=True
   except Exception as error:
    report['error']=repr(error)
    report['state']=await t.query('menu.get_state');report['network']=await t.query('net.get_state')
    report['errors']=await t.query('game.errors')
    if t.last_capture:await t.tool('capture_screenshot',{'eye':'both','max_width':4096},'route-failure')
    raise
   finally:
    report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
    if t.last_capture:
     for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
 print(json.dumps({'passed':report['passed'],'route':a.route}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--route',choices=('browse','host','party-lobby'),required=True);p.add_argument('--keep',action='store_true');asyncio.run(run(p.parse_args()))
