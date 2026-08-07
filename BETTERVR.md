# Using this runtime with BetterVR

[BetterVR](https://github.com/Crementif/BotW-BetterVR) is a Cemu VR layer. Today its
OpenXR session uses `XrGraphicsBindingD3D12KHR` and it does its own Vulkan to D3D12
interop; the port in progress replaces that with a native Vulkan session on Cemu's own
device. This runtime hosts both — see "Vulkan sessions" below.

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

The dll and scripts are **symlinked** into that folder, so a rebuild is live in
BetterVR with no reinstall — the CMake tree and `bin\` stay in this repo and the
BetterVR side is just a view of them. Creating symlinks needs Developer Mode
(Settings > System > For developers) or an elevated shell; without it the script
copies and tells you. Pass `-Copy` for real copies when the folder has to stand on
its own — moved to another machine, or still working after the build tree is gone.

One consequence of linking: if Cemu or the probe still has the runtime loaded, the
*build* now fails at the link step rather than the install step, because the dll it
writes is the one that process holds open. Close it and rebuild.

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

`XR_PROBE_SUBRECT=1` switches it to a 2048x2048 swapchain with a 1920x1080
`imageRect` at offset (64,128). Everything outside the rect is cleared bright
green, so any green in the preview means a runtime ignored the rect somewhere.

It runs with the D3D12 debug layer on and fails on any validation error. That
matters: the probe renders into each acquired image and releases it without a
barrier, exactly as `Layer3D::RecordRender` does, which is the only way
resource-state bugs show up at all. An earlier version only did
acquire/wait/release and passed a runtime that was corrupting every frame.

## Vulkan sessions

```powershell
.\probe\run_xr_probe.ps1 -Vulkan
.\probe\run_xr_probe.ps1 -Vulkan -Enable1
```

`probe/xr_probe_vk.cpp` is the same replay over `XR_KHR_vulkan_enable2` (or
`XR_KHR_vulkan_enable` with `-Enable1`), for the native-Vulkan BetterVR: the runtime's
`xrCreateVulkanInstanceKHR` / `xrCreateVulkanDeviceKHR` / `xrGetVulkanGraphicsDevice2KHR`
handshake, `VkFormat` swapchains, and a real render pass into every acquired image whose
`initialLayout` claims the runtime already put it in `COLOR_ATTACHMENT_OPTIMAL`.

It runs with `VK_LAYER_KHRONOS_validation`, and proves the layer is watching by provoking a
harmless VUID first — a probe whose callback is silent passes every runtime, which is the
same trap the D3D12 probe fell into once. Note the one thing it *cannot* catch: Vulkan
validation does not track image layouts for images backed by imported external memory, so a
runtime handing swapchain images over in the wrong layout goes unreported here. That
guarantee is covered instead by the two probes producing a pixel-identical preview: run
either one with `XR_PROBE_PROJECTION_ONLY=1` and compare screenshots.

## Differences from the Meta simulator

- `xrGetInstanceProperties` reports `"OpenXR Simulator Runtime"`, so BetterVR's
  `m_capabilities.isMetaSimulator` stays false and the swapchain-size workaround in
  `src\hooking\framebuffer.cpp` does not kick in. That is deliberate: this runtime
  accepts swapchains at the game's own render resolution, so BetterVR takes the
  same code path a real headset takes.
- Recommended per-eye resolution is the active headset profile's panel (2064x2208 for
  the default Quest 3; Meta's is 1440x1584). BetterVR ignores it and asks for the
  game's own render resolution either way.
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

## Sub-rect swapchains

An app may render into part of a swapchain image and declare the used region with
`XrSwapchainSubImage::imageRect` — a 1920x1080 eye inside a square 2048x2048
texture, say. The D3D11 path honoured that rect; the D3D12 path was never handed
it, so it copied the whole texture and the preview showed the unrendered remainder
at the wrong aspect.

The rect now drives the D3D12 eye copy, the quad readback and the preview size:
`g_sourceWidth`/`g_sourceHeight` carry the rect extent rather than the swapchain
dimensions, so the offscreen RT and the window aspect follow the region the app
actually rendered. `imageArrayIndex` selects the source slice on the quad path too,
which it previously ignored. The "show full render" toggle still bypasses cropping
on both backends.

## Why the preview used to look stretched

The FOV this runtime reports is a real headset frustum — Quest 3's is `(-54, +40,
+43.98, -54.27)`, whose tan ranges are 2.22 wide by 2.36 tall, so it is *taller than
it is wide*. BetterVR renders that frustum into a 2560x1440 buffer. That is not a bug:
the buffer just has non-square pixels, each covering about twice as much angle
vertically as horizontally. A real compositor resolves it when it maps the buffer onto
the panel.

The preview did not. It blitted the app's pixels 1:1, so the squeeze survived to the
screen and everything came out about 1.89x too wide.

Each headset profile now carries its native per-eye panel resolution, and the preview
maps the eyes onto *that* shape rather than the app's buffer — one stretch in the final
GDI blit, which is what turns the non-square pixels back into square ones. The app's
pixels still land in the offscreen RT 1:1, so nothing is resampled twice.

Panel aspect and FOV aspect are not identical on real hardware (Quest 3: 0.935 vs
0.941), so a world-space square still renders slightly off — well under 1% on Quest 3,
but around 7% on a PS VR2. Driving the display shape from the FOV instead would be
exact, at the cost of the window no longer being the panel's proportions.

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
