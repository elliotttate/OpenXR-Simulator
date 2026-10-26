"""Run with qrenderdoc --python; FOX_LIVERY_GPU_JOB names a local job JSON."""
import json, math, os, struct, traceback
from pathlib import Path

job = json.loads(Path(os.environ['FOX_LIVERY_GPU_JOB']).read_text())
result = dict(passed=False, capture=job['capture'])
controller = capture = None
try:
    import renderdoc as rd
    capture = rd.OpenCaptureFile()
    result['open'] = str(capture.OpenFile(job['capture'], '', None))
    status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
    result['replay'] = str(status)
    if controller is None:
        raise RuntimeError(str(status))
    def walk(actions):
        for a in actions:
            yield a
            yield from walk(a.children)
    actions = {a.eventId:a for a in walk(controller.GetRootActions())}
    def constants(stage):
        pipe = controller.GetPipelineState()
        reflection = pipe.GetShaderReflection(stage)
        values = {}
        if reflection is None:
            return values
        def flatten(items, prefix=''):
            for item in items:
                key = prefix+item.name
                if item.members:
                    yield from flatten(item.members, key+'.')
                else:
                    yield key, list(item.value.f32v)[:max(1,item.rows*item.columns)]
        for index, block in enumerate(reflection.constantBlocks):
            binding = pipe.GetConstantBlock(stage, index, 0).descriptor
            variables = controller.GetCBufferVariableContents(rd.ResourceId.Null(), pipe.GetShader(stage),
                stage, pipe.GetShaderEntryPoint(stage), index, binding.resource, binding.byteOffset, binding.byteSize)
            values.update(dict(flatten(variables)))
        return values
    candidates = []
    for texture in controller.GetTextures():
        if (texture.width, texture.height) != (1920,1080):
            continue
        usages = list(controller.GetUsage(texture.resourceId))
        draws = []
        for usage in usages:
            if usage.usage != rd.ResourceUsage.PS_Resource:
                continue
            controller.SetFrameEvent(usage.eventId, True)
            values = constants(rd.ShaderStage.Pixel)
            tile = next((v for k,v in values.items() if k=='TileUV'), None)
            if tile is None:
                continue
            pipe = controller.GetPipelineState()
            viewport = pipe.GetViewport(0)
            mesh = controller.GetPostVSData(0,0,rd.MeshDataStage.VSOut)
            raw = controller.GetBufferData(mesh.vertexResourceId,mesh.vertexByteOffset,mesh.vertexByteStride*mesh.numIndices)
            points = [list(struct.unpack_from('4f',raw,i)) for i in range(0,len(raw)-15,mesh.vertexByteStride)]
            points = [p for p in points if abs(p[3])>1e-6 and all(math.isfinite(v) for v in p)]
            draws.append(dict(event=usage.eventId, tile_uv=tile, postvs=points,
                instances=max(1,actions[usage.eventId].numInstances), instanced=mesh.instanced,
                vertex_constants=constants(rd.ShaderStage.Vertex),
                viewport=dict(x=viewport.x,y=viewport.y,width=viewport.width,height=viewport.height)))
        if draws:
            candidates.append(dict(texture=str(texture.resourceId), draws=draws,
                usage=[dict(event=u.eventId, usage=str(u.usage)) for u in usages]))
    result['candidates'] = candidates
    matching = [c for c in candidates if len(c['draws'])==4 and all(
        abs(d['tile_uv'][0]-.499)<1e-5 and abs(d['tile_uv'][1]-.998)<1e-5 and
        min(abs(d['tile_uv'][2]-.001),abs(d['tile_uv'][2]-.500))<1e-5 and
        abs(d['tile_uv'][3]-.001)<1e-5 for d in c['draws'])]
    assert len(matching)==1, 'Expected exactly one shared UI texture, sampled by two cropped panels in each eye'
    selected = matching[0]
    assert all(d['instances']==1 for d in selected['draws']), 'Unexpected duplicate UI instances'
    counts = [sum(abs(d['tile_uv'][2]-x)<1e-5 for d in selected['draws']) for x in (.001,.500)]
    assert counts==[2,2], ('Each side must draw once per eye', counts)
    selected['projection_checks'] = []
    for draw in selected['draws']:
        side = 'left' if draw['tile_uv'][2]<.1 else 'right'
        points = draw['postvs']
        bounds = [min(p[axis]/p[3] for p in points) for axis in (0,1)] + [max(p[axis]/p[3] for p in points) for axis in (0,1)]
        depth = [min(p[3] for p in points), max(p[3] for p in points)]
        checks=[]
        for expected in job.get('expected',[]):
            if expected['side']!=side:
                continue
            error = max(abs(a-b) for a,b in zip(bounds,expected['ndc_bounds']))
            depth_error = max(abs(a-b) for a,b in zip(depth,expected['depth_range']))
            checks.append(dict(eye=expected['eye'], ndc_error=error, depth_error=depth_error))
        best = min(checks,key=lambda c:c['ndc_error'])
        assert best['ndc_error']<.005 and best['depth_error']<.01, (side,bounds,depth,checks)
        selected['projection_checks'].append(dict(event=draw['event'],side=side,bounds=bounds,depth=depth,match=best))
    assert len({(p['side'],p['match']['eye']) for p in selected['projection_checks']})==4
    first_read=min(d['event'] for d in selected['draws'])
    writes = [u for u in selected['usage'] if 'ColorTarget' in u['usage'] or 'Clear' in u['usage']]
    assert writes and max(u['event'] for u in writes)<first_read, 'The shared UI must finish rendering before either eye samples it'
    clears=[u for u in selected['usage'] if 'Clear' in u['usage']]
    assert len(clears)==1, ('Expected one shared UI render-target clear', clears)
    selected['writes_before_first_read'] = True
    selected['render_target_clears']=len(clears)
    texture=next(t.resourceId for t in controller.GetTextures() if str(t.resourceId)==selected['texture'])
    controller.SetFrameEvent(first_read,True)
    center=list(controller.PickPixel(texture,960,540,rd.Subresource(),rd.CompType.Typeless).floatValue)
    selected['canvas_center_rgba']=center
    assert center[3]<.001, ('The UI canvas must leave the car corridor transparent', center)
    params=rd.TextureSave();params.resourceId=texture;params.destType=rd.FileType.PNG;params.alpha=rd.AlphaMapping.Preserve
    image=Path(job['result']).with_name('shared-ui-texture.png')
    selected['texture_image']=str(image)
    controller.SaveTexture(params,str(image))
    result['passed']=True
except BaseException:
    result['error']=traceback.format_exc()
finally:
    if controller is not None:
        controller.Shutdown()
    if capture is not None:
        capture.Shutdown()
    Path(job['result']).write_text(json.dumps(result,indent=2))
    os._exit(0)
