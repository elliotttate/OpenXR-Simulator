"""Read-only optical button audit for native Atlas captures.

Uses the shipped BMFont glyph bearings plus live label geometry. It checks
caption+icon groups, safe insets and preserved fonts; screenshot review remains
necessary for paint edges, anti-aliasing and ImGui controls.
"""
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET
import json
import html

CAPTIONS=set('CarsClassLabel CarsFooterLabel ClassActionLabel CupActionLabel CareerFooterLabel CupStandingsActionLabel EventActionLabel CreditsActionLabel DriversFooterLabel CustomizationFooterLabel CustomizationCategoryLabel ExtrasBackLabel PartyPlayerActionLabel PartyRoomLabel PartyLeaveLabel GarageBackLabel GarageRaceLabel BrowserJoinLabel BrowserActionLabel RaceLobbyPlayerActionLabel RaceLobbySideActionLabel RaceLobbyFooterLabel MultiplayerActionLabel LobbyActionLabel QuickActionLabel UpgradesFooterLabel StatisticsBackLabel LeaderboardTabLabel LeaderboardChooseEventLabel LeaderboardFooterLabel RoomCodeKeyLabel WelcomeActionLabel RayActionLabel'.split())
OPTIONS_ACTIONS={'OptionsBack','OptionsReset','OptionsDone','OptionsStep'}

def issues(record,icon=None):
    ink=record['ink'];button=record['button'];errors=[]
    if max(abs(c) for c in record['center_error'])>1.5:errors.append('content group not centered')
    expected_y=record.get('expected_center',[0,(button[1]+button[3])*.5])[1]
    if abs((ink[1]+ink[3])*.5-expected_y)>1.5:errors.append('visible text not vertically centered')
    if icon and not 8<=ink[0]-icon['rect'][2]<=24:errors.append('icon/caption gap outside 8–24 canvas px')
    if min(record['padding'])<12:errors.append('less than 12 canvas px safe padding')
    if '/FO_' not in record['font'].upper():errors.append('original game font missing')
    return errors

@lru_cache(None)
def font_glyphs(fox_root,resource):
    name=resource.rsplit('/',1)[-1].casefold()
    path=next(p for p in (Path(fox_root)/'Flatout/Data/Common/Fonts').glob('*.fnt') if p.name.casefold()==name)
    xml=ET.parse(path).getroot()
    return {int(n.attrib['id']):{k:float(v) for k,v in n.attrib.items() if k in ('xoffset','yoffset','xadvance','width','height')} for n in xml.findall('chars/char')}

@lru_cache(None)
def font_line_height(fox_root,resource):
    name=resource.rsplit('/',1)[-1].casefold()
    path=next(p for p in (Path(fox_root)/'Flatout/Data/Common/Fonts').glob('*.fnt') if p.name.casefold()==name)
    return float(ET.parse(path).getroot().find('common').attrib['lineHeight'])


def ink_bounds(text,glyphs,font_scale,technical_width,letter_spacing=None):
    chars=[glyphs.get(ord(c),glyphs.get(ord('_'))) for c in text]
    advance=sum(g['xadvance']*font_scale for g in chars if g)
    spacing=letter_spacing if letter_spacing is not None else (technical_width-advance)/len(chars) if chars else 0
    pen=0.;boxes=[]
    for c,g in zip(text,chars):
        if g:
            if not c.isspace() and g['width']>0 and g['height']>0:
                boxes.append((pen+g['xoffset']*font_scale,g['yoffset']*font_scale,pen+(g['xoffset']+g['width'])*font_scale,(g['yoffset']+g['height'])*font_scale))
            pen+=g['xadvance']*font_scale+spacing
    if not boxes:return None
    return [min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]


