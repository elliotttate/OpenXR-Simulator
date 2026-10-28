"""Profile an owned FlatOut menu: idle, repeated hover, and recovery.

No clicks, racing, purchases or Wreck Run. Screenshots are outside timed windows.
Native sweep isolates presentation without screenshot/input IPC inside the window.
--real-xr measures a six-second screenshot-derived controller hover, including haptics.
Retains CPU hitch stacks and final submitted physical-button geometry.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import struct
import sys
import time
import uuid
from types import SimpleNamespace

import numpy as np

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_challenge_style_e2e import ChallengeStyle
from flatout_ui_depth_contract import assert_hover_lift
from flatout_frontend_e2e import Frontend
from flatout_ray_offset import native_pointer_offset
from flatout_livery_e2e import ui_surface, quaternion_matrix
from flatout_ui_motion_policy import HOVER_LIFT_METERS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from flatout_ui_atlas import bounded_pipe_read
sys.path.insert(0,str(ROOT/'mcp-server'))
from ui_targeting import UiAutomation,resolve_target


def call(pid, method, params=None):
    assert method in {'perf.capture_start', 'perf.capture_read', 'game.errors', 'vr.beam', 'vr.targets', 'race.end_screen'}
    wire = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode()
    deadline = time.monotonic() + 15
    with open(r'\\.\pipe\FlatoutMCP_' + str(pid), 'r+b', buffering=0) as pipe:
        pipe.write(struct.pack('<I', len(wire)) + wire)
        size = struct.unpack('<I', bounded_pipe_read(pipe, 4, deadline))[0]
        assert 0 < size < 32*1024*1024
        result = json.loads(bounded_pipe_read(pipe, size, deadline))
    assert 'error' not in result, result
    result = result['result']
    assert 'error' not in result, result
    return result


def summarize(capture):
    result = {'frames': capture['count'], 'metrics': {}}
    for name in ('frame_ms', 'cpu_main_wall_ms', 'cpu_render_wall_ms', 'gpu_ms', 'dx11_query_wait_ms', 'draw_calls'):
        col = capture['columns'].index(name)
        values = sorted(r[col] for r in capture['rows'] if r[col] is not None)
        result['metrics'][name] = {'median': statistics.median(values), 'p95': values[int((len(values)-1)*.95)],
                                   'max': max(values)} if values else None
    col = capture['columns'].index('frame_ms')
    result['over_50ms'] = sum(r[col] > 50 for r in capture['rows'])
    return result


class ShowroomProfile(Frontend):
    async def frame(self, label):
        capture = await self.tool('capture_screenshot', {'eye':'both','max_width':2048}, label)
        self.last_capture = capture
        return SimpleNamespace(capture=capture)

    async def park(self):
        target = (await self.query('vr.targets'))['targets'][0]
        l, top, r, b = target['rect']
        for hand in ('left','right'):
            _, aim = await self.point_args(target['name'], ((960-l)/(r-l),(30-top)/(b-top)),hand)
            receipt = await self.tool('hover_ui_at',aim,'park-'+hand)
            assert receipt['status']=='input_delivered'
        await asyncio.sleep(.5)
        assert not (await self.query('vr.targets'))['hovered']

    async def depth(self, capture):
        native = await self.query('vr.targets')
        menu = await self.query('menu.get_state')
        spatial = False if self.args.menu=='next-race' else menu['is_3d']
        base = ui_surface({'vr':{'frame':native['vr_frame']}}) if native.get('vr_frame') and not spatial else max(
            (s for s in capture['geometry']['surfaces'] if s['visibility']==0),key=lambda s:s['size'][0]*s['size'][1])
        normal = quaternion_matrix(base['pose']['orientation'])[:,2]
        tiles = [s for s in capture['geometry']['surfaces'] if s['visibility']==0 and s.get('id')!=base.get('id')]
        lifts = [float(np.dot(np.array(s['pose']['position'])-base['pose']['position'],normal)) for s in tiles]
        assert any(abs(lift-HOVER_LIFT_METERS)<.001 for lift in lifts), lifts
        assert len(tiles)<=2, tiles
        return {'lift_meters':lifts,'base':base,'tiles':tiles}


async def run(a):
    a.evidence.mkdir(parents=True, exist_ok=False)
    a.hand = 'right'
    a.capture_width = 2048
    result = {'pid': a.pid, 'real_xr': a.real_xr, 'windows': {}, 'limits': [
        'Simulator runtime, Release build; not a headset/SteamVR qualification.',
        'GPU timestamps are delayed; CPU and GPU rows are not synchronized.',
        'Hitch stack collection itself has a reported copy cost; screenshots excluded from windows.']}
    server = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / 'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ, 'SIMXR_DATA_DIR': str(a.data_dir.resolve())})
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            t = ShowroomProfile(a,session) if a.menu in ('garage','cars','drivers','upgrades','customization','career','classes','events','cup','welcome','standings','next-race','online','multiplayer','browser','extras','statistics','credits','friends') else ChallengeStyle(a, session)
            t.frames = []
            menus = {'options': 'OPTIONS', 'challenge': 'INSANITY', 'quick': 'QUICK EVENTS', 'garage':'GARAGE', 'cars':'CARS','drivers':'DRIVERS','upgrades':'UPGRADES','customization':'SKINS','career':'FLATOUT','classes':'FLATOUT','events':'FLATOUT_MODE','cup':'FLATOUT_MODE','welcome':'MAIN MENU','standings':'FLATOUT','next-race':'in_game','online':'ONLINE','multiplayer':'MULTIPLAYER','browser':'ONLINE','extras':'EXTRAS','statistics':'PLAYER STATS','credits':'CREDITS','friends':'ONLINE'}
            assert (await t.query('menu.get_state'))['current_menu'] == menus[a.menu]
            if a.menu == 'next-race':
                assert (await asyncio.to_thread(call,a.pid,'race.end_screen'))['screen']==7
                assert not (await t.query('ui.get_message_box'))['open']
            if a.menu == 'welcome':
                assert (await t.query('ui.get_message_box'))['open']
                assert not (await t.query('menu.get_state'))['is_open']
            await t.park()
            points = {}
            rects = {x['name']: x['rect'] for x in (await t.query('vr.targets'))['targets']}
            targets_by_menu = {
                'options': ('settings.category.0', 'settings.category.2', 'helper.Cancel.OptionsDone'),
                'challenge': ('challenge.event.0', 'challenge.event.1', 'helper.Validate.2'),
                'quick': ('quick.mode.0', 'quick.mode.1', 'helper.Validate.QuickContinue'),
                'garage': ('item.1', 'item.2', 'helper.Cancel.0'),
                'cars': ('cars.item.0', 'cars.item.1', 'helper.Cancel.4'),
                'drivers': ('drivers.item.0', 'drivers.item.1', 'helper.Cancel.4'),
                'upgrades': ('upgrades.item.0', 'upgrades.item.1', 'helper.Cancel.UpgradeBack'),
                'customization': ('custom.item.0', 'custom.item.1', 'helper.Cancel.CustomBack'),
                'career': ('career.hero', 'career.garage', 'helper.Cancel.CareerBack'),
                'classes': ('item.0', 'item.1', 'helper.Cancel.ClassBack'),
                'events': ('tile.0.0', 'tile.0.1', 'helper.Cancel.EventsBack'),
                'cup': ('helper.Cancel.CupBack','cup.car','helper.Validate.CupStart'),
                'next-race': ('helper.Cancel.CupBack','helper.Validate.CupStart'),
                'online': ('setup.0.next','setup.4.next','helper.Cancel.LobbySetupBack'),
                'multiplayer': ('multiplayer.online','multiplayer.host','helper.Cancel.MultiplayerBack'),
                'browser': ('browser.refresh','browser.code','helper.Cancel.BrowserBack'),
                'extras': ('extras.statistics','extras.credits','helper.Cancel.ExtrasBack'),
                'statistics': ('statistics.page.next','helper.Cancel.StatisticsBack'),
                'credits': ('helper.Y.0','helper.LB.1','helper.Cancel.3'),
                'friends': ('player.0','party.action.0','helper.Cancel.PartyLeave'),
                'welcome': ('msgbox.recenter','msgbox.validate'),
                'standings': ('helper.Cancel.CupStandingsBack','helper.Validate.CupStandingsContinue'),
            }
            names = targets_by_menu[a.menu]
            for name in names:
                l, top, r, b = rects[name]
                points[name] = {'x': (l+r)/2, 'y': (top+b)/2}
            clicks = (await t.query('vr.targets'))['clicks']
            if a.menu=='friends':
                result['avatar_cache_before']={str(s['index']):s.get('steam_avatar') for s in (await t.query('net.get_state'))['slots']}
            try:
                for phase in ('idle', 'hover', 'recovery'):
                    if a.real_xr:
                        await asyncio.to_thread(call, a.pid, 'vr.beam', {'off': True})
                        await t.park()
                        if phase == 'hover':
                            if a.menu in ('next-race','online','multiplayer','browser','extras','statistics','credits','friends'):
                                # The moving scene can make park/capture take
                                # long enough that the spare 10 s lease expires
                                # during the window. Release it before aiming
                                # the active hand, never during measurement.
                                await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':'left'})
                            await t.hover(names[0])
                    else:
                        await asyncio.to_thread(call, a.pid, 'vr.beam', {'x': 10, 'y': 10, 'trigger': 0})
                    duration = a.window_ms if a.window_ms is not None else 6000 if a.real_xr else 18000
                    renewals=[]
                    if a.real_xr and duration>8000:
                        # Long windows outlive the normal ten-second input lease.
                        # Renew the observed XR poses without screenshots or native
                        # hover injection inside the measured window.
                        for hand in ('left','right'):
                            name=names[0] if phase=='hover' and hand=='right' else next(iter(rects))
                            l,top,r,b=rects[name]
                            fraction=(.5,.5) if phase=='hover' and hand=='right' else ((960-l)/(r-l),(30-top)/(b-top))
                            _,aim=await t.point_args(name,fraction,hand)
                            source=t.last_capture
                            offset=native_pointer_offset(await t.query('vr.get_state'),hand)
                            target=resolve_target(source,aim['x'],aim['y'],hand=hand,
                                origin_mode=aim.get('origin_mode','hand'),surface=aim.get('surface'),
                                surface_id=aim.get('surface_id'),ray_offset=offset or aim.get('ray_offset'))
                            pose=target['aim_pose']
                            payload={'action':'hover','session_id':source['geometry']['session_id'],
                                'capture_id':source['capture_id'],'hand':0 if hand=='left' else 1,
                                'button':'trigger','dwell_frames':1,'hold_frames':1,'lease_ms':10000,
                                'surface_id':target.get('surface_id',-1)}
                            payload.update(dict(zip(('px','py','pz'),pose['position'])))
                            payload.update(dict(zip(('qx','qy','qz','qw'),pose['orientation'])))
                            renewals.append(payload)
                        lease_backend=UiAutomation(a.data_dir)
                    await asyncio.to_thread(call, a.pid, 'perf.capture_start', {
                        'scope': 'transition' if a.menu=='next-race' else 'ui', 'duration_ms': duration, 'max_frames': 4000,
                        'hitch_detail_threshold_ms': 40, 'max_hitch_details': 32, 'hitch_thread_mode': 'all_cpu'})
                    print('CAPTURE_STARTED', phase, duration, flush=True)
                    start = time.monotonic()
                    renew_at=start+4
                    events = []
                    while time.monotonic() - start < duration/1000+.3:
                        if renewals and time.monotonic()>=renew_at:
                            for payload in renewals:
                                delivery=await lease_backend.command({**payload,'command_id':uuid.uuid4().hex},5)
                                assert delivery['success'],delivery
                                events.append({'lease_renewal':True,'hand':payload['hand'],'time_s':time.monotonic()-start,'delivery':delivery})
                            renew_at=time.monotonic()+4
                        if phase == 'hover' and not a.real_xr:
                            name = list(points)[len(events) % len(points)]
                            before = time.monotonic() - start
                            await asyncio.to_thread(call, a.pid, 'vr.beam', points[name])
                            state = await t.query('vr.targets')
                            events.append({'start_s': before, 'end_s': time.monotonic()-start,
                                           'target': name, 'hovered': state['hovered'], 'clicks': state['clicks']})
                        await asyncio.sleep(.65 if phase == 'hover' else .5)
                    capture = await asyncio.to_thread(call, a.pid, 'perf.capture_read', {'limit': 2000, 'hitch_details': True})
                    assert not capture['active'], capture['stop_reason']
                    offset = capture['next_offset']
                    while offset < capture['count']:
                        page = await asyncio.to_thread(call, a.pid, 'perf.capture_read', {'offset': offset, 'limit': 2000})
                        capture['rows'].extend(page['rows'])
                        offset = page['next_offset']
                    (a.evidence / (phase + '.json')).write_text(json.dumps({'capture': capture, 'events': events}, indent=2), encoding='utf-8')
                    if a.menu=='next-race':
                        assert (await asyncio.to_thread(call,a.pid,'race.end_screen'))['screen']==7
                        assert capture['scope']=='transition'
                    result['windows'][phase] = summarize(capture)
                    if a.real_xr:
                        pointer = await t.query('vr.targets')
                        result.setdefault('window_pointer', {})[phase] = {
                            'hovered': pointer['hovered'], 'clicks': pointer['clicks']}
                        assert pointer['clicks'] == clicks
                        assert pointer['hovered'] == (names[0] if phase == 'hover' else ''), (phase, pointer)
                    print(phase, json.dumps(result['windows'][phase]), flush=True)
                    if phase == 'hover':
                        await asyncio.to_thread(call, a.pid, 'vr.beam', {'off': True})
                        # The parked spare hand has a ten-second input lease.
                        # Renew both outside the measured window before taking
                        # more screenshots, so its expiry cannot steal focus.
                        if a.real_xr:
                            await t.park()
                        await t.hover(names[0])
                        await asyncio.sleep(1.2)
                        frame = await t.frame('depth-after-hover-stress')
                        result['physical_depth_after_stress'] = await t.depth(frame.capture) if a.menu in ('garage','cars','drivers','upgrades','customization','career','classes','events','cup','welcome','standings','next-race','online','multiplayer','browser','extras','statistics','credits','friends') else assert_hover_lift(frame.capture)
                    (a.evidence / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                assert (await t.query('vr.targets'))['clicks'] == clicks
                if a.menu=='friends':
                    result['avatar_cache_after']={str(s['index']):s.get('steam_avatar') for s in (await t.query('net.get_state'))['slots']}
                    assert result['avatar_cache_before']==result['avatar_cache_after'],'Hover stress decoded/uploaded an avatar again'
                result['native_errors'] = await asyncio.to_thread(call, a.pid, 'game.errors')
                assert result['native_errors']['total'] == 0, result['native_errors']
                hitches = sum(window['over_50ms'] for window in result['windows'].values())
                result['performance_gate'] = {'passed': hitches <= a.max_hitches,
                    'frames_over_50ms': hitches, 'allowed_frames_over_50ms': a.max_hitches,
                    'scope': 'Recorded windows only; not hardware or sustained-session qualification'}
                assert result['performance_gate']['passed'], result['performance_gate']
                result['completed'] = True
            finally:
                await asyncio.to_thread(call, a.pid, 'vr.beam', {'off': True})
                if t.last_capture:
                    for hand in ('left', 'right'):
                        await session.call_tool('release_ui_pointer', {'capture_id': t.last_capture['capture_id'], 'hand': hand})
                (a.evidence / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--real-xr', action='store_true')
    p.add_argument('--window-ms', type=int, help='Override each measured window (1000..20000 ms); default 6000 for real XR')
    p.add_argument('--max-hitches', type=int, default=0, help='Allowed recorded frames over 50 ms; default zero')
    p.add_argument('--menu', choices=('challenge', 'options', 'quick', 'garage', 'cars','drivers','upgrades','customization','career','classes','events','cup','welcome','standings','next-race','online','multiplayer','browser','extras','statistics','credits','friends'), default='challenge')
    args = p.parse_args()
    if args.max_hitches < 0:
        p.error('--max-hitches must be nonnegative')
    if args.window_ms is not None and not 1000 <= args.window_ms <= 20000:
        p.error('--window-ms must be in 1000..20000')
    asyncio.run(run(args))
