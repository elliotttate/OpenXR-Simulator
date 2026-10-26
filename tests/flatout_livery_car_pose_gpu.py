"""Read-only native body/wheel transform proof, run in RenderDoc Python.

FOX_LIVERY_CAR_GPU_JOB names JSON with capture, native_state and result paths.
reference_state may name the matching Studio state before Roof. Parts follow
native diagnostics: body first, then front wheels, then rear wheels. The test
matches active matrices on main-eye color draws, never unused cbuffer slots.
"""
import json
import math
import os
from pathlib import Path
import traceback

import renderdoc as rd


def flatten(nodes):
    for action in nodes:
        yield action
        yield from flatten(action.children)


def find(nodes, name):
    for value in nodes:
        if value.name == name:
            return value
        nested = find(value.members, name)
        if nested is not None:
            return nested
    return None


def row_matrix(frame):
    return frame['right']+frame['up']+frame['back']+frame['origin']


def maximum_error(a, b):
    return max(abs(x-y) for x, y in zip(a, b))


def world_eye(camera, eye):
    return [camera['origin'][i]+sum(camera[axis][i]*eye['origin'][j]
            for j, axis in enumerate(('right', 'up', 'back'))) for i in range(3)]


def normalize(vector):
    length = math.sqrt(sum(v*v for v in vector))
    assert length > 1e-8
    return [v/length for v in vector]


job = json.loads(Path(os.environ['FOX_LIVERY_CAR_GPU_JOB']).read_text())
result = {'passed': False, 'capture': job['capture'], 'matches': [],
          'scope': 'Active GPU object matrices on actual main-eye color draws; body, front wheels and rear wheels must each match native presentation diagnostics in both eyes.'}
cap = controller = None
try:
    native = json.loads(Path(job['native_state']).read_text())
    frame = native['vr']['frame']
    presentation = frame['car_presentation']
    parts = presentation['parts']
    assert len(parts) == 5, ('Need body plus both front/rear wheel roots', len(parts))
    expected = [row_matrix(p) for p in parts]
    tolerance = job.get('tolerance', .0002)
    if job.get('reference_state'):
        before = json.loads(Path(job['reference_state']).read_text())['vr']['frame']
        for key in ('camera', 'panel', 'head_reference'):
            assert maximum_error(row_matrix(frame[key]), row_matrix(before[key])) < 2e-5, key+' changed'
        assert maximum_error(row_matrix(presentation['showroom']),
                             row_matrix(before['car_presentation']['showroom'])) < 2e-5, 'Showroom changed'
        assert maximum_error(expected[0], row_matrix(before['car_presentation']['parts'][0])) > .1, \
            'Roof must actually change the car pose'
    assert maximum_error(frame['panel']['up'], [0, 0, 1]) < 2e-5
    assert maximum_error(frame['camera']['up'], [0, 0, 1]) < 2e-5
    if presentation['roof_active']:
        assert presentation.get('progress', 0) >= .999, 'Capture settled Roof pose'
        roof = normalize(parts[0]['back'])
        toward = normalize(frame['camera']['back'][:2]+[0])
        lateral = normalize(frame['camera']['right'])
        longitudinal = normalize(parts[0]['up'])
        assert maximum_error(roof, toward) < 2e-5, 'Roof must face the viewer'
        assert abs(sum(a*b for a, b in zip(longitudinal, lateral))) > .9999, 'Roof must be landscape'
    cap = rd.OpenCaptureFile()
    result['open'] = str(cap.OpenFile(job['capture'], '', None))
    status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    result['replay'] = str(status)
    assert controller is not None, status
    camera = frame['camera']
    eyes = {name:world_eye(camera, frame[name]) for name in ('left_eye', 'right_eye')}
    found = {(part, eye):False for part in range(5) for eye in eyes}
    metadata = {}
    inspected = 0
    for action in flatten(controller.GetRootActions()):
        if not action.flags & rd.ActionFlags.Drawcall or action.numIndices <= 0:
            continue
        if not any(output != rd.ResourceId.Null() for output in action.outputs):
            continue
        controller.SetFrameEvent(action.eventId, True)
        pipe = controller.GetPipelineState()
        if pipe.GetShader(rd.ShaderStage.Pixel) == rd.ResourceId.Null():
            continue
        shader = pipe.GetShader(rd.ShaderStage.Vertex)
        key = str(shader)
        if key not in metadata:
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
            if reflection is None:
                metadata[key] = None
            else:
                blocks = [(i, b.name) for i, b in enumerate(reflection.constantBlocks)]
                obj = next((i for i, n in blocks if 'ObjectInstanced' in n or 'ObjectStd' in n), None)
                cam = next((i for i, n in blocks if 'CameraStd' in n), None)
                metadata[key] = (reflection.entryPoint, obj, cam) if obj is not None and cam is not None else None
        if metadata[key] is None:
            continue
        entry, object_slot, camera_slot = metadata[key]
        def contents(index):
            binding = pipe.GetConstantBlock(rd.ShaderStage.Vertex, index, 0).descriptor
            return controller.GetCBufferVariableContents(rd.ResourceId.Null(), shader,
                rd.ShaderStage.Vertex, entry, index, binding.resource, binding.byteOffset, binding.byteSize)
        view = find(contents(camera_slot), 'viewPosition')
        if view is None:
            continue
        position = list(view.value.f32v)[:3]
        eye = min(eyes, key=lambda e:maximum_error(position, eyes[e]))
        if maximum_error(position, eyes[eye]) > tolerance:
            continue
        value = find(contents(object_slot), 'world')
        if value is None:
            continue
        inspected += 1
        active = list(value.members) if value.members else [value]
        start = action.instanceOffset
        count = max(1, action.numInstances)
        assert start+count <= len(active), ('Active instance range exceeds matrix data', action.eventId)
        for instance in range(start, start+count):
            matrix = list(active[instance].value.f32v)[:12]
            if len(matrix) != 12:
                continue
            for part, expected_matrix in enumerate(expected):
                if found[part, eye]:
                    continue
                error = maximum_error(matrix, expected_matrix)
                if error > tolerance:
                    continue
                found[part, eye] = True
                result['matches'].append({'part':part, 'role':'body' if part == 0 else 'front wheel' if part < 3 else 'rear wheel',
                    'eye':eye, 'event':action.eventId, 'instance':instance, 'instance_count':count,
                    'indices':action.numIndices, 'vs':key, 'ps':str(pipe.GetShader(rd.ShaderStage.Pixel)),
                    'vertex_buffers':[str(v.resourceId) for v in pipe.GetVBuffers()],
                    'index_buffer':str(pipe.GetIBuffer().resourceId),
                    'maximum_matrix_error':error, 'world':matrix})
        if all(found.values()):
            break
    result['main_eye_object_draws_inspected'] = inspected
    result['missing'] = [{'part':part, 'eye':eye} for (part, eye), present in found.items() if not present]
    assert not result['missing'], ('No active GPU root-matrix match. Child local transforms require paired capture analysis; do not claim GPU proof.', result['missing'])
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
