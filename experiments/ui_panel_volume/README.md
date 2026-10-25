# Native UI volume probe

This isolated D3D11 / Shader Model 5 experiment tests thin, solid menu panels and raised buttons. It renders existing FlatOut UI artwork on an unchanged front, with real side geometry and a small matte rear chamfer. It does not modify Fox, the simulator runtime, the live game, or a RenderDoc session.

## Confirmed brief

- Panels and buttons should have visible outer thickness from side angles.
- Keep the existing front artwork, text, color, layout, and rectangle. No front relighting or new front border.
- Compare two subtle thicknesses: 4 mm and 8 mm for panels.
- Buttons rest 3 mm ahead of the panel; hover adds a real 30 mm translation. No hover scaling.
- A subtle soft button shadow is allowed straight-on at rest.
- Use a small matte bevel. A 0.6 mm rear chamfer preserves the original front edge; button chamfers are 0.4 mm.

Buttons use 4 mm bodies with the 4 mm panel preset and 5 mm bodies with the 8 mm panel preset. At rest their rear body intersects the panel slightly; they are solid raised plaques rather than disconnected paper faces. The native disabled up arrow stays at rest when the enabled down arrow hovers.

## Why geometry is required

The [OpenXR quad specification](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerQuad.html) explicitly gives a quad layer no thickness. A bevel/normal or parallax effect confined to that original quad can change the face appearance, but cannot add an outer side silhouette. That is insufficient for this brief.