async def inspect(snapshot,query,fox_root):
    nodes=snapshot['nodes'];by_path={n['path']:n for n in nodes};root='msgbox' if snapshot['message'].get('open') else 'menu'
    result={'checks':[],'failures':[],'unverified':[],'scope':'Native captions using shipped glyph bounds; screenshot/ImGui review is separate'}
    for node in nodes:
        path=node['path']
        if '/' not in path:continue
        parent_path,name=path.rsplit('/',1)
        parent=nodes[node['parent_index']] if node.get('parent_index',-1)>=0 else by_path.get(parent_path)
        if not node.get('effective_visible',node.get('visible')) or not node.get('text') or not parent:continue
        tracked='content_center' in node.get('presentation',{})
        if not tracked and name not in CAPTIONS and not (name=='OptionsText' and parent_path.rsplit('/',1)[-1] in OPTIONS_ACTIONS):continue
        if 'presentation' in node:
            binding=node['presentation'];size=binding['local_size']
        else:
            if sum(n['path']==path for n in nodes)>1 or sum(n['path']==parent_path for n in nodes)>1:
                result['unverified'].append({'path':path,'text':node['text'],'reason':'Repeated HUD path needs per-node presentation bindings'});continue
            info=await query('hud.get_node',{'root':root,'name':path.split('/',1)[1],'presentation':True});binding=info['presentation'];size=[info['size']['px'],info['size']['py']]
        resource=binding['font_resource'];glyphs=font_glyphs(str(fox_root),resource)
        local=ink_bounds(node['text'],glyphs,binding['font_scale'],size[0],binding.get('letter_spacing'))
        if local is None:continue
        # Older captions can still use a fitted line box. Account for their
        # baseline and alignment rather than mistaking unused width for tracking.
        if 'baseline_offset' in binding:
            advance=sum(glyphs.get(ord(c),glyphs.get(ord('_'),{})).get('xadvance',0)*binding['font_scale']+binding['letter_spacing'] for c in node['text'])
            font_height=font_line_height(str(fox_root),resource)*binding['font_scale']
            fit=min(1.,size[0]/max(1e-6,advance),size[1]/max(1e-6,font_height)) if binding['fit'] else 1.
            line=max(binding['line_height'],font_height)
            ha=binding['horizontal_align'];va=binding['vertical_align']
            ax=size[0]*(.5 if ha==2 else 1 if ha==1 else 0);ay=size[1]*(.5 if va==2 else 1 if va==1 else 0)
            ox=-advance*(.5 if ha==2 else 1 if ha==1 else 0);oy=binding['baseline_offset']-line*(.5 if va==2 else 1 if va==1 else 0)
            local=[ax+fit*(local[0]+ox),ay+fit*(local[1]+oy),ax+fit*(local[2]+ox),ay+fit*(local[3]+oy)]
        lr=node['rect'];pr=parent['rect'];sx=(lr[2]-lr[0])/max(1e-6,size[0]);sy=(lr[3]-lr[1])/max(1e-6,size[1])
        ink=[lr[0]+local[0]*sx,lr[1]+local[1]*sy,lr[0]+local[2]*sx,lr[1]+local[3]*sy]
        icons=[n for n in nodes if (n.get('parent_index')==node['parent_index'] if 'parent_index' in node else n['path'].rsplit('/',1)[0]==parent_path) and n['path'].endswith('Icon') and n.get('effective_visible',n.get('visible'))]
        # ToggleIcon is one 32px container whose pause/play glyphs share bounds.
        icon=next(iter(icons),None)
        group=ink[:] if icon is None else [min(ink[0],icon['rect'][0]),min(ink[1],icon['rect'][1]),max(ink[2],icon['rect'][2]),max(ink[3],icon['rect'][3])]
        padding=[group[0]-pr[0],group[1]-pr[1],pr[2]-group[2],pr[3]-group[3]]
        relative=binding.get('content_center',[.5,.5]);expected=[pr[0]+(pr[2]-pr[0])*relative[0],pr[1]+(pr[3]-pr[1])*relative[1]]
        center=[(group[0]+group[2])*.5-expected[0],(group[1]+group[3])*.5-expected[1]]
        record={'path':path,'text':node['text'],'button':pr,'ink':ink,'content':group,'padding':padding,'center_error':center,'expected_center':expected,'font':resource,'node_index':node.get('node_index'),'parent_index':node.get('parent_index')}
        failure=issues(record,icon);record['passed']=not failure
        result['checks'].append(record)
        if failure:result['failures'].append({**record,'issues':failure})
    result['passed']=not result['failures']
    return result


