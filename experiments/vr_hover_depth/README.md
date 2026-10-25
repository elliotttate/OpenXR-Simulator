# Real stereo depth for hovered VR buttons

This directory contains an isolated OpenXR experiment and tests for the first
native Fox integration. The standalone probe does not change game files or saves.
Both paths demonstrate a button physically moving toward the camera while
preserving its physical width and height.

The native slice currently covers shared dialog actions on the HUD composition
path. It uses the GPU HUD renderer and a separate 1024 x 128 atlas, with at most
two transient BOTH-eye button layers. Enable the experiment before initialization
with `Engine.Renderer.VR.ExperimentalHudButtonDepth = true`; it remains off by
default while integration coverage is completed.

Native tests on RC `00c4999b0` plus the UI changes verify a 30 mm normal lift,
about 28.1 mm approach toward the camera, unchanged button size, both hands,
rapid focus changes and return, raised-face top-edge clicks whose rays miss the
base rectangle, one held-trigger activation, same-ID retry and immediate layer
cleanup. These are native game checks, separate from the standalone probe.

RenderDoc confirms that the source label disappears from the base HUD while the
GPU atlas contains the isolated button: the captured source region changes from
563 bright label pixels to zero, and the atlas contains 370 bright label pixels
inside the 200 x 80 tile. The guard remains RGBA zero. Both final composed eyes
have matching button-plate color. This validates rendering as well as submitted
layer metadata; it does not constitute a headset performance measurement.

## Result

The native D3D11 probe uses **one atlas swapchain and two actual quad composition
layers**. Both layers use `XR_EYE_VISIBILITY_BOTH`. The first displays the menu;
the second displays only the button from another atlas rectangle. Hover changes
the second quad's metric pose by 30 mm toward the viewer. It does not change the
button's physical width or height.

The actual captured poses were:

| State | Button center in LOCAL, meters | Distance from camera center |
| --- | --- | --- |
| Idle | `(0.315000, 1.700000, -1.200000)` | 1.240655 m |
| Hover | `(0.315000, 1.700000, -1.170000)` | 1.211662 m |
| Pressed | 6 mm in front of the base plane | Returns toward the plate |
| Exit | Returns to the original plane | Original placement restored |

The hover therefore reduces camera distance by **28.993 mm**. The difference
from 30 mm is the button's off-center position. At 64 mm IPD, the expected change
in horizontal tangent disparity is 0.00136752. The final screenshot measurement
was 0.00189609, within its explicitly computed 0.000703156 tolerance (0.65 image
pixel at the actual 4096 x 2028 capture size). Both eyes' asymmetric FOV and
viewports are used in this comparison. The matched flat control measured only
0.00006666 and fails the normal stereo gate.

The final native right-hand and left-hand runs each pass five groups: metric
approach and stereo pixels; all four visible raised edges; one click with one
press/release and a same-ID retry; hover exit; and standing-height placement that
remains stable when the head moves. The reduced-motion run also passes, retaining
hover color and click feedback while deliberately disabling depth movement.

Evidence is under the ignored `build-ui-automation/hover-depth-evidence/` folder:

- `native-red-fiducial-stereo/`: failing screenshot gate before implementing motion.
- `native-red-highres-flat-control/`: reproducible high-resolution flat control.
- `native-red-raised-edge/`: fixed-plane-only collision loses the visible raised edge.
- `native-green-final-right/`, `native-green-final-left/`: final native passes.
- `native-green-reduced-motion/`: accessible alternative pass.
- `review.html`: before/hover/return viewer with unmodified native stereo images.

Earlier `native-red-*` harness-calibration runs and the lower-resolution initial
green are preserved, but are not the final precision proof. Each run has its own
directory; no red result was replaced with a green one.

## What Fox does today

Source inspection found two distinct transports:

- `Flatout/Code/Sources/VR/FOVRMenuRTT.cpp` renders a 1920 x 1080 menu texture onto
  a world-space mesh on the projection layer, after post-processing. This already
  gives the whole menu stereo depth and permits scene rays to sort over it.
