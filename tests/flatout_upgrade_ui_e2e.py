"""Check upgrade preview and non-purchasing dialog recovery through real XR input."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT


class UpgradeUI(Frontend):
    async def gauges(self):
        nodes=(await self.query('hud.rects',{'root':'menu','max_depth':22}))['nodes']
        values={n['path']:n['rect'] for n in nodes if '/Gauge_Gain/Gauge_1' in n['path']}
        assert len(values)==6,values
        return values

    async def stick_round_trip(self):
        initial=(await self.query('menu.get_state'))['selection_index']
        for direction in (1,-1):
            native=await self.query('vr.targets')
            back=next(t for t in native['targets'] if t['name'].startswith('helper.Cancel.'))
            l,t,r,b=back['rect']
            _,args=await self.point_args(back['name'],((100-l)/(r-l),(500-t)/(b-t)))
            args.update(stick=[direction,0],dwell_frames=2,hold_frames=3,command_id=uuid.uuid4().hex)
            before=(await self.query('menu.get_state'))['selection_index']
            receipt=await self.tool('scroll_ui_at',args,'upgrade-native-stick-'+str(direction))
            assert receipt['status']=='input_delivered'
            selected=(await self.query('menu.get_state'))['selection_index']
            assert selected!=before,'Native thumbstick did not navigate the upgrade carousel'
            await self.hover('legacy.'+str(selected))
            labels=(await self.query('hud.rects',{'root':'menu','max_depth':22}))['nodes']
            captions=[n['text'] for n in labels if n['visible'] and n['path'].endswith('/RayActionLabel')]
            assert 'Buy' in captions and 'Confirm' not in captions,('Ray return lost its specific Buy action',captions)
        assert (await self.query('menu.get_state'))['selection_index']==initial,'Opposite stick steps did not restore selection'
        self.cases.append({'case':'native-thumbstick-to-ray-retains-buy-caption','passed':True})

    async def run_upgrades(self):
        initial=await self.query('menu.get_state')
        assert initial['current_menu']=='UPGRADES' and initial['item_count']>=4,initial
        assert not (await self.query('ui.get_message_box'))['open']
        await self.audit('upgrade-controls-spaced')
        snapshots=[]
        for target in ('legacy.0','legacy.3'):
            await self.hover(target)
            await asyncio.sleep(.25)
            assert (await self.query('menu.get_state'))['selection_index']==initial['selection_index'],'Preview committed an upgrade selection'
            assert not (await self.query('ui.get_message_box'))['open'],'Hover opened a purchase dialog'
            snapshots.append(await self.gauges())
            await self.capture('upgrade-preview-'+target)
        assert snapshots[0]!=snapshots[1],('Different upgrades retained the same preview bars',snapshots)
        self.cases.append({'case':'hover-preview-follows-upgrade-without-commit','passed':True,'gauges':snapshots})
        await self.stick_round_trip()
        # A tile activation may only open the existing purchase/error dialog.
        # Cancel a purchase; acknowledge an insufficient-cash message. Never buy.
        await self.click('legacy.3')
        native=await self.query('vr.targets')
        assert native['targets'] and all(t['name'].startswith('msgbox.') for t in native['targets']),native
        await self.popup_spacing()
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'upgrade-dialog-both-eyes')
        names=[t['name'] for t in native['targets']]
        dismiss='msgbox.cancel' if 'msgbox.cancel' in names else 'msgbox.validate'
        assert dismiss=='msgbox.cancel' or names==['msgbox.validate'],names
        await self.click(dismiss)
        assert not (await self.query('ui.get_message_box'))['open']
        assert (await self.query('menu.get_state'))['current_menu']=='UPGRADES'
        self.cases.append({'case':'upgrade-dialog-exclusive-and-recoverable-without-purchase','passed':True,'dismiss':dismiss})


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    report={'passed':False,'pid':args.pid,'hand':args.hand}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=UpgradeUI(args,session)
            try:
                if args.stick_only:
                    await test.stick_round_trip()
                else:
                    await test.run_upgrades()
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
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--hand',choices=('left','right'),default='right')
    p.add_argument('--stick-only',action='store_true',help='Check native thumbstick navigation and restoration of the specific ray Buy action')
    asyncio.run(run(p.parse_args()))
