"""RenderDoc regression: showroom controls must contribute RGB in both eyes.

Run with qrenderdoc --python and FOX_SHOWROOM_VISIBILITY_JOB pointing to a JSON
job containing capture, result and targets (native canvas names/rectangles).
This tests the actual depth-tested scene draws, not just the UI render target.
"""
import json
import math
import os
from pathlib import Path
import struct
import traceback


def inspect(job, rd):
    report = {"passed": False, "capture": job["capture"], "eyes": [],
              "scope": "Actual showroom RTT draw contribution and depth history in both projection eyes"}
    cap = controller = None
    try:
        cap = rd.OpenCaptureFile()
        assert cap.OpenFile(job["capture"], "", None) == rd.ResultCode.Succeeded
        status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
        assert status == rd.ResultCode.Succeeded
        candidates = []
        for texture in controller.GetTextures():
            if (texture.width, texture.height) != (1920, 1080):
                continue
            uses = controller.GetUsage(texture.resourceId)
            reads = [u.eventId for u in uses if u.usage == rd.ResourceUsage.PS_Resource]
            writes = [u for u in uses if u.usage == rd.ResourceUsage.ColorTarget]
            if len(reads) == 2 and len(writes) > 5:
                candidates.append((texture.resourceId, reads))
        assert len(candidates) == 1, candidates
        source, draws = candidates[0]
        report["source"] = str(source)
        for eye, event in zip(("left", "right"), draws):
            controller.SetFrameEvent(event, True)
            pipe = controller.GetPipelineState()
            output = pipe.GetOutputTargets()[0].resource
            viewport = pipe.GetViewport(0)
            mesh = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
            # The existing VRMenu mesh shader emits float4 SV_Position followed
            # by float2 UV. Validate that signature before interpreting bytes.
            signature = pipe.GetShaderReflection(rd.ShaderStage.Vertex).outputSignature
            assert signature[0].compCount == 4 and signature[1].compCount == 2
            assert mesh.vertexByteStride >= 24
            raw = controller.GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset,
                                          mesh.vertexByteStride * mesh.numIndices)
            vertices = [struct.unpack_from("6f", raw, i) for i in range(0, len(raw)-23, mesh.vertexByteStride)]
            vertices = [v for v in vertices if abs(v[3]) > .001 and all(math.isfinite(x) for x in v)]
            assert vertices
            a = vertices[0]
            b = next(v for v in vertices if abs(v[4]-a[4]) < .0001 and abs(v[5]-a[5]) > .1)
            c = next(v for v in vertices if abs(v[4]-a[4]) > .1)
            assert max(v[3] for v in vertices)-min(v[3] for v in vertices) < .001, "Expected upright parallel UI plane"
            det = (b[4]-a[4])*(c[5]-a[5])-(c[4]-a[4])*(b[5]-a[5])
            assert abs(det) > .001

            def pixel(u, v):
                du, dv = u-a[4], v-a[5]
                s = (du*(c[5]-a[5])-(c[4]-a[4])*dv)/det
                t = ((b[4]-a[4])*dv-du*(b[5]-a[5]))/det
                x = a[0]/a[3]+s*(b[0]/b[3]-a[0]/a[3])+t*(c[0]/c[3]-a[0]/a[3])
                y = a[1]/a[3]+s*(b[1]/b[3]-a[1]/a[3])+t*(c[1]/c[3]-a[1]/a[3])
                return int(viewport.x+(x+1)*viewport.width*.5), int(viewport.y+(1-y)*viewport.height*.5)

            checks = []
            for target in job["targets"]:
                left, top, right, bottom = target["rect"]
                if target.get('sample_source_ink'):
                    # A text node's layout box can be mostly transparent. Choose
                    # actual RTT ink first; then still require its RGB change
                    # and passing scene depth history independently in each eye.
                    candidates = [(int(left+(right-left)*x/40), int(top+(bottom-top)*y/8))
                                  for x in range(1, 40) for y in range(1, 8)]
                    ink = [p for p in candidates if
                           controller.PickPixel(source, *p, rd.Subresource(), rd.CompType.Typeless).floatValue[3] > .2]
                    assert len(ink) >= 3, ('Missing source text ink', target, len(ink))
                    points = sorted(set(pixel((x+.5)/1920, (y+.5)/1080) for x,y in ink))
                else:
                    points = sorted(set(pixel((left+(right-left)*x)/1920, (top+(bottom-top)*y)/1080)
                                        for x in (.15, .35, .5, .65, .85) for y in (.25, .5, .75)))
                assert all(viewport.x <= x < viewport.x+viewport.width and viewport.y <= y < viewport.y+viewport.height for x, y in points), (eye, target, points)
                controller.SetFrameEvent(event-1, True)
                before = [list(controller.PickPixel(output, x, y, rd.Subresource(), rd.CompType.Typeless).floatValue) for x, y in points]
                controller.SetFrameEvent(event, True)
                after = [list(controller.PickPixel(output, x, y, rd.Subresource(), rd.CompType.Typeless).floatValue) for x, y in points]
                deltas = [max(abs(a[i]-b[i]) for i in range(3)) for a, b in zip(after, before)]
                changed = sum(d > 1/255 for d in deltas)
                x, y = points[max(range(len(deltas)), key=deltas.__getitem__)]
                history = controller.PixelHistory(output, x, y, rd.Subresource(), rd.CompType.Typeless)
                draw_history = [{"depth_failed": h.depthTestFailed, "discarded": h.shaderDiscarded,
                                 "culled": h.backfaceCulled, "shader_rgb": list(h.shaderOut.col.floatValue)[:3]}
                                for h in history if h.eventId == event]
                passed = changed >= 3 and bool(draw_history) and any(not h["depth_failed"] and not h["discarded"] and not h["culled"] for h in draw_history)
                checks.append({"name": target["name"], "passed": passed, "changed_samples": changed,
                               "samples": len(points), "max_rgb_delta": max(deltas), "history_pixel": [x, y], "history": draw_history})
            image_path = Path(job["result"]).with_name("showroom-"+eye+".png")
            save = rd.TextureSave()
            save.resourceId, save.destType, save.alpha = output, rd.FileType.PNG, rd.AlphaMapping.Preserve
            controller.SaveTexture(save, str(image_path))
            report["eyes"].append({"eye": eye, "event": event, "output": str(output), "image": str(image_path), "checks": checks})
        report["passed"] = len(report["eyes"]) == 2 and all(c["passed"] for e in report["eyes"] for c in e["checks"])
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if controller is not None:
            controller.Shutdown()
        if cap is not None:
            cap.Shutdown()
        Path(job["result"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report["passed"]


if __name__ == "__main__":
    job = json.loads(Path(os.environ["FOX_SHOWROOM_VISIBILITY_JOB"]).read_text(encoding="utf-8-sig"))
    import renderdoc as rd
    os._exit(0 if inspect(job, rd) else 1)
