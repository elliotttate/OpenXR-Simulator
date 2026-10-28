"""Verify actual Steam avatar rendering/cache in an owned native party.

Requires Steam signed in with a profile image. An unavailable/default image
does not qualify as a successfully fetched Steam avatar.
"""
import argparse,asyncio,json,os,sys
from pathlib import Path
import numpy as np
from PIL import Image
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend,ROOT,quad_pixel
from flatout_ui_depth_contract import assert_reading_distance,assert_hover_lift


async def prove(t):
    assert (await t.query('game.get_state'))['diagnostic_save_slot']==641062
    deadline=asyncio.get_running_loop().time()+25
    while True:
        net=await t.query('net.get_state')
        assert net['alive'] and net['private'] and net['is_host'] and net['connected_players'] in (1,2),net
        local=next(s for s in net['slots'] if s['local'])
        if local.get('steam_avatar',{}).get('status')=='ready':break
        assert asyncio.get_running_loop().time()<deadline,local
        await asyncio.sleep(.2)
    original=local['steam_avatar'];assert original['width']==original['height']==64,original
    assert original['uploads']==original['decodes']==1,original
    nodes=(await t.query('hud.rects',{'root':'menu','max_depth':18}))['nodes']
    prefix='/PartyPlayer'+str(local['index'])
    photo=next(n for n in nodes if n['effective_visible'] and n['path'].endswith(prefix+'/SteamAvatar'))
    name=next(n for n in nodes if n['effective_visible'] and n['path'].endswith(prefix+'/PartyPlayerName'))
    assert name['rect'][0]-photo['rect'][2]>=15,(photo,name)
    assert abs((photo['rect'][2]-photo['rect'][0])-(photo['rect'][3]-photo['rect'][1]))<.1
    assert not any(n['effective_visible'] and n['path'].endswith(prefix+'/PartyAvatarFallback') for n in nodes)
    for i in set(range(8))-{s['index'] for s in net['slots']}:
        assert not any(n['effective_visible'] and n['path'].endswith('/PartyPlayer'+str(i)+'/SteamAvatar') for n in nodes),'Empty slot retained a player image'
    if net['lan']:
        for slot in (s for s in net['slots'] if not s['local']):
            remote='/PartyPlayer'+str(slot['index'])
            assert slot['steam_avatar']['status']=='unavailable',slot
            assert not any(n['effective_visible'] and n['path'].endswith(remote+'/SteamAvatar') for n in nodes),'LAN guest inherited another Steam image'
            assert any(n['effective_visible'] and n['path'].endswith(remote+'/PartyAvatarFallback') for n in nodes),'Missing passive LAN fallback'
    source=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},'actual-steam-avatar-both-eyes');t.last_capture=source
    assert_reading_distance(source,3.,4.5)
    pixels=np.asarray(Image.open(t.last_image_path).convert('RGB'));patches=[]
    l,top,r,b=photo['rect']
    for eye in ('left','right'):
        corners=[quad_pixel(source,(x,y,x,y),eye) for x,y in ((l,top),(r,b))]
        x0,y0=np.ceil(np.min(corners,axis=0)).astype(int)+2;x1,y1=np.floor(np.max(corners,axis=0)).astype(int)-2
        assert 0<=x0<x1<pixels.shape[1] and 0<=y0<y1<pixels.shape[0],corners
        patch=pixels[y0:y1,x0:x1];patches.append({'eye':eye,'bounds':[int(x0),int(y0),int(x1),int(y1)],'mean':patch.mean(axis=(0,1)).tolist(),'deviation':float(patch.std())})
    assert np.max(np.abs(np.array(patches[0]['mean'])-patches[1]['mean']))<20,patches
    # Hover stress must reuse the image. These are real screenshot-derived rays,
    # never a diagnostic-driven texture upload or synthetic avatar fixture.
    for target in ('player.'+str(local['index']),'party.action.0','helper.Cancel.PartyLeave'):
        await t.hover(target)
        capture=await t.tool('capture_screenshot',{'eye':'both','max_width':2048},'avatar-cache-hover-'+target);t.last_capture=capture;assert_hover_lift(capture)
    after=next(s for s in (await t.query('net.get_state'))['slots'] if s['local'])['steam_avatar'];assert after==original,(original,after)
    t.cases.append({'case':'actual-steam-image-stereo-stable-gutter-no-empty-slot-leak-and-cache-reuse','passed':True,'avatar':after,'eyes':patches})


async def race_lobby(t,enter=True):
    net=await t.query('net.get_state')
    assert (await t.query('game.get_state'))['diagnostic_save_slot']==641062
    assert net['is_host'] and net['private'] and net['connected_players']==1
    if enter:
        await t.click('party.action.4')
        await t.click('helper.Validate.LobbySetupApply')
    deadline=asyncio.get_running_loop().time()+15
    while True:
        state=await t.query('menu.get_state');net=await t.query('net.get_state')
        if any(i['title'].lower()=='garage' for i in state['items']):break
        assert asyncio.get_running_loop().time()<deadline,state;await asyncio.sleep(.1)
    assert net['is_host'] and net['private'] and net['connected_players']==1
    nodes=(await t.query('hud.rects',{'root':'menu','max_depth':22,'all':True}))['nodes']
    photos=[n for n in nodes if n['effective_visible'] and n['path'].endswith('/SteamAvatar')]
    assert len(photos)==1,photos
    image=next(s for s in net['slots'] if s['local'])['steam_avatar']
    assert image['status']=='ready' and image['uploads']==image['decodes']==1,image
    # Human avatar replaces the driver portrait; bots retain their own artwork.
    owner=photos[0]['path'].rsplit('/',1)[0]
    assert not any(n['effective_visible'] and n['path']==owner+'/Portrait' for n in nodes)
    portraits=[n for n in nodes if n['effective_visible'] and n['path'].endswith(('/Portrait','/RaceLobbyDriverPortrait'))]
    assert len(portraits)==len(net['bots']),(portraits,net['bots'])
    capture=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},'actual-avatar-private-race-lobby');t.last_capture=capture
    assert_reading_distance(capture,3.,4.5)
    t.cases.append({'case':'real-private-race-lobby-shares-avatar-cache-and-keeps-bot-driver-portraits','passed':True,'photos':photos,'avatar':image,'bots':len(net['bots'])})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);report={'passed':False,'scope':'Actual signed-in local Steam avatar; LAN remote identity/fallback and remote Steam account remain separate cases'}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Frontend(a,session)
            try:
                if a.race_lobby or a.current_race_lobby:await race_lobby(t,not a.current_race_lobby)
                else:await prove(t)
                report['errors']=await t.query('game.errors');assert report['errors']['total']==0;report['passed']=True
            except Exception as error:report['error']=repr(error);raise
            finally:
                report['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(report,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',choices=('left','right'),default='right');p.add_argument('--race-lobby',action='store_true');p.add_argument('--current-race-lobby',action='store_true');asyncio.run(run(p.parse_args()))
