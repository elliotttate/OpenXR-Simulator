"""Cold Welcome must use the same level camera as the revealed spatial Main menu.

Attach only to a freshly launched, owned, isolated simulator game WITHOUT -silent.
The real WelcomeFlow must still be waiting for its first OK. This harness never
launches a game, reopens a synthetic welcome, or acknowledges unknown notices.
Run against an old binary before rebuilding to retain its failing camera baseline.
Focus simulation checks OpenXR FOCUSED/VISIBLE input safety, not SteamVR composition.
"""
import argparse
import asyncio
import copy
import uuid
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET, quad_pixel
from flatout_showroom_level_e2e import assert_rendered_eyes
from flatout_ui_depth_contract import assert_hover_lift
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(ROOT / 'tools'))
from flatout_ui_atlas import tracking_head_pose


def camera_delta(before, after):
    """Record useful failing evidence before any tolerance assertions."""
    def heading(direction):
        return math.atan2(direction[1], direction[0])

    def angle(a, b):
        return abs(math.degrees(math.atan2(math.sin(b-a), math.cos(b-a))))

    return {
        'base_position_m': float(np.linalg.norm(np.array(after['base_camera']['position']) - before['base_camera']['position'])),
        'base_yaw_deg': angle(heading(before['base_camera']['z']), heading(after['base_camera']['z'])),
        'base_basis_max_error': max(float(np.max(np.abs(np.array(after['base_camera'][k])-before['base_camera'][k]))) for k in ('x', 'y', 'z')),
        'eyes': [{'index': i,
                  'position_m': float(np.linalg.norm(np.array(b['render_position'])-a['render_position'])),
                  'yaw_deg': angle(heading(a['render_direction']), heading(b['render_direction'])),
                  'direction_max_error': float(np.max(np.abs(np.array(b['render_direction'])-a['render_direction'])))}
                 for i, (a, b) in enumerate(zip(before['eyes'], after['eyes']))]}


def assert_same_camera(before, after):
    delta = camera_delta(before, after)
    assert delta['base_position_m'] < .001 and delta['base_yaw_deg'] < .01 and delta['base_basis_max_error'] < 1e-4, ('Welcome acknowledgement relocated the base camera', delta)
    assert len(delta['eyes']) == 2 and all(e['position_m'] < .001 and e['yaw_deg'] < .01 and e['direction_max_error'] < 1e-4 for e in delta['eyes']), ('Welcome acknowledgement moved a rendered eye', delta)
    return delta


