"""Stage real saved Cup standings in the owned disposable UI slot.

Uses the existing race-end diagnostic once to shorten the first round; all
score accumulation is the normal result flow. Never edits profile scores or
unlocks, never submits leaderboards, and never accepts unknown dialogs.
This is fixture setup, not a substitute for screenshot-derived UI input tests.
"""
import argparse,asyncio,json,struct,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from flatout_ui_atlas import Atlas,bounded_pipe_read

def race(pid,method,params=None):
    assert method in {'race.get_state','race.skip_intro','race.force_end','race.end_screen'}
    wire=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}).encode()
    with open(r'\\.\pipe\FlatoutMCP_'+str(pid),'r+b',buffering=0) as p:
        p.write(struct.pack('<I',len(wire))+wire);deadline=time.monotonic()+12
        n=struct.unpack('<I',bounded_pipe_read(p,4,deadline))[0];assert 0<n<2**24
        result=json.loads(bounded_pipe_read(p,n,deadline))
    assert 'error' not in result,result
    result=result['result'];assert 'error' not in result,result
    return result

async def run(a):
    assert a.fixture_slot==641062,'Only the explicitly launched disposable Career slot'
    a.evidence.mkdir(parents=True,exist_ok=False)
    args=SimpleNamespace(pid=a.pid,output=Path('unused'),current=False,owner_queue=None,native_timeout=8,settle=.25,dwell_frames=3,hand='right',fixture=['showroom'])
    r=Atlas(args,None,{'operations':[]});r.active_case={'id':'saved-cup-standings-fixture'}
    report={'passed':False,'fixture_slot':a.fixture_slot,'pid':a.pid,'staging_only':True,'steps':[]}
    try:
        actual_slot=(await r.query('game.get_state')).get('diagnostic_save_slot')
        report['actual_diagnostic_save_slot']=actual_slot
        assert actual_slot==a.fixture_slot, ('Running process is not in the requested disposable slot; no staging performed',actual_slot)
        if not a.from_results:
            if a.replace_active:
                await r.normal_flow('career');hub=await r.query('menu.get_state')
                if hub['career_hub']['active_cup']:
                    report['replaced_owned_fixture_cup']=hub['career_hub']
                    action=next(i for i in hub['items'] if 'cancel' in i.get('title','').lower())
                    await r.select(action['index']);await asyncio.sleep(.2)
                    text=[n.get('text','') for n in (await r.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes'] if n['visible']]
                    assert (await r.query('ui.get_message_box'))['open'] and any('cup' in t.lower() for t in text),text
                    await r.query('ui.confirm_message_box',{'cancel':False});await asyncio.sleep(.25)
            await r.normal_flow('event_selection')
            grid=await r.query('menu.get_state');cup=grid['career']['tiles'][0]
            assert cup['rounds']>1 and not cup['locked'] and cup['index']==0 and cup['line']==0
            await r.select(0);preview=await r.query('menu.get_state');assert preview['cup']['can_start']
            report['cup']=preview['cup'];await r.query('menu.validate')
            for _ in range(180):
                g=await r.query('game.get_state')
                if not g['is_loading'] and not g['is_main_menu'] and g['state']=='race':break
                await asyncio.sleep(.5)
            else:raise AssertionError(('Cup did not load',g))
            assert g['game_mode']=='career',g
            report['steps'].append({'loaded':g})
            report['steps'].append({'skip_intro':race(a.pid,'race.skip_intro')})
            for _ in range(120):
                state=race(a.pid,'race.get_state')
                if state['state']=='racing':break
                assert state['state'] in ('intro','countdown','wait_init'),state['state']
                await asyncio.sleep(.1)
            else:raise AssertionError(('Race did not begin',state['state']))
            result=race(a.pid,'race.force_end');assert result.get('success'),result
            report['steps'].append({'shortened_owned_race':result})
        for _ in range(120):
            state=race(a.pid,'race.end_screen')
            if state['screen'] in (1,2,3,4,6,7):break
            await asyncio.sleep(.25)
        else:raise AssertionError(('Results did not open',state))
        for _ in range(8):
            state=race(a.pid,'race.end_screen');report['steps'].append({'end_screen':state})
            if state['screen']==7:break
            assert state['screen'] in (1,2,3,4,6) and state['can_continue'],state
            race(a.pid,'race.end_screen',{'action':'continue'});await asyncio.sleep(.8)
        else:raise AssertionError(('Next-round preview absent',state))
        report['next_preview_nodes']=await r.query('hud.rects',{'root':'menu','max_depth':12})
        report['errors_at_next_preview']=await r.query('game.errors')
        assert report['errors_at_next_preview']['total']==0,report['errors_at_next_preview']
        if a.stop_at_next_preview:
            report['passed']=True
            return
        race(a.pid,'race.end_screen',{'action':'quit'});await asyncio.sleep(.3)
        nodes=(await r.query('hud.rects',{'root':'msgbox','max_depth':20}))['nodes']
        texts=[n.get('text','') for n in nodes if n['visible']]
        assert (await r.query('ui.get_message_box'))['open'] and any('main menu' in t.lower() for t in texts),texts
        await r.query('ui.confirm_message_box',{'cancel':False})
        for _ in range(180):
            g=await r.query('game.get_state')
            if g['is_main_menu'] and g['main_menu_ready'] and not g['is_loading']:break
            await asyncio.sleep(.5)
        else:raise AssertionError(('Frontend did not return',g))
        await r.main();await r.select(0)
        hub=await r.query('menu.get_state');assert hub['career_hub']['active_cup'],hub
        report['hub']=hub['career_hub'];await r.select(0)
        state=await r.query('menu.get_state');assert state['current_menu']=='FLATOUT' and state['item_count']==0,state
        report['standings']=state;report['targets']=await r.query('vr.targets');report['nodes']=await r.query('hud.rects',{'root':'menu','max_depth':24})
        assert (await r.query('game.errors'))['total']==0
        report['passed']=True
    finally:(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--fixture-slot',type=int,required=True);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--stop-at-next-preview',action='store_true',help='Leave the actual in-race next-round menu open for XR E2E qualification')
    p.add_argument('--replace-active',action='store_true',help='Cancel an active cup only in the owned disposable slot before creating the fixture')
    p.add_argument('--from-results',action='store_true',help='Continue staging from a previously shortened owned Cup race')
    asyncio.run(run(p.parse_args()))
