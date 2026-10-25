# Foreground VR beam composition

Status: read-only production plan, 2026-10-25. No rendering, interaction or
simulator source changes are included in this document. Wreck Run is excluded.

## Confirmed native failure

The valid native evidence is
`build-ui-automation/flatout4-evidence/rc00c-foreground-pointer-red-r2/`.
`tests/flatout_pointer_visibility_e2e.py` ran against PID 4024 on the Challenge
reader (`INSANITY`). It took a fresh screenshot, delivered an actual hand-origin
XR hover, then captured both eyes immediately while that aim lease was live.
Read-only native diagnostics confirmed the expected hovered action and no click.

| Hand | Left-eye cursor pixels | Right-eye cursor pixels | Foreground beam samples in each eye |
| --- | ---: | ---: | --- |
| Left, cyan | 16 | 15 | 0 at both sampled positions |
| Right, amber | 15 | 14 | 0 at both sampled positions |

The samples are at 90% and 95% of the actual hand-to-hit segment, physically in
front of the opaque UI. The surface cursor is visible for both hands in both
eyes; the foreground beam is covered. This evidence calls for a beam composition
fix. It does not justify a Challenge reticle change.

The earlier `rc00c-foreground-pointer-red/` attempt stopped on a harness
`TypeError` and is not a valid rendering red. Preserve it separately.

Native RenderDoc pixel history independently confirms the cause in
`latest-rc-ui-integration/foreground-beam-alpha-order-verification.json`:
the left eye contains cyan beam pixels at event 20039, then the opaque menu
overwrites them at 20083; the right eye does the same at 20053 then 20088.
This occurs at both sampled points before the hit. The final hit cursor remains
cyan in both eyes. The capture is `rc00c-foreground-beam-red_frame67670.rdc`;
the exported final image and original GPU history are beside the verification.

## Actual source trace

Paths below are relative to the active Fox checkout unless marked Simulator.
Line numbers describe the inspected source and may move.

- `Flatout/Code/Sources/VR/VRMenuPointer.cpp:2483`, `CreateBeamVisual()` creates
  separate `HudList3D` line/dot lists using `HudProjectionMode::E_NONE`, front
  pass and list layer 2. `DrawBeam()` at 2791 builds real world matrices from
  the controller origin and direction to the hit distance. The line begins
  50 mm out of the hand and is 2 mm wide for a controller. The two hands have
  distinct colors and matrices. This is scene geometry, not a quad-layer beam.
- `PlayAll/Code/Runtime/Common/PlayAll/Common/Graphics/DeferredRenderLoop.cpp:875`
  draws HUD 3D into each eye projection after post-processing. The HUD swapchain
  target switch happens afterward, at 889. A front-pass/list-order change can
  alter ordering within this projection; it cannot put these pixels over a
  later OpenXR composition layer.
