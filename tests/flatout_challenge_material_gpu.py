"""Read-only RenderDoc proof of native Challenge photo/style GPU bindings.

Run with qrenderdoc --python. FOX_CHALLENGE_MATERIAL_GPU_JOB names a JSON file:
capture/result/layout_manifest/styles_manifest/art_map paths; photo_focus
(0..2 or null), selected_card(0..2), and pointer_uv from native ray diagnostics.
Default event_ids are Challenge01/02/03; expected_photo_draws/body_draws/fx_draws
count three LOGICAL instances. Base UI passes default to two, lifted passes one.
Duplicate ordered pass signatures must match complete PS uniforms and bindings.
Settled is true. Capture after the first card hover has settled1.2s.
source_art_dimensions defaults to2048x1152 (logical source cells); the observed
compiler-normalized compiled_photo_dimensions defaults to2048x1024. Normalized
UVs retain the logical source crop; do not infer its aspect from the GPU extent.

No draw IDs or marker names select the evidence. Actual reflected uniforms,
bound constant buffers, SRVs and render targets do. Existing repeated base-eye
passes and the separate lifted-card atlas are checked, not counted as extra
logical controls. This does not independently prove final compositor eyes or FPS.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import traceback


PHOTO={'g_PhotoBounds','g_PhotoDynamics','g_PhotoShape'}
STYLE={'g_StyleUV0','g_StyleUV1','g_StyleUV2','g_StyleUV3','g_StyleMap0','g_StyleMap1',
       'g_StyleState','g_StyleShape','g_StylePointer','g_StyleContent'}


def near(a,b,tolerance=0.0001):
    return len(a)==len(b) and all(abs(float(x)-float(y))<=tolerance for x,y in zip(a,b))


def declaration_names(items):
    result=set()
    for item in items:
        result.add(item.name.split('.')[-1])
        nested=getattr(item,'members',None) or getattr(getattr(item,'type',None),'members',[])
        if nested:result.update(declaration_names(nested))
    return result


def values(items):
    result={}
    for item in items:
        if item.members:result.update(values(item.members))
        else:
            count=max(1,item.rows*item.columns)
            result[item.name.split('.')[-1]]=list(item.value.f32v)[:count]
    return result


def walk(actions,structured,parents=()):
    for action in actions:
        name=action.GetName(structured)
        yield action,parents
        for nested in walk(action.children,structured,parents+(name,)):yield nested


def main(job,rd):
    report={'passed':False,'capture':job['capture'],'photos':[],'styles':[],'checks':[],'draw_candidates':[],
        'scope':'Actual PS constant-buffer values, texture SRVs and output bindings on native Challenge draws',
        'limits':['One capture cannot prove animation duration, headset performance or FPS',
                  'Base UI may render twice and lifted cards once; final compositor eye images are not proven by these material draws',
                  'Resource/UV binding proof is not a byte-for-byte texture-content or final-pixel equivalence proof']}
    capture=controller=None
    def check(condition,message):
        report['checks'].append({'passed':bool(condition),'requirement':message})
        assert condition,message
    try:
        check('pointer_uv' in job,'Job supplies exact pointer_uv from saved native ray diagnostics')
        layout=json.loads(Path(job['layout_manifest']).read_text(encoding='utf-8'))
        style=json.loads(Path(job['styles_manifest']).read_text(encoding='utf-8'))
        art=json.loads(Path(job['art_map']).read_text(encoding='utf-8'))
        check(style['schema']=='fox.challenge-css-materials.v1','Styles manifest schema matches')
        materials={m['name']:m for m in style['materials']}
        events=job.get('event_ids',['Challenge01','Challenge02','Challenge03'])
        entries=[next(row for row in art if event in row['events']) for event in events]
        check(len(entries)==3,'Three actual event-art entries supplied')
        focus=job.get('photo_focus',0);selected=job.get('selected_card',0)
        source_art_dimensions=job.get('source_art_dimensions',[2048,1152])
        compiled_photo_dimensions=job.get('compiled_photo_dimensions',[2048,1024])
        check(focus in (None,0,1,2) and selected in (0,1,2),'Focus and selection refer to the three slots')
        check(len(job['pointer_uv'])==2 and all(0<=v<=1 for v in job['pointer_uv']),'Expected pointer UV is within the logical card')
        report['expected']={'photo_focus':focus,'selected_card':selected,'pointer_uv':job['pointer_uv'],
                            'event_ids':events,'settled':job.get('settled',True),
                            'source_art_dimensions':source_art_dimensions,'compiled_photo_dimensions':compiled_photo_dimensions}
        report['texture_coordinate_convention']='Normalized art-map UVs and cover crop follow logical source cells; compiler-normalized GPU dimensions are validated separately.'
        report['input_hashes']={key:hashlib.sha256(Path(job[key]).read_bytes()).hexdigest()
                               for key in ('layout_manifest','styles_manifest','art_map')}
        capture=rd.OpenCaptureFile()
        status=capture.OpenFile(job['capture'],'',None);report['open']=str(status)
        check(status==rd.ResultCode.Succeeded,'RenderDoc opened the capture')
        status,controller=capture.OpenCapture(rd.ReplayOptions(),None);report['replay']=str(status)
        check(controller is not None,'RenderDoc replay controller exists')
        structured=controller.GetStructuredFile()
        textures={str(t.resourceId):t for t in controller.GetTextures()}
        names={str(r.resourceId):r.name for r in controller.GetResources()}
        reflection_cache={};candidate_shaders={};draw_count=0
        ps=rd.ShaderStage.Pixel
        def texture_info(resource):
            key=str(resource);texture=textures.get(key)
            return {'id':key,'name':names.get(key,''),'width':texture.width if texture else None,
                    'height':texture.height if texture else None,'format':texture.format.Name() if texture else None}
        def bound_textures(pipe,reflection):
            if not hasattr(pipe,'GetReadOnlyResources'):
                raise RuntimeError('RenderDoc lacks GetReadOnlyResources; actual SRV proof is unavailable')
            rows=[]
            for used in pipe.GetReadOnlyResources(ps):
                index=used.access.index
                if index>=len(reflection.readOnlyResources):continue
                reflected=reflection.readOnlyResources[index];descriptor=used.descriptor
                item={'binding':reflected.name,'reflection_index':index,**texture_info(descriptor.resource)}
                item['view_format']=descriptor.format.Name();item['srgb_view']=descriptor.format.SRGBCorrected()
                check(descriptor.resource!=rd.ResourceId.Null(),'Used PS texture is bound: '+reflected.name)
                check(item['width'] is not None and item['height'] is not None,'Bound SRV has captured texture dimensions')
                rows.append(item)
            diffuse=[row for row in rows if 'diffuse' in row['binding'].lower()]
            check(bool(diffuse),'Shader has its actual diffuse texture SRV bound')
            return rows,diffuse[0]
        for action,parents in walk(controller.GetRootActions(),structured):
            if not action.flags & rd.ActionFlags.Drawcall:continue
            draw_count+=1
            controller.SetFrameEvent(action.eventId,False)
            pipe=controller.GetPipelineState();shader=pipe.GetShader(ps);key=str(shader)
            if key not in reflection_cache:
                reflection=pipe.GetShaderReflection(ps)
                fields=set()
                if reflection:
                    for block in reflection.constantBlocks:fields.update(declaration_names(block.variables))
                kind='photo' if PHOTO<=fields else 'style' if {'g_StyleState','g_StyleShape','g_StyleUV0'}<=fields else None
                reflection_cache[key]=(reflection,fields,kind)
            reflection,fields,kind=reflection_cache[key]
            if kind is None:continue
            required=PHOTO if kind=='photo' else STYLE
            check(required<=fields,'Actual '+kind+' shader reflects all required uniform fields')
            constants={};blocks=[]
            for index,block in enumerate(reflection.constantBlocks):
                if not declaration_names(block.variables)&required:continue
                binding=pipe.GetConstantBlock(ps,index,0).descriptor
                check(binding.resource!=rd.ResourceId.Null() and binding.byteSize>0,'Actual '+kind+' constant buffer is bound')
                data=controller.GetCBufferVariableContents(rd.ResourceId.Null(),shader,ps,
                    reflection.entryPoint,index,binding.resource,binding.byteOffset,binding.byteSize)
                constants.update(values(data))
                blocks.append({'block':block.name,'index':index,'resource':str(binding.resource),
                               'offset':binding.byteOffset,'bytes':binding.byteSize})
            check(required<=set(constants),'Captured '+kind+' buffer contents include required values')
            for uniform in required:
                check(len(constants[uniform])==4 and all(math.isfinite(v) for v in constants[uniform]),'Finite float4 binding: '+uniform)
            bindings,diffuse=bound_textures(pipe,reflection)
            targets=[texture_info(target.resource) for target in pipe.GetOutputTargets() if target.resource!=rd.ResourceId.Null()]
            check(bool(targets) and all(t['width'] and t['height'] for t in targets),'Native material draw has actual color output targets')
            viewport=pipe.GetViewport(0)
            check(viewport.width>0 and viewport.height>0,'Native material viewport is nonempty')
            row={'event':action.eventId,'shader':key,'entry_point':reflection.entryPoint,'markers':list(parents),
                 'indices':action.numIndices,'instances':action.numInstances,'uniforms':{n:constants[n] for n in sorted(required)},
                 'constant_buffers':blocks,'textures':bindings,'diffuse':diffuse,'targets':targets,
                 'viewport':{'x':viewport.x,'y':viewport.y,'width':viewport.width,'height':viewport.height}}
            candidate_shaders[key]={'family':kind,'entry_point':reflection.entryPoint,'uniform_fields':sorted(fields)}
            report['draw_candidates'].append(row)
            if kind=='photo':
                check([diffuse['width'],diffuse['height']]==compiled_photo_dimensions,'Photo uses the independently observed compiled-art atlas dimensions')
                bounds=constants['g_PhotoBounds'];shape=constants['g_PhotoShape'];dynamic=constants['g_PhotoDynamics']
                choices=[]
                for slot,(entry,card) in enumerate(zip(entries,layout['cards'])):
                    uv=entry['uv'];source_ratio=((uv[2]-uv[0])*source_art_dimensions[0])/((uv[3]-uv[1])*source_art_dimensions[1])
                    cover=min((card['photo']['width']/card['photo']['height'])/source_ratio,1)
                    trim=(uv[2]-uv[0])*(1-cover)/2
                    expected=[uv[0]+trim,uv[1],uv[2]-trim,uv[3]]
                    if near(bounds,expected):choices.append((slot,expected))
                check(len(choices)==1,'Photo bounds match exactly one real event-art crop')
                slot,expected_bounds=choices[0];card=layout['cards'][slot]
                check(near(shape,[card['photo']['width'],card['photo']['height'],float(slot==selected),0],.02),'Photo logical dimensions and selection uniform match slot'+str(slot))
                expected_dynamic=[1.055,1.08,1.10,1] if slot==focus else [1.005,.91,.86,0]
                if job.get('reduced_motion',False):expected_dynamic[0]=1
                if job.get('settled',True):check(near(dynamic,expected_dynamic,.001),'Settled photo zoom/brightness/saturation/sheen match source slot'+str(slot))
                else:check(1<=dynamic[0]<=1.056 and .90<=dynamic[1]<=1.081 and .85<=dynamic[2]<=1.101 and 0<=dynamic[3]<=1,'Transient photo constants stay within authored limits')
                row.update(slot=slot,event_id=events[slot],expected_bounds=expected_bounds,expected_settled_dynamics=expected_dynamic)
                report['photos'].append(row)
            else:
                check([diffuse['width'],diffuse['height']]==style['atlas_size'],'Style material binds the actual Styles atlas dimensions')
                shape=constants['g_StyleShape'];mode=int(round(shape[2]))
                check(mode in (0,1,2) and abs(shape[2]-mode)<.0001,'Style mode is0(body/footer),1(four-stateFX) or2(pointer)')
                check(0<=constants['g_StyleState'][0]<=1 and 0<=shape[3]<=1,'Style mix and disabled state are valid')
                row['mode']=mode
                row['uv_materials']={}
                for field in ('g_StyleUV0','g_StyleUV1','g_StyleUV2','g_StyleUV3'):
                    uv=constants[field]
                    row['uv_materials'][field]=[name for name,m in materials.items() if near(uv,m['uv'])]
                report['styles'].append(row)
        report['draws_examined']=draw_count;report['reflected_shader_cache']=candidate_shaders
        def target_key(row):
            return tuple(t['id'] for t in row['targets'])
        def same_signature(a,b):
            exact=('shader','entry_point','indices','instances','constant_buffers','textures','targets','viewport')
            return (all(a[k]==b[k] for k in exact) and set(a['uniforms'])==set(b['uniforms']) and
                    all(near(a['uniforms'][k],b['uniforms'][k],.00001) for k in a['uniforms']))
        photos_by_slot={slot:[r for r in report['photos'] if r['slot']==slot] for slot in range(3)}
        check(all(photos_by_slot.values()),'All three logical event slots have actual GPU photo draws')
        base_slots=[slot for slot in range(3) if slot!=focus]
        base_targets=set(target_key(r) for slot in base_slots for r in photos_by_slot[slot])
        check(len(base_targets)==1,'Unlifted event photos share one captured base UI render target')
        base_target=next(iter(base_targets));base_passes=job.get('base_ui_passes',2)
        check(base_passes in (1,2),'Base pass count is explicitly one mono or two existing eye passes')
        for slot,rows in photos_by_slot.items():
            count=1 if slot==focus else base_passes
            check(len(rows)==count,'Photo slot'+str(slot)+' has exact physical pass multiplicity '+str(count))
            check(all(same_signature(rows[0],r) for r in rows[1:]),'Repeated photo slot'+str(slot)+' has identical full PS uniforms and resource/target bindings')
        lifted_target=target_key(photos_by_slot[focus][0]) if focus is not None else None
        check(focus is None or lifted_target!=base_target,'Focused photo is rendered once into a distinct lifted-card target')
        logical_photos=[photos_by_slot[slot][0] for slot in range(3)]
        check(len(logical_photos)==job.get('expected_photo_draws',3),'Three logical photo instances, separately reporting physical draws')
        target_passes={base_target:base_passes}
        if lifted_target is not None:target_passes[lifted_target]=1
        def logical_passes(rows,label,expected_base,expected_lift):
            result=[];groups={}
            for row in rows:groups.setdefault(target_key(row),[]).append(row)
            required={base_target:expected_base}
            if lifted_target is not None and expected_lift:required[lifted_target]=expected_lift
            check(set(groups)==set(required),label+' draws use precisely the expected base/lift target routing')
            for target,count in required.items():
                physical=groups[target];passes=target_passes[target]
                check(len(physical)==count*passes,label+' has exact logical-instance times physical-pass count')
                reference=physical[:count]
                for repeat in range(1,passes):
                    check(all(same_signature(a,b) for a,b in zip(reference,physical[repeat*count:(repeat+1)*count])),
                          label+' duplicate ordered pass matches every PS uniform, resource binding, target and viewport')
                for ordinal,row in enumerate(reference):
                    result.append(row)
                    report.setdefault('logical_instances',[]).append({'family':label,'ordinal_within_target':ordinal,
                        'target_ids':list(target),'events':[physical[n*count+ordinal]['event'] for n in range(passes)]})
            return result
        report['render_architecture']={'base_target':list(base_target),'lifted_target':list(lifted_target) if lifted_target else None,
            'base_ui_passes':base_passes,'physical_photo_draws':len(report['photos']),'logical_photo_slots':3,
            'instance_identity_limit':'Body/FX identity is ordered logical instances within matching base passes; vertices are not reconstructed.'}

        mode_counts={str(mode):sum(row['mode']==mode for row in report['styles']) for mode in (0,1,2)}
        report['style_mode_counts']=mode_counts
        check(all(mode_counts[str(mode)]>0 for mode in (0,1,2)),'All body/footer, four-stateFX and pointer shader modes executed')
        def matched(row,field,name):return name in row['uv_materials'][field]
        body=[r for r in report['styles'] if matched(r,'g_StyleUV0','CardNormalBody') and matched(r,'g_StyleUV1','CardSelectedBody')]
        physical_body=body
        body=logical_passes(body,'card_body',len(base_slots),1 if focus is not None else 0)
        check(len(body)==job.get('expected_body_draws',3),'Three logical gradient bodies bind source UV regions')
        if job.get('settled',True):
            check(sorted(round(r['uniforms']['g_StyleState'][0],3) for r in body)==[0,0,1],'One selected and two normal gradient body mixtures')
        fx=[r for r in report['styles'] if r['mode']==1]
        physical_fx=fx
        fx=logical_passes(fx,'card_fx',3,0)
        check(len(fx)==job.get('expected_fx_draws',3),'Three logical card FX instances execute the four-state path')
        for row in fx:
            for field,name in zip(('g_StyleUV0','g_StyleUV1','g_StyleUV2','g_StyleUV3'),('CardNormalFx','CardHoverFx','CardSelectedFx','CardSelectedHoverFx')):
                check(matched(row,field,name),'Four-state FX has correct bound '+name+' UVs')
            check(near(row['uniforms']['g_StyleShape'],[0,0,1,0]),'Four-state FX mode/shape are exact')
        if job.get('settled',True):
            expected_focus=1 if focus is not None else 0
            check(sum(r['uniforms']['g_StyleState'][0]>.999 for r in fx)==expected_focus,'Correct number of focused FX channels')
            check(sum(r['uniforms']['g_StyleState'][2]>.999 for r in fx)==1,'One selected FX channel')
        pointers=[r for r in report['styles'] if r['mode']==2]
        for row in pointers:
            check(matched(row,'g_StyleUV0','CardHoverLight'),'Pointer shader binds its intended hover-light material')
            check(near(row['uniforms']['g_StyleShape'],[0,0,2,0]),'Pointer shader mode is exact')
            check(all(0<=v<=1 for v in row['uniforms']['g_StylePointer'][:2]),'Pointer hotspot UV lies inside its card')
            check(near(row['uniforms']['g_StyleState'][2:],[layout['cards'][0]['card']['width'],layout['cards'][0]['card']['height']],.02),'Pointer shader receives logical card pixel dimensions')
        check(any(near(row['uniforms']['g_StylePointer'][:2],job['pointer_uv'],job.get('pointer_tolerance',.002)) for row in pointers),'Actual pointer uniform matches saved native ray hotspot')
        footers=[]
        for index,action in enumerate(layout['actions']):
            matches=[r for r in report['styles'] if matched(r,'g_StyleUV0','Footer'+str(index)+'Normal') and matched(r,'g_StyleUV1','Footer'+str(index)+'Hover')]
            matches=logical_passes(matches,'footer'+str(index),1,0)
            check(len(matches)==1,'One logical footer'+str(index)+' gradient draw exists')
            row=matches[0];shape=row['uniforms']['g_StyleShape'];state=row['uniforms']['g_StyleState']
            skew=action['height']*.249328/(action['width']+action['height']*.249328)
            check(row['mode']==0 and near(shape,[skew,1,0,0],.0001),'Footer has correct skew clipping, enabled sheen and non-disabled shape')
            check(0<=state[1]<=1 and state[2]>0 and state[3]>0,'Footer sheen phase and logical union size are bound')
            footers.append({'index':index,'event':row['event'],'shape':shape,'state':state})
        report['physical_counts']={'photo':len(report['photos']),'card_body':len(physical_body),'card_fx':len(physical_fx),
            'style_modes':mode_counts}
        report['logical_counts']={'photo':len(logical_photos),'card_body':len(body),'card_fx':len(fx),'footer':len(footers)}
        report['body_events']=[r['event'] for r in body];report['fx_events']=[r['event'] for r in fx]
        report['pointer_events']=[r['event'] for r in pointers];report['footer_summary']=footers
        style_textures=sorted(set(r['diffuse']['id'] for r in report['styles']))
        photo_textures=sorted(set(r['diffuse']['id'] for r in report['photos']))
        report['texture_reuse']={'style_atlas_ids':style_textures,'photo_atlas_ids':photo_textures}
        check(len(style_textures)==1,'All style materials reuse one captured Styles atlas resource')
        check(len(photo_textures)==len(set(entry['resource'] for entry in entries)),'Photo draws reuse the expected installed atlas resources')
        report['output_targets']=list({t['id']:t for row in report['photos']+report['styles'] for t in row['targets']}.values())
        report['passed']=True
    except BaseException:
        report['error']=traceback.format_exc()
    finally:
        if controller:controller.Shutdown()
        if capture:capture.Shutdown()
        Path(job['result']).write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report['passed']


if __name__=='__main__':
    job=json.loads(Path(os.environ['FOX_CHALLENGE_MATERIAL_GPU_JOB']).read_text(encoding='utf-8-sig'))
    try:
        import renderdoc as rd
        passed=main(job,rd)
    except BaseException:
        Path(job['result']).write_text(json.dumps({'passed':False,'error':traceback.format_exc()},indent=2),encoding='utf-8')
        passed=False
    os._exit(0 if passed else 1)
