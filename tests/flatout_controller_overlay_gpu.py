"""Read-only GPU proof of stereo controller composition.

Run with qrenderdoc --python; FOX_CONTROLLER_GPU_JOB identifies a JSON object
with capture, native_state and result paths. Checks the actual submitted draws,
not the presence of implementation strings. The overlay-disabled control must fail.
"""
import json
import math
import os
from pathlib import Path
import traceback
import renderdoc as rd


def walk(nodes, parents=()):
    for action in nodes:
        name = action.GetName(structured)
        markers = parents + ((name,) if action.children else ())
        yield action, markers
        yield from walk(action.children, markers)


def find(nodes, name):
    for value in nodes:
        if value.name == name:
            return value
        nested = find(value.members, name)
        if nested is not None:
            return nested
    return None


job = json.loads(Path(os.environ['FOX_CONTROLLER_GPU_JOB']).read_text())
out = Path(job['result'])
report = {'passed': False, 'capture': job['capture'], 'draws': [], 'targets': []}
cap = ctl = None
try:
    cap = rd.OpenCaptureFile()
    report['open'] = str(cap.OpenFile(job['capture'], '', None))
    status, ctl = cap.OpenCapture(rd.ReplayOptions(), None)
    report['replay'] = str(status)
    assert ctl is not None, status
    structured = ctl.GetStructuredFile()
    actions = list(walk(ctl.GetRootActions()))
    candidates = [(a, m) for a, m in actions if a.flags & rd.ActionFlags.Drawcall
                  and any('VR controller models' in s for s in m)]
    assert len(candidates) >= 4, ('Need both controllers in both eyes', len(candidates))
    native = json.loads(Path(job['native_state']).read_text())
    expected = {name: eye['render_position'] for name, eye in
                zip(('left_eye', 'right_eye'), native['vr']['eyes'])}
    targets, vertex_buffers, depths = {}, set(), set()
    for action, markers in candidates:
        ctl.SetFrameEvent(action.eventId, True)
        pipe = ctl.GetPipelineState()
        target = next(t.resource for t in pipe.GetOutputTargets() if t.resource != rd.ResourceId.Null())
        depth = pipe.GetDepthTarget().resource
        assert depth != rd.ResourceId.Null(), 'Controllers need their own depth buffer'
        depth_state = ctl.GetD3D11PipelineState().outputMerger.depthStencilState
        assert depth_state.depthEnable and depth_state.depthWrites, 'Controller surfaces must self-occlude'
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        position = None
        for index, block in enumerate(reflection.constantBlocks):
            if 'CameraStd' not in block.name:
                continue
            binding = pipe.GetConstantBlock(rd.ShaderStage.Vertex, index, 0).descriptor
            values = ctl.GetCBufferVariableContents(rd.ResourceId.Null(), pipe.GetShader(rd.ShaderStage.Vertex),
                rd.ShaderStage.Vertex, reflection.entryPoint, index, binding.resource, binding.byteOffset, binding.byteSize)
            value = find(values, 'viewPosition')
            if value is not None:
                position = list(value.value.f32v)[:3]
        assert position is not None, 'Missing actual eye camera'
        error = lambda eye: math.sqrt(sum((a-b)**2 for a,b in zip(position, expected[eye])))
        eye = min(expected, key=error)
        # World positions around 600 m quantize to 61 micrometres in float32.
        assert error(eye) < 1e-4, (position, expected)
        targets.setdefault(target, {'eye': eye, 'events': []})['events'].append(action.eventId)
        assert targets[target]['eye'] == eye
        depths.add(str(depth))
        buffers = [v.resourceId for v in pipe.GetVBuffers() if v.resourceId != rd.ResourceId.Null()]
        vertex_buffers.update(buffers)
        report['draws'].append({'event': action.eventId, 'eye': eye, 'camera': position,
                               'target': str(target), 'depth': str(depth), 'indices': action.numIndices})
    assert len(targets) == len(depths) == 2, 'Eyes must have separate color and depth targets'
    assert {t['eye'] for t in targets.values()} == {'left_eye', 'right_eye'}
    # The same model geometry must not also be rendered into the world image.
    candidate_ids = {a.eventId for a,_ in candidates}
    duplicates = []
    for action, markers in actions:
        if not action.flags & rd.ActionFlags.Drawcall or action.eventId in candidate_ids:
            continue
        ctl.SetFrameEvent(action.eventId, True)
        if any(v.resourceId in vertex_buffers for v in ctl.GetPipelineState().GetVBuffers()):
            duplicates.append(action.eventId)
    assert not duplicates, ('Controller geometry was drawn twice', duplicates)
    textures = {t.resourceId:t for t in ctl.GetTextures()}
    ctl.SetFrameEvent(actions[-1][0].eventId, True)
    for target, entry in targets.items():
        texture = textures[target]
        size = native['overlay']['eyes'][0 if entry['eye']=='left_eye' else 1]
        assert (texture.width, texture.height) == (size['width'], size['height']), 'Wrong overlay extent'
        # A sparse grid finds actual model pixels while all four corners stay transparent.
        alpha = lambda x,y:list(ctl.PickPixel(target,x,y,rd.Subresource(),rd.CompType.Typeless).floatValue)
        corners = [alpha(x,y) for x,y in ((0,0),(texture.width-1,0),(0,texture.height-1),(texture.width-1,texture.height-1))]
        assert all(p[3] == 0 for p in corners), ('Overlay has an opaque backdrop', corners)
        samples = []
        for y in range(texture.height//4, texture.height*3//4, 12):
            for x in range(texture.width//5, texture.width*4//5, 12):
                rgba = alpha(x,y)
                if rgba[3] > .9 and max(rgba[:3]) > .02:
                    history = ctl.PixelHistory(target,x,y,rd.Subresource(),rd.CompType.Typeless)
                    writes = [h.eventId for h in history if h.eventId in entry['events'] and not h.depthTestFailed]
                    if writes:
                        samples.append({'pixel':[x,y], 'rgba':rgba, 'writes':writes})
                        break
            if samples:
                break
        assert samples, ('No native controller draw wrote visible pixels', entry['eye'])
        save = rd.TextureSave(); save.resourceId = target; save.destType = rd.FileType.PNG; save.alpha = rd.AlphaMapping.Preserve
        image = out.with_name(out.stem+'-'+entry['eye']+'.png')
        ctl.SaveTexture(save,str(image))
        report['targets'].append({**entry,'id':str(target),'size':[texture.width,texture.height],
                                  'samples':samples,'image':str(image),'corner_rgba':corners})
    report['passed'] = True
except BaseException:
    report['error'] = traceback.format_exc()
finally:
    if ctl: ctl.Shutdown()
    if cap: cap.Shutdown()
    out.write_text(json.dumps(report,indent=2))
os._exit(0 if report['passed'] else 1)
