"""Qualify the native fixed-event vehicle preview with actual screenshot XR input.

Owning menu callbacks stage an unlocked FlatOut event. All tested hover, blank
press, input takeover and Back actions then use real XR input. No save, car,
readiness or statistics memory is injected. Wreck Run is never opened.
"""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import sys
import uuid

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_frontend_e2e import Frontend, ROOT, RAY_OFFSET
from flatout_livery_e2e import screenshot_pixel, ui_surface, quaternion_matrix
from flatout_ui_motion_policy import HOVER_LIFT_METERS
sys.path.insert(0, str(ROOT/'tools'))
from flatout_ui_atlas import Atlas
from flatout_button_audit import inspect


class SingleCar(Frontend):
    async def nodes(self):
        return (await self.query('hud.rects', {'root':'menu','max_depth':48,'presentation':True}))['nodes']

    async def ray_mode(self):
        native=await self.query('vr.targets');frame=native['vr_frame']
        cap=await self.capture('quick-ray-takeover-source')
        for x in (160,1760):
            px,py=screenshot_pixel({'vr':{'frame':frame}},cap,x,120)
            receipt=await self.tool('hover_ui_at',{'capture_id':cap['capture_id'],
                'hand':self.args.hand,'x':float(px),'y':float(py),'surface':ui_surface({'vr':{'frame':frame}}),
                'origin_mode':'hand','ray_offset':RAY_OFFSET,'dwell_frames':3},'quick-ray-takeover-'+str(x))
            assert receipt['status']=='input_delivered',receipt
        assert (await self.query('vr.targets'))['clicks']==native['clicks']

    async def open_with_motion_observation(self,stage,index):
        # Observe before the owning callback. Waiting for screenshot-driven
        # wrist preparation first misses the half-second entrance entirely.
        # Share the staging pipe lock; concurrent native pipe opens can race.
        async def observe():
            samples=[];midpoint=False;deadline=asyncio.get_running_loop().time()+15
            while asyncio.get_running_loop().time()<deadline:
                nodes=(await stage.query('hud.rects',{'root':'menu','max_depth':48}))['nodes']
                root=next((n for n in nodes if n['path'].endswith('/SingleCarWorkspace') and n.get('effective_visible',n['visible'])),None)
                if root:
                    fill=next((n['rect'][2]-n['rect'][0] for n in nodes if n['path'].endswith('/SingleCarStatFill0')),None)
                    samples.append({'alpha':root['color']['a'],'fill_width':fill})
                    if root['color']['a']<.99 and not midpoint:
                        self.last_capture=await self.tool('capture_screenshot',{'eye':'both','max_width':4096},self.args.hand+'-event-'+str(index)+'-entrance-midpoint');midpoint=True
                    if len(samples)>=6 and root['color']['a']>=.999:break
                await asyncio.sleep(.015)
            alphas=[s['alpha'] for s in samples];widths=[s['fill_width'] for s in samples if s['fill_width'] is not None]
            assert alphas and alphas[-1]>=.999,samples
            assert all(b>=a-.005 for a,b in zip(alphas,alphas[1:])),samples
            if self.args.reduced_motion:assert min(alphas)>=.999,samples
            else:
                assert min(alphas)<.99 and midpoint,samples
                if index==1:assert widths and max(widths)-min(widths)>5,samples
            return {'hand':self.args.hand,'event_index':index,'reduced_motion':self.args.reduced_motion,'samples':samples}
        task=asyncio.create_task(observe())
        try:
            # Keep the real pointer active across the transition. Atlas staging
            # releases its parked wrists before menu.select, which can briefly
            # restore the original presentation and obscure entrance sampling.
            await self.click('challenge.event.'+str(index))
            return await task
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def layout(self,expected_title,expected_goals,expected_ratings):
        nodes=await self.nodes()
        def node(name):
            return next(n for n in nodes if n.get('effective_visible',n['visible']) and n['path'].endswith('/'+name))
        g=json.loads((self.args.fox_root/'Flatout/Code/Sources/Menus/FOSingleCarHtmlLayout.json').read_text())['geometry']
        header=node('SingleCarHeader')['rect']
        assert np.allclose(header,[g['StageX']+8,g['Top'],g['StageX']+g['StageWidth']-8,g['Top']+g['HeaderHeight']],atol=1.1),header
        stats=node('SingleCarStats')['rect']
        assert np.allclose(stats,[g['StatsX'],g['BodyY'],g['StatsX']+g['StatsWidth'],g['BodyY']+g['BodyHeight']],atol=1.1),stats
        name,brand,event=[node('SingleCar'+s) for s in ('Name','Brand','Event')]
        assert name['text'].strip() and brand['text'].strip() and event['text'].strip()
        assert event['text']==expected_title,(event,expected_title)
        assert brand['rect'][1]-name['rect'][3]>=5,(name,brand)
        assert event['rect'][3]+32<=g['FooterY']
        values=[]
        if expected_ratings is not None:
            for i in range(6):
                label,value,track,fill=[node('SingleCarStat'+s+str(i)) for s in ('Label','Value','Track','Fill')]
                v=int(value['text']);assert 0<=v<=100 and label['text'].strip(),(label,value)
                assert track['rect'][1]>=max(label['rect'][3],value['rect'][3])+2,(label,value,track)
                assert abs((fill['rect'][2]-fill['rect'][0])-(track['rect'][2]-track['rect'][0])*v/100)<=1.5,(track,fill,v)
                values.append({'label':label['text'],'value':v})
            assert len({v['label'] for v in values})==6 and [v['value'] for v in values]==expected_ratings,values
            variant='ratings'
        else:
            assert node('SingleCarDescription')['text'].strip()
            goals=[node('SingleCarGoal'+str(i))['text'] for i in range(3)]
            assert goals==expected_goals,(goals,expected_goals)
            assert not any(n.get('effective_visible',n['visible']) and '/SingleCarStatFill' in n['path'] for n in nodes)
            values=goals;variant='event-objectives'
        captions=[n for n in nodes if n.get('effective_visible',n['visible']) and n.get('text') and '/SingleCarWorkspace/' in n['path']]
        assert all('FO_ALDO_THE_APACHE' in n['presentation']['font_resource'].upper() or 'FO_MONTSERRAT_REGULAR' in n['presentation']['font_resource'].upper() for n in captions),captions
        actions=(await self.query('vr.targets'))['targets']
        assert {t['name'] for t in actions}=={'helper.Cancel.0','helper.Validate.1'},actions
        for t in actions:
            l,y,r,b=t['rect'];assert y>=g['BodyY']+g['BodyHeight']+28 and b<=1080-48+4,t
            assert r-l>=g['BackWidth'] and b-y>=g['FooterHeight'],t
            x=g['StartX'] if t['name']=='helper.Validate.1' else g['BackX'];w=g['StartWidth'] if t['name']=='helper.Validate.1' else g['BackWidth']
            assert np.allclose(t['rect'],[x-4,g['FooterY']-4,x+w+4,g['FooterY']+g['FooterHeight']+4],atol=1.1),t
        audit=await inspect({'nodes':nodes,'message':await self.query('ui.get_message_box')},self.query,self.args.fox_root)
        assert audit['passed'] and len(audit['checks'])==2,audit
        self.cases.append({'case':'measured-native-layout-original-fonts-actual-event-data-and-caption-padding','passed':True,'variant':variant,'data':values,'audit':audit})

    async def blank_press(self):
        before=await self.query('vr.targets');cap=await self.capture('inert-car-source')
        state={'vr':{'frame':before['vr_frame']}};x,y=screenshot_pixel(state,cap,900,540);carrier=ui_surface(state)
        assert not any(l<=900<=r and t<=540<=b for l,t,r,b in (n['rect'] for n in before['targets']))
        args={'capture_id':cap['capture_id'],'x':float(x),'y':float(y),'hand':self.args.hand,
              'surface':carrier,'origin_mode':'hand','ray_offset':RAY_OFFSET,
              'dwell_frames':8,'hold_frames':30,'command_id':uuid.uuid4().hex}
        receipt=await self.tool('click_ui_at',args,'inert-car-held-click');assert receipt['status']=='input_delivered'
        await self.tool('click_ui_at',args,'inert-car-retry')
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        assert (await self.query('menu.get_state'))['current_menu']=='CARS'
        assert not (await self.query('game.get_state'))['is_loading']
        self.cases.append({'case':'car-and-empty-preview-held-click-retry-are-inert','passed':True,'hand':self.args.hand})

    async def takeover(self):
        before,aim=await self.point_args('helper.Cancel.0')
        await self.tool('scroll_ui_at',{**aim,'hand':'left','stick':[0,1.],'dwell_frames':2,'hold_frames':8,'command_id':uuid.uuid4().hex},'navigation-stick-restores-original-layout')
        original=await self.nodes()
        assert not any(n.get('effective_visible',n['visible']) and n['path'].endswith('/SingleCarWorkspace') for n in original)
        assert (await self.query('menu.get_state'))['current_menu']=='CARS'
        await self.ray_mode();await asyncio.sleep(.8)
        assert any(n.get('effective_visible',n['visible']) and n['path'].endswith('/SingleCarWorkspace') for n in await self.nodes())
        assert (await self.query('vr.targets'))['clicks']==before['clicks']
        self.cases.append({'case':'actual-navigation-stick-original-layout-and-fresh-ray-return-without-activation','passed':True,'navigation_hand':'left','ray_hand':self.args.hand})


