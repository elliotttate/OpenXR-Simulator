# Native livery volume pilot

This developer experiment is **Off by default** and compiled only for non-retail
OpenXR builds. It adds real matte sides and a small rear bevel to the livery
footer and currently promoted hover buttons. It does not enable volume on other
menus or change existing artwork, layout, hit rectangles, or physical hover.

## Enable and disable

Open the native livery editor, then send the existing game bridge a
`livery.command` request:

```json
{"op":"ui_volume","mode":4}
```

Use `mode:8` for the other thickness and `mode:0` to disable. Values are
millimeters; only `0`, `4`, and `8` are accepted. These are developer commands,
not saved production settings. Opening the editor normally leaves the pilot Off.

Read `livery.command` with `{"op":"state"}` to inspect `vr.ui_volume`:

- `mode_mm`: selected mode.
- `shells`: linked bodies, bounded by one footer plus two accepted button tiles.
- `mesh_rebuilds`: cumulative shape rebuilds for the current pilot instance.
- `footer_rect`: the actual ImGui footer rectangle in the 1920 × 1080 canvas.
- `rest_lift` and `soft_shadow`: both false in this pilot.

An unchanged layout/preset should keep `mesh_rebuilds` steady while hovering or
moving the head. Switching Off removes the bodies. Closing the menu or leaving
VR clears the pilot and resets its mode; popups and car grabs hide its bodies.

## Rendering scope

Each body has **18 cached triangles**: four side walls, four rear bevel strips,
and a back. The existing front supplies the remaining face. Thickness extends
behind that front, with a rear bevel up to 0.6 mm; there is no new front border or
front lighting. The existing white 3D HUD shader supplies restrained matte color.

The renderer uses the same upright panel anchor and accepted button bounds/lift
as the native UI. It explicitly enables back-face culling and scene depth
testing/writes, then restores those states. During press/return, only the exposed
button depth above the carrier is drawn. Geometry is rebuilt on size or preset
changes; lift changes its transform. The pilot adds no render targets,
compositor layers, shadow maps, or new compiled shaders.

Button fronts retain the current physical hover/press behavior. Always-raised
3 mm resting buttons and soft contact shadows are **deferred**. The panel front
is a world mesh; promoted button fronts remain compositor quads. Continuous
headset motion must still be assessed for face/body registration and compositor
reprojection differences.

## Native capture procedure

1. Use an isolated test profile. Enter livery with a neutral head pose so its
   opening anchor is reproducible. Keep one enabled control, such as STICKERS,
   hovered until its existing front settles at 15 mm.
2. Capture the flat mode first. Keep the same menu, control, and eye pose when
   comparing modes. Save the actual native state alongside each RDC.
3. Capture modes 4 and 8 head-on, then from an oblique **eye position**. A useful
   strong proof pose translates the eye about +1.6 m horizontally and turns its
   gaze toward the original panel, while retaining the original opening anchor.
   Confirm the native eye positions changed and the panel anchor stayed fixed.
4. Capture both eyes. Refresh the same real hover if its input lease expires;
   avoid comparing different hover/press states. Save the genuine screenshots.
5. Switch Off and check zero linked bodies; also check menu replacement, close,
   VR loss, popup suppression, and returning/pressed controls.

Head rotation in place alone does not expose a centered plaque's sides. The
2.2 m-wide footer needs an eye position past an outer edge for a clear vertical
side view; smaller off-center buttons expose their sides with less translation.
This exaggerated simulator pose is a geometry proof, not a comfort recommendation.

## Read-only GPU test

`flatout_livery_volume_gpu.py` runs inside RenderDoc's embedded Python. It reads
a saved capture and state; it never connects to the game or delivers input.
From the simulator repository root, supply your local paths:

```powershell
$renderdoc = '<RenderDoc installation>/qrenderdoc.exe'
$evidence = (Resolve-Path '<evidence directory>').Path
$jobPath = Join-Path $evidence 'volume-job.json'
$job = @{
    capture = (Resolve-Path (Join-Path $evidence '<side capture>.rdc')).Path
    native_state = (Resolve-Path (Join-Path $evidence '<native state>.json')).Path
    result = Join-Path $evidence 'volume-gpu-proof.json'
    thickness_mm = 4
}
$job | ConvertTo-Json | Set-Content -LiteralPath $jobPath
$env:FOX_VOLUME_GPU_JOB = $jobPath
try {
    Start-Process -FilePath $renderdoc -ArgumentList @(
        '--python', ('"{0}"' -f (Join-Path (Get-Location).Path 'tests/flatout_livery_volume_gpu.py'))
    ) -WindowStyle Hidden -Wait
} finally {
    Remove-Item Env:FOX_VOLUME_GPU_JOB
}
```

Read the result JSON; the RenderDoc process exit alone is not the pass criterion.
Repeat with `thickness_mm:8` and the corresponding saved capture/state. For an
independent single-button check, add these fields to the job:

```json
{"button":"STICKERS","lift_meters":0.015}
```

That capture must contain one promoted button, settled at the specified lift.
The default check targets the footer. The flat native control must fail with
zero matching shell draws before the implementation is accepted.

## Acceptance criteria

- Actual GPU input contains 54 vertices per shell, with front, rear shoulder,
  and rear depth rings; its rear depth is the selected 4 or 8 mm.
- The submitted object transform and dimensions register to the native front,
  rather than merely producing unrelated 3D geometry elsewhere.
- Both the native panel and actual submitted GPU body have world up `(0, 0, 1)`.
  Matching a consistently pitched CPU anchor is insufficient.
- Both eye draws use their actual native eye positions and distinct outputs.
  Back-face culling and scene depth testing are enabled.
- Post-VS projection and pixel history show actual written body pixels outside
  the original flat front silhouette. Metadata or a draw marker alone is
  insufficient.
- Export and compare the native 1920 × 1080 panel RTT and 2560 × 512 button
  atlas at matched UI states. Their dimensions, colors, artwork, and alpha must
  remain unchanged between presets. This checks source artwork; it does not
  assert identical whole-scene screenshots across different camera poses.
- Visually inspect head-on and oblique images, clipping, pressed/returning
  controls, and face/body alignment during motion. Retain normal click,
  raised-edge, cancellation, and disabled-control behavior.
- Check stable cached shapes at idle and complete removal when disabled or
  closed. Measure native Off/4/8 CPU/GPU timing at matched scene/input states,
  with capture/replay activity excluded from the timing window. Geometry counts
  and software-probe timings are not headset performance measurements.
