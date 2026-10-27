"""Verify spatial label transforms on actual GPU draws in both eyes.

Run with qrenderdoc --python. FOX_SPATIAL_GPU_JOB names JSON containing capture,
native_state and result paths. native_state is a screenshot/diagnostic snapshot.
"""
import json
import math
import os
from pathlib import Path
import traceback
import renderdoc as rd


def find(nodes,name):
    for node in nodes:
        if node.name==name:
            return node
        nested=find(node.members,name)
        if nested is not None:
            return nested
    return None


def walk(actions,parents=()):
    for action in actions:
        name=action.GetName(structured)
        yield action,parents
        yield from walk(action.children,parents+(name,))


def distance(a,b):
    # RenderDoc embeds Python versions that predate math.dist.
    return math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))


job=json.loads(Path(os.environ['FOX_SPATIAL_GPU_JOB']).read_text())
report={'passed':False,'capture':job['capture'],'matches':[]}
cap=controller=None
try:
    native=json.loads(Path(job['native_state']).read_text())['menu']['result']
    basis=native['spatial_layout']
    expected=basis['right']+basis['up']+basis['back']
    assert abs(basis['right'][2])+abs(basis['up'][0])+abs(basis['up'][1])+abs(basis['back'][2])<1e-5
    cap=rd.OpenCaptureFile();report['open']=str(cap.OpenFile(job['capture'],'',None))
    status,controller=cap.OpenCapture(rd.ReplayOptions(),None);assert controller is not None,status
    structured=controller.GetStructuredFile()
    for action,parents in walk(controller.GetRootActions()):
        if not action.flags & rd.ActionFlags.Drawcall or 'Hud3D' not in parents:
            continue
        controller.SetFrameEvent(action.eventId,True);pipe=controller.GetPipelineState()
        if not any(v.byteStride==24 for v in pipe.GetVBuffers()):
            continue
        shader=pipe.GetShader(rd.ShaderStage.Vertex)
        reflection=pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        if reflection is None:
            continue
        slots={b.name:i for i,b in enumerate(reflection.constantBlocks)}
        object_slot=next((i for name,i in slots.items() if 'ObjectStd' in name),None)
        camera_slot=next((i for name,i in slots.items() if 'CameraStd' in name),None)
        if object_slot is None or camera_slot is None:
            continue
        def contents(slot):
            binding=pipe.GetConstantBlock(rd.ShaderStage.Vertex,slot,0).descriptor
            return controller.GetCBufferVariableContents(rd.ResourceId.Null(),shader,rd.ShaderStage.Vertex,
                reflection.entryPoint,slot,binding.resource,binding.byteOffset,binding.byteSize)
        world=find(contents(object_slot),'world')
        eye=find(contents(camera_slot),'viewPosition')
        if world is None or eye is None:
            continue
        matrix=list(world.value.f32v)[:12]
        if len(matrix)!=12:
            continue
        for item in native['items']:
            # Hover changes only translation by up to 20 mm while the capture
            # queues. The title and its authored shadow are far farther apart.
            separation=distance(matrix[9:12],item['rendered_position'])
            if separation>.025:
                continue
            error=max(abs(a-b) for a,b in zip(matrix[:9],expected))
            assert error<2e-5,('GPU label did not use the leveled basis',item['title'],matrix,expected)
            report['matches'].append({'index':item['index'],'title':item['title'],'event':action.eventId,
                'view_position':list(eye.value.f32v)[:3],'world':matrix,'basis_error':error,
                'indices':action.numIndices,'targets':[str(x) for x in action.outputs]})
    views=[]
    for match in report['matches']:
        if not any(distance(match['view_position'],v)<.001 for v in views):
            views.append(match['view_position'])
    assert len(views)==2,('Spatial titles were not observed in two main-eye views',views)
    assert .055<distance(*views)<.075,('Unexpected eye separation',views)
    for item in native['items']:
        for view in views:
            assert any(m['index']==item['index'] and distance(m['view_position'],view)<.001
                       for m in report['matches']),('Missing native title draw',item['title'],view)
    report['views']=views;report['passed']=True
except BaseException:
    report['error']=traceback.format_exc()
finally:
    if controller:controller.Shutdown()
    if cap:cap.Shutdown()
    Path(job['result']).write_text(json.dumps(report,indent=2))
os._exit(0 if report['passed'] else 1)
