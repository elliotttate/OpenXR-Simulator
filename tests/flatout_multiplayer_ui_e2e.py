"""Audit multiplayer reading surfaces and the isolated room-code keyboard.

Start in Browse Games, the player's party lobby, or a private bot lobby.
The keypad check types and deletes a local code, then closes it without joining.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import Frontend, ROOT, quad_pixel


class MultiplayerUI(Frontend):
    async def labels(self):
        return [n for n in (await self.query('hud.rects', {'root':'menu','max_depth':22}))['nodes']
                if n['visible'] and n.get('text')]

    async def code(self):
        lines=[n['text'] for n in await self.labels()
               if re.fullmatch(r'[0-9-](  [0-9-]){4}',n['text'])]
        assert len(lines)==1, ('Expected exactly one room-code field',lines)
        return ''.join(c for c in lines[0] if c.isdigit())

    async def dock(self):
        targets=(await self.query('vr.targets'))['targets']
        actions=sorted([t for t in targets if t['name'].startswith('button.')],key=lambda t:t['rect'][0])
        assert actions, 'No direct menu actions'
        for action in actions:
            l,t,r,b=action['rect']
            assert r-l>=200 and b-t>=80, ('Menu action is too small',action)
            assert 80<=l<r<=1840 and b<=952, ('Action dock lacks canvas margins',action)
        for a,b in zip(actions,actions[1:]):
            assert b['rect'][0]-a['rect'][2]>=19.5, ('Adjacent actions are crowded',a,b)
        footer=[t for t in targets if t['name'].startswith('helper.')]
        assert all(t['rect'][1]-max(a['rect'][3] for a in actions)>=24 for t in footer),footer
        await self.audit('multiplayer-action-spacing')
        for action in actions:
            await self.hover(action['name'])
        self.cases.append({'case':'cohesive-multiplayer-action-dock','passed':True,'actions':actions})

    async def inert(self,name,label):
        before,args=await self.point_args(name)
        code=await self.code()
        command={**args,'hold_frames':45,'timeout':30,'command_id':uuid.uuid4().hex}
        receipt=await self.tool('click_ui_at',command,label)
        repeat=await self.tool('click_ui_at',command,label+'-retry')
        assert receipt['delivery']['sync_id']==repeat['delivery']['sync_id']
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert await self.code()==code
        self.cases.append({'case':label,'passed':True})

    async def keypad(self):
        await self.click('button.3')
        native=await self.query('vr.targets')
        assert native['targets'] and all(t['name'].startswith('code.key.') for t in native['targets']), native
        close=next(t for t in native['targets'] if t['name']=='code.key.12')
        l,t,r,b=close['rect']
        assert r-l>=80 and b-t>=80, ('Close must be a full ray button',close)
        title=next(n for n in await self.labels() if n['text']=='ENTER ROOM CODE')
        assert l-title['rect'][2]>=16, ('Close crowds the title',title,close)
        keys=[t for t in native['targets'] if t['name']!='code.key.12']
        assert max(t['rect'][3] for t in keys)<=928, 'Key grid crowds the panel bottom'
        await self.audit('room-code-keypad-spacing')
        await self.tool('capture_screenshot',{'eye':'both','max_width':2048},'room-code-both-eyes')
        assert await self.code()==''
        await self.inert('code.key.11','incomplete-code-cannot-join')
        await self.inert('code.key.10','empty-code-delete-is-inert')
        typed=''
        for digit in (1,2,3,4,5):
            await self.click('code.key.'+str(digit))
            typed+=str(digit)
            assert await self.code()==typed, 'A held/retried key typed more than once'
        await self.inert('code.key.6','full-code-rejects-extra-digits')
        await self.click('code.key.10')
        assert await self.code()=='1234', 'Delete must remove exactly one digit'
        # The old browser/footer button positions must not receive input while
        # the keypad owns the task, including regions outside its visible panel.
        native=await self.query('vr.targets')
        source=await self.capture('keypad-outside-source')
        x,y=quad_pixel(source,(1640,904,1640,904))
        receipt=await self.tool('click_ui_at',dict(capture_id=source['capture_id'],x=float(x),y=float(y),
            origin_mode='hand',hand=self.args.hand,dwell_frames=15,hold_frames=45,
            timeout=30,command_id=uuid.uuid4().hex),'outside-keypad-held')
        assert receipt['status']=='input_delivered'
        assert (await self.query('vr.targets'))['clicks']==native['clicks']
        assert await self.code()=='1234'
        await self.click('code.key.12')
        restored=await self.query('vr.targets')
        assert not any(t['name'].startswith('code.key.') for t in restored['targets'])
        assert any(t['name']=='button.3' for t in restored['targets'])
        self.cases.append({'case':'keypad-local-editing-and-background-isolation','passed':True})
        await self.dock()

    async def run_multiplayer(self):
        menu=await self.query('menu.get_state')
        assert menu['is_open'] and not menu['is_3d'],menu
        await self.reading_comfort(self.args.surface)
        await self.dock()
        if self.args.surface=='browser':
            assert menu['current_menu']=='ONLINE',menu
            await self.keypad()


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'pid':args.pid,'surface':args.surface,'hand':args.hand}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=MultiplayerUI(args,session)
            try:
                await test.run_multiplayer();result['passed']=True
            except Exception as error:
                result['error']=repr(error);raise
            finally:
                result['cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if test.last_capture:
                    for hand in ('left','right'):
                        await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--surface',choices=('browser','party-lobby','race-lobby'),default='browser')
    p.add_argument('--hand',choices=('left','right'),default='right')
    asyncio.run(run(p.parse_args()))
