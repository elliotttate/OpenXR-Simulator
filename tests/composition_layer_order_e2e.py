"""Real swapchains -> ordered compositor -> screenshots of both eyes."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ui_automation_e2e import ROOT, call


async def run(args):
    out = args.evidence.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out/'settings.json').write_text(json.dumps(dict(headset_profile='quest3', layout='side_by_side',
        view_mode='both', asymmetric_fov=True, zoom_mode='fill', window_width=1024, window_height=512, preview_fps=-1)))
    env = {**os.environ, 'SIMXR_DATA_DIR': str(out), 'SIMXR_AUTOMATION_ONLY': '1',
           'SIMXR_RUNTIME_DLL': str(args.runtime.resolve()), 'XR_UI_BACKEND': args.backend}
    if args.stereo_quads: env['XR_UI_STEREO_QUADS']='1'
    result = {'passed': False, 'eyes': {}}
    with (out/'probe.log').open('w') as log:
        proc = subprocess.Popen([str(args.probe.resolve())], env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until = time.monotonic()+25
            while 'READY' not in (out/'probe.log').read_text():
                assert proc.poll() is None, (out/'probe.log').read_text()
                assert time.monotonic()<until, 'Probe did not become ready'
                await asyncio.sleep(.1)
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')], env=env)
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    for eye in ('left', 'right'):
                        info, image = await call(session, 'capture_screenshot', {'eye':eye,'max_width':640}, out, eye)
                        assert len(info['geometry']['projection_eyes']) == 2, 'Foreground projection replaced or duplicated the primary view metadata'
                        if args.stereo_quads:
                            assert len(info['geometry']['surfaces'])==2, 'Foreground eye images intercepted UI targeting'
                            assert len([d for d in info['geometry']['decorations'] if d['role']=='stereo_eye_image'])==2
                        r,g,b = image[:,:,:3].transpose(2,0,1)
                        foreground = (g>230)&(b<30)&((r<30) if eye=='left' else (r>230))
                        red = (r>230)&(g<30)&(b<30)
                        blue = (r<30)&(g<30)&(b>230)
                        blended = (r>235)&(g>170)&(g<205)&(b>170)&(b<205)
                        result['eyes'][eye] = {k:int(v.sum()) for k,v in dict(foreground=foreground,
                            preserved_menu=red, last_quad=blue, premultiplied_blend=blended).items()}
            for eye, counts in result['eyes'].items():
                assert counts['foreground']>500, (eye, 'Foreground projection was covered by the menu', counts)
                assert counts['preserved_menu']>1000, (eye, 'Transparent projection cleared the menu', counts)
                assert counts['last_quad']>20, (eye, 'Final quad was not composed after the foreground', counts)
                assert counts['premultiplied_blend']>100, (eye, 'Projection alpha/gamma blend is incorrect', counts)
            result['passed'] = True
            print(json.dumps(result))
        except Exception as error:
            result['error'] = repr(error)
            raise
        finally:
            (out/'result.json').write_text(json.dumps(result,indent=2))
            (out/'layer_probe_stop').touch()
            proc.wait(timeout=15)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--probe',type=Path,required=True)
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--backend',choices=('d3d11','d3d12'),default='d3d11')
    p.add_argument('--stereo-quads',action='store_true')
    p.add_argument('--evidence',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