async def run(a):
    a.evidence.mkdir(parents=True,exist_ok=False)
    a.output=a.evidence;a.current=False;a.owner_queue=None;a.fixture=[];a.native_timeout=8;a.fixture_timeout=25;a.settle=.2;a.dwell_frames=3
    result={'passed':False,'scope':__doc__,'pid':a.pid}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],env={**os.environ,'SIMXR_DATA_DIR':str(a.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();t=SingleCar(a,session);stage=Atlas(a,session,{'operations':[]});t.query_lock=stage.lock
            try:
                assert (await t.query('game.get_state'))['diagnostic_save_slot']==641062
                assert not (await t.query('ui.get_message_box'))['open'],'Acknowledge only the known cold Welcome before this harness'
                baseline=json.loads(a.baseline_capture.read_text());base=ui_surface({'vr':{'frame':baseline['targets']['vr_frame']}})
                reference_distance=abs(float(np.dot(np.array(base['pose']['position'])-baseline['capture']['geometry']['head']['position'],quaternion_matrix(base['pose']['orientation'])[:,2])))
                specs=(a.fox_root/'Flatout/Data/Common/Tuning/Factories/CarsSpecs.cfg').read_text()
                beetle=re.search(r'\bDERBY_Beetle\s*=\s*\{(.*?)\}',specs,re.S).group(1)
                ratings=dict((k,int(v)) for k,v in re.findall(r'(\w+)\s*=\s*(\d+)',beetle))
                expected_ratings=[ratings[k] for k in ('TopSpeed','Acceleration','Handling','Boost','Life','Strenght')]
                for hand,index in (('left',0),('left',1),('right',0),('right',1)):
                    if t.last_capture:
                        for parked in ('left','right'):
                            await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':parked})
                    a.hand=hand;stage.active_case={'id':'single-car-'+hand+'-'+str(index)};await stage.normal_flow('challenge')
                    menu=await t.query('menu.get_state');assert not menu['items'][index]['locked'],'The owned fixture needs its first two normally unlocked events'
                    await stage.prepare_ray_presentation()
                    goals=sorted((n for n in await t.nodes() if n.get('effective_visible',n['visible']) and n['path'].endswith('/ChallengeMedalGoal')),key=lambda n:((n['rect'][0]+n['rect'][2])/2))
                    expected_goals=[n['text'] for n in goals[index*3:index*3+3]];assert len(expected_goals)==3
                    result.setdefault('entrance_samples',[]).append(await t.open_with_motion_observation(stage,index))
                    deadline=asyncio.get_running_loop().time()+20
                    while not next(n for n in (await t.query('vr.targets'))['targets'] if n['name']=='helper.Validate.1').get('enabled',True):
                        assert asyncio.get_running_loop().time()<deadline,'Vehicle/material readiness did not settle'
                        await asyncio.sleep(.1)
                    await asyncio.sleep(.6);await t.layout(menu['items'][index]['title'],expected_goals,expected_ratings if index==1 else None)
                    clicks=(await t.query('vr.targets'))['clicks']
                    for target in ('helper.Validate.1','helper.Cancel.0'):
                        before,_,_=await t.hover(target);cap=await t.tool('capture_screenshot',{'eye':'both','max_width':4096},hand+'-'+target+'-stereo-hover');t.last_capture=cap
                        # The showroom base is rendered in the projection, while
                        # raised faces are submitted compositor quads. Measure
                        # those actual faces against the native affine carrier.
                        carrier=ui_surface({'vr':{'frame':before['vr_frame']}});normal=quaternion_matrix(carrier['pose']['orientation'])
                        assert abs(normal[1,0])<1e-4 and abs(normal[1,2])<1e-4,normal.tolist()
                        distance=abs(float(np.dot(np.array(carrier['pose']['position'])-cap['geometry']['head']['position'],normal[:,2])))
                        assert abs(distance-reference_distance)<.01 and abs(carrier['size'][0]-base['size'][0])<.01,(distance,reference_distance,carrier)
                        rect=next(n['rect'] for n in before['targets'] if n['name']==target);l,y,r,b=rect
                        point=np.array(carrier['pose']['position'])+normal[:,0]*((l+r)/3840-.5)*carrier['size'][0]+normal[:,1]*(.5-(y+b)/2160)*carrier['size'][1]
                        tiles=[s for s in cap['geometry']['surfaces'] if s['visibility']==0];matching=[];lifts=[]
                        for tile in tiles:
                            lift=float(np.dot(np.array(tile['pose']['position'])-carrier['pose']['position'],normal[:,2]));lifts.append(lift)
                            local=quaternion_matrix(tile['pose']['orientation']).T@(point-tile['pose']['position'])
                            if abs(lift-HOVER_LIFT_METERS)<.001 and all(abs(local[i])<=tile['size'][i]/2+.002 for i in (0,1)):matching.append(tile)
                        assert matching and len(tiles)<=2,(target,lifts,tiles)
                        depth={'lift_meters':lifts,'native_rtt_carrier':carrier,'submitted_tiles':tiles}
                        placement={'distance_meters':distance,'width_meters':carrier['size'][0]}
                        assert (await t.query('vr.targets'))['clicks']==clicks
                        t.cases.append({'case':'both-eye-physical-hover-and-unchanged-level-reading-carrier','passed':True,'hand':hand,'target':target,'depth':depth,'placement':placement})
                    await t.blank_press();await t.takeover();await t.click('helper.Cancel.0')
                    back=await t.query('menu.get_state');assert back['current_menu']=='INSANITY' and back['selection_index']==index,back
                result['errors']=await t.query('game.errors');assert result['errors']['total']==0,result['errors']
                result['passed']=True
            except Exception as e:result['error']=repr(e);raise
            finally:
                result['cases']=t.cases;(a.evidence/'result.json').write_text(json.dumps(result,indent=2))
                if t.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':t.last_capture['capture_id'],'hand':hand})
    print(json.dumps({'passed':result['passed'],'cases':len(result['cases']),'entrance_samples':result.get('entrance_samples')}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--fox-root',type=Path,required=True);p.add_argument('--baseline-capture',type=Path,required=True,help='Saved Atlas metadata for the original preview carrier');p.add_argument('--evidence',type=Path,required=True);p.add_argument('--reduced-motion',action='store_true',help='Require an already-configured reduced-motion runtime to settle immediately');p.add_argument('--hand',default='right');asyncio.run(run(p.parse_args()))
