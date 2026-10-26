"""Read-only RenderDoc proof of real livery side geometry and silhouette pixels.

Run inside qrenderdoc --python. FOX_VOLUME_GPU_JOB names a JSON file containing
capture, result, native_state and thickness_mm (4 or 8). This never connects to the game or
delivers input. The unmodified flat binary must fail with no volume draws.
"""
import json
import math
import os
from pathlib import Path
import struct
import traceback

import renderdoc as rd


def flatten(nodes, markers=()):
    for action in nodes:
        name = action.GetName(structured)
        here = markers + ((name,) if action.children else ())
        yield action, here
        yield from flatten(action.children, here)


def cross(a, b, p):
    return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])


def hull(points):
    points = sorted(set(points))
    halves = []
    for seq in (points, list(reversed(points))):
        out = []
        for p in seq:
            while len(out) >= 2 and cross(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        halves.append(out[:-1])
    return halves[0]+halves[1]


def outside(poly, p):
    return any(cross(poly[i], poly[(i+1) % len(poly)], p) < -1
               for i in range(len(poly)))


def find_variable(nodes, name):
    for variable in nodes:
        if variable.name == name:
            return variable
        nested = find_variable(variable.members, name)
        if nested is not None:
            return nested
    return None


def vec_add(*vectors):
    return [sum(values) for values in zip(*vectors)]


def mul(vector, scalar):
    return [v*scalar for v in vector]


job = json.loads(Path(os.environ['FOX_VOLUME_GPU_JOB']).read_text())
result = {'passed': False, 'capture': job['capture'], 'thickness_mm': job['thickness_mm'],
          'contract': 'Actual GPU vertices behind an unchanged front plane, plus written silhouette pixels outside its projected rectangle in both eye draws.',
          'draws': []}
cap = controller = None
try:
    cap = rd.OpenCaptureFile()
    result['open'] = str(cap.OpenFile(job['capture'], '', None))
    status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    result['replay'] = str(status)
    assert controller is not None, status
    structured = controller.GetStructuredFile()
    marker = 'LiveryVolume.Button' if job.get('button') else 'LiveryVolume.Footer'
    candidates = [(a, m) for a, m in flatten(controller.GetRootActions())
                  if a.flags & rd.ActionFlags.Drawcall and any(marker in s for s in m)]
    assert len(candidates) == 2, ('Expected one actual shell draw per eye; flat control has none', len(candidates))
    native = json.loads(Path(job['native_state']).read_text())['vr']
    frame = native['frame']
    panel = frame['panel']
    assert max(abs(a-b) for a, b in zip(panel['up'], (0, 0, 1))) < 2e-5, \
        ('Native panel must be upright in world Z', panel['up'])
    x, y, width, height = native['ui_volume']['footer_rect']
    lift = 0
    if job.get('button'):
        control = next(c for c in native['controls'] if c['label'] == job['button'] and c['enabled'])
        x, y, width, height = (control[k] for k in ('x', 'y', 'width', 'height'))
        lift = job.get('lift_meters', .015)
    assert native['ui_volume']['mode_mm'] == job['thickness_mm']
    center = vec_add(panel['origin'], mul(panel['right'], ((x+width*.5)/1920-.5)*frame['panel_width']),
                     mul(panel['up'], (.5-(y+height*.5)/1080)*frame['panel_height']),
                     mul(panel['back'], -lift))
    expected_world = panel['right']+panel['up']+mul(panel['back'], -1)+center
    targets = set()
    eyes = set()
    for action, markers in candidates:
        controller.SetFrameEvent(action.eventId, True)
        pipe = controller.GetPipelineState()
        vertex = pipe.GetVBuffers()[0]
        count = action.numIndices
        assert count == 54 and vertex.byteStride == 24, (count, vertex.byteStride)
        raw = controller.GetBufferData(vertex.resourceId,
            vertex.byteOffset+action.vertexOffset*vertex.byteStride, count*vertex.byteStride)
        positions = [struct.unpack_from('<3f', raw, i*vertex.byteStride) for i in range(count)]
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        object_matrix = None
        eye_position = None
        for index, block in enumerate(reflection.constantBlocks):
            if 'ObjectStd' not in block.name and 'CameraStd' not in block.name:
                continue
            binding = pipe.GetConstantBlock(rd.ShaderStage.Vertex, index, 0).descriptor
            values = controller.GetCBufferVariableContents(rd.ResourceId.Null(),
                pipe.GetShader(rd.ShaderStage.Vertex), rd.ShaderStage.Vertex, reflection.entryPoint,
                index, binding.resource, binding.byteOffset, binding.byteSize)
            value = find_variable(values, 'world')
            if value is not None:
                object_matrix = list(value.value.f32v)[:12]
            eye = find_variable(values, 'viewPosition')
            if eye is not None:
                eye_position = list(eye.value.f32v)[:3]
        assert object_matrix is not None, 'No actual object transform'
        registration_error = max(abs(a-b) for a, b in zip(object_matrix, expected_world))
        assert registration_error < 2e-5, ('Shell is not registered to the native front', registration_error)
        assert max(abs(a-b) for a, b in zip(object_matrix[3:6], (0, 0, 1))) < 2e-5, \
            ('GPU body retained authored camera pitch/roll', object_matrix[3:6])
        assert eye_position is not None, 'No actual per-eye view position'
        camera = frame['camera']
        expected_eyes = {name:vec_add(camera['origin'], *[mul(camera[axis], value)
            for axis, value in zip(('right', 'up', 'back'), frame[name]['origin'])])
            for name in ('left_eye', 'right_eye')}
        error = lambda name:math.sqrt(sum((a-b)**2 for a, b in zip(eye_position, expected_eyes[name])))
        eye_name = min(expected_eyes, key=error)
        assert error(eye_name) < 2e-5, ('Body draw does not use a native eye', eye_position, expected_eyes)
        eyes.add(eye_name)
        assert abs((max(p[0] for p in positions)-min(p[0] for p in positions))-width/1920*frame['panel_width']) < 2e-5
        assert abs((max(p[1] for p in positions)-min(p[1] for p in positions))-height/1080*frame['panel_height']) < 2e-5
        zs = [p[2] for p in positions]
        thickness = job['thickness_mm']*.001
        assert abs(max(zs)) < 1e-6 and abs(min(zs)+thickness) < 1e-6, (min(zs), max(zs))
        assert len({round(z, 7) for z in zs}) == 3, 'Need true side plus rear chamfer planes'
        assert all(z <= 1e-7 for z in zs), 'Body moved in front of existing artwork'
        state = controller.GetD3D11PipelineState()
        assert str(state.rasterizer.state.cullMode).lower().endswith('back'), str(state.rasterizer.state.cullMode)
        assert state.outputMerger.depthStencilState.depthEnable, 'Shell must obey scene depth'
        post = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
        data = controller.GetBufferData(post.vertexResourceId, post.vertexByteOffset,
                                        count*post.vertexByteStride)
        clips = [struct.unpack_from('<4f', data, i*post.vertexByteStride) for i in range(count)]
        vp = pipe.GetViewport(0)
        screen = [(vp.x+(p[0]/p[3]+1)*vp.width*.5,
                   vp.y+(1-p[1]/p[3])*vp.height*.5) for p in clips]
        front = hull([screen[i] for i, p in enumerate(positions) if abs(p[2]) < 1e-7])
        assert len(front) == 4, front
        output = next(x.resource for x in pipe.GetOutputTargets() if x.resource != rd.ResourceId.Null())
        targets.add(str(output))
        samples = []
        for i in range(0, count, 3):
            tri = screen[i:i+3]
            if abs(cross(tri[0], tri[1], tri[2])) < 2:
                continue
            p = tuple(int(sum(v[k] for v in tri)/3) for k in (0, 1))
            if not outside(front, (p[0]+.5, p[1]+.5)):
                continue
            history = controller.PixelHistory(output, p[0], p[1], rd.Subresource(), rd.CompType.Typeless)
            writes = [h for h in history if h.eventId == action.eventId
                      and any(abs(a-b) > 1/1024 for a, b in zip(h.preMod.col.floatValue, h.postMod.col.floatValue))]
            if writes:
                samples.append({'pixel': p, 'triangle': i//3, 'post': list(writes[-1].postMod.col.floatValue)})
        item = {'event': action.eventId, 'eye': eye_name, 'eye_position': eye_position,
                'eye_position_error_meters':error(eye_name),'markers': markers, 'target': str(output),
                'vertex_buffer': str(vertex.resourceId), 'vertices': count,
                'world': object_matrix, 'front_registration_error': registration_error,
                'z_range': [min(zs), max(zs)], 'projected_front': front,
                'silhouette_writes': samples}
        result['draws'].append(item)
        assert samples, 'No actual shell pixels outside the flat front silhouette'
    assert len(targets) == 2, 'Must inspect distinct left/right eye targets'
    assert eyes == {'left_eye', 'right_eye'}, 'Duplicated one eye into both outputs'
    result['passed'] = True
except BaseException:
    result['error'] = traceback.format_exc()
finally:
    if controller:
        controller.Shutdown()
    if cap:
        cap.Shutdown()
    Path(job['result']).write_text(json.dumps(result, indent=2))
os._exit(0)