class StartupCamera(Frontend):
    async def screenshot(self, label):
        capture = await self.tool('capture_screenshot', {'eye': 'both', 'max_width': 2048, 'timeout': 30}, label)
        assert capture['geometry']['session_id'].split('-')[0] == str(self.args.pid), ('Screenshot belongs to another game process', capture['geometry']['session_id'])
        assert capture['geometry']['layout'] == 'side_by_side'
        self.last_capture = capture
        return capture

    async def welcome(self):
        menu = await self.query('menu.get_state')
        message = await self.query('ui.get_message_box')
        nodes = (await self.query('hud.rects', {'root': 'msgbox', 'max_depth': 48}))['nodes']
        assert message['open'] and any(n.get('visible') and n.get('text') == 'Welcome to FlatOut VR' for n in nodes), ('Expected the real cold-start Welcome; will not acknowledge another notice', message, nodes)
        assert menu['current_menu'] == 'MAIN MENU' and not menu['is_open'], ('WelcomeFlow gate was bypassed; launch fresh without -silent', menu)
        assert not any(i.get('rendered_bounds', {}).get('visible') for i in menu.get('items', [])), 'White Main controls are visible behind Welcome'
        targets = await self.query('vr.targets')
        names = [t['name'] for t in targets['targets']]
        assert names == ['msgbox.validate'], ('Welcome must expose Continue only; recenter is the two-thumbstick gesture', targets)
        return {'menu': menu, 'message': message, 'nodes': nodes, 'targets': targets}

    async def wait_welcome(self):
        deadline = asyncio.get_running_loop().time() + self.args.startup_timeout
        while True:
            message = await self.query('ui.get_message_box')
            if message['open']:
                return await self.welcome()  # An unknown notice fails without input.
            menu = await self.query('menu.get_state')
            assert not menu.get('is_open'), ('Main is already open; the one-shot cold-start gate is gone', menu)
            assert asyncio.get_running_loop().time() < deadline, 'Real WelcomeFlow did not open; check fresh launch and remove -silent'
            await asyncio.sleep(.1)

    async def focus(self, focused):
        response = await self.tool('set_session_focus', {'focused': focused}, 'focus-' + str(focused))
        assert response.get('queued'), response
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            response = await self.session.call_tool('get_session_state', {})
            text = '\n'.join(getattr(c, 'text', '') for c in response.content)
            if 'Session State: ' + ('FOCUSED' if focused else 'VISIBLE') in text:
                return
            assert asyncio.get_running_loop().time() < deadline, ('Focus handoff was not observed', focused, text)
            await asyncio.sleep(.05)

    async def state(self, label):
        capture = await self.screenshot(label)
        vr = await self.query('vr.get_state')
        assert_rendered_eyes(vr)
        return {'capture': capture, 'vr': vr}

    async def focus_cycles(self, report, original_pose):
        source = await self.screenshot('welcome-focus-source')
        carrier = max((s for s in source['geometry']['surfaces'] if s['visibility'] == 0), key=lambda s: np.prod(s['size']))
        welcome = await self.welcome()
        body = next(n for n in welcome['nodes'] if n['visible'] and n['path'].endswith(('/RayDialogBody','/WelcomeRoadmapFrame')))
        l, t, _, _ = body['rect']
        x, y = quad_pixel(source, (l+8, t+8, l+8, t+8))
        # Inert padding inside the known popup, outside every real action.
        assert all(not (target['rect'][0] <= l+8 <= target['rect'][2] and target['rect'][1] <= t+8 <= target['rect'][3])
                   for target in welcome['targets']['targets']), 'Focus-test padding overlaps a welcome action'
        baseline = await self.query('vr.get_state')
        for cycle in range(self.args.focus_cycles):
            source = await self.screenshot('focus-padding-source-' + str(cycle))
            held = await self.tool('hold_ui_at', {'capture_id': source['capture_id'], 'surface_id': carrier['id'],
                'x': float(x), 'y': float(y), 'hand': self.args.hand, 'origin_mode': 'hand', 'ray_offset': RAY_OFFSET,
                'dwell_frames': 1}, 'focus-held-padding-' + str(cycle))
            assert held['status'] == 'input_delivered', held
            assert (await self.query('vr.get_state'))['controllers'][1 if self.args.hand == 'right' else 0]['trigger'] > .8
            await self.focus(False)
            assert all(c['trigger'] == 0 for c in (await self.query('vr.get_state'))['controllers'])
            moved = {**original_pose, 'x': original_pose['x']+.03, 'yaw_deg': original_pose['yaw_deg']+6,
                     'pitch_deg': original_pose['pitch_deg']-3, 'roll_deg': original_pose['roll_deg']+2}
            self.last_capture = await self.observed_head_pose('welcome-visible-motion-' + str(cycle), moved)
            current = await self.state('welcome-visible-' + str(cycle))
            await self.welcome()
            assert current['capture']['geometry']['frame_id'] > source['geometry']['frame_id'], 'Rendering froze during VISIBLE'
            plane = max((s for s in current['capture']['geometry']['surfaces'] if s['visibility'] == 0), key=lambda s: np.prod(s['size']))
            assert np.allclose(plane['pose']['position'], carrier['pose']['position'], atol=.001) and np.allclose(plane['pose']['orientation'], carrier['pose']['orientation'], atol=.001), 'Welcome moved during focus loss'
            assert all(np.allclose(current['vr']['base_camera'][k], baseline['base_camera'][k], atol=1e-4) for k in ('position', 'x', 'y', 'z')), 'Focus loss moved the base camera'
            await self.focus(True)
            assert all(c['trigger'] == 0 for c in (await self.query('vr.get_state'))['controllers']), 'Held trigger resumed after focus restoration'
            self.last_capture = await self.observed_head_pose('welcome-refocus-restored-' + str(cycle), original_pose)
            await self.welcome()
            assert (await self.query('vr.targets'))['clicks'] == welcome['targets']['clicks'], 'Focus handoff activated OK'
            report['cases'].append({'case': 'focus-loss-continued-stereo-rendering-and-inactive-held-trigger-' + str(cycle), 'passed': True})

    async def roadmap(self, report):
        welcome = await self.welcome()
        nodes = welcome['nodes']
        def node(name):
            return next(n for n in nodes if n['visible'] and n['path'].endswith('/'+name))
        frame = node('WelcomeRoadmapFrame')['rect']
        assert np.allclose(frame, [240,64,1680,1016], atol=.1), frame
        title = node('WelcomeRoadmapTitle')
        assert title['text'] == 'Welcome to FlatOut VR'
        image = node('WelcomeRoadmapIllustration')
        rect = image['rect']
        assert abs((rect[3]-rect[1])-(rect[2]-rect[0])*520/1276) <= 1, rect  # hud.rects truncates pixel coordinates
        art = await self.query('hud.get_node', {'root':'msgbox','name':image['path'].split('/',1)[1],'presentation':True})
        image['presentation'] = art['presentation']
        assert np.allclose(image['presentation']['uv'], [0,0,1,1]), image
        assert 'F2VR_POPUP_ROADMAP' in str(image['presentation']).upper(), image
        assert not any(n['visible'] and n['path'].endswith(('/Title','/Content','/ContentImage')) for n in nodes), 'Legacy content duplicates native roadmap'
        report['roadmap_layout'] = {'frame':frame,'image':image,'title':title}
        report['cases'].append({'case':'uncropped-original-roadmap-and-native-margins','passed':True})
        help=node('WelcomeRecenterHelp')['rect']
        sticks=[]
        for name,uv in (('WelcomeThumbstickLeft',[.375,.25,.5,.375]),('WelcomeThumbstickRight',[0,.5,.125,.625])):
            stick=node(name);l,t,r,b=stick['rect']
            assert r-l>=111 and b-t>=111 and r<=help[0]-39,stick
            art=await self.query('hud.get_node',{'root':'msgbox','name':stick['path'].split('/',1)[1],'presentation':True})
            assert np.allclose(art['presentation']['uv'],uv),art
            assert 'INPUTS_QUEST' in art['presentation']['texture_resource'].upper(),art
            sticks.append(stick)
        assert sticks[1]['rect'][0]-sticks[0]['rect'][2]>=47,sticks
        targets=welcome['targets']['targets']
        action=node('WelcomeContinue')['rect']
        assert not any(n['visible'] and n['path'].endswith('/WelcomeRecenter') for n in nodes)
        assert 'choose' not in node('WelcomeRecenterHelp')['text'].lower()
        assert 'both thumbstick buttons' in node('WelcomeRecenterHelp')['text'].lower()
        # The enlarged passive gesture and its copy occupy the old left action
        # area; Continue is separate on the right, including its 4px hit padding.
        instruction_right=max(help[2],max(s['rect'][2] for s in sticks))
        assert action[0]-instruction_right>=63,(action,help,sticks)
        assert frame[3]-action[3]>=31,(frame,action)
        assert min(s['rect'][1] for s in sticks)-rect[3]>=40
        assert targets[0]['rect'][0]-instruction_right>=59
        capture=await self.screenshot('thumbstick-instructions-stereo')
        pixels=np.asarray(Image.open(self.last_image_path).convert('RGB'))
        visible=[]
        for stick in sticks:
            l,t,r,b=stick['rect']
            for eye in ('left','right'):
                points=[quad_pixel(capture,(x,y,x,y),eye) for x,y in ((l,t),(r,b))]
                x0,y0=np.floor(np.min(points,axis=0)).astype(int)+1
                x1,y1=np.ceil(np.max(points,axis=0)).astype(int)-1
                assert 0<=x0<x1<=pixels.shape[1] and 0<=y0<y1<=pixels.shape[0]
                patch=pixels[y0:y1,x0:x1]
                bright=int(np.count_nonzero(np.min(patch,axis=2)>150))
                assert bright>=12,('Thumbstick diagram is absent in composed pixels',eye,stick,bright)
                visible.append({'name':stick['path'],'eye':eye,'bright_pixels':bright})
        report['cases'].append({'case':'large-passive-two-thumbsticks-instead-of-recenter-button','passed':True,'sticks':sticks,'composed_visibility':visible})
        hand_before = self.args.hand
        try:
            for hand in ('left','right'):
                self.args.hand = hand
                for name in ('msgbox.validate',):
                    before, hovered, _ = await self.hover(name)
                    assert hovered['clicks'] == before['clicks']
                    depth = assert_hover_lift(await self.screenshot('roadmap-depth-'+hand+'-'+name))
                    report['cases'].append({'case':'physical-hover-'+hand+'-'+name,'passed':True,'depth':depth})
                    await self.session.call_tool('release_ui_pointer', {'capture_id':self.last_capture['capture_id'],'hand':hand})
                    await asyncio.sleep(.2)
            self.args.hand = hand_before
        finally:
            self.args.hand = hand_before

    async def takeover(self, report):
        source = await self.screenshot('roadmap-takeover-source')
        carrier = max((s for s in source['geometry']['surfaces'] if s['visibility']==0),key=lambda s:np.prod(s['size']))
        targets = await self.query('vr.targets')
        validate = next(t['rect'] for t in targets['targets'] if t['name']=='msgbox.validate')
        _, aim = await self.point_args('msgbox.validate')
        x,y=quad_pixel(self.last_capture,(300,900,300,900))
        aim.update(x=float(x),y=float(y))
        receipt = await self.tool('scroll_ui_at', {**aim,'hand':'left','stick':[0,1.],
            'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex}, 'welcome-stick-takeover')
        assert receipt['status']=='input_delivered', receipt
        legacy = await self.welcome()
        assert {t['name'] for t in legacy['targets']['targets']} == {'msgbox.validate'}, legacy['targets']
        assert not any(n['visible'] and n['path'].endswith('/WelcomeRoadmap') for n in legacy['nodes'])
        assert any(n['visible'] and n['path'].endswith('/Title') for n in legacy['nodes'])
        report['cases'].append({'case':'actual-stick-restores-original-welcome','passed':True})
        fresh = await self.screenshot('original-welcome-input-mode')
        projection = copy.deepcopy(fresh);projection['geometry']['surfaces']=[carrier]
        x,y = quad_pixel(projection,validate)
        receipt = await self.tool('hover_ui_at', {'capture_id':fresh['capture_id'],'x':float(x),'y':float(y),
            'hand':'left','surface':{k:carrier[k] for k in ('pose','size')},'origin_mode':'hand','dwell_frames':45},'ray-restores-roadmap')
        assert receipt['status']=='input_delivered', receipt
        native = await self.welcome()
        assert [t['name'] for t in native['targets']['targets']] == ['msgbox.validate']
        assert native['targets']['clicks']==targets['clicks']
        assert any(n['visible'] and n['path'].endswith('/WelcomeRoadmap') for n in native['nodes'])
        report['cases'].append({'case':'fresh-ray-restores-roadmap-without-activation','passed':True})
        await self.session.call_tool('release_ui_pointer', {'capture_id':self.last_capture['capture_id'],'hand':'left'})

    async def run_startup(self, report):
        report['initial_gate'] = await self.wait_welcome()
        if self.args.roadmap:
            await self.roadmap(report)
        if self.args.takeover:
            await self.takeover(report)
        original = await self.state('cold-welcome-both-eyes')
        original_pose = tracking_head_pose(original['vr'])
        report['original_head_pose'] = original_pose
        await self.focus_cycles(report, original_pose)
        report['before'] = await self.state('before-real-OK')
        await self.welcome()
        report['cases'].append({'case': 'real-cold-welcome-hides-spatial-main', 'passed': True})
        # Frontend.click takes a screenshot, hovers at projected OK, clicks once,
        # checks the native click count, then retries the same idempotency token.
        await self.click('msgbox.validate')
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            menu = await self.query('menu.get_state')
            if menu['is_open']:
                break
            assert asyncio.get_running_loop().time() < deadline, 'The real WelcomeFlow did not reveal Main after one OK'
            await asyncio.sleep(.05)
        assert menu['current_menu'] == 'MAIN MENU' and menu['is_3d'], menu
        assert not (await self.query('ui.get_message_box'))['open'], 'An unexpected notice followed Welcome; left unacknowledged'
        assert all(i['rendered_bounds']['visible'] for i in menu['items']), 'The white Main menu did not fully reveal'
        # Read two fresh frames: a transition must not pass on its opening frame.
        await asyncio.sleep(.25)
        report['after'] = await self.state('revealed-main-both-eyes')
        report['camera_delta'] = camera_delta(report['before']['vr'], report['after']['vr'])
        assert_same_camera(report['before']['vr'], report['after']['vr'])
        report['cases'].append({'case': 'unchanged-level-base-and-both-rendered-eyes-after-OK', 'passed': True, 'delta': report['camera_delta']})
        report['native_errors'] = await self.query('game.errors')
        assert report['native_errors']['total'] == 0, report['native_errors']


async def run(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'pid': args.pid, 'build_label': args.build_label, 'scope': __doc__, 'cases': []}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
                                  env={**os.environ, 'SIMXR_DATA_DIR': str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = StartupCamera(args, session)
            try:
                # Establish immutable-capture ownership before focus or ray input.
                await test.screenshot('owned-cold-session')
                await test.run_startup(report)
                report['passed'] = True
            except Exception as error:
                report['error'] = repr(error)
                raise
            finally:
                report['native_input_cases'] = test.cases
                (args.evidence/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                if test.last_capture:
                    await test.focus(True)
                    for hand in ('left', 'right'):
                        await session.call_tool('release_ui_pointer', {'capture_id': test.last_capture['capture_id'], 'hand': hand})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True, help='Owned freshly launched isolated simulator process')
    parser.add_argument('--data-dir', type=Path, required=True, help='That process\'s unique SIMXR_DATA_DIR')
    parser.add_argument('--evidence', type=Path, required=True, help='New directory; captures and failing baseline are retained')
    parser.add_argument('--build-label', required=True)
    parser.add_argument('--hand', choices=('left', 'right'), default='right')
    parser.add_argument('--takeover', action='store_true', help='Exercise actual XR-stick fallback and fresh-ray restoration while welcome remains open')
    parser.add_argument('--roadmap', action='store_true', help='Require the original roadmap, enlarged passive thumbstick instructions and both-hand Continue hover')
    parser.add_argument('--focus-cycles', type=int, choices=range(4), default=1)
    parser.add_argument('--startup-timeout', type=float, default=60)
    asyncio.run(run(parser.parse_args()))
