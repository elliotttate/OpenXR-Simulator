"""RenderDoc regression for controller-overlay composition into the game mirror.

Run using qrenderdoc --python and FOX_CONTROLLER_MIRROR_GPU_JOB pointing to
{capture,result,eye:0|1}. Checks actual resource flow, blending and pixel writes.
The simulator's compositor preview is not the game's desktop mirror.
"""
import json
import math
import os
from pathlib import Path
import traceback


def walk(nodes, structured, parents=()):
    for action in nodes:
        name = action.GetName(structured)
        scope = parents + ((name,) if action.children else ())
        yield action, scope
        yield from walk(action.children, structured, scope)


def main(job, rd):
    report = {'passed': False, 'capture': job['capture'], 'controller_targets': [], 'copies': []}
    capture = controller = None
    try:
        capture = rd.OpenCaptureFile()
        assert capture.OpenFile(job['capture'], '', None) == rd.ResultCode.Succeeded
        status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
        assert controller is not None, status
        structured = controller.GetStructuredFile()
        actions = list(walk(controller.GetRootActions(), structured))
        textures = {t.resourceId: t for t in controller.GetTextures()}
        models = [(a, p) for a, p in actions if a.flags & rd.ActionFlags.Drawcall
                  and any('VR controller models' in marker for marker in p)]
        assert len(models) >= 4, 'Need actual controller geometry in both eye overlays'
        targets = {}
        for action, _ in models:
            controller.SetFrameEvent(action.eventId, False)
            pipe = controller.GetPipelineState()
            target = next(t.resource for t in pipe.GetOutputTargets() if t.resource != rd.ResourceId.Null())
            depth = controller.GetD3D11PipelineState().outputMerger.depthStencilState
            assert depth.depthEnable and depth.depthWrites, 'Controller models must keep their own self-occlusion'
            targets.setdefault(target, []).append(action.eventId)
        assert len(targets) == 2, 'Separate controller images for the two eyes'
        source = list(targets)[job.get('eye', 0)]
        report['controller_targets'] = [{'id': str(k), 'model_events': v,
                                        'size': [textures[k].width, textures[k].height]} for k, v in targets.items()]
        reads = []
        for action, parents in actions:
            if not action.flags & rd.ActionFlags.Drawcall: continue
            controller.SetFrameEvent(action.eventId, False)
            pipe = controller.GetPipelineState()
            sources = [r.descriptor.resource for r in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel)]
            outputs = [t.resource for t in pipe.GetOutputTargets() if t.resource != rd.ResourceId.Null()]
            reads.append((action, sources, outputs))
            for sampled in sources:
                if sampled not in targets: continue
                blend = pipe.GetColorBlends()[0]
                depth = controller.GetD3D11PipelineState().outputMerger.depthStencilState
                viewport = pipe.GetViewport(0)
                report['copies'].append({'event': action.eventId, 'source': str(sampled),
                    'targets': [str(t) for t in outputs], 'indices': action.numIndices,
                    'vertices': action.numIndices or action.numVertices,
                    'blend_enabled': blend.enabled, 'src_blend': str(blend.colorBlend.source),
                    'dst_blend': str(blend.colorBlend.destination), 'depth_enabled': depth.depthEnable,
                    'viewport': [viewport.x, viewport.y, viewport.width, viewport.height]})
        assert report['copies'], 'Desktop mirror never samples either controller overlay (VR-only layers are missing)'
        assert len(report['copies']) == 1, 'Only one texture composite for the selected mirror eye'
        copy = report['copies'][0]
        assert copy['source'] == str(source), 'Mirror sampled the wrong controller eye'
        assert (copy['blend_enabled'] and str(rd.BlendMultiplier.One) == copy['src_blend']
                and str(rd.BlendMultiplier.InvSrcAlpha) == copy['dst_blend']), copy
        assert not copy['depth_enabled'], 'Controllers composite above UI without scene-depth rejection'
        assert copy['vertices'] == 6, 'Reuse the rendered texture; no second controller-model pass'
        mirror = next(t for t in textures if str(t) == copy['targets'][0])
        present = [(a, outputs) for a, sources, outputs in reads if mirror in sources and a.eventId > copy['event']]
        assert len(present) == 1, 'Composited mirror must reach the game window'
        present_action, outputs = present[0]
        desktop = outputs[0]
        report['desktop_present_event'] = present_action.eventId
        report['mirror_target'] = str(mirror); report['desktop_target'] = str(desktop)
        # The models are fully opaque in their interior. Find real covered
        # pixels, then require their copy to alter the mirror and window.
        controller.SetFrameEvent(copy['event'], True)
        texture = textures[source]
        pixels = bytes(controller.GetTextureData(source, rd.Subresource()))
        assert len(pixels) == texture.width * texture.height * 4, 'Expected RGBA8 controller target'
        width, height = texture.width, texture.height
        mirror_tex, desktop_tex = textures[mirror], textures[desktop]
        source_aspect = mirror_tex.width / mirror_tex.height
        monitor_aspect = desktop_tex.width / desktop_tex.height
        uv = [0., 0., 1., 1.]
        if source_aspect < monitor_aspect:
            uv[3] = source_aspect / monitor_aspect; uv[1] = (1-uv[3])/2
        else:
            uv[2] = monitor_aspect / source_aspect; uv[0] = (1-uv[2])/2
        candidates = []
        for y in range(height//5, height*9//10, 8):
            for x in range(width//8, width*7//8, 8):
                offset = (y*width+x)*4
                if pixels[offset+3] < 254 or max(pixels[offset:offset+3]) < 8: continue
                u, v = (x+.5)/width, (y+.5)/height
                if not (uv[0]+.01 < u < uv[0]+uv[2]-.01 and uv[1]+.01 < v < uv[1]+uv[3]-.01): continue
                candidates.append((x, y, u, v))
            if len(candidates) >= 12: break
        assert candidates, 'Need opaque controller pixels inside the actual desktop crop'
        samples = []
        for x, y, u, v in candidates[:12]:
            mx, my = int(u*mirror_tex.width), int(v*mirror_tex.height)
            history = controller.PixelHistory(mirror, mx, my, rd.Subresource(), rd.CompType.Typeless)
            changed = [h for h in history if h.eventId == copy['event'] and not h.depthTestFailed
                       and max(abs(a-b) for a, b in zip(h.preMod.col.floatValue, h.postMod.col.floatValue)) > 1/255]
            if not changed: continue
            dx = int((u-uv[0])/uv[2]*desktop_tex.width)
            dy = int((v-uv[1])/uv[3]*desktop_tex.height)
            controller.SetFrameEvent(present_action.eventId, True)
            rgba = list(controller.PickPixel(desktop, dx, dy, rd.Subresource(), rd.CompType.Typeless).floatValue)
            source_rgba = list(controller.PickPixel(source, x, y, rd.Subresource(), rd.CompType.Typeless).floatValue)
            assert max(abs(a-b) for a,b in zip(rgba[:3],source_rgba[:3])) < .07, (rgba,source_rgba)
            samples.append({'source': [x,y], 'mirror': [mx,my], 'desktop': [dx,dy],
                            'source_rgba': source_rgba, 'desktop_rgba': rgba})
            if len(samples) >= 3: break
        assert samples, 'No controller pixels reached the desktop window'
        report['pixel_samples'] = samples
        controller.SetFrameEvent(present_action.eventId, True)
        for label, resource in [('mirror', mirror), ('desktop', desktop)]:
            save = rd.TextureSave(); save.resourceId = resource; save.destType = rd.FileType.PNG
            path = Path(job['result']).with_name(label+'.png'); controller.SaveTexture(save, str(path))
            report[label+'_image'] = str(path)
        report['passed'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
    finally:
        if controller: controller.Shutdown()
        if capture: capture.Shutdown()
        Path(job['result']).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report['passed']


if __name__ == '__main__':
    import renderdoc as rd
    job = json.loads(Path(os.environ['FOX_CONTROLLER_MIRROR_GPU_JOB']).read_text(encoding='utf-8-sig'))
    os._exit(0 if main(job, rd) else 1)
