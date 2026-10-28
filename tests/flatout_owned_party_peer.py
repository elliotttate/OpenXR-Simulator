"""Join two explicitly owned test games through the native room-code keypad.

The guest uses real menu navigation/Validate callbacks. The host remains under
its XR test's control. No direct slot injection, invitations or public matching.
"""
import argparse,asyncio,json,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from flatout_ui_atlas import Atlas


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    def runner(pid):
        args=SimpleNamespace(pid=pid,output=Path('unused'),current=False,owner_queue=None,native_timeout=10,settle=.25,dwell_frames=3,hand='right',fixture=[])
        r=Atlas(args,None,{'operations':[]});r.active_case={'id':'owned-private-peer-join'};return r
    host,guest=runner(a.host_pid),runner(a.guest_pid)
    report={'passed':False,'host_pid':a.host_pid,'guest_pid':a.guest_pid,'scope':'Two owned isolated test profiles, native callbacks and LAN sockets, no synthetic slots'}
    async def net(r):return await r.query('net.get_state')
    async def nodes(r):return (await r.query('hud.rects',{'root':'menu','max_depth':20}))['nodes']
    try:
        assert (await host.query('game.get_state'))['diagnostic_save_slot']==a.host_slot
        assert (await guest.query('game.get_state'))['diagnostic_save_slot']==a.guest_slot
        h=await net(host);g=await net(guest)
        assert h['alive'] and h['is_host'] and h['private'] and h['lan'] and h['connected_players']==1,h
        # A previous bounded observation may leave the owned guest in its
        # temporary one-person browser party. Resume only that exact state.
        if g['alive']:
            assert g['is_host'] and g['private'] and g['lan'] and g['connected_players']==1,g
            state=await guest.query('menu.get_state')
            if state.get('browser',{}).get('keypad_open'):
                assert not state['browser']['code'],state
                await guest.query('menu.back')
            assert 'BROWSE GAMES' in [n.get('text','') for n in await nodes(guest)]
        code=str(h['room_code']);assert len(code)==5
        if (await guest.query('ui.get_message_box'))['open']:
            text=[n.get('text','') for n in (await guest.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes']]
            assert 'Welcome to FlatOut VR' in text and any('thumbstick' in t for t in text),text
            await guest.query('ui.confirm_message_box',{'cancel':False})
        if not g['alive']:
            await guest.normal_flow('multiplayer');await guest.select(0)
        deadline=time.monotonic()+40
        while True:
            text=[n.get('text','') for n in await nodes(guest)]
            if 'BROWSE GAMES' in text:break
            assert time.monotonic()<deadline,{'texts':text,'network':await net(guest)}
            # Network startup owns a transient progress box. Observe it until
            # the actual browser is ready; never acknowledge unknown dialogs.
            if (await guest.query('ui.get_message_box'))['open']:
                assert (await net(guest))['state']=='NetworkInitializing','Unexpected entry modal'
            await asyncio.sleep(.1)
        g=await net(guest);assert g['alive'] and g['is_host'] and g['private'] and g['lan'] and g['connected_players']==1,g
        state=await guest.query('menu.get_state');enter=next(i['index'] for i in state['items'] if i['title']=='Enter Code')
        for _ in range(len(state['items'])+1):
            if (await guest.query('menu.get_state'))['selection_index']==enter:break
            await guest.query('menu.navigate',{'direction':'right'})
        assert (await guest.query('menu.get_state'))['selection_index']==enter
        await guest.query('menu.select')
        def shown(entries):return next(n['text'] for n in entries if n['path'].endswith('/RoomCodeValue')).replace(' ','')
        assert shown(await nodes(guest))=='-----'
        grid=((1,2,3),(4,5,6),(7,8,9),(10,0,11));position={key:(r,c) for r,row in enumerate(grid) for c,key in enumerate(row)}
        row,col=position[1];typed=''
        async def focus(key):
            nonlocal row,col
            r,c=position[key]
            while row!=r:
                await guest.query('menu.navigate',{'direction':'down' if r>row else 'up','handler':True});row+=1 if r>row else -1
            while col!=c:
                await guest.query('menu.navigate',{'direction':'right' if c>col else 'left','handler':True});col+=1 if c>col else -1
        for digit in code:
            await focus(int(digit));await guest.query('menu.validate');typed+=digit
            deadline=time.monotonic()+3
            while shown(await nodes(guest))!=typed+'-'*(5-len(typed)):
                assert time.monotonic()<deadline,shown(await nodes(guest));await asyncio.sleep(.02)
        assert shown(await nodes(guest))==code
        assert (await net(host))['room_code']==int(code) and (await net(host))['connected_players']==1
        await focus(11);await guest.query('menu.validate')
        trace=[];deadline=time.monotonic()+40
        while True:
            h,g=await net(host),await net(guest);trace.append({'time':time.monotonic(),'host':h,'guest':g})
            assert h['connected_players']<=2 and g['connected_players']<=2,'Unexpected third player: preserve session'
            if h['connected_players']==g['connected_players']==2 and not g['is_host'] and g['room_code']==h['room_code']:break
            assert time.monotonic()<deadline,{'host':h,'guest':g,'text':[n.get('text','') for n in await nodes(guest)]}
            await asyncio.sleep(.1)
        assert h['is_host'] and h['lan'] and g['lan'] and h['private'] and g['private']
        report.update(passed=True,host=h,guest=g,trace=trace,guest_keypad_verified=True)
        assert (await host.query('game.errors'))['total']==(await guest.query('game.errors'))['total']==0
    except Exception as error:
        report['error']=repr(error);report['host']=await net(host);report['guest']=await net(guest);raise
    finally:(a.evidence/'result.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host-pid',type=int,required=True);p.add_argument('--guest-pid',type=int,required=True)
    p.add_argument('--host-slot',type=int,required=True);p.add_argument('--guest-slot',type=int,required=True);p.add_argument('--evidence',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