- `PlayAll/Code/Runtime/Win32/PlayAll/Drivers/Game/XrRuntime.cpp` submits the HUD
  and some modal menus using a real `XrCompositionLayerQuad`. Its quad is monoscopic
  texture content positioned in 3D; the compositor still projects that surface
  differently for each eye.
- `VRMenuPointer::UpdateHoverGrow` currently scales HUD nodes by 1.1 and changes
  their 2D drawing order and shadow. Those operations cannot move an individual
  button closer in stereo when flattened into either menu texture.

The startup popup also exposed a separate anchor issue: the unoverridden HUD
description uses `(0, 0, -HudDistance)` but is submitted in LOCAL, rather than VIEW.
A standing head at y=1.7 therefore sees a panel centered at y=0. The prototype
locates the VIEW origin in LOCAL once at spawn, places the panel 1.2 m ahead at
that height, and keeps the resulting LOCAL pose stable. Its regression moves the
head to `(0.12, 1.82, 0.10)` and verifies the panel stays put. Production integration
should use the existing menu anchor when one exists; otherwise snapshot a
head-relative, level pose on modal open/recenter and share it with the pointer.

## Recommended integration

See [the motion direction](MOTION.md) for the popup fade audit, proposed timing,
interruption rules and native verification of modal prompt suppression.

Use a small, shared **button surface renderer**, with transport matching the menu:

| Technique | Real binocular approach? | Fit for Fox |
| --- | --- | --- |
| Another compositor quad for the hovered button | Yes | Best incremental choice for current HUD quad menus; preserves compositor text sampling |
| Isolated button tile on a 3D mesh | Yes | Best incremental choice for FOVRMenuRTT and existing spatial menus; no extra composition layer |
| Convert every menu to independent 3D widgets | Yes | Larger migration; requires preserving Flash/ImGui clipping, text, lifetime, and all menu behavior |
| Scale/shadow/draw-order change within the original texture | No | Useful color/focus feedback, but cannot satisfy physical forward movement |
| Add a depth extension to the original flat menu | Not by itself | Projection depth metadata does not turn flat color artwork into separately positioned buttons |

1. Give eligible `VRBeamTarget`s a weak visual subtree/tile reference and stable
   semantic ID. Start with the native shared action buttons, whose plate and label
   already form a known visual unit. Do not initially extract arbitrary nested
   Flash symbols or livery canvas gestures.
2. Render the base menu without the lifted button subtree, retaining the correct
   underlying backplate. Render the complete button, label, icon and local clipping
   into an isolated transparent atlas tile. Do not copy the final rectangular menu
   crop: that would lift background art and adjacent controls. Do not leave a
   second original button underneath, which would create a stereo ghost.
3. Compute the isolated surface from the unchanged canvas rectangle. For base pose
   `(R, t)`, canvas `(W, H)`, physical menu size `(wm, hm)`, rectangle `(l,t0,r,b)`
   and lift `d`, its local center is
   `(((l+r)/(2W)-0.5)*wm, (0.5-(t0+b)/(2H))*hm, d)`.
   Transform that center through the base pose; use the same orientation and
   physical size `((r-l)/W*wm, (b-t0)/H*hm)`.
4. For a composition-layer menu submit base first, then the isolated button quad;
   use BOTH eyes, alpha consistent with the source renderer, and a valid padded
   atlas rectangle. For the world-space path, render the same tile as separate
   after-post-process geometry in both eye passes. Reuse Fox's existing engine/XR
   world conversion rather than assuming engine units equal meters.
5. Preserve the immutable source rectangle for acquisition and layout. While the
   same target is raised, also intersect its visible front face and map the hit
   into that same target ID. The native edge red showed that base-only collision
   makes some visible edges dead because hand-to-button parallax differs from the
   camera projection. The union passed all four raised-edge tests for both hands.
6. Keep click/drag ownership in `VRMenuPointer`: one deliberate active hand owns
   selection, spare-ray candidate checks remain read-only, and held-slider capture
   remains latched through release. Both rays may show their own reticle, placed
   at the surface each actually intersects. A reticle flattened into the base HUD
   must be removed/repainted on the raised tile when that face occludes it.
