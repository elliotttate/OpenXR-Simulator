"""Native screenshot -> hover -> held click/retry -> Main-menu result.

Menu staging uses existing native callbacks. Button activation itself uses the
captured image and both real XR hands; no direct activation or UI memory writes.
"""
import argparse,asyncio,json,os,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT
from flatout_ui_depth_contract import assert_hover_lift
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas
from flatout_button_audit import inspect

async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);a.output=a.evidence;a.current=False;a.owner_queue=None;a.native_timeout=8;a.settle=.25;a.dwell_frames=3;a.fixture=[]
    result={'passed':False,'scope':__doc__,'cases':[]}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Frontend(a,session);stage=Atlas(a,session,{'operations':[]})
            try:
                assert (await t.query('game.get_state'))['diagnostic_save_slot']==641062
                for hand in ('left','right'):
                    a.hand=hand
                    for route,target in [('single_events','helper.Cancel.QuickBack'),('options','helper.Cancel.OptionsBack'),('challenge','helper.Cancel.0')]:
                        stage.active_case={'id':'button-'+route};await stage.normal_flow(route);await stage.prepare_ray_presentation()
                        message=await t.query('ui.get_message_box');assert not message['open']
                        nodes=(await t.query('hud.rects',{'root':'menu','max_depth':48,'presentation':True}))['nodes']
                        audit=await inspect({'nodes':nodes,'message':message},t.query,a.fox_root)
                        assert audit['checks'] and audit['passed'],audit
                        before=(await t.query('vr.targets'))['clicks'];await t.hover(target)
                        capture=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},hand+'-'+route+'-hover');t.last_capture=capture
                        depth=assert_hover_lift(capture);assert (await t.query('vr.targets'))['clicks']==before
                        await t.click(target)
                        after=await t.query('menu.get_state');assert after['current_menu']=='MAIN MENU',after
                        result['cases'].append({'hand':hand,'menu':route,'target':target,'caption_checks':len(audit['checks']),'physical_depth':depth,'result':'MAIN MENU','passed':True})
                result['errors']=await t.query('game.errors');assert result['errors']['total']==0;result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['input_cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
