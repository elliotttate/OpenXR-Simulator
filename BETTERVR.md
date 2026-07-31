# Using this runtime with BetterVR

[BetterVR](https://github.com/Crementif/BotW-BetterVR) is a Cemu VR layer. It is a
Vulkan layer, but its OpenXR session uses `XrGraphicsBindingD3D12KHR` and does its
own Vulkan to D3D12 interop, so this runtime needs no Vulkan support to host it.

This branch fills the same slot as Meta's XR Simulator: a desktop OpenXR runtime
for developing without a headset, but source-built and patchable.

## What BetterVR requires

Enabled unconditionally at `xrCreateInstance`, so all three must be advertised:

- `XR_KHR_D3D12_enable`
- `XR_KHR_composition_layer_depth`
- `XR_KHR_win32_convert_performance_counter_time`

Plus `DXGI_FORMAT_R8G8B8A8_UNORM_SRGB` and `DXGI_FORMAT_D32_FLOAT` swapchain
formats, a `STAGE` and a `VIEW` reference space, and a stereo view configuration
with exactly two views.

## Build and install

```powershell
.\build_simulator.ps1
```

Builds the runtime and installs `openxr_simulator.dll`, a relocatable
`openxr_simulator.json` and the activate/deactivate scripts into
`..\BotW-BetterVR\OpenXRSimulator` — laid out the same way `MetaXRSimulator\` is,
and gitignored by BetterVR so only build output ever lands in that checkout. Use
`-InstallTo` for a different location.

## Use

Per-process, which leaves the machine-wide runtime registration (Virtual Desktop,
SteamVR, Quest Link) untouched — prefer this:

```powershell
$env:XR_RUNTIME_JSON = "C:\path\to\BotW-BetterVR\OpenXRSimulator\openxr_simulator.json"
```

BetterVR's own harness takes it directly, and its Visual Studio and CLion launch
configurations have matching entries:

```powershell
.\run_probe_test.ps1              # uses this simulator
.\run_probe_test.ps1 -Runtime meta
```

Machine-wide, if something ignores the environment variable: `activate_simulator.ps1`
in the install folder (self-elevates, stashes the old runtime in
`PreviousActiveRuntime`) and `deactivate_simulator.ps1` to put it back.

## Checking a runtime against BetterVR

```powershell
.\probe\run_xr_probe.ps1
.\probe\run_xr_probe.ps1 -RuntimeJson ..\BotW-BetterVR\MetaXRSimulator\meta_openxr_simulator.json
```

`probe/xr_probe.cpp` replays BetterVR's OpenXR sequence — the three extensions
above, the D3D12 binding, both swapchain formats, the action shapes from
`CreateActions`, and a 30-frame loop. The loop runs BetterVR's two shapes of frame
in turn: quad-only first (its boot and title sequence), then a projection layer
with chained depth plus a quad layer (in game). Exit code 0 means BetterVR should
run. It borrows `openxr_loader.lib` from a configured BetterVR `cmake-build-*` tree.

It runs with the D3D12 debug layer on and fails on any validation error. That
matters: the probe renders into each acquired image and releases it without a
barrier, exactly as `Layer3D::RecordRender` does, which is the only way
resource-state bugs show up at all. An earlier version only did
acquire/wait/release and passed a runtime that was corrupting every frame.

## Differences from the Meta simulator

- `xrGetInstanceProperties` reports `"OpenXR Simulator Runtime"`, so BetterVR's
  `m_capabilities.isMetaSimulator` stays false and the swapchain-size workaround in
  `src\hooking\framebuffer.cpp` does not kick in. That is deliberate: this runtime
  accepts swapchains at the game's own render resolution, so BetterVR takes the
  same code path a real headset takes.
- Recommended per-eye resolution is 1280x720 (Meta's is 1440x1584).
- `xrEndSession` immediately after `xrRequestExitSession` succeeds here. The Meta
  simulator returns `XR_ERROR_SESSION_NOT_STOPPING`, which older BetterVR builds
  turn into a throw from `RND_Renderer::~RND_Renderer`.

## Preview window lifetime

The window is created in `xrBeginSession`, before any frame is submitted. It used
to be created lazily from `presentProjection`, which meant it only appeared once
the app submitted a projection layer — and BetterVR submits quad layers only for
its whole boot and title sequence (`RND_Renderer::EndFrame` gates the projection
layer on `IsInGame()`), so the window never appeared at all and the session never
reached `FOCUSED`.

Frames with no projection layer now clear the preview to black and still
composite their quad layers, so a 2D-only screen shows its HUD rather than a
frozen copy of the last 3D frame.

## Quad layer placement

A composition layer's pose is in the space it was submitted with, and BetterVR
submits its 2D screen in `STAGE` — pose `(0, 1.70, -1.90)`, a world position at
standing height. Reading that as head-relative (which is only right for a `VIEW`
space layer) pushed the layer a screen and a half above the top of the preview,
so a quad-only frame drew an empty window even though the layer was arriving and
being read back correctly.

Reference space types are now recorded at `xrCreateReferenceSpace`, and the quad
is resolved through its space into world coordinates, transformed into each eye's
view space and projected with the same per-eye pose (IPD included) and FOV
`xrLocateViews` handed the app. So a world-locked layer sits where the app put it,
moves when you look around, and carries real stereo disparity. GDI can only place
an axis-aligned rect, so a rotated quad shows up as its screen-space bounding box,
and a layer crossing the near plane is dropped for that eye rather than projected
through a divide by zero.

Because it opens before any swapchain exists, the window starts out sized from
the recommended 1280x720 rather than the app's real per-eye resolution. The
first projection layer re-fits it through the same aspect snap `WM_SIZE` uses:
client width is the user's to choose, height is derived from the content aspect.
So it never opens at 2x the game's render width the way it used to — a 2120x2280
per-eye BetterVR session opened a 4240x2280 window before this.
