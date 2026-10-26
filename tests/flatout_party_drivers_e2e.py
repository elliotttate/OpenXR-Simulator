"""Audit eight-player Party driver selection through real screenshot-derived rays.

Start in DRIVERS for eight players, before any choices. Native diagnostics are
read-only. --reselect-only isolates the Back/reselection model regression.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys

import numpy as np

from flatout_frontend_e2e import Frontend, ROOT, ClientSession, StdioServerParameters, stdio_client


class PartyDrivers(Frontend):
    async def finish_event(self):
        # Party presents the native stunt instructions and Start before Pause
        # is available. Loading the level alone does not start the event.
        if not self.args.finish_only:
            for _ in range(60):
                targets=(await self.query('vr.targets'))['targets']
                start=next((t['name'] for t in targets if t['name'].startswith('endhelper.Validate.')),None)
                if start:break
                await asyncio.sleep(.25)
            else:raise AssertionError(('Party instructions did not present Start',targets))
            await self.audit('party-stunt-start-control-spacing')
            await self.click(start)
        for _ in range(60):
            if not any(t['name'].startswith('endhelper.') for t in (await self.query('vr.targets'))['targets']):break
            await asyncio.sleep(.25)
        else:raise AssertionError('Start did not leave Party instructions')
        self.cases.append({'case':'party-instructions-already-dismissed' if self.args.finish_only else 'party-instructions-start-once','passed':True})
        for _ in range(60):
            if (await self.query('ui.get_message_box'))['open']:break
            await asyncio.sleep(.25)
        else:raise AssertionError('Party did not present the first player Launch Round prompt')
        await self.popup_spacing()
        await self.click('msgbox.validate')
        for _ in range(60):
            state=await self.query('game.get_state')
            if not state['is_menu_active'] and not state['is_paused']:break
            await asyncio.sleep(.25)
        else:raise AssertionError(('Launch Round did not release gameplay',state))
        await self.return_main()

    async def observed_head_pose(self, label, pose):
        capture=await super().observed_head_pose(label,pose)
        vr=await self.query('vr.get_state')
        camera=vr['base_camera']
        assert all(np.allclose(camera[k],self.preview_camera[k],atol=1e-5) for k in ('position','x','y','z')),('Driver camera followed physical head movement',camera)
        basis=np.array([camera[k] for k in ('x','y','z')]).T
        for eye in vr['eyes']:
            expected=np.array(camera['position'])+basis@eye['tracking_pose']['position']
            assert np.allclose(eye['render_position'],expected,atol=.001),('Driver preview lost one-to-one head movement',eye,expected)
        self.cases.append({'case':label+'-level-camera-and-one-to-one-head-motion','passed':True,'camera':camera,'eyes':vr['eyes']})
        return capture

    async def view(self):
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':48}))['nodes']
        slots=[n for n in nodes if n['visible'] and n['path'].endswith('/Element_PartyGame_Player')]
        captions=[n.get('text','') for n in nodes if n['visible'] and n['path'].endswith('/ELEMENT_PARTYGAME_TXT/TXT_PlayerNum')]
        cursor=next((int(re.search(r'\d+',c).group()) for c in captions if re.search(r'\d+',c)),None)
        names={n['path'].count('/Element_PartyGame_Player')-1:n['text'].strip() for n in nodes
               if n['visible'] and n['path'].endswith('/TXT_DriverName')}
        # Each loaded Flash row contains an empty child insertion point for
        # the next row. Count populated driver fields, not that final template.
        return {'slots':len(names),'slot_insertion_points':len(slots),'cursor':cursor,'names':names,'native':await self.query('party.get_state')}

    async def cursor(self, expected):
        for _ in range(30):
            view=await self.view()
            if view['cursor']==expected:return view
            await asyncio.sleep(.1)
        raise AssertionError(('Driver player prompt did not advance correctly',expected,view))

    async def action(self, token):
        name=next(t['name'] for t in (await self.query('vr.targets'))['targets'] if t['name'].startswith('helper.'+token+'.'))
        await self.click(name)

    async def reselect(self):
        await self.cursor(1)
        await self.action('Validate');await self.cursor(2)
        await self.action('Validate');await self.cursor(3)
        before=(await self.query('party.get_state'))['driver_choices']
        assert len(before)==2,before
        await self.action('Cancel');await self.cursor(2)
        await self.action('Validate');after=await self.cursor(3)
        self.cases.append({'case':'back-and-reselect-replaces-the-same-player','passed':after['native']['driver_choices']==before,
                           'before':before,'after':after})
        assert after['native']['driver_choices']==before,('Reselecting Player2 appended another driver',before,after)

    async def run_drivers(self):
        if self.args.finish_only:
            await self.finish_event();return
        assert (await self.query('menu.get_state'))['current_menu']=='DRIVERS'
        initial=await self.cursor(1)
        assert initial['native']['players']==8,initial
        if self.args.reselect_only:
            await self.reselect();return
        await self.capture('party-driver-slot-count')
        self.cases.append({'case':'exactly-eight-native-player-slots','passed':initial['slots']==8,'view':initial})
        assert initial['slots']==8,('Extra Party player widget exceeds the eight-player capacity',initial)
        assert not any(initial['names'].values()),('New driver selection retains old names',initial)
        self.preview_camera=(await self.query('vr.get_state'))['base_camera']
        camera=self.preview_camera
        assert abs(camera['z'][2])<1e-5 and camera['y']==[0,0,1],('Driver preview tilts the room',camera)
        await self.reading_comfort('party-drivers')
        self.cases.append({'case':'driver-preview-keeps-the-ground-level','passed':True,'camera':camera})
        await self.audit('party-driver-controls-spaced')
        await self.reselect()
        for player in range(3,9):
            await self.cursor(player)
            await self.action('Validate')
            state=await self.query('party.get_state')
            assert len(state['driver_choices'])==player,('Wrong stored driver count',player,state)
            if player<8:await self.cursor(player+1)
        assert (await self.query('ui.get_message_box'))['open'],'Last player needs a start confirmation'
        complete=await self.view()
        assert len(complete['native']['driver_choices'])==8,complete
        assert not complete['names'],'The modal must hide the background roster'
        await self.popup_spacing()
        await self.popup_anchor()
        await self.click('msgbox.cancel')
        returned=await self.cursor(8)
        assert len([n for n in returned['names'].values() if n])==7,returned
        await self.action('Validate')
        assert len((await self.query('party.get_state'))['driver_choices'])==8,'Reselecting last player appended a ninth choice'
        assert (await self.query('ui.get_message_box'))['open']
        self.cases.append({'case':'eight-choices-confirmation-cancel-and-reselect','passed':True,'complete':complete,'returned':returned})
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'party-eight-players-start-confirmation')
        if not self.args.start_race:
            await self.click('msgbox.cancel');return
        await self.click('msgbox.validate')
        for _ in range(180):
            game=await self.query('game.get_state')
            if not game['is_main_menu'] and not game['is_loading']:break
            await asyncio.sleep(.5)
        else:raise AssertionError(('Party race did not finish loading',game))
        state=await self.query('party.get_state')
        assert state['players']==8 and len(state['driver_choices'])==8,('Start confirmation added another player',state)
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'party-eight-players-race-started')
        self.cases.append({'case':'confirmed-start-preserves-exactly-eight-driver-choices','passed':True,'game':game,'state':state})
        await self.finish_event()


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'hand':args.hand,'reselect_only':args.reselect_only,'start_race':args.start_race}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=PartyDrivers(args,session)
            try:
                await test.run_drivers()
                report['native_errors']=await test.query('game.errors')
                assert report['native_errors']['total']==0,report['native_errors']
                report['passed']=True
            except Exception as error:
                report['error']=repr(error);raise
            finally:
                report['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--reselect-only',action='store_true');p.add_argument('--start-race',action='store_true')
    p.add_argument('--finish-only',action='store_true',help='Resume at the first player Launch Round prompt after a diagnostic interruption')
    p.add_argument('--expected-menu',default='MULTIPLAYER',help='Frontend destination after leaving the test Party race')
    asyncio.run(run(p.parse_args()))
