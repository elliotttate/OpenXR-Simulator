"""Native regressions from the startup and FlatOut-mode tester videos."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame
from flatout_frontend_e2e import Frontend,RAY_OFFSET,quad_pixel
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client


def default_layout_manifest():
    explicit=os.environ.get('FOX_UI_LAYOUT_MANIFEST')
    if explicit:return Path(explicit)
    return Path(os.environ.get('FOX_SOURCE_ROOT',ROOT.parent/'Fox'))/'Flatout/Code/Sources/Menus/FOChallengeHtmlLayout.json'


def load_layout_manifest(path=None):
    path=Path(path) if path else default_layout_manifest()
    assert path.is_file(),('Pass --layout-manifest for the active Fox checkout',str(path))
    data=json.loads(path.read_text(encoding='utf-8'))
    assert data['schema']=='fox.challenge-html-layout.v1' and data['canvas']=={'width':1920,'height':1080},data.get('schema')
    assert len(data['cards'])==len(data['actions'])==3,'Expected three generated card and action slots'
    return data


def load_styles_manifest(path=None,layout_path=None):
    if path is None:path=os.environ.get('FOX_UI_STYLES_MANIFEST')
    if path is None:
        source=Path(layout_path) if layout_path else default_layout_manifest()
        path=source.parents[3]/'Data/Assets/GUI_FlatOut/Textures/Menus/ChallengeEnhanced/Styles.manifest.json'
    path=Path(path)
    assert path.is_file(),('Pass --styles-manifest for the active Fox style atlas',str(path))
    styles=json.loads(path.read_text(encoding='utf-8'))
    assert styles['schema']=='fox.challenge-css-materials.v1',styles.get('schema')
    return styles


def style_union(styles,names,reference):
    entries={s['name']:s for s in styles['materials']}
    boxes=[]
    for name in names:
        sprite=entries[name]
        boxes.append([reference['x']+sprite['draw_offset'][0],reference['y']+sprite['draw_offset'][1],
                      reference['x']+sprite['draw_offset'][0]+sprite['draw_size'][0],
                      reference['y']+sprite['draw_offset'][1]+sprite['draw_size'][1]])
    l=min(b[0] for b in boxes);t=min(b[1] for b in boxes)
    return {'x':l,'y':t,'width':max(b[2] for b in boxes)-l,'height':max(b[3] for b in boxes)-t}


async def settle_challenge_baseline(test):
    """Park both real rays outside controls, then let authored visual tracks exit."""
    source=await test.capture('challenge-park-source')
    planes=[s for s in source['geometry']['surfaces'] if s['visibility']==0]
    assert planes,'Challenge requires a submitted BOTH-eye carrier'
    carrier=max(planes,key=lambda s:s['size'][0]*s['size'][1])
    canvas_source={**source,'geometry':{**source['geometry'],'surfaces':[carrier]}}
    x,y=quad_pixel(canvas_source,(70,1000,70,1000))
    for hand in ('left','right'):
        result=await test.tool('hover_ui_at',{'capture_id':source['capture_id'],'x':float(x),'y':float(y),
            'surface_id':carrier['id'],'hand':hand,'origin_mode':'hand','ray_offset':RAY_OFFSET,
            'dwell_frames':1,'timeout':30},'challenge-park-'+hand)
        assert result['status']=='input_delivered',result
        if result.get('capture'):test.last_capture=result['capture']
    # Full initial footer sequence is1.35s; longest hover exit is1.05s.
    # Input remains live throughout. Static ray hit rectangles are checked
    # separately; animated medals/photos/footer surfaces are not hit targets.
    await asyncio.sleep(1.5)
    native=await test.query('vr.targets')
    assert not native['hovered'],('A parked ray still hovered a control',native['hovered'])
    test.cases.append({'case':'parked-rays-authored-baseline-settled','passed':True,'settle_seconds':1.5})


def rect_edges(rect):
    return [rect['x'],rect['y'],rect['x']+rect['width'],rect['y']+rect['height']]


def assert_rect(actual,expected,label,tolerance=1.5):
    expected=rect_edges(expected) if isinstance(expected,dict) else expected
    assert len(actual)==len(expected)==4 and max(abs(a-b) for a,b in zip(actual,expected))<=tolerance,(label,actual,expected)


def challenge_nodes(nodes,name):
    return sorted((n for n in nodes if n.get('visible') and '/ChallengeReader/' in n['path'] and n['path'].endswith('/'+name)),key=lambda n:(n['rect'][0],n['rect'][1]))


def badge_stage(measured):
    scale=min(measured['width'],measured['height'])/302
    return {'x':measured['x']+measured['width']/2-196*scale,
            'y':measured['y']+measured['height']/2-163*scale,'width':392*scale,'height':334*scale}


def check_thin_decoration(nodes,name,expected):
    """GetNodeRect omits bounds smaller than VR_MIN_TARGET_SIZE (4px)."""
    assert all(min(box['width'],box['height'])<4 for box in expected),('Use strict measurable geometry checks',name)
    found=challenge_nodes(nodes,name)
    assert len(found)<=len(expected),(name,'Unexpected duplicate decoration',len(found))
    remaining=list(expected)
    for node in found:
        box=min(remaining,key=lambda b:max(abs(a-e) for a,e in zip(node['rect'],rect_edges(b))))
        assert_rect(node['rect'],box,('Reported thin decoration',name))
        remaining.remove(box)
    return {'status':'below-native-geometry-cutoff','native_minimum_size_px':4,
            'reason':'VRHud::GetNodeRect rejects width or height below VR_MIN_TARGET_SIZE; hud.rects cannot certify omitted decorations',
            'expected':expected,'reported_rects':[n['rect'] for n in found],
            'unmeasurable_expected_count':len(remaining)}


def check_challenge_layout(nodes,targets,manifest,count=3,styles=None):
    """Compare native layout boxes to generated CSS; input bounds stay separate."""
    report={}
    def group(name,expected,unordered=False):
        found=challenge_nodes(nodes,name)
        assert len(found)==len(expected),(name,len(found),len(expected))
        remaining=list(found)
        for slot,box in enumerate(expected):
            # Equal-x rotated rim quadrants may round one pixel differently;
            # pair each expected box to a unique native box before comparison.
            node=min(remaining,key=lambda n:max(abs(a-b) for a,b in zip(n['rect'],rect_edges(box)))) if unordered else found[slot]
            assert_rect(node['rect'],box,(name,slot))
            if unordered:remaining.remove(node)
        report[name]=[n['rect'] for n in found]
    for name,key in [('ChallengeHeader','header'),('ChallengeHeaderAccent','header_accent')]:
        group(name,[manifest['visual_bounds'][key]])
    for name,key in [('ChallengeHeading','heading'),('ChallengeSubtitle','subtitle'),('ChallengePageCount','page_count')]:
        group(name,[manifest['rects'][key]])
    cards=manifest['cards'][:count]
    for name,key in [('ChallengeCard','card'),('ChallengeTrackArt','photo'),('ChallengeEventTitle','title'),
                     ('ChallengeDescription','description'),('ChallengeEventStatus','best_score_line')]:
        group(name,[c[key] for c in cards])
    group('ChallengeCardBody',[{'x':c['card']['x']+1,'y':c['card']['y']+1,'width':c['card']['width']-2,'height':c['card']['height']-2} for c in cards])
    group('ChallengeCardGlow',[style_union(styles,('CardNormalFx','CardHoverFx'),c['card']) if styles else
        {'x':c['card']['x']-8,'y':c['card']['y']-8,'width':c['card']['width']+16,'height':c['card']['height']+16} for c in cards])
    group('ChallengeModeBadge',[badge_stage(c['badge_extension']) for c in cards])
    group('ChallengeModeRim',[badge_stage(c['badge_extension']) for c in cards])
    discs=[]
    quarters=[]
    for card in cards:
        measured=card['badge_extension'];stage=badge_stage(measured);scale=min(measured['width'],measured['height'])/302
        discs.append({'x':stage['x']+45*scale,'y':stage['y']+12*scale,'width':302*scale,'height':302*scale})
        quarters.extend({'x':stage['x']+x*scale,'y':stage['y']+y*scale,'width':151*scale,'height':151*scale} for x,y in ((45,12),(45,163),(196,12),(196,163)))
    group('ChallengeBadgeDisc',discs)
    for name in ('ChallengeRimGold','ChallengeRimLines'):group(name,quarters,unordered=True)
    for index,name in enumerate(('ChallengeMedalGold','ChallengeMedalSilver','ChallengeMedalBronze')):
        group(name,[c['medal_items'][index]['icon'] for c in cards])
    group('ChallengeMedalGoal',[m['score'] for c in cards for m in c['medal_items']])
    report['ChallengeMedalDivider']=check_thin_decoration(nodes,'ChallengeMedalDivider',[
        {'x':c['photo']['x'],'y':c['medal_items'][0]['icon']['y']-16,'width':c['photo']['width'],'height':1} for c in cards])
    group('ChallengeActionFrame',manifest['visual_bounds']['actions'])
    group('ChallengeActionBody',[style_union(styles,(f'Footer{i}Normal',f'Footer{i}Hover'),manifest['actions'][i]) if styles else
        {'x':a['x']+1,'y':a['y']+1,'width':a['width']-2,'height':a['height']-2} for i,a in enumerate(manifest['visual_bounds']['actions'])])
    group('ChallengeActionIcon',[a['icon'] for a in manifest['action_contents']])
    group('RayActionLabel',[a['label'] for a in manifest['action_contents']])
    group('ChallengeActionNext',[manifest['action_contents'][2]['end_arrow']])
    group('ChallengePagerFrame',[manifest['visual_bounds'][k] for k in ('previous','next')])
    group('ChallengePagerBody',[{'x':a['x']+1,'y':a['y']+1,'width':a['width']-2,'height':a['height']-2} for a in (manifest['visual_bounds'][k] for k in ('previous','next'))])
    if styles:
        group('ChallengeHeaderSurface',[style_union(styles,('HeaderSurface',),manifest['rects']['header'])])
        group('ChallengePagerSurface',[style_union(styles,('PagerNormal' if k=='previous' else 'PagerNext','PagerHover'),manifest['rects'][k]) for k in ('previous','next')])
        group('ChallengeHoverLight',[style_union(styles,('CardHoverLight',),c['card']) for c in cards])
        group('ChallengeCardLight',[style_union(styles,('CardLight',),{'x':c['card']['x']+12,'y':c['card']['y']+c['card']['height']-1,
            'width':c['card']['width']-24,'height':1}) for c in cards])
        for i,name in enumerate(('Gold','Silver','Bronze')):
            group('Challenge'+name+'Glow',[style_union(styles,('Medal'+name+'Glow',),c['medal_items'][i]['icon']) for c in cards])
        report['style_schema']=styles['schema']
    for chip in challenge_nodes(nodes,'ChallengeRecommended'):
        slot=min(range(count),key=lambda i:abs(chip['rect'][0]-cards[i]['recommendation']['x']))
        assert_rect(chip['rect'],cards[slot]['recommendation'],('recommended chip',slot))
    for star in challenge_nodes(nodes,'ChallengeRecommendedStar'):
        slot=min(range(count),key=lambda i:abs(star['rect'][0]-cards[i]['recommendation_icon']['x']))
        assert_rect(star['rect'],cards[slot]['recommendation_icon'],('recommended star',slot))
    event_targets=sorted((t for t in targets if t['name'].startswith('challenge.event.')),key=lambda t:t['rect'][0])
    assert len(event_targets)==count,event_targets
    for slot,target in enumerate(event_targets):assert_rect(target['rect'],cards[slot]['card'],('card ray target',slot))
    action_names=('helper.Cancel.0','helper.Secondary.1','helper.Validate.2')
    for target in targets:
        name=target['name']
        if name.startswith('challenge.page.'):
            key='previous' if name.endswith('.prev') else 'next'
            visual=manifest['visual_bounds'][key]
        elif name in action_names:visual=manifest['visual_bounds']['actions'][action_names.index(name)]
        else:continue
        width=max(visual['width'],80);height=max(visual['height'],80)
        cx=visual['x']+visual['width']/2;cy=visual['y']+visual['height']/2
        assert_rect(target['rect'],[cx-width/2,cy-height/2,cx+width/2,cy+height/2],('ray hit padding',name),.1)
        l,t,r,b=target['rect'];assert r-l>=80-.01 and b-t>=80-.01,(name,'Ray buttons need80px minimum',target)
    # Independent spacing checks catch malformed/overlapping generated data.
    boxes=[rect_edges(c['card']) for c in cards]
    assert all(boxes[i+1][0]-boxes[i][2]>=20 for i in range(len(boxes)-1)),boxes
    assert manifest['rects']['actions']['y']-max(b[3] for b in boxes)>=24,'Separate cards and footer'
    assert all(0<=x<1920 for box in boxes for x in (box[0],box[2])) and all(0<=b[1]<b[3]<=1080 for b in boxes)
    return report


def check_card_padding(nodes):
    bodies=sorted((n for n in nodes if n.get('visible') and n['path'].endswith('/ChallengeCardBody')),key=lambda n:n['rect'][0])
    statuses=sorted((n for n in nodes if n.get('visible') and n['path'].endswith('/ChallengeEventStatus')),key=lambda n:n['rect'][0])
    assert len(bodies)==len(statuses)==3
    for body,status in zip(bodies,statuses):
        assert body['rect'][3]-status['rect'][3]>=20,('Event status needs at least 20 px bottom padding',body,status)


def check_card_artwork(nodes, count=3, manifest=None):
    manifest=manifest or load_layout_manifest()
    visible=[n for n in nodes if n.get('visible')]
    names=('ChallengeModeBadge','ChallengeMedalGold','ChallengeMedalSilver','ChallengeMedalBronze')
    groups={name:sorted((n for n in visible if n['path'].endswith('/'+name)),key=lambda n:n['rect'][0]) for name in names}
    assert all(len(group)==count for group in groups.values()),('Every card needs original mode and cup artwork',{k:len(v) for k,v in groups.items()})
    for part in ('Menu_Icons_Main_Icon','Menu_Icons_Main_Text'):
        panels=[n for n in visible if n['kind']=='panel' and '/ChallengeModeBadge/' in n['path'] and '/'+part+'/' in n['path']]
        assert len(panels)==count,('Paging must replace emblem skins without accumulating artwork',part,len(panels),count)
    bodies=sorted((n for n in visible if n['path'].endswith('/ChallengeCardBody')),key=lambda n:n['rect'][0])
    images=sorted((n for n in visible if n['path'].endswith('/ChallengeTrackArt')),key=lambda n:n['rect'][0])
    titles=sorted((n for n in visible if n['path'].endswith('/ChallengeEventTitle')),key=lambda n:n['rect'][0])
    for index,body in enumerate(bodies):
        l,t,r,b=body['rect']
        for name,group in groups.items():
            al,at,ar,ab=group[index]['rect']
            assert l+18<=al<ar<=r-18 and t+18<=at<ab<=b-20,('Artwork exceeds card padding',name,body,group[index])
            if name=='ChallengeModeBadge':
                assert_rect(group[index]['rect'],badge_stage(manifest['cards'][index]['badge_extension']),('Original circular badge stage',index))
                # Flash's transparent wrapper extends beyond the visible disc;
                # the actual circle, including its reconstructed rim, fits the photo.
                discs=challenge_nodes(nodes,'ChallengeBadgeDisc')
                assert len(discs)==count,('One original badge disc per card',len(discs))
                dl,dt,dr,db=discs[index]['rect'];il,it,ir,ib=images[index]['rect']
                assert il-1.5<=dl<dr<=ir+1.5 and it-1.5<=dt<db<=ib+1.5,('Badge disc must fit its photo',discs[index],images[index])
                assert ab<titles[index]['rect'][1],('Badge wrapper must not cover event title',group[index],titles[index])
        descriptions=sorted((n for n in visible if n['path'].endswith('/ChallengeDescription')),key=lambda n:n['rect'][0])
        assert len(descriptions)==count and descriptions[index].get('text','').strip(),('Every event needs a readable description',index)
        medals=[groups[name][index] for name in names[1:]]
        assert max(n['rect'][1] for n in medals)-min(n['rect'][1] for n in medals)<1,'Medals need one aligned row'
        for medal,node in enumerate(medals):assert_rect(node['rect'],manifest['cards'][index]['medal_items'][medal]['icon'],('Original trophy art',index,medal))
    for name in ('ChallengeRimGold','ChallengeRimLines'):
        assert len(challenge_nodes(nodes,name))==4*count,('Continuous original four-quarter badge rim',name)
    return {name:[node['rect'] for node in group] for name,group in groups.items()}


async def run(args):
    args.evidence.mkdir(parents=True,exist_ok=False);args.hand='right'
    result={'passed':False,'cases':[],'operations':[]}
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp-server/openxr_simulator_mcp.py')],
        env={**os.environ,'SIMXR_DATA_DIR':str(args.data_dir.resolve())})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize();test=Frontend(args,session);native=NativeGame(args.pid,result['operations'],8)
            try:
                capture=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'before')
                test.last_capture=capture
                menu=await test.query('menu.get_state');message=await test.query('ui.get_message_box')
                if not args.welcome_only:
                    assert menu['current_menu']=='INSANITY' and not message['open'],menu
                    await settle_challenge_baseline(test)
                    capture=await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'before-settled')
                    test.last_capture=capture
                nodes=(await test.query('hud.rects',{'root':'msgbox' if args.welcome_only else 'menu','max_depth':48}))['nodes']
                (args.evidence/'before.json').write_text(json.dumps({'capture':capture,'menu':menu,'message':message,'nodes':nodes},indent=2))
                if args.welcome_only:
                    assert message['open'] and any(n.get('text')=='Welcome to FlatOut VR' for n in nodes)
                    visible=[i['title'] for i in menu.get('items',[]) if menu.get('is_open') and i.get('rendered_bounds',{}).get('visible')]
                    assert not visible,('Main navigation must wait until welcome acknowledgement',visible)
                    result['cases'].append({'case':'welcome-has-no-main-navigation-behind-it','passed':True})
                    targets=(await test.query('vr.targets'))['targets']
                    assert [t['name'] for t in targets]==['msgbox.validate'],targets
                    await test.click('msgbox.validate')
                    await asyncio.sleep(.4)
                    after=await test.query('menu.get_state')
                    assert after['current_menu']=='MAIN MENU' and after['is_open'] and not (await test.query('ui.get_message_box'))['open'],after
                    assert all(i['rendered_bounds']['visible'] for i in after['items'])
                    await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'main-revealed')
                    result['cases'].append({'case':'one-ray-acknowledgement-reveals-main','passed':True})
                else:
                    manifest=load_layout_manifest(args.layout_manifest)
                    styles=load_styles_manifest(args.styles_manifest,args.layout_manifest)
                    assert menu['current_menu']=='INSANITY' and not message['open'],menu
                    images=[n for n in nodes if n.get('visible') and n['path'].endswith('/ChallengeTrackArt')]
                    targets=(await test.query('vr.targets'))['targets']
                    events=[t for t in targets if t['name'].startswith('challenge.event.')]
                    assert len(images)==len(events)==3,('Use three image-led cards instead of six dense thumbnails',len(images),len(events))
                    geometry=check_challenge_layout(nodes,targets,manifest,styles=styles)
                    assert all(abs((n['rect'][2]-n['rect'][0])/(n['rect'][3]-n['rect'][1])-1.72)<.015 for n in images),images
                    check_card_padding(nodes)
                    artwork=check_card_artwork(nodes,manifest=manifest)
                    result['cases'].append({'case':'native-layout-matches-generated-html-coordinates','passed':True,'geometry':geometry,'manifest_schema':manifest['schema']})
                    result['cases'].append({'case':'original-mode-badges-and-medals','passed':True,'artwork':artwork})
                    result['cases'].append({'case':'large-track-art-three-card-layout','passed':True,'images':images})
                    for target in events:await test.hover(target['name'])
                    before=(await test.query('menu.get_state'))['selection_index']
                    await test.click('challenge.page.next')
                    after=(await test.query('menu.get_state'))['selection_index']
                    assert after//3==before//3+1,(before,after)
                    await test.click('challenge.page.prev')
                    assert (await test.query('menu.get_state'))['selection_index']//3==before//3
                    await settle_challenge_baseline(test)
                    after_nodes=(await test.query('hud.rects',{'root':'menu','max_depth':48}))['nodes']
                    check_card_artwork(after_nodes,manifest=manifest)
                    check_challenge_layout(after_nodes,(await test.query('vr.targets'))['targets'],manifest,styles=styles)
                    (args.evidence/'after-page-round-trip.json').write_text(json.dumps(after_nodes,indent=2))
                    await test.tool('capture_screenshot',{'eye':'both','max_width':2048},'image-led-cards')
                    result['cases'].append({'case':'ray-hover-and-page-round-trip','passed':True})
                result['native_errors']=await test.query('game.errors');assert result['native_errors']['total']==0
                result['passed']=True
            except Exception as error:result['error']=repr(error);raise
            finally:
                result['native_input_cases']=test.cases
                (args.evidence/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                if test.last_capture:
                    for hand in ('left','right'):await session.call_tool('release_ui_pointer',{'capture_id':test.last_capture['capture_id'],'hand':hand})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--welcome-only',action='store_true')
    p.add_argument('--layout-manifest',type=Path,default=default_layout_manifest())
    p.add_argument('--styles-manifest',type=Path)
    asyncio.run(run(p.parse_args()))