def report(folder):
    """Re-evaluate saved observations without controlling the game."""
    folder=Path(folder);atlas=json.loads((folder/'report.json').read_text(encoding='utf-8'));screens=[];failures=[];unique=set();unverified=[]
    for state in atlas['states']:
        snapshot=json.loads((folder/state['metadata']).read_text(encoding='utf-8'));audit=snapshot.get('button_audit',{})
        for check in audit.get('checks',[]):
            parent=check['path'].rsplit('/',1)[0]
            exact=check.get('parent_index') is not None
            if not exact and (sum(n['path']==check['path'] for n in snapshot['nodes'])>1 or sum(n['path']==parent for n in snapshot['nodes'])>1):
                unverified.append({'screen':state['title'],'path':check['path'],'text':check['text'],'reason':'Older capture has repeated paths without per-node bindings'});check['unverified']=True;continue
            icon=next((n for n in snapshot['nodes'] if (n.get('parent_index')==check['parent_index'] if exact else n['path'].rsplit('/',1)[0]==parent) and n['path'].endswith('Icon') and n.get('effective_visible',n.get('visible'))),None)
            failure=issues(check,icon);check['issues']=failure;check['passed']=not failure
            unique.add((check['path'],check['text']))
            if failure:failures.append({'screen':state['title'],'image':state['image'],**check})
        unverified.extend({'screen':state['title'],**x} for x in audit.get('unverified',[]))
        screens.append({'title':state['title'],'case':state['case'],'image':state['image'],'checks':sum(not x.get('unverified') for x in audit.get('checks',[])),'failures':sum(not x['passed'] for x in audit.get('checks',[]) if not x.get('unverified'))})
    result={'passed':not failures,'screens':screens,'observations':sum(x['checks'] for x in screens),'unique_captions':len(unique),'failures':failures,'unverified':unverified,
            'limits':['Read-only native glyph geometry; screenshot review remains necessary','ImGui livery buttons require separate rendered review','Only captured states are qualified; deferred/unavailable game flows are not certified']}
    (folder/'button-audit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    esc=html.escape
    rows=''.join(f'<tr><td><a href="{esc(s["image"])}">{esc(s["title"])}</a></td><td>{s["checks"]}</td><td>{s["failures"]}</td></tr>' for s in screens)
    problems=''.join(f'<li><a href="{esc(f["image"])}">{esc(f["screen"])}</a>: {esc(f["text"])} — {esc(", ".join(f["issues"]))}</li>' for f in failures)
    (folder/'button-audit.html').write_text(f'<!doctype html><meta charset="utf-8"><title>FlatOut button audit</title><style>body{{font:16px system-ui;background:#171918;color:#eee;margin:40px;max-width:1100px}}a{{color:#ffa800}}td,th{{text-align:left;padding:10px 22px 10px 0;border-bottom:1px solid #444}}table{{border-collapse:collapse}}</style><h1>FlatOut button audit</h1><p>{result["observations"]} observations · {len(unique)} distinct captions · {len(failures)} findings</p><p>Original game fonts; visible glyph and icon centering, padding and icon gaps. <a href="index.html">Full stereo gallery</a> · <a href="button-audit.json">Measurements and limits</a></p><ul>{problems}</ul><table><tr><th>Captured state</th><th>Button checks</th><th>Findings</th></tr>{rows}</table>',encoding='utf-8')
    return result

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('atlas',type=Path);args=parser.parse_args()
    result=report(args.atlas);print(json.dumps({k:v for k,v in result.items() if k not in ('screens','failures')},indent=2));print('findings',len(result['failures']))