7. Reserve two transient tile slots: an old target can return while a new one
   rises. This also bounds a possible two-hover visual policy without allocating
   one compositor layer per button. Modal exclusivity invalidates underlying
   surfaces immediately. Weak references and screen revisions must cancel
   animation without touching a destroyed menu.

Motion communicates readiness and commitment: 30 mm hover, 6 mm press, no extra
scale animation, no bounce. The prototype uses interruptible exponential approach
with 45 ms hover, 25 ms press and 35 ms exit time constants (about 135/75/105 ms
to reach 95%). Production should tune roughly 15–30 mm in a headset at the actual
menu distance and expose reduced motion without erasing hover/press feedback.
Do not move the layout, add actions on hover, or delay clicking until animation
finishes.

## MCP targeting contract

The existing screenshot geometry names quad surfaces by submitted layer index;
it does not know a game's semantic button IDs. `CompatibleCapture` intentionally
rejects a changed target quad pose. That is appropriate for an opaque moving
surface, but aiming at an animated button's own quad can invalidate a screenshot
during an otherwise valid hover/press animation.

The prototype's center-click path therefore explicitly targets the stable base
`surface_id=0`, while the native application preserves the same button ID and
also accepts its moving face. The four raised-edge tests use a fresh settled
front-face capture. Fox's current test helpers that project through the first
BOTH-eye base surface can retain their stable source-plane route during lift.

For general screenshot targeting, keep visual surface and interaction carrier
separate. An app bridge can declare the stable parent plane, button ID, original
canvas bounds and menu revision alongside the moving visual surface. Use the
visual face to reconstruct the exact visible point, and the declared stable
carrier/revision to validate the action; preserve that button identity across
lift, press and return. A menu transition or destroyed target still invalidates
the request. Do not globally relax camera/pose safety, infer parenthood from
overlapping quads, or treat layer-array index as a durable widget ID. Without
trusted app metadata the generic MCP should continue requiring a fresh capture.

Add a production end-to-end case that captures during a rise, advances the visual
pose before click, and still activates the same declared button once; its paired
negative changes the menu revision and must deliver no click. Cover raised-face
edges from both natural hand origins, background gaps, an outgoing/entering pair
of tiles, and screenshot retries while the press animation is moving.

## Budget and limitations

For menus using the HUD composition layer, Fox submits projection plus HUD, so
one active hover quad makes three composition layers; two transient slots make
at most four. RTT menus instead use projection without the HUD quad. Query the actual
`maxLayerCount` and account for other overlays rather than assuming spare slots.
The experiment's runtime reports 16. A world-space isolated mesh costs no extra
composition slot. Neither approach needs one layer for every visible button.

The experiment uses one 1024 x 1024 RGBA atlas (4 MiB per swapchain image) and one
extra quad. Its per-frame CPU texture upload is test scaffolding, not a shipping
renderer or performance measurement. Production should use the existing GPU HUD
renderer, cached atlas tiles and pose-only animation where content is unchanged.
Measure CPU/GPU/compositor timing on the supported runtimes; count layers,
swapchain images, copies and full-screen overdraw explicitly.

Layer order is not general scene occlusion. A later composition overlay may cover
a nearer scene ray unless depth testing is available and correctly configured.
Adding projection depth metadata alone does not solve per-button extraction or
quad/scene occlusion. The existing world-space mesh path handles scene occlusion
more naturally, but must retain the after-post-process crisp-text path.

The first native integration is experimental; it does not yet promote all menu
controls or the world-space RTT path. Physical-headset comfort, real-controller
jitter, GPU performance and runtime compatibility remain unmeasured. The
screenshot precision is reported, not presented as a submillimeter display
measurement. Before global enablement, cover dialog replacement, recenter/resize,
disabled/loading actions, localized labels, slider ownership and the remaining
menu transports. A production reduced-motion option is still needed: disabling
the experiment currently restores the legacy flat hover treatment. Within the
enabled experimental transport, startup setting
`Engine.Renderer.VR.HudButtonReducedMotion = true` now provides an intentional
flat alternative. Native tests with both hands verify zero travel, visible white
outlines, fixed size, rapid focus/return and one click/retry. There is no public
in-game preference for this experimental setting yet.

