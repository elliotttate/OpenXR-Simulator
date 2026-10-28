"""Qualify the native reward reveal with real queued reward data and XR input.

The presentation-only ui.preview_reward fixture queues money, car and driver
displays without unlocking anything or granting credits. Controller rays make
every Continue; the authored readiness gate and Flash outro callback stay in
control of the queue.
"""
import argparse,asyncio,json,os,sys,uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from flatout_frontend_e2e import ROOT
from flatout_party_native_ui_e2e import Party
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import Atlas

ACTION='helper.Validate.RewardContinue'
KINDS=('money','car','driver')


class Rewards(Party):
    async def wait_phase(self,presentation,phases,timeout=20):
        loop=asyncio.get_running_loop();deadline=loop.time()+timeout
        while True:
            s=await self.query('ui.reward_state')
            if s['presentation']>=presentation and s['phase'] in phases:return s
            assert loop.time()<deadline,(presentation,phases,s)
            await asyncio.sleep(.03)

    @staticmethod
    def node(nodes,name):
        return next((n for n in nodes if n['path'].endswith('/'+name) and n.get('effective_visible',n['visible'])),None)

    @staticmethod
    def legacy_visible(nodes):
        return any('/Dummy/Results' in n['path'] and n.get('effective_visible',n['visible']) for n in nodes)

    async def layout(self,kind):
        await self.park();nodes=await self.nodes();g=self.geometry
        names=('RewardPanel','RewardArt','RewardEyebrow','RewardName','RewardContinue')
        panel,art,eyebrow,name,action=(self.node(nodes,n) for n in names)
        detail=self.node(nodes,'RewardDetail')
        assert all((panel,art,eyebrow,name,action)),dict(zip(names,(panel,art,eyebrow,name,action)))
        assert not self.legacy_visible(nodes),'Authored Flash must stay hidden while the ray reveal is shown'
        l,t,r,b=panel['rect']
        assert abs(l-g['StageX'])<1 and abs(r-g['StageX']-g['StageWidth'])<1 and abs(t-g['BodyY'])<1 and b<=1080-48,panel
        assert eyebrow['text'].strip() and not eyebrow['text'].rstrip().endswith(':'),eyebrow
        assert name['text'].strip(),name
        for n in (art,eyebrow,name,action):
            assert n['rect'][0]>=l+20 and n['rect'][2]<=r-20 and n['rect'][1]>=t+20 and n['rect'][3]<=b-20,(n['path'],n['rect'],panel['rect'])
        assert eyebrow['rect'][3]<=art['rect'][1]+1 and art['rect'][3]<=name['rect'][1]+1 and name['rect'][3]<=action['rect'][1]+1,(eyebrow,art,name,action)
        assert (detail is not None)==(kind=='car'),detail
        if detail:assert detail['text'].strip() and name['rect'][3]<=detail['rect'][1]+1 and detail['rect'][3]<=action['rect'][1]+1,detail
        box={'car':(g['ShowroomWidth'],g['ShowroomHeight']),'driver':(g['PortraitSize'],)*2,'money':(g['IconSize'],)*2}[kind]
        w,h=art['rect'][2]-art['rect'][0],art['rect'][3]-art['rect'][1]
        assert w<=box[0]+1 and h<=box[1]+1 and (abs(w-box[0])<1.5 or abs(h-box[1])<1.5),(art['rect'],box)
        assert abs((art['rect'][0]+art['rect'][2])/2-(l+r)/2)<1.5,art
        await self.audit('RewardWorkspace',1)
        copy={'eyebrow':eyebrow['text'],'name':name['text'],'detail':detail['text'] if detail else None,'art':art['rect']}
        self.cases.append({'case':'native-reward-layout-real-data-and-measured-slots','passed':True,'hand':self.args.hand,'kind':kind,**copy})
        return copy

    async def inert_intro(self,reward):
        targets={x['name']:x for x in (await self.query('vr.targets'))['targets']}
        assert set(targets)=={ACTION} and not targets[ACTION]['enabled'],targets
        before,aim=await self.point_args(ACTION);aim.update(command_id=uuid.uuid4().hex,hold_frames=30)
        await self.tool('click_ui_at',aim,'intro-'+ACTION);await self.tool('click_ui_at',aim,'intro-retry-'+ACTION)
        after=await self.query('ui.reward_state')
        assert (await self.query('vr.targets'))['clicks']==before['clicks'] and after['presentation']==reward['presentation'] and after['phase']!='outro',(reward,after)
        self.cases.append({'case':'continue-inert-during-authored-intro','passed':True,'hand':self.args.hand,'phase_after':after['phase']})

    async def takeover(self,reward):
        before,aim=await self.point_args(ACTION)
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'hold_frames':8,'dwell_frames':2,'command_id':uuid.uuid4().hex},'reward-controller-takeover')
        nodes=await self.nodes()
        assert not self.node(nodes,'RewardWorkspace') and self.legacy_visible(nodes),'Controller input must restore the original reward Flash'
        await self.ray_mode()
        after=await self.query('ui.reward_state')
        assert (await self.query('vr.targets'))['clicks']==before['clicks'] and after['presentation']==reward['presentation'] and after['phase']=='ready',(reward,after)
        assert self.node(await self.nodes(),'RewardWorkspace')
        self.cases.append({'case':'original-controller-layout-and-fresh-ray-restoration','passed':True,'hand':self.args.hand})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False);result={'passed':False,'pid':a.pid,'scope':__doc__}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=Rewards(a,session)
            t.geometry=json.loads((a.fox_root/'Flatout/Code/Sources/Menus/FORewardHtmlLayout.json').read_text(encoding='utf-8'))['geometry']
            sa=SimpleNamespace(pid=a.pid,output=a.evidence,current=False,owner_queue=None,native_timeout=8,settle=.1,dwell_frames=3,hand='right',ray_presentation=True)
            stage=Atlas(sa,session,{'operations':[]});stage.active_case={'id':'native-reward-qualification'};t.query_lock=stage.lock
            try:
                for hand in ('left','right'):
                    a.hand=hand;await stage.main()
                    initial=await t.query('ui.reward_state');assert not initial['open'] and not initial['queued'],initial
                    # Presentation-only staging goes through the Atlas owner, never the read-only probe.
                    await stage.query('ui.preview_reward',{'sequence':[{'kind':k,'index':0} for k in KINDS]})
                    presentation=initial['presentation']
                    for i,kind in enumerate(KINDS):
                        presentation+=1
                        reward=await t.wait_phase(presentation,('intro',))
                        if i==0:
                            observer=asyncio.create_task(t.entrance('RewardWorkspace'))
                            try:await t.ray_mode()
                            finally:await observer
                            reward=await t.query('ui.reward_state')
                            if reward['phase']=='intro':await t.inert_intro(reward)
                        reward=await t.wait_phase(presentation,('ready',))
                        targets={x['name']:x for x in (await t.query('vr.targets'))['targets']}
                        assert targets[ACTION]['enabled'],targets
                        await t.layout(kind)
                        if i==0:await t.depth(ACTION);await t.takeover(reward)
                        assert (await t.query('ui.reward_state'))['credits']==initial['credits'],'Presentation fixture changed credits'
                        await t.click(ACTION)
                        after=await t.query('ui.reward_state')
                        assert after['presentation']>presentation or after['phase'] in ('outro','closed'),(presentation,after)
                    loop=asyncio.get_running_loop();deadline=loop.time()+20
                    while (await t.query('ui.reward_state'))['open']:
                        assert loop.time()<deadline,'Reward outro never returned to the frontend'
                        await asyncio.sleep(.05)
                    final=await t.query('ui.reward_state')
                    assert not final['queued'] and final['presentation']==initial['presentation']+len(KINDS) and final['credits']==initial['credits'],(initial,final)
                    t.cases.append({'case':'three-reward-queue-one-continue-each-and-no-credit-change','passed':True,'hand':hand,'final':final})
                    if t.last_capture:
                        for h in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':h})
                result['errors']=await t.query('game.errors');assert result['errors']['total']==0,result['errors'];result['passed']=True
            except Exception as e:result['error']=repr(e);raise
            finally:
                result['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases'])}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
