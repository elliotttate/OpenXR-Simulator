# OpenXR Simulator MCP Server

A Model Context Protocol (MCP) server that provides tools for diagnosing OpenXR issues and capturing screenshots from the OpenXR Simulator runtime.

## Features

- **Screenshot Capture**: Capture the current XR frame being rendered (stereo view)
- **Frame Diagnostics**: Get detailed frame timing, resolution, and format information
- **Log Analysis**: Read and analyze simulator logs for debugging
- **Issue Diagnosis**: Automated analysis of common OpenXR problems
- **Session Monitoring**: Track session state and head tracking
- **UI Flicker Detection**: Inspect quad-layer continuity independently from world/projection motion
- **Screenshot UI Targeting**: Hover and click with a simulated XR controller using coordinates in a captured image

## Installation

### Prerequisites
- Python 3.10 or higher
- The OpenXR Simulator runtime installed and active

### Install from source
```bash
cd mcp-server
pip install -e .
```

Or install dependencies directly:
```bash
pip install -r requirements.txt
```

## Usage

### Running the MCP Server

```bash
python openxr_simulator_mcp.py
```

### Configuring with Claude Code

Add to your Claude Code MCP settings (`~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "openxr-simulator": {
      "command": "python",
      "args": ["path/to/mcp-server/openxr_simulator_mcp.py"]
    }
  }
}
```

## Available Tools

### `capture_screenshot`
Capture a screenshot of the current OpenXR frame.

Parameters:
- `eye`: Which eye view to capture ("both", "left", "right") - default: "left"
- `timeout`: Timeout in seconds - default: 5.0
- `max_width`: Maximum returned image width, 128–4096 - default: 1280

Returns the composed image (including quad menus), `capture_id`, actual image
dimensions, and frame-matched camera, viewport, crop, scale, and surface metadata.
Coordinates refer to **the returned image**, with a top-left origin and pixel centres.
Stereo layouts and eye crops are taken from compositor metadata, not image aspect guesses.
Each capture publishes a separate `screenshot_status.<capture_id>.json` so a
Windows reader of an earlier status file cannot block the next acknowledgment.

### `hover_ui_at` / `click_ui_at` / `hold_ui_at` / `release_ui_pointer`

Example tool calls, using the ID returned by the first call:

```text
capture_screenshot(eye="left")
hover_ui_at(capture_id="...", x=475, y=313, hand="right", dwell_frames=10)
click_ui_at(capture_id="...", x=475, y=313, hand="right", button="primary",
            command_id="aUniqueAlphanumericRetryKey")
release_ui_pointer(capture_id="...", hand="right")
```

`hover_ui_at` aims without pressing. `click_ui_at` dwells, presses once, releases,
and waits for release to reach action synchronization before returning. Both
return a fresh application image plus a separately marked reference image, the
resolved ray/aim pose, and frame/sync acknowledgments. These results mean
`input_delivered`; inspect the application's highlight, tooltip, changed menu,
or application telemetry to confirm an actual UI activation.

`hold_ui_at` presses and leaves the selected button held. Another `hold_ui_at`
for that hand moves the ray without another press, which supports slider drags
and grabbing objects. Left and right hands have independent holds, so both can
grab and transform the same object. Call `release_ui_pointer` for **each** hand
in `finally`. Refresh a hold within 10 seconds; expiry and focus loss release
injected buttons automatically. Use fresh captures as the scene changes. A held
pointer must be released before changing its button or starting hover/click.

`scroll_ui_at` aims at the screenshot pixel, dwells with neutral input, supplies
the selected hand's `stick: [x, y]` for `hold_frames` action-sync cycles, then
returns the stick to neutral before acknowledging delivery. Each axis is in
`[-1, 1]`; positive Y points up. It never presses a trigger. Applications must
bind a thumbstick action and implement scrolling under the ray. For example:

```python
scroll_ui_at(capture_id="...", x=475, y=313, hand="left",
             stick=[0, -0.65], hold_frames=12, command_id="scrollOnce")
```

Retry the same command ID and arguments to recover its receipt without applying
the scroll twice. `input_delivered` confirms synchronized axis delivery; inspect
the application's list position and screenshot to verify the resulting scroll.

Common parameters:

- `capture_id`, `x`, `y`: Target a pixel in a capture from this MCP session.
- `hand`: `left` or `right` (default).
- `origin_mode`: `hand` (default) intersects a known quad or an explicit scene
  surface, then aims from the normal hand position. `eye` explicitly places the
  controller aim origin at the captured eye and reproduces that view ray.
  Eye-origin beams point along that eye's view ray, so their visible length can
  collapse to a dot in that eye. For normal stereo controller visuals, use hand
  mode with a known quad or explicit scene surface.
- `surface_id`: Optional quad ID from the capture metadata, useful for overlapping menus.
- `surface`: Optional application-provided scene UI rectangle in simulator world
  coordinates: `{"pose":{"position":[x,y,z],"orientation":[qx,qy,qz,qw]},"size":[width,height]}`.
  Units are metres; orientation must be a unit quaternion. The rectangle lies in
  its local XY plane. Projection UI currently needs this explicit surface or
  `origin_mode="eye"`; the runtime does not infer scene depth from image colour.
- `ray_offset`: Optional correction for an application that modifies its XR aim
  ray before hit-testing. Supply `{"position":[x,y,z],"orientation":[qx,qy,qz,qw]}`.
  By default these are independent offsets in simulator world axes: the app adds
  the translation to the controller origin and rotates its direction by the unit
  quaternion. Set `position_space: "aim"` when the translation rotates with the
  controller, such as a physical controller-shell emitter. The MCP solves the
  resulting pose and translation together. Omit this for apps
  that use the XR aim pose directly. Eye mode keeps the resulting application
  ray at the captured eye; hand mode keeps the controller at its natural origin.
- `dwell_frames`: 1–90 action-sync cycles before completion/press (default 2).
- `timeout`: 0–30 seconds (default 5).
- `command_id`: Optional alphanumeric retry key. Reusing a completed key in the
  same runtime session returns its delivery result without sending another click.
  Reusing it for a different target, button, or operation returns an error.

Click adds `button` (`trigger`, `select`, `primary`, `secondary`, `menu`) and `hold_frames`
(1–90 action-sync cycles, default 2). `primary` is right A / left X;
`secondary` is right B / left Y (for Back and orbit-camera controls); `select` uses the
trigger/select source. Choose the component the application actually binds.
The runtime uses suggested interaction-profile bindings, including arbitrary
application action names, and honors active action sets and subaction paths.

Captures expire after 30 seconds, with the last 32 held by the MCP. Session,
camera, FOV, viewport, and selected quad changes reject stale targets. Anaglyph
stereo and an active pose sweep cannot be targeted unambiguously. Hover/input
ownership lasts up to 10 seconds; a release, timeout cleanup, focus loss, or session
end clears injected buttons. The application must synchronize its relevant aim
and button actions for an interaction to complete. Moving/animated scene UI still
requires a fresh capture or application-provided feedback.

Screenshot metadata separates `geometry.decorations` from targetable
`geometry.surfaces`. A quad sampling one texel, at most 4 mm across, at least
5 cm long and with aspect ratio at least 12:1 is treated as a decorative ribbon.
This keeps controller-ray quads from intercepting automatic UI targeting. Its
real layer id, BOTH/eye visibility, metric size and pose remain in the capture.
Large solid panels, textured narrow UI and small square buttons remain targetable.
Applications using shorter ray ribbons should omit those quads (retain the hit
cursor) or supply explicit target geometry; this classifier is deliberately
geometric and does not infer whether arbitrary application content is a button.

### End-to-end UI tests

Install the requirements in a Python environment, then enable the optional CTest
suite. This builds probes directly against the runtime DLL; no sibling checkout,
installed OpenXR loader library, or machine-wide runtime registration is required.

```powershell
python -m pip install -r mcp-server/requirements.txt
cmake -S . -B build-ui-automation `
  -DSIMXR_OUTPUT_DIRECTORY="$PWD/bin/ui-automation" `
  -DSIMXR_ENABLE_UI_AUTOMATION_TESTS=ON `
  -DPython3_EXECUTABLE="C:/path/to/python.exe"
cmake --build build-ui-automation --config Release
ctest --test-dir build-ui-automation -C Release --output-on-failure
```

The tests launch a real XR app and the MCP over stdio, locate a blue button in a
resized screenshot, hover until it turns yellow and shows a tooltip, click once,
and verify its green result and application-side activation/press/release counters.
The app's independent raycast is reprojected to verify error below one source
pixel. Coverage includes D3D11, D3D12, OpenGL, Vulkan, both hands/eyes, both-eye
captures, over-under layout, asymmetric FOV, angled quads, sub-image rectangles,
idempotent retry, invalid coordinates, timeout cleanup, and stale camera rejection.
Vulkan layout validation additionally requires the optional Khronos validation layer.
The capture-publication regression holds the legacy status file open without
delete sharing throughout screenshot, hover, click, and retry. Captures must
still complete; this reproduces the Windows file-sharing race found in a long
FlatOut menu sweep.

Two additional tests render the menu plane directly into the stereo projection
images and submit **no quad composition layer**. These verify world-space menu
interaction using both the eye-origin ray and a hand-origin ray aimed at an
explicit scene surface. This covers the distinction between a game's quad-layer
overlay menus and its main menu rendered in the scene, such as the FlatOut 4
cases identified for game-level validation. Three more cases apply FlatOut's
application-side ray offsets before the independent fixture hit-test, verifying
the correction with subpixel accuracy for all three targeting modes.

#### Foreground controller composition regression

`xr_layer_order` submits a world projection, an opaque menu quad, a transparent
stereo projection, and a final quad. The screenshot test verifies submission
order, different left/right foreground content, alpha blending, array slices,
and capture geometry still referring to the primary world projection:

```powershell
python tests/composition_layer_order_e2e.py --probe bin/xr_layer_order.exe --runtime bin/openxr_simulator.dll --evidence build-ui-automation/order-d3d11 --backend d3d11
python tests/composition_layer_order_e2e.py --probe bin/xr_layer_order.exe --runtime bin/openxr_simulator.dll --evidence build-ui-automation/order-d3d12 --backend d3d12
```

Add `--stereo-quads` to either order test to exercise the eye-specific quad
carrier used for SteamVR compatibility. It also verifies these carriers remain
decorations rather than intercepting screenshot-based UI rays.

For Fox, launch a Release build with an isolated test profile, open Options, and
keep the simulator head level and facing forward. Run:

```powershell
python tests/flatout_controller_overlay_e2e.py --pid GAME_PID --data-dir SIMULATOR_DATA --evidence build-ui-automation/controller-visible
```

This compares actual rendered controller pixels with the native diagnostic
`vr.controller_overlay` disabled/enabled in the same build, in both eyes. It
restores the original controller poses and switch. The foreground uses two eye-specific quad layers with native per-eye cameras,
controller materials and self-depth. This avoids SteamVR ordering quads above
a projection layer submitted later; neither eye is copied from the other.
Full-frustum eye-image carriers are reported as decorations, not UI hit planes.
When launching Fox with an explicit simulator manifest, its non-Retail builds
also accept `PLAYALL_OPENXR_RUNTIME_JSON`. The Fox simulator launch recipes set
this alongside `XR_RUNTIME_JSON`, preserving the selected runtime if Steam
startup changes the latter. Verify the actual `vr.get_state.runtime` after launch.

For GPU inspection, run `tests/flatout_controller_overlay_gpu.py` in RenderDoc's
Python environment with `FOX_CONTROLLER_GPU_JOB` naming a JSON file containing
`capture`, `native_state`, and `result` paths. `native_state` contains the `vr.get_state` result under a top-level `vr` key. The check requires separate color/depth targets per eye,
matching eye cameras, transparent clear pixels, actual controller pixel history,
and no duplicate world-pass draws. Compare performance in the same Release
build, without capture/replay or builds running during the sample.

#### FlatOut 4 validation

The Fox Release DLL build was tested on 2026-10-24 with the D3D11 simulator,
asymmetric Quest 3 FOV, and a 640x720 left-eye screenshot. Hovering the scene's
Options button selected item 5; one trigger press opened Options. The native
quad-layer Quit dialog was tested by hovering No and clicking once: the game
reported `msgbox.cancel`, one activation, and a closed dialog. Retrying the same
command ID kept the activation count at one. The Options submenu itself uses
a scene-rendered RTT plane in this Fox build, so the modal supplied the actual
OpenXR quad-layer coverage.

FlatOut's `VRMenuPointer::UpdateRay` applies an extra +0.03 m X translation and
-5 degree Y rotation to the aim ray. An uncorrected aim missed the small No
button. Use the following `ray_offset` for this build, on both hover and click:

```json
{"position":[0.03,0,0],"orientation":[0,-0.043619387365336,0,0.999048221581858]}
```

This is an explicit application calibration, not a simulator-wide default.
The game expects a seated LOCAL origin; `set_head_pose(x=0,y=0,z=0)` brings its
default quad HUD into view. Launch through Fox's documented `/launch-game`
Release recipe with `XR_RUNTIME_JSON` pointing to this build's manifest and a
shared, isolated `SIMXR_DATA_DIR` for the game and MCP. Use `origin_mode="eye"`
for the main menu and hand mode with the captured `surface_id` for the native
quad dialog. Fresh screenshots and application telemetry are still required.

The later RC controller integration uses each native controller's shell emitter
and removes that older translation/yaw calibration. Do not apply the historical
constant to these builds. The FlatOut tests derive an aim-space translation from
read-only `vr.get_state` controller and pointer poses using
`tests/flatout_ray_offset.py`, then send actual XR input. The helper caches the
calibrated arguments per command ID so retries preserve their original digest.
Current menu tests cover screenshot-derived leaderboard paging, visible gray
inactive endpoints, and proportional joystick scrolling in Options, Statistics
and the livery library. Run them against a fresh isolated native profile and
inspect their `result.json`, captures, native error queue and complete game log.

Each run uses its own `SIMXR_DATA_DIR` for IPC and settings, and saves before,
hover, click, retry, and release JPEGs plus JSON evidence under
`build-ui-automation/ui-evidence/`. You can also run one case directly:

```powershell
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe.exe --negative-tests
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_desktop.exe --backend opengl
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe_vk.exe --backend vulkan
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe.exe --ui-mode world --origin-mode eye
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe.exe --ui-mode world --origin-mode hand
python tests/ui_automation_e2e.py --probe bin/ui-automation/xr_ui_probe.exe --app-ray-offset
```

Fixture runs set `SIMXR_AUTOMATION_ONLY=1`: the preview does not take desktop
focus, desktop keyboard shortcuts/movement are ignored, and synthetic XR focus
is maintained. Leave this unset for normal interactive simulator use.

### `get_frame_info`
Get detailed information about the current frame including timing, resolution, format, and head tracking state.

### `get_ui_flicker_status`
Read UI-only quad submission, GPU readback, fresh composition, cached recomposition, and omission counters. When an incident exists, optionally returns a contact sheet cropped to the projected UI rectangle in both eyes.

### `capture_ui_flicker_window`
Force a rolling pre/post UI-only capture even when automatic thresholds remain clean.

Parameters:
- `timeout`: Seconds to wait for a valid quad capture - default: 5.0
- `max_frames`: Maximum contact-sheet frames - default: 12

### `read_logs`
Read recent entries from the OpenXR Simulator log file.

Parameters:
- `lines`: Number of recent log lines to read - default: 100
- `filter`: Optional regex pattern to filter log lines

### `get_diagnostics`
Get comprehensive diagnostic information including session state, frame count, swapchain info, and errors.

### `diagnose_issue`
Analyze potential OpenXR issues based on symptoms.

Parameters:
- `symptoms`: Description of the issue or symptoms (required)

### `get_session_state`
Get the current OpenXR session state (IDLE, READY, SYNCHRONIZED, VISIBLE, FOCUSED, etc.).

### `get_head_tracking`
Get the current head tracking state including position and orientation.

### `clear_logs`
Clear the OpenXR Simulator log file to start fresh.

## File Locations

The MCP server uses the following files in `%LOCALAPPDATA%\OpenXR-Simulator\`:

- `openxr_simulator.log` - Runtime log file
- `screenshot.bmp` - Captured screenshots
- `screenshot_request.json` - Screenshot request trigger
- `screenshot.<capture_id>.bmp` / `screenshot_status.<capture_id>.json` - Correlated pixels and frame metadata
- `ui_pointer_command.json` / `ui_pointer_result.<command_id>.json` - Atomic interaction requests and delivery acknowledgments
- `runtime_status.json` - Current frame status
- `ui_flicker_status.json` - UI-only continuity and temporal status
- `ui_flicker_incidents\` - UI-rectangle pre/post evidence packets

Set `SIMXR_DATA_DIR` to the same directory in the app and MCP environments to
isolate a test or multiple app processes. Do not share an IPC directory between
independent runtime processes. Restart the MCP and launch the target app with the
updated runtime DLL to use the new tools.

### Live FlatOut RC livery regression

Build the current Fox RC, launch it with the simulator and the same
`SIMXR_DATA_DIR`, and open Garage → Livery editor. Use an isolated settings
profile. The latest RC derives a separate settings and livery folder from
`FOX_WRECK_RUN_TEST_SLOT >= 1000`; older branches may isolate progression only,
so back up their `Liveries` folder separately. Start with no unsaved edits:

```powershell
python tests/flatout_livery_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory>
```

For the current two-column layout, start in Livery with an unlocked sticker in
an isolated profile and run:

```powershell
python tests/flatout_livery_workspace_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory>
```

This turns the simulated head between the car and its two side columns, checks
both-eye clearance and floor margins with the panel level, and uses real ray input for paint,
tool selection, two-hand sticker manipulation, Roof/Studio views, keyboard,
Workshop browsing and sharing-preview cancellation. It never publishes. A captured
drag may cross the car silhouette without losing its grab. A single Undo must
restore the entire gesture; the successful run leaves the document unchanged.
It saves both-eye car and column screenshots for visual inspection. The keyboard
and Workshop use centered layouts; `--dialogs-only` reruns those exceptions. The shared
click helper glances toward controls on the wide canvas without moving the eye
position. The scroll regression measures physical target sizes so denser pixel
layouts cannot silently shrink their ray targets.

With an unlocked layer available, add `--dual-hands-only` to verify real
left/right car grabs, scaling, rotation, hand release, continuing movement,
and a single Undo/Redo for the entire gesture. This uses the controllers' normal
separated origins and an independently observed car depth plane. `--footer-only`
checks the Save button's actual eye-image pixels to detect scene occlusion.
`--stereo-rays-only` projects each controller beam into both eye images and
checks for its cyan/amber pixels. The livery tests supply the known panel
surface and use hand origins, so a test ray cannot collapse into its source eye.
`--layout-only` checks visible control rectangles and spacing across Paint, Layers, all five
tool panels, My Liveries, the keyboard, Workshop browsing, and the sticker
library. It also clicks all six views with Paint open and verifies the category
arrows advance once even with a held trigger and a retry. A real Gloss slider
drag must reach both requested values and undo as one operation.
`--properties-only` edits every paint/sticker numeric track, both palettes, all
four paint finishes, flips, and mirroring through actual XR input. Each edit
must change the native document and a single Undo must restore it. Scene-panel
targeting uses the fractional projection viewport, including at 50% preview zoom.
`--ray-dialogs-only` checks that modals preserve background brightness, the six
surface presets have separated ray targets, blocked background clicks do not act,
and custom RGB changes support Cancel and a single Undo after Apply.
`--footer-actions-only` uses the Edit Layer, Search and Rename ray buttons,
including a keyboard search and clearing it. `--layer-tools-only` checks linked
dimensions, single-step layer ordering and each tool's Reset. With a saved design
in an isolated profile, `--saved-copies-only` saves/replaces test slot 1, edits the
design, loads the copy, and verifies both Undo steps restore the prior states.
`--workshop-controls-only` uses the browser's Sort, Search, Vehicle Filter and Back
buttons. It verifies each sort step once with a held press and an idempotent retry;
community download and publishing operations are outside this control-only check.
`--galleries-only` needs at least seven unlocked layers in the isolated fixture.
It visits full-sized layer cards and all 32 saved slots using the ray page buttons,
checks one-page steps and disabled boundaries, and proves slot selection does not
load a design. It also empties the in-memory layer gallery, checks its Add action,
then restores the complete design with Undo. It never saves those temporary edits.

`tests/flatout_wreck_footer_e2e.py` checks the native Debug Wreck Run hub in an
isolated profile between tours. It ray-navigates Change Loadout, all four pickers,
Daily Wreck, Workshop, and Interface Settings, and checks footer target sizes,
spacing, hover without clicks, held presses and retries. It does not start a tour,
buy items, or apply settings. `--practice-only` starts in the practice offer and
checks its Select/Back layout and Back action. Use the same `--pid`, `--data-dir`
and fresh `--evidence` arguments as the other live tests. Debug fixture setup is
separate; interaction tests use real XR actions and read-only game diagnostics.
Use the fixture's standard interface colors: the test verifies bright hover
outlines after a dwell, restoration of the previous outline, and clearing over
empty space through read-only native node colors.

`tests/flatout_wreck_route_e2e.py` starts on an existing saved Wreck Run route
in an isolated profile. It checks all three sectors, visible tile hover, large
route actions and persistent hover outlines. It then previews all eight text,
contrast and motion combinations through the real Select button, discards the
draft through Back, and verifies the original preferences and tour sequence.
It rejects full-screen scene panels and full-width yellow footer strips using
the read-only `hud.rects.kind` / `color` diagnostics. Use `--pid`, `--data-dir`,
`--evidence` and optionally `--hand left`. Setup stays separate; the test never
starts an event, places a bet, applies settings or issues gameplay commands.

`tests/flatout_wreck_rewards_e2e.py` checks native result/reward/build navigation
or the in-race Wreck Run pause (`--screen result|build|pause`). It verifies footer
spacing, persistent hover, saved-state preservation, modal exclusivity, muted
background selection during confirmation, and real Select/Back/Resume actions.
`--choose` additionally selects the first scrap or
category card in that explicit fixture and verifies its effect occurs once.
Opening a pending roadside stop is a separate automatic journal commit, which
the oracle accounts for while still rejecting duplicate payouts/upgrades.

`tests/flatout_wreck_stop_e2e.py` starts on a Parts Swap with several eligible
parts. It checks the large arrow buttons, one-step/reverse/wrap behavior, and
unchanged saved inventory. Picker targets intentionally sit inside their card;
they must precede the containing card and a picker click must never accept it.
`--shop-only` checks shop hover, View Build and Back, plus a round trip to
Stunt Trial without buying or starting an activity. It finds that button through
its visible label and current target rectangle: shop target ordinals change as
installed parts change.
`--activity-only` starts on a Stunt Trial picker. It checks the bounded multiline
details panel, comfortable ray targets for its arrows, every trial's rules,
arrow priority over the Start card, and Back without consuming the visit.
Rules that fit should be readable together; longer rules expose More Info for
page advancement and wrap. The information action disappears on a single page.
Changing a trial must return its rules to page one. The screen-kit fixture also
checks that refreshing the same description keeps the current reading position.
`--leave` then checks one actual Drive On action without purchasing or swapping.
Both tests accept the same `--pid`, `--data-dir`, fresh `--evidence`, and `--hand`
arguments. Navigation checks sample ready saved state repeatedly and verify it
again after releasing both pointers; evidence retains complete initial and final
state snapshots. Debug fixture setup stays separate from these input-only regressions.
`hud.rects.text` exposes visible label values without ambiguous ancestor lookup.

The test captures screenshots, hovers and clicks through actual XR actions, and
uses the game's read-only livery diagnostics to verify the result. It checks
paint, undo/redo, sticker creation, duplicate/delete, layer locks and visibility,
the VR name keyboard, and local save/apply. Held triggers and idempotent retries
must not activate twice. It saves screenshots and `result.json`; it never uses
the game's test pointer or publishes to Workshop. The livery editor uses raw
controller rays, so the legacy Flash menu's application ray offset is omitted.

### Live FlatOut front-end regression

Use the same isolated RC launch for the rest of the UI:

```powershell
# From a loaded race: open Pause, resume, cancel Restart/Main Menu, then open Options.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory>
# From Quick Play in-race Options or Pause: ray Back, then confirm Main Menu once.
# The game restores the mode-specific front-end page (Quick Start for Quick Play).
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --return-main-only --expected-menu "QUICK EVENTS"
# From already-open Options, in the main menu or during a race:
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --options-only
# From Options: verify the category rail, focused list and target spacing.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --options-redesign-layout-only
# From Options: edit/restore choices, toggles and volume; test Reset and binding capture cancellation.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --options-interactions-only
# From Options: rebuild and page input settings for each available peripheral, then restore the original.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --options-devices-only
# Run the same coverage with the other controller (for example, on the in-race quad):
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --options-only --hand left
# From already-open Options: drag Audio with the left ray while the right ray points and presses elsewhere.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --dual-hand-options --hand left
# From a 2D menu: measure the removed strip, hover every action, and click Back once.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --footer-only
# From Options: check idle action wording, excluding the bright input icons.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --footer-contrast-only
# From another 2D menu: verify every ray target and reject overlaps in a flat layout.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --menu-hover-only --audit-only
# From the spatial main menu: aim at every rendered label and open Career once.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --spatial-hover-only --spatial-click-index 0 --expected-menu FLATOUT
# From the Career hub: verify its scaled spatial labels and open Garage with the left ray.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --spatial-hover-only --spatial-click-index 1 --expected-menu GARAGE --hand left
# From a spatial submenu: check the Back button in both eyes, reject outside/other-ray presses, then click once and retry.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --spatial-back-only --expected-menu "MAIN MENU"
# From this RC's root: verify the Coming Soon item is reported locked and cannot open with a ray.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --spatial-locked-only
# From Career's cup/event grid: check visible page controls, card spacing, every series and locked events.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --career-only
# From FlatOut Mode: sweep every Challenge event page, fixed hit bounds, locked cards, and a safe vehicle-preview round trip.
python tests/flatout_challenge_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --preview-round-trip --hand left
# From a page containing a locked Challenge card: verify stable hover bounds and inert held/retried clicks.
python tests/flatout_challenge_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --locked-only --hand left
# From a populated Steam leaderboard at rank 1: check separate row bands, grey Up and one held/retried page step each way.
python tests/flatout_leaderboard_scroll_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --navigation-only
# From a populated Friends list: verify the list-position range separately from players' global ranks.
python tests/flatout_leaderboard_scroll_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --layout-only
# From class selection or a Career grid/preview: check standing head-height placement, both eyes and world locking.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --reading-comfort-only
# From the grid with an unlocked cup: verify readable placement, Start/Back, blank-area safety and a confirmed race start.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --career-cup-only
# From Best Pilot, Race Recap or Cup Ranking after a non-final race in an isolated Career cup: advance once per page, check Continue/Main Menu and cancellation, and load the next round.
python tests/flatout_career_results_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --hand left
# From the same result pages after the final round: finish the cup once and return to the event browser.
python tests/flatout_career_results_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --hand left --final-round
# Stage the native development reward preview with the Fox bridge method ui.preview_reward first.
# It reuses the existing money-animation preview and does not grant credits. Read the unchanged balance with ui.reward_state.
# Then verify head-height placement, Continue, clean controls, closing input retirement and one dismissal through real XR input.
python tests/flatout_reward_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --expected-credits <unchanged-balance>
# From the Career hub with an active isolated test cup: cancel abandonment, then confirm it.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --career-cancel-only
# From class selection with the isolated Derby car: preserve scenery during both class warnings, then open filtered cars after OK.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --career-wrong-class-only
# From Garage: verify large actions, car-body clearance, all submenu round trips, and ray Back.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --garage-only
# Check the lower dock with the other hand and confirm a car-body click does not navigate.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --garage-clearance-only --hand left
# From Garage: press a trigger over the car while the other hand rests on a button; test both hand assignments.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --garage-two-rays-only
# From unrestricted Cars: verify class-button spacing, hover stability, and class selection both ways.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --car-classes-only
# From a car purchase confirmation: check that background arrows hide, then Cancel once.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --car-modal-only
# From Upgrades: verify a purchase popup preserves the scene within the menu quad, then Cancel once.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --popup-scene-only
# From Quick Play: verify the redesigned event cards, compact rules, setting round trips and vehicle/driver flow.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --quick-start-only
# Inspect the current primary action wording without changing a setting.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --quick-action-copy-only
# From a menu, dialog or results screen: check actual button size, visual alignment, contrast and ray hover.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --ray-buttons-only
# From any open native message box: check content/action padding and illustrated content visibility.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --popup-spacing-only
# From a single-button informational dialog: check spacing, hover and confirm once, then verify dismissal.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --single-dialog-only
# From the top of Statistics: check page arrows, scrolling endpoints, stable row hovers and categories.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --statistics-only
# From Party Mode's player selector: hover all seven counts, then choose eight players once.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --party-only
# From the first Party stunt page: select all twelve, test page endpoints, remove/clear, then start driver selection.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --party-stunts-only
# From post-race results: check the separate HUD footer and advance exactly one page.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --end-results-only
# During a leaderboard read: cancel via the ray and verify the request stops.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --leaderboard-cancel-only
# With an empty leaderboard: check the message and reject inactive page/profile targets.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --leaderboard-empty-only
# From results or the leaderboard: ray-click Try Again and verify the same race restarts.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --results-retry-only
# From in-race Options: check cyan/amber beam pixels in both eyes.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --stereo-menu-rays-only --hand left
# From a game-setup screen: reject row, Start, or footer target overlaps.
python tests/flatout_frontend_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --setup-layout-only
```

The popup spacing check measures the visible bounded dialog, its title, image,
caption and actual ray targets. It requires consistent inner padding, a separate
80-pixel action row and breathing room between an illustration and its caption.
An illustrated popup also checks the composed screenshot for image detail, so a
misordered dialog plate cannot silently cover the picture. It leaves the popup
open; `--single-dialog-only` additionally holds and retries a real controller
click and requires exactly one dismissal. Native bounds alone do not prove
readability or viewport visibility at every head pose.

The spatial-label sweep requires the current Fox `menu.get_state.spatial_layout`
and each item's `rendered_position`. It projects the actual text position through
the captured eye geometry, aims a normal hand ray, and checks native selection
without navigation. The optional click holds 45 frames and retries its command ID;
the destination must remain stable. Dismiss startup dialogs before running it.
Spatial menus expose their world-space `spatial_back.position` and hover state;
they do not advertise the hidden 2D helper line as clickable canvas targets.
Spatial item diagnostics include the actual lock state and `coming_soon` flag.
`spatial_back.half_extents` is measured along `spatial_layout.right/up`, with
the initial menu scale already applied. The Back frame, caption and chevron
stay fixed while hovering. The test checks visible padding in both eyes, the
chevron's leftward direction, misses beside/below the button, and a trigger
outside it while the other hand points at Back.

Career's `menu.get_state.career` supplies read-only page, selected row and named
event data for its two-row grid. The Career sweep checks page bounds, at least
20 pixels between cards, and full-size page buttons that stay inside the canvas.
It hovers each cup/event, verifies locked clicks cannot open a race, and holds
and retries page clicks before restoring the original series. The cup-flow check
also verifies that blank preview areas cannot start the race through legacy
pointer fallback; only the visible Confirm button may do so.
The wrong-class sweep also compares scene brightness above the warning. The
class screen remains behind the modal until OK; switching to the showroom while
the modal pauses the menu can leave its camera-transition fade black.

The Garage sweep covers both the five-action Quick Play dock and the four-action
Career dock, including submenu round trips. It checks that the dock stays below
the car body and that a car-side trigger cannot activate the other hand's hovered
button. Livery Back uses its independent ImGui surface and raw controller aim.
Each submenu round trip must also leave the engine error counter unchanged;
run from a fresh launch to catch first-entry collider lifecycle failures.

The Quick Start sweep checks all three event cards, compact visible setting groups,
heading/button contrast, unchanged values on hover, reversible arrow edits, and
the correct vehicle/driver selection step. It checks locked tracks through the
normal track arrows. Clicks allow up to 30 seconds for transitions that load car
assets, while retaining the held-trigger and duplicate-command checks.
The read-only `menu.get_state.setup.actions` reports the helper text used to
build the buttons. The sweep requires Choose Vehicle or Choose Driver after
mode changes and setting edits; a legacy helper refresh must not replace those
verbs with generic Confirm. Back stays at the right end of the action row.

The Options sweep checks all seven directly accessible category cards and every
present setting in one focused list. It pages long categories to both ends and
back, verifies setting values stay unchanged on hover, and rejects targets that
overlap settings or the footer. A page contains at most seven 80-pixel rows;
arrows have their own 80-pixel buttons and paging sits below the list. Every
active button is held for 45 sync cycles and retried
with the same command ID; the game must observe exactly one click. The test uses
submitted quad geometry during a race and the game's read-only `vr.targets`
panel geometry in the world-space front end, with normal controller origins in both.
The Options diagnostic remains a fallback for older test builds. World-space
menus beyond Options require the current `vr.targets.vr_frame` diagnostic.
The current Fox RC must include that diagnostic and the focused Options controls.
The separate interaction sweep verifies inert labels, choice/toggle changes,
slider stepping and direct positioning, Reset cancellation/confirmation, and
binding capture entered on trigger release. Capture must expose only Cancel;
canceling must keep Options open without assigning either ray trigger.
The peripheral sweep selects every available device, checks all rebuilt input
pages for blank labels or overlapping targets, and restores the original device
and setting values. Binding capture additionally checks the binding table's
dirty flag, rather than relying only on the rows' numeric values.

For visual validation, export the native RGBA Options render target from a
RenderDoc capture and save `options.get_state` and `vr.targets` under the
`options` and `targets` keys in a JSON snapshot. From General with an enabled
ON toggle visible, `tests/flatout_options_render_audit.py --texture <png>
--snapshot <json> --output <new-json>` checks its dark caption, opaque dividers
inside the settings panel, and transparency outside the functional panels.
The dual-hand check requires the two-controller menu pointer. It verifies that
the spare ray reaches Music before the gesture, cannot change it during a left
Master-volume drag, and does not activate from an already-held trigger when the
owner releases. It also checks cyan/amber reticle pixels in both eye images and
restores the original Audio values.

## How It Works

1. The MCP server communicates with the OpenXR Simulator runtime through file-based IPC
2. Screenshot requests are made by writing a JSON file that the runtime watches for
3. The runtime captures the preview backbuffer and saves it as a BMP file
4. Frame status is periodically written to a JSON file for diagnostics

## Troubleshooting

### Screenshots not capturing
- Ensure an OpenXR application is actively rendering frames
- Check that the OpenXR Simulator is set as the active runtime
- Verify the simulator data directory exists: `%LOCALAPPDATA%\OpenXR-Simulator\`

### Log file not found
- The log file is created when the OpenXR Simulator runtime first runs
- Start an OpenXR application to generate log entries

### MCP connection issues
- Ensure the MCP SDK is installed: `pip install mcp`
- Check the Python version is 3.10 or higher

### Multiplayer UI layout checks

Start in the game's Browse Games screen using the isolated simulator profile:

```powershell
python tests/flatout_multiplayer_ui_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --surface browser
```

The test checks neutral-gaze reading position, both-eye visibility after head
movement, shared action-dock spacing and actual ray hovers. It opens room-code
entry, verifies its title/Close spacing, types and deletes a local code through
held/retried ray clicks, rejects disabled keys and background clicks, and closes
back to the browser. It never joins a room. Use `--surface party-lobby` or
`--surface race-lobby` to check the corresponding reading surface and action dock.

Lobby setup uses `--surface online-setup` to verify every setting's visible arrow
pair, localized label/value, hover safety and held-click/opposite-arrow round
trip. From private creation setup, `--create-private` additionally activates
Create Private Lobby once and verifies a private host lobby with one human
player. `--dock-only` rechecks the browser's reading surface and captions without
repeating code entry. No test joins a public room.

### Customization and orbit UI checks

Start in Garage > Customize on the isolated simulator profile:

```powershell
python tests/flatout_customization_ui_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --hand right
```

The check visits all four categories through direct tabs and both arrows. It
requires 80-pixel controls, 20-pixel gaps, separate carousel space, readable
category captions, grey endpoint arrows and inert endpoint presses. Hovering
a tab must preserve the category and item selection. Active clicks are held
for 45 sync cycles and retried with the same command ID, with exactly one
native activation. Both-eye captures retain visual evidence for each category.

Orbit must expose only Return to selection, keep that action at a level reading
position in both eyes after head movement, and return without changing the
selected item. Use `--orbit-only` from Cars or Drivers to check their same return
path. The test navigates previews without buying or equipping items.

### Spatial menu leveling and upgrade previews

From Career's spatial hub, verify the drawn row positions, level basis and real
hand hovers, while retaining the authored position, yaw and nonuniform scale:

```powershell
python tests/flatout_spatial_level_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --menu FLATOUT --reference-layout <prior-layout-json>
```

Omit the reference for a standalone world-up check. The test saves `layout.json`
before asserting, so a failing baseline can supply the reference for the fixed
build. Use the same scene and menu state when comparing authored placement.

For an independent rendering check, `tests/flatout_spatial_menu_gpu.py` replays a
RenderDoc capture and verifies every spatial title's actual GPU world basis in
both eye views. Set `FOX_SPATIAL_GPU_JOB` to a JSON file containing `capture`,
`native_state` (a diagnostic snapshot with `menu.result`) and `result` paths, then
run `qrenderdoc --python tests/flatout_spatial_menu_gpu.py`. Read its result JSON;
a failed check also exits with a nonzero status.

From Career > Garage > Upgrades, check stable hover previews and dialog recovery:

```powershell
python tests/flatout_upgrade_ui_e2e.py --pid <game-pid> --data-dir <ipc-directory> --evidence <new-output-directory> --hand left
```

Hovering different parts must change their rendered stat gauges without moving
the committed selection or opening a dialog. Activating a tile opens its existing
purchase or insufficient-cash dialog; the test cancels a purchase or acknowledges
the cash error, checks padding and exclusive modal input, and never buys a part.
