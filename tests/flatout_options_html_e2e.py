"""Native Options HTML fidelity and real screenshot -> XR ray regressions.

Start in Options with an isolated simulator profile. No menu input injection.
The original settings interaction suite supplies choice/toggle/gauge/reset and
binding-capture checks; geometry comes from the retained CSS import manifest.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT


class OptionsHtmlTest(Frontend):
    async def options_redesign_layout(self):
        state = await self.query('options.get_state')
        assert state['open']
        targets = (await self.query('vr.targets'))['targets']
        nodes = (await self.query('hud.rects', {'root':'menu','max_depth':14}))['nodes']
        g = self.geometry
        panel = next(n for n in nodes if n['path'].endswith('/OptionsSettings') and n['visible'])
        expected = [g['SettingsX'],g['PanelY'],g['SettingsX']+g['SettingsWidth'],g['PanelY']+g['PanelHeight']]
        assert all(abs(a-b)<1 for a,b in zip(panel['rect'],expected)), (panel,expected)
        categories = [t for t in targets if t['name'].startswith('settings.category.')]
        assert len(categories)==len(state['boxes'])
        assert all(t['rect'][2]-t['rect'][0]>400 and t['rect'][3]-t['rect'][1]>=80 for t in categories)
        rows = [t for t in targets if '.row' in t['name']]
        assert rows
        for t in rows:
            assert t['name'].startswith('box'+str(state['selection_index'])+'.row')
            assert t['rect'][1]>=g['RowsY']-.5 and t['rect'][3]<=g['HelpY']+.5, t
        actions = [t for t in targets if t['name'].startswith('helper.')]
        assert len(actions)==3, actions
        assert max((t['rect'][1]+t['rect'][3])/2 for t in actions)-min((t['rect'][1]+t['rect'][3])/2 for t in actions)<=1
        assert all(t['rect'][1]>=panel['rect'][3]+23 and t['rect'][3]<=1056 for t in actions)
        for t in [t for t in targets if t['name'].startswith('settings.scroll.')]:
            assert g['PanelY']<t['rect'][1]<g['RowsY'] and t['rect'][3]<g['RowsY']
        self.cases.append(dict(case='native-css-geometry-and-separate-action-dock',passed=True,category=state['selection_index']))

    async def run(self):
        if self.args.review_only:
            for index in range(7):
                await self.click('settings.category.'+str(index))
                await self.options_redesign_layout()
                await asyncio.sleep(.6)
                await self.capture('options-category-'+str(index)+'-final')
        else:
            await self.options()  # every category/page, hover does not edit settings
        await self.options_interactions()  # includes restoration of edited values
        await self.click('settings.category.2')
        await self.hover('helper.Secondary.OptionsReset')
        await asyncio.sleep(.6)
        from flatout_ui_depth_contract import assert_hover_lift
        self.cases.append(dict(case='submitted-20mm-physical-hover',passed=True,
            geometry=assert_hover_lift(await self.capture('options-mirrors-final'))))
        self.final_snapshot = {
            'options':await self.query('options.get_state'),
            'targets':await self.query('vr.targets'),
            'hud':await self.query('hud.rects',{'root':'menu','max_depth':14}),
        }


async def main(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    result=dict(passed=False,pid=args.pid,hand=args.hand)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test=OptionsHtmlTest(args,session)
            test.geometry=json.loads(args.layout.read_text())['geometry']
            try:
                await test.run()
                result['passed']=True
                (args.evidence/'native-snapshot.json').write_text(json.dumps(test.final_snapshot,indent=2))
            except Exception as error:
                result['error']=repr(error)
                raise
            finally:
                result['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    await test.tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':args.hand},'final-release')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--layout',type=Path,required=True)
    p.add_argument('--hand',choices=['left','right'],default='right')
    p.add_argument('--review-only',action='store_true',help='Final visual recapture plus all setting interactions; initial full run covers every page')
    asyncio.run(main(p.parse_args()))
