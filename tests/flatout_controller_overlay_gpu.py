"""Read-only GPU proof of stereo controller composition.

Run with qrenderdoc --python; FOX_CONTROLLER_GPU_JOB identifies a JSON object
with capture, native_state and result paths. Checks the actual submitted draws,
not the presence of implementation strings. The overlay-disabled control must fail.
Set tracked_pose_reference=true for a moving camera: the fixed XR grips from
native_state are compared with model/camera buffers captured in the same frame.
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
    # Moving end-of-race cameras invalidate an earlier IPC camera position. In
    # this mode derive the eye basis from its actual captured projection and
    # compare the model against the fixed XR grip pose in that same frame.
    tracked = bool(job.get('tracked_pose_reference', False))
    def camera_values(pipe):
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        if reflection is None:
            return None
        for index, block in enumerate(reflection.constantBlocks):
            if 'CameraStd' not in block.name:
                continue
            binding = pipe.GetConstantBlock(rd.ShaderStage.Vertex, index, 0).descriptor
            return ctl.GetCBufferVariableContents(rd.ResourceId.Null(), pipe.GetShader(rd.ShaderStage.Vertex),
                rd.ShaderStage.Vertex, reflection.entryPoint, index, binding.resource, binding.byteOffset, binding.byteSize)
        return None
    targets, vertex_buffers, depths = {}, set(), set()
    captured_eyes, captured_hands = {}, {}
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
        pose_evidence = None
        if tracked:
            values = camera_values(pipe)
            vp = list(find(values, 'viewProjection').value.f32v)[:16]
            forward = [vp[i*4+2] for i in range(3)]
            length = math.sqrt(sum(v*v for v in forward))
            forward = [v/length for v in forward]
            offset = sum(vp[i*4]*forward[i] for i in range(3))
            fovs = [[e['fov_radians'][i] for i in (0,1,3,2)] for e in native['vr']['eyes']]
            # Native diagnostic FOVs are radians in left/right/up/down order.
            offsets = [-(math.tan(f[0])+math.tan(f[1]))/(math.tan(f[1])-math.tan(f[0])) for f in fovs]
            eye_index = min(range(2), key=lambda i:abs(offset-offsets[i]))
            assert abs(offset-offsets[eye_index]) < 1e-4, ('Captured asymmetric eye projection mismatch', offset, offsets)
            eye = ('left_eye','right_eye')[eye_index]
            l,r,u,d = map(math.tan,fovs[eye_index])
            right = [(vp[i*4]*(r-l)+forward[i]*(l+r))*.5 for i in range(3)]
            up = [(vp[i*4+1]*(u-d)+forward[i]*(u+d))*.5 for i in range(3)]
            for axis in (right,up,forward):
                assert abs(sum(v*v for v in axis)-1) < 1e-4, axis
            # Match a regular scene draw in this same Split, not a stale IPC read.
            split = tuple(s for s in markers if 'Split' in s)
            scene_camera = None
            for scene_action, scene_markers in actions:
                if scene_action.eventId >= action.eventId:
                    break
                if not scene_action.flags & rd.ActionFlags.Drawcall or any('VR controller models' in s for s in scene_markers):
                    continue
                if tuple(s for s in scene_markers if 'Split' in s) != split:
                    continue
                ctl.SetFrameEvent(scene_action.eventId, True)
                scene_values = camera_values(ctl.GetPipelineState())
                if scene_values is not None:
                    value = find(scene_values, 'viewPosition')
                    if value is not None:
                        candidate = list(value.value.f32v)[:3]
                        if math.sqrt(sum((a-b)**2 for a,b in zip(position,candidate))) < 1e-4:
                            scene_camera = {'event':scene_action.eventId,'position':candidate}
                            break
            assert scene_camera, ('No matching same-frame scene eye', position, split)
            ctl.SetFrameEvent(action.eventId, True)
            pipe = ctl.GetPipelineState()
            world = None
            for index, block in enumerate(reflection.constantBlocks):
                if 'ObjectInstanced' in block.name:
                    binding = pipe.GetConstantBlock(rd.ShaderStage.Vertex,index,0).descriptor
                    objects = ctl.GetCBufferVariableContents(rd.ResourceId.Null(),pipe.GetShader(rd.ShaderStage.Vertex),rd.ShaderStage.Vertex,reflection.entryPoint,index,binding.resource,binding.byteOffset,binding.byteSize)
                    world = find(objects,'world')
                    break
            assert world and world.members, 'Missing captured controller world transform'
            model_matrix = list(world.members[0].value.f32v)[:12]
            model = model_matrix[9:12]
            local_eye = native['vr']['eyes'][eye_index]['tracking_pose']['position']
            expected_models = []
            for hand in native['vr']['controllers']:
                delta = [a-b for a,b in zip(hand['grip_pose']['position'],local_eye)]
                expected_models.append([position[i]+right[i]*delta[0]+up[i]*delta[1]-forward[i]*delta[2] for i in range(3)])
            errors = [math.sqrt(sum((a-b)**2 for a,b in zip(model,point))) for point in expected_models]
            hand_index = min(range(2),key=lambda i:errors[i])
            grip = native['vr']['controllers'][hand_index]['grip_pose']
            expected_axes = [[right[i]*grip[axis][0]+up[i]*grip[axis][1]-forward[i]*grip[axis][2] for i in range(3)] for axis in ('x','y','z')]
            orientation_error = max(abs(model_matrix[axis*3+i]-expected_axes[axis][i]) for axis in range(3) for i in range(3))
            pose_evidence = {'hand':hand_index,'model_origin':model,'expected_origins':expected_models,'errors_m':errors,'orientation_max_error':orientation_error,'scene_camera':scene_camera}
            captured_eyes[eye] = position
            captured_hands.setdefault(eye,set()).add(hand_index)
            report.setdefault('tracked_poses',[]).append({'event':action.eventId,'eye':eye,**pose_evidence})
            assert min(errors) < .002, ('Controller world transform did not follow final camera', pose_evidence)
            assert orientation_error < 1e-4, ('Controller orientation did not follow final camera', pose_evidence)
        # World positions around 600 m quantize to 61 micrometres in float32.
        if not tracked:
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
    if tracked:
        assert set(captured_eyes)=={'left_eye','right_eye'} and all(h=={0,1} for h in captured_hands.values()), ('Each eye must contain both hands',captured_hands)
        baseline = math.sqrt(sum((a-b)**2 for a,b in zip(captured_eyes['left_eye'],captured_eyes['right_eye'])))
        tracking = [e['tracking_pose']['position'] for e in native['vr']['eyes']]
        expected_baseline = math.sqrt(sum((a-b)**2 for a,b in zip(*tracking)))
        assert abs(baseline-expected_baseline)<1e-4, ('Captured stereo baseline differs from tracking',baseline,expected_baseline)
        report['captured_eye_separation_m'] = baseline
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