The implementation uses a 20-triangle closed mesh per plaque: unchanged front, four side walls, four rear chamfers, and a back. An ordinary SM5 vertex/pixel shader transforms that mesh independently for each eye. No geometry shader, tessellation, height-map ray marching, or pixel-depth replacement is needed. Geometry shaders could generate vertices, as [Microsoft documents](https://learn.microsoft.com/en-us/windows/win32/direct3d11/geometry-shader-stage), but offer no necessary capability for this small fixed mesh.

The front samples the native UI without lighting. Only the shell receives restrained diffuse graphite shading. A receiver-plane analytic area-light approximation supplies the button shadow; its offset and penumbra grow with the actual separation. This is a deliberate approximation for parallel rectangular UI surfaces, not a general scene shadow system.

## Native artwork and camera

The fixture is the existing GPU-exported native statistics HUD from:

`build-ui-automation/flatout4-evidence/rc17f-popup-spacing-renderdoc/rc00c-statistics-reader-green/hud-ResourceId-1147.png`

The source is 2100 by 1181 pixels. The main panel crop is `(105,61,1901,971)`. The two real arrow buttons are cropped at `(1772,74,87,79)` and `(1903,74,88,79)`. No text, icon, or menu artwork is synthesized. For the raised-button scene, their source rectangles are removed from the backing using the exact native header color `(8,11,14)` before the cropped fronts are drawn separately. That models suppressing the original button HudNodes when promoting them. The full source hash and crop provenance are written to `evidence/volume/provenance.json`.

The synthetic test camera uses perspective, a 48 degree vertical field of view, 1280 by 800 pixels, near/far 0.05/10 m, and a 1.3 m panel-center distance. The panel is 1.25 m wide; its height preserves the native crop aspect. The front lies at local Z=0, facing positive Z; thickness extends behind it, entirely within its original XY footprint. Tests use yaw 0 and +/-60 degrees. Stereo eyes are parallel at X=-0.032/+0.032 m, with a 64 mm IPD and no toe-in. The reference head-on image uses the center eye at X=0.

This is a software-rendered geometry proof with a neutral background, not a screenshot of the feature integrated into the game. Device creation is hard-coded to [D3D_DRIVER_TYPE_WARP](https://learn.microsoft.com/en-us/windows/win32/api/d3dcommon/ne-d3dcommon-d3d_driver_type); the probe opens no HWND, OpenXR instance, simulator DLL, game process, or hardware adapter.

## What the zero-pixel gate means

The strict gate renders the **original complete panel crop as one front at Z=0**, including its original painted arrow buttons. It has **no extracted/raised buttons and no shadow**. The same native texture, sampler, camera, face vertices, UVs, and render target are used for flat, 4 mm, and 8 mm versions. This isolates the effect of adding thickness: both shells change exactly zero head-on pixels against that flat control. Three absolute native-color samples and PNG roundtrip checks also prevent a shared channel-order/relighting error from hiding in a relative comparison.

The comparison sheet is a separate scene: **all three columns already place extracted button fronts 3 mm forward and enable their resting shadow**, including the flat control. The flat column has no bodies. Adding a body to these off-center raised buttons changes 36 head-on pixels because a tiny physical side is visible from the center camera. Changing the real body preset from 4 mm to 8 mm changes zero head-on pixels in that raised scene. The 3 mm rest offset shifts the original-plane button raster bounds by at most one pixel at this reference camera, measured without scale compensation. Other viewpoints/resolutions can have different perspective shifts. An always-raised button cannot promise identical original-plane projection at every eye pose.

The resting soft shadow changes 162 pixels relative to the same raised scene without a shadow. Every changed pixel lies on the panel within the declared button receiver mask. Artwork outside that explicit shadow allowance stays unchanged.

## Run and evidence

From the simulator repository root, in PowerShell:

```powershell
& .\experiments\ui_panel_volume\test.ps1
```

For a different location of the same exported native HUD:

```powershell
& .\experiments\ui_panel_volume\test.ps1 -NativeHud <native-statistics-hud.png>
```

The script discovers Visual Studio with `vswhere`, builds only this directory, checks the mesh, verifies both negative controls fail for their intended reason, and runs the WARP tests. No root CMake integration is required. A standalone `CMakeLists.txt` is also provided for the mesh tests and probe build.

Direct probe invocation from this directory after building:

```powershell
& .\build\ui_panel_volume_probe.exe <native-statistics-hud.png> evidence\volume
```

`comparison.png` has columns **flat / 4 mm / 8 mm** and rows **head-on / 60 degrees**. Individual eye images, the untouched native panel crop, no-shadow/resting-shadow/hover views, `results.json`, and provenance are in `evidence/volume/`. Negative-control logs are one level above it. Generated binaries and evidence are ignored by Git.

Latest measured results:

| Check | 4 mm | 8 mm |
|---|---:|---:|
| Strict head-on changed pixels | 0 | 0 |
| Side pixels across +/-60 degrees and both eyes | 7,554 | 16,596 |
| Silhouette pixels outside the flat control | 7,552 | 16,596 |
| Matched sampled side world points across eyes | 756/756 | 1,656/1,656 |
| Maximum independent reprojection error | 0.00171 px | 0.00147 px |

The original flat implementation was tested first and failed because it had no side vertices. The persistent flat GPU control fails with zero side pixels and zero silhouette extension. A second negative control deliberately renders both eyes using the left eye; the stereo world-point check rejects it. Positive GPU diagnostics verify button fronts at exactly 0.003 m at rest and 0.033 m on enabled hover, with the disabled arrow unchanged. Shadow checks require visible soft falloff confined to the actual receiver.

## Source findings and production plan

Paths below are relative to `<fox-root>`, the authoritative worktree supplied by the parent task. They were inspected read-only.

1. `PlayAll/Code/Runtime/Common/PlayAll/Common/Graphics/HudRenderer.cpp` loads `QuadTextureColor3D.PFX`. Its `VertexUVColorQuad3DUVColor` entry in `Flatout/Data/Assets/Engine/Effects/VertexShaders.cg` preserves UVs and transforms positions; `PixelTextureColorMain` in `quad_utils_pixel.cg` multiplies the texture by the existing vertex color. `EffectProcessorWin32.cpp` compiles the native pipeline as SM5. Preserve that unlit face path and its sRGB/premultiplied-alpha behavior rather than relighting the UI.
2. `PlayAll/Code/Runtime/Win32/PlayAll/Drivers/Game/OpenXR.cpp::RenderHudButtonTiles` rasterizes promoted buttons into the existing atlas. `XrRuntime.cpp::GetHudButtonLayerDesc` composes the physical lift into the base HUD pose; `EndFrame` submits the base and button fronts as both-eye quad layers. Adding a shell cannot be accomplished by changing only this atlas pixel shader.
3. `Graphics/HudItemList.h` defines `HudList3D`, its actual world matrix, `HudProjectionMode::E_NONE`, and `Add3DList`/`Remove3DList`. `HudRenderer.cpp` renders these through the current view context. `DeferredRenderLoop.cpp` draws this 3D HUD late, after postprocess and before switching to the HUD render target. `Flatout/Code/Sources/Menus/FO3DNavigationMenu.cpp::VRUpdateBack` already uses that world-matrix route. It is a candidate late-pass insertion point for a prototype, not proof that the existing flat HudNode batching can accept arbitrary shell vertices unchanged.

Recommended next implementation sequence:

1. Add an opt-in native proof on one ordinary menu panel and its buttons. Obtain semantic panel/button rectangles, visibility, atlas UVs, colors, and the same authoritative anchor pose from the current HUD. Extrude container faces only; do not extrude text glyphs, full transparent canvases, or decorative atlas elements. Cache geometry on layout changes. Keep hit testing in the established stable base-plane rectangles, independent of the visual lift.
2. Start with the existing showroom/livery RTT world-mesh route, or a dedicated late per-eye pass that renders **front and shell together** using the native eye matrices. This gives coherent stereo, depth, mirror output, and motion for the complete object. Verify that the front remains sufficiently sharp and stays outside unwanted tone mapping/TAA. A generic front-HUD material change or global enable is insufficient.
3. If compositor-sharp fronts must remain quad layers, test a hybrid separately: shell in each projection eye, front as its existing quad. Drive both from exactly the same pose, dimensions, generation/visibility, and predicted display time. The compositor can reproject the quad differently from the projection shell; head motion can expose a seam even when a still frame aligns. Standard layer ordering also does not guarantee world-depth occlusion. The [depth-test extension](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrCompositionLayerDepthTestFB.html) must be supported/enabled before depending on its behavior. Do not add many side-face compositor quads without an evidenced layer-budget decision.
4. Specify and test depth policy: bodies/buttons must self-occlude correctly and agree with their fronts; choose how menu volume interacts with scene geometry, hands, and the pointer. The current HUD renderer disables Z writes, while this isolated solid probe uses real depth writes. A production late volume pass needs an explicit state choice and restoration. Keep reduced-motion behavior and disabled/button ownership semantics from the existing hover path.
5. In the native runtime, capture front-on, +/-60 degree, left/right eye, mirror, and moving-head evidence. Repeat hover/press/release, page/menu replacement, disabled states, overlapping panels, clipping, shadows, and no-depth-extension fallback. Check actual asymmetric OpenXR frusta, premultiplied alpha, sharp text, body/front registration, and pointer target stability. Use the required native shader draw/binding capture before declaring integration complete.
6. Only after that proof, extend to eligible menu panels/buttons via shared semantic metadata. Measure native CPU/GPU cost and headset readability before picking a default. The probe creates/readbacks diagnostic resources and is not a performance benchmark.

## Limits

This proves the isolated HLSL/mesh approach and its discriminating tests. It does not prove native integration, OpenXR timewarp alignment, asymmetric headset projection, compositor/depth-extension compatibility, hand/scene occlusion policy, alpha-shaped or rounded panels, nonparallel shadow receivers, or headset performance. The native export is a fixed fixture, not a live dynamic HUD feed. No production switch is enabled.