## Primary research

- [Khronos: XrCompositionLayerQuad](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerQuad.html): metric pose/size, a flat surface in 3D, eye visibility, and swapchain subimages.
- [Khronos: XrSwapchainSubImage](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrSwapchainSubImage.html): valid atlas region and the possibility of sampling outside it; use guard texels.
- [Khronos: XrFrameEndInfo](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrFrameEndInfo.html): query `maxLayerCount`; exceeding it returns an error.
- [Khronos: XrCompositionLayerDepthInfoKHR](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerDepthInfoKHR.html): depth images attach to projection views, rather than making a quad's image into independent 3D objects.
- [Meta: compositor layers](https://developers.meta.com/vr/documentation/unity/unity-ovroverlay/): crisp UI sampling, layer ordering and scene-depth conflicts, world-locked placement and filtering costs.
- [Meta: hands UI guidance](https://developers.meta.com/vr/design/hands-ui-best-practices/): clear hover/press feedback and avoiding layout changes on hover.
- [Meta: gaze guidance](https://developers.meta.com/vr/design/eyes-best-practices/): keep targeting stable and feedback subtle; do not make the interactive target chase the user. The requested small depth motion should remain cosmetic to the target's identity/layout.
- [Microsoft: mixed-reality buttons](https://learn.microsoft.com/en-us/windows/mixed-reality/design/button): legible focus/press feedback, bounded backplates and distance-dependent target size. Its HoloLens guidance is supporting design research, not a Quest-specific compliance rule.

## Reproduce

From the simulator repository root, with CMake and Visual Studio available:

```powershell
cmake -S experiments/vr_hover_depth -B build-ui-automation/hover-depth-build -A x64
cmake --build build-ui-automation/hover-depth-build --config Release
& ./build-ui-automation/hover-depth-build/Release/vr_hover_depth_model_test.exe
& ./mcp-server/.venv/Scripts/python.exe experiments/vr_hover_depth/test_e2e.py `
  --probe build-ui-automation/hover-depth-build/Release/vr_hover_depth_probe.exe `
  --runtime bin/ui-sweep-2/openxr_simulator.dll `
  --evidence build-ui-automation/hover-depth-evidence/new-right-run --edge-tests
```

Use a fresh evidence directory each time. Add `--hand left` to test the other
hand, `--reduced-motion` for the intentional flat alternative, or `--flat-control`
without `--reduced-motion` to reproduce the failing stereo gate. The probe sizes
only its own preview window offscreen for deterministic pixel precision, never
takes focus, and exits through normal OpenXR teardown via a private stop marker.

To compile the actual production motion helper rather than only the prototype:

```powershell
cmake -S experiments/vr_hover_depth -B build-ui-automation/hover-depth-build -DFOX_SOURCE_DIR="$FoxSource"
cmake --build build-ui-automation/hover-depth-build --config Debug --target vr_hover_depth_production_model_test
ctest --test-dir build-ui-automation/hover-depth-build -C Debug -R hover_depth_production_model --output-on-failure
```

With the rebuilt native game running and a cancelable dialog already open:

```powershell
& ./mcp-server/.venv/Scripts/python.exe tests/flatout_hover_depth_e2e.py `
  --pid $GamePid --data-dir $SimulatorData --evidence $FreshEvidenceDirectory `
  --hand right --motion-lifecycle --raised-edge-top
```

Repeat with `--hand left` and a fresh dialog/evidence directory. Diagnostics are
read-only; hover and click are delivered through actual XR action input.
For the startup reduced setting, replace `--raised-edge-top` with
`--reduced-motion`; that gate requires zero travel and visible flat feedback in
each eye. Keep `--motion-lifecycle` to exercise return and cleanup.