- `PlayAll/Code/Runtime/Win32/PlayAll/Drivers/Game/XrRuntime.cpp:1003`,
  `EndFrame()`, submits the scene projection first, then the visible HUD quad,
  then transient physical-button quads. The quads can overwrite foreground
  beam pixels regardless of their metric distance. This follows core OpenXR
  layer ordering, which uses successive composition rather than universal
  inter-layer scene-depth sorting. [Khronos compositing specification](https://registry.khronos.org/OpenXR/specs/1.0-khr/html/xrspec.html#compositing)
- `VRMenuPointer.cpp:322`, `UpdateReticle()`, intentionally supplies a separate
  cursor within the HUD; the world dot is suppressed in HUD mode at 2869.
  Physical-button cursors are placed inside the promoted tile by
  `VRHud::SetPhysicalReticle()` in `VRHudButtonDepth.inl`.
- `MessageBoxManager.h:90` gives `MESSAGEBOX_ROOT` deferred layer 2000 as well
  as depth -2000. `HudNode::RenderChildren()` at 601 adds the parent's deferred
  layer to descendants, and `RenderDefferedLayers()` at 806 flushes that queue.
  Consequently, comparing its depth with `ChallengeReader` depth -99980 alone
  cannot establish final cursor order. The native pixels already show that
  these Challenge cursors work.
- `OpenXR.cpp:1083`, `ComposeMirrorEye()`, independently composites the HUD and
  physical tiles over the eye texture. A headset rendering fix must update this
  mirror path too; changing only the desktop mirror would leave the HMD broken.

## Preferred increment: physical ribbon compositor quads

The user explicitly requires the rays themselves to move to quad layers. Use
one narrow, metric `XrCompositionLayerQuad` per visible hand, oriented **along**
the hand-to-hit segment and submitted after the HUD and promoted buttons. Its
plane is not parallel to the menu: points along it have their actual controller
ray depths. This retains true stereo geometry while fixing composition order.
It supersedes this document's earlier additional-projection recommendation.

There is a directly inspectable OpenXR precedent. WayVR commit
`abafddefd82e3263a48cdbe14ec64d8bc6ee75da`,
`wayvr/src/backend/openxr/lines.rs`, computes a controller-to-hit midpoint,
orientation and length, then submits a 2 mm by actual-length quad with
`EyeVisibility::BOTH`. `openxr/mod.rs` assigns its lines the last composition
order. Its line texture is only 1 by 1 pixels; the metric pose and size create
the spatial ray. This is a research pattern, not code to copy from WayVR's GPL
implementation. Fox can independently use its existing endpoints and width.
[WayVR line source](https://github.com/wayvr-org/wayvr/blob/abafddefd82e3263a48cdbe14ec64d8bc6ee75da/wayvr/src/backend/openxr/lines.rs),
[WayVR submission order](https://github.com/wayvr-org/wayvr/blob/abafddefd82e3263a48cdbe14ec64d8bc6ee75da/wayvr/src/backend/openxr/mod.rs)

### Geometry and coordinate contract

Use the current calibrated per-hand ray and hit distance from `DrawBeam()`.
Let `A = origin + direction * .05` and `B = origin + direction * hitDistance`;
the existing no-hit length is 1.5 m. Build a proper rotation with local +X
across the ribbon, +Y along `normalize(B-A)`, and +Z as `X cross Y` facing the
eye midpoint. The center is `(A+B)/2`, width .002 m and height `length(B-A)`.
Reject degenerate lengths and choose a deterministic orthogonal fallback when
the beam and eye direction are nearly collinear. The OpenXR plane is centered
in its local XY axes, has its front normal along +Z, and is visible only from
the front; `BOTH` does not make it double-sided.
[Khronos quad convention](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerQuad.html)

Do not extract a quaternion directly from Fox's `mLine`: its I and J columns
include half-length/half-width scale, and the I=direction, J=side, K=side-cross-
direction ordering is reflected. Use the unscaled right-handed basis above.
Submit pose in the runtime's LOCAL reference space, by applying the inverse of
`m_mBeamCamera` to the world endpoints/directions, or by consuming the matching
calibrated LOCAL per-hand state. `DrawControllerBeams()` currently swaps world
ray fields for the spare hand, but not `m_vLocalRayOrigin/Dir`; use that hand's
`HandAim.localOrigin/localDirection`, or convert the already-swapped world ray.
Do not accidentally draw both quads from the active hand's LOCAL ray.

Offline math against the valid red's delivered rays verifies that this basis
is proper and front-facing for both eyes in that case: left length .745470 m,
right length .685712 m, determinant 1, and front-normal dot products to the two
eyes respectively (.510149, .534714) and (.549599, .520280). This is only a
geometry check; native compositor pixels must still prove visibility, including
near-collinear rays and head/IPD variations that could expose a back face.

### Bounded production changes

These are proposed contracts, rather than claims that new APIs already exist.

1. `VRMenuPointer.h/.cpp`: publish two current-frame LOCAL ray descriptors with
   valid flag, endpoints, width, color and hand identity. Keep current immediate
   click callbacks, ownership, fixed source rectangle and raised-face union.
   Clear descriptors when input is suppressed, tracking/focus is lost or menus
   are replaced. Suppress duplicate scene lines only after the quad route is
   available for that frame. Keep the visible surface cursors unchanged.
2. `OpenXR.h/.cpp`: own a tiny GPU color texture/atlas and render it after
   graphics initialization, following the existing atlas lifetime. Do not embed
   an eagerly constructed `HudRenderer` before the graphics render layer exists.
   Keep ribbon resources separate from the legacy five-element eye/HUD/depth
   swapchain array. Acquire/wait, render, release and destroy through the normal
   runtime lifecycle. Consume descriptors once per frame; no delayed callbacks
   or stale beam can survive a menu replacement.
3. `XrRuntime.h/.cpp`: submit at most two `BOTH` quads after HUD/button tiles,
   using current LOCAL pose, actual metric size, matching source subrect and
   correctly premultiplied color/alpha. Keep descriptor storage alive through
   `xrEndFrame()`. The headset, capture and mirror must consume the same
   descriptors. A ribbon is visual data and must not become an input target.
4. Reserve two ray layers before assigning physical-button slots. The maximum
   ordinary HUD frame becomes scene + HUD + two tiles + two rays = **six**
   layers. `GetHudButtonSlotCount()` currently reserves only scene + HUD.
   Handle constrained `maxLayerCount` deliberately, without submitting too many
   layers or silently presenting a false success. Texture sharing reduces
   swapchains, not composition-layer count.
   [Khronos frame layer limit](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrFrameEndInfo.html)
5. `OpenXR::ComposeMirrorEye()`: draw the same ribbon quads after the HUD and
   tiles. Its camera is already LOCAL: `GetSubmittedEyeFov(iEye)` plus
   `XrPoseToMatrix(GetPose(iEye))`. Transform each metric corner
   `(+/-width/2, +/-length/2, 0)` by the ribbon LOCAL pose and draw it with this
   eye camera and matching atlas UV. Do not transform it back through the game
   camera a second time. Move the ribbon loop outside the visible-HUD-only block
   if ribbons are also enabled on scene RTT menus. Preserve alpha, viewport,
   asymmetric FOV and current upscale/output texture selection.

Last-layer order deliberately makes the visible hand-to-UI segment foreground.
It does not supply universal world occlusion. End the segment at the actual
nearest valid hit, including a raised face; do not continue it behind the menu.
Audit the car/world and non-UI no-hit policy independently. The existing scene
ray can remain the validated transport for contexts outside this UI scope until
their quad route and suppression/lifecycle are tested. Do not describe a
parallel UI-plane stripe or desktop-only overlay as this physical ribbon.

## Simulator and screenshot-targeting prerequisites

The preferred route needs no second projection. Simulator
`src/runtime.cpp:7258` currently draws projections then quads, preserving the
relative quad order. Scene, HUD, tiles, then ribbon quads fit that path.
`renderQuadLayer()` at 6558 already projects metric planes into each eye;
native D3D11/D3D12 fixtures must confirm thin front-facing quads and alpha.

The current targeting path does need explicit classification. Simulator
`src/ui_automation_runtime.h::BuildUiFrame()` puts every submitted quad in
`surfaces`, using its composition index as ID.
`mcp-server/ui_targeting.py::resolve_target()` examines those surfaces in reverse
composition order. A last-submitted ribbon can therefore intercept a screenshot
pixel intended for the menu; subsequent ribbon movement may also appear to
invalidate that capture. Thinness/aspect ratio is not a sound exemption, since
legitimate UI may be narrow. OpenXR has no standard quad interactivity bit.

Add explicit, session-scoped decorative identity at an agreed app/runtime
boundary, bound to the actual swapchain/subrect or equivalent stable resource
identity. Capture evidence must retain the real ribbon pose/size and order,
while automatic target resolution skips a known decorative ribbon. This is a
required new contract, not an already available `role` field. Debug-utils names
are not an existing solution here: Fox does not currently enable that extension,
and the simulator does not implement the object-name entry point.

An explicit stable base `surface_id` already provides a useful native-test
carrier. Its compatibility checks compare that selected base surface, so added
moving ribbons need not invalidate a valid base capture. Keep all stale-camera,
selected-surface and prepress checks. Also prove normal automatic screenshot
targeting still chooses the menu when a ray crosses the selected pixel.
Update physical-button tests' raw extra-layer counts to distinguish identified
tiles from identified decorative rays; do not weaken them by ignoring arbitrary
small quads. Retain the hold/idempotency and world-surface regression fixtures.

## Roboquest comparison, inspected without changing its checkout

The existing local refs in `Flat2VRStudios/RoboquestVR` were inspected with
`git show` only; the active AndroidXR checkout was not changed or fetched.

| Ref | Inspected commit |
| --- | --- |
| `origin/ui-compositor-layers` | `f4f307a1c2317748c48639aaf0af6fed69f5d892` |
| `origin/ui-compositor-layers-tuned` | `33cc35617c8c411dbc7d40a18869f1fd21d27995` |
| `origin/compositor_layers` | `a4d8f8e9bccdc3eb1a15a8cb2ab85e6c7e60bd8e` |

At `f4f307a1`, `Source/RoboQuest/Private/GameSystem/RQMenuLayerSubsystem.cpp`,
`ScanForMenuWidgets()`/`SyncPair()`/`DemotePair()`, promotes known plane widgets
to `URQStereoLayerComponent` world-locked layers. It keeps the widget collider,
turns off main-pass rendering to prevent a duplicate, keeps its RT painting,
and updates texture, visibility and pivot offset. The pair uses weak ownership;
hidden/destroyed widgets cannot leave the last frame floating. The pivot
correction is `(0, RTwidth*(.5-pivotX), RTheight*(.5-pivotY))`; component scale
is folded into layer size. A 180-degree yaw reconciles Unreal's widget/layer
facing convention. These are useful lifecycle and no-ghost patterns for Fox,
not OpenXR axis conventions to copy directly.
[Roboquest menu promotion](https://github.com/Flat2VRStudios/RoboquestVR/blob/f4f307a1c2317748c48639aaf0af6fed69f5d892/Source/RoboQuest/Private/GameSystem/RQMenuLayerSubsystem.cpp)

`Source/RoboQuest/Private/Character/Character_Player.cpp:20044-20232` separates
`UpdateHUDCompositorLayer()`, `ActivateHUDCompositorLayer()`,
`DeactivateHUDCompositorLayer()` and `SyncHUDCompositorLayer()`. It keeps the
collision widget as the source of input, uses face-locked gameplay HUD versus
world-locked menu pose, rebinds recreated RTs, freezes physical size while changing
RT density, and demotes during loading to avoid two independently presented
loading overlays. The tiny subclass setter in
`Public/Component/RQStereoLayerComponent.h` calls `MarkStereoLayerDirty()` when
lock type changes. Fox similarly needs current content/pose/visibility ownership;
promoting pixels must not replace its input callbacks.
[Roboquest HUD source](https://github.com/Flat2VRStudios/RoboquestVR/blob/f4f307a1c2317748c48639aaf0af6fed69f5d892/Source/RoboQuest/Private/Character/Character_Player.cpp),
[Roboquest layer setter](https://github.com/Flat2VRStudios/RoboquestVR/blob/f4f307a1c2317748c48639aaf0af6fed69f5d892/Source/RoboQuest/Public/Component/RQStereoLayerComponent.h)

The same player source at 2180-2205 updates both `UWidgetInteractionComponent`
world transforms from each hand's aim, with mirrored-model socket selection and
opposite-sign yaw offsets. At 20350-20411 the press/release methods select the
active hand, set focus for the hovered non-HUD widget, then call
`PressPointerKey(LeftMouseButton)`/`ReleasePointerKey(LeftMouseButton)` and emit
`OnDelegatePointerReleased`. Scroll uses the existing interaction component.
`Private/Character/VRPointerInteraction.cpp` supplies object hover/unhover and
interaction callbacks, not inspectable compositor beam geometry. The tuned
branch's change to that file caches a class-interface query; it does not add a
quad beam. These distinct interaction layers should not be confused.

The `compositor_layers` tip explicitly changes the Quest UI laser from additive
to opaque, but its changed laser material, Niagara system and `BP_APlayerVR`
are binary `.uasset` files represented by LFS pointers in Git. No ray-layer pose
or mirror implementation is proven by these source files or the branch name.
The inspected C++ sets a spectator texture from the HUD RT for loading; it does
not establish a full per-eye compositor mirror. Therefore use Fox's verified
LOCAL-corner/per-eye-FOV math above rather than claim Roboquest mirror parity.
[Roboquest laser-material commit](https://github.com/Flat2VRStudios/RoboquestVR/commit/a4d8f8e9bccdc3eb1a15a8cb2ab85e6c7e60bd8e)

## Larger alternatives

**Transparent late stereo projection** can render arbitrary interaction meshes
after the HUD, but costs a new two-eye render target, correct alpha and an extra
layer. It is no longer the preferred increment given the user's quad requirement
and the smaller ribbon route. If later needed, the simulator must first composite
mixed layer types in submitted order, clear/present only once, honor projection
alpha in both D3D11/D3D12, and resolve its `BuildUiFrame()` second-projection
ambiguity without weakening capture validation. A scene/quad/transparent-stereo-
projection native probe must prove order, alpha and both-eye content.
[Khronos projection layer](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerProjection.html)

**UI underlay with a depth-tested zero-alpha mask** is an official Meta sample
pattern for integrating layers with scene occlusion. Its current documentation
describes a scene-depth-writing mask that reveals the underlay. The pinned
`ZeroAlpha.shader` outputs zero RGBA, not just a zero alpha component; this is
not a one-line order change. Fox's present opaque projection (`layerFlags=0`)
would need source-alpha composition, accurate masks at base and raised planes,
and preserved premultiplied RGB/alpha through post-processing/upscale. A front
ray must keep its color and alpha while the behind-UI scene is masked. Existing
raised overlays, desktop mirror and simulator mixed ordering also need a
consistent route. The documentation is updated August 2026, but the inspected
pinned project's Core package is SDK 69; do not conflate those versions. This is
a larger general-occlusion architecture, not the smallest quad-ray fix.
[Meta compositor-layers sample](https://developers.meta.com/vr/documentation/unity/unity-sample-compositor-layers/),
[Pinned zero-alpha shader](https://github.com/oculus-samples/Unity-CompositorLayers/blob/556ceaad06cc63f9a9dfa3ea3b69b286e4677196/Assets/CompositorLayers/Shaders/ZeroAlpha.shader)

**Unified late UI geometry in the scene projection** is portable and avoids
an extra composition layer. The existing `ComposeMirrorEye()` already contains
metric textured-quad drawing that could be factored into a common renderer.
To affect the headset, composite the current HUD texture, raised tiles and
3D pointer into each actual submitted eye target before `Flip()` releases it,
then suppress duplicate HUD/tile submission for that transport. This retains
physical button depth and needs no multi-projection simulator exception.
However it changes UI sampling from compositor quads to eye resolution, requires
current-frame HUD readiness and consistent depth/alpha handling, and must respect
upscaled eye output targets. It is a larger UI transport change and a reasonable
constrained-layer fallback or later unification, not a mirror-only patch.

**`XR_FB_composition_layer_depth_test`** can depth-test composition layers when
supported. Fox currently requests `ALWAYS` with depth writes disabled on the HUD;
its existing `XR_KHR_composition_layer_depth` metadata alone does not change
that. Moreover `HudRenderer::Render()` disables depth writes for the present
beam, so the scene's submitted depth does not contain this beam. A viable FB
route needs correctly mapped beam depth, projection depth writes, appropriate
HUD/tile comparisons and actual runtime support. It also needs mirror and
simulator semantics. Keep it as an optional optimization, not the universal fix.
[Khronos FB depth testing](https://raw.githubusercontent.com/KhronosGroup/OpenXR-Docs/main/specification/sources/chapters/extensions/fb/fb_composition_layer_depth_test.adoc),
[Khronos projection depth metadata](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerDepthInfoKHR.html)

## Native acceptance gates

- Preserve `flatout_pointer_visibility_e2e.py` and the valid `red-r2` evidence.
  Both hand colors must appear at the independently projected foreground sample
  points in both eyes, while the existing surface-cursor checks still pass.
- Capture must expose two real decorative `BOTH` quad descriptors with the
  hand-specific metric center, orientation and length. The long axis must follow
  the controller-to-hit segment, not the menu plane. Prove that the prior scene
  line is suppressed for this route, with no second overlapping beam.
- Extend with several points along each metric beam and a head translation.
  Their measured stereo coordinates must follow their own depth, rather than
  collapse to the UI-plane disparity. Use capture FOV/viewport and explicit
  pixel tolerances; current predicted-point color counts alone do not establish
  the entire beam's stereo geometry.
- Exercise near-collinear/controller-near-eye poses and IPD variation. A `BOTH`
  flag alone cannot pass a one-eye visibility test; require front-facing pixels
  in each eye for the accepted pose range.
- Verify no segment continues behind its actual hit, raised-face hits terminate
  at the moving face, and the free ray does not steal active hover/drag ownership.
- Check scene RTT, reading HUD, modal takeover, modal replacement, suppressed
  input, empty UI, closed-menu cleanup and both hands. Retain fixed source hit
  rectangles, original-capture compatibility, one held click and UUID retry.
- Check composed HMD/simulator pixels and mirror parity. Confirm background
  transparency, correct alpha and a native layer-count budget on reduced limits.
- A screenshot target crossed by either ribbon must still resolve automatically
  to its intended UI carrier, then hover/click once using real hand-origin XR
  input. Moving decorative geometry must not change an immutable base target.
  Preserve the ribbons in evidence rather than remove them from metadata.
- Re-run ordinary and reduced-motion physical-button gates. Reduced motion
  changes button travel, not the real controller ray or its stereo depth.

Physical-headset comfort, compositor reprojection/runtime compatibility and
CPU/GPU/frame-budget measurement remain unverified. Existing physical-hover
entrance-time replacement, recenter/resize, constrained-budget, remaining-menu
transport and public-motion-preference gates remain separate work. This plan
adds no same-plane beam illusion and makes no claim that those gates are done.
