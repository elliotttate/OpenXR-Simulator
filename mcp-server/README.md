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

### `hover_ui_at` / `click_ui_at` / `release_ui_pointer`

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

Common parameters:

- `capture_id`, `x`, `y`: Target a pixel in a capture from this MCP session.
- `hand`: `left` or `right` (default).
- `origin_mode`: `hand` (default) intersects a known quad or an explicit scene
  surface, then aims from the normal hand position. `eye` explicitly places the
  controller aim origin at the captured eye and reproduces that view ray.
- `surface_id`: Optional quad ID from the capture metadata, useful for overlapping menus.
- `surface`: Optional application-provided scene UI rectangle in simulator world
  coordinates: `{"pose":{"position":[x,y,z],"orientation":[qx,qy,qz,qw]},"size":[width,height]}`.
  Units are metres; orientation must be a unit quaternion. The rectangle lies in
  its local XY plane. Projection UI currently needs this explicit surface or
  `origin_mode="eye"`; the runtime does not infer scene depth from image colour.
- `ray_offset`: Optional correction for an application that modifies its XR aim
  ray before hit-testing. Supply `{"position":[x,y,z],"orientation":[qx,qy,qz,qw]}`.
  These are independent offsets in simulator world axes: the app adds the
  translation to the controller origin and rotates its direction by the unit
  quaternion. The MCP inverts those operations when aiming. Omit this for apps
  that use the XR aim pose directly. Eye mode keeps the resulting application
  ray at the captured eye; hand mode keeps the controller at its natural origin.
- `dwell_frames`: 1–90 action-sync cycles before completion/press (default 2).
- `timeout`: 0–30 seconds (default 5).
- `command_id`: Optional alphanumeric retry key. Reusing a completed key in the
  same runtime session returns its delivery result without sending another click.
  Reusing it for a different target, button, or operation returns an error.

Click adds `button` (`trigger`, `select`, `primary`, `menu`) and `hold_frames`
(1–90 action-sync cycles, default 2). `primary` is A/X; `select` uses the
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

Two additional tests render the menu plane directly into the stereo projection
images and submit **no quad composition layer**. These verify world-space menu
interaction using both the eye-origin ray and a hand-origin ray aimed at an
explicit scene surface. This covers the distinction between a game's quad-layer
overlay menus and its main menu rendered in the scene, such as the FlatOut 4
cases identified for game-level validation. Three more cases apply FlatOut's
application-side ray offsets before the independent fixture hit-test, verifying
the correction with subpixel accuracy for all three targeting modes.

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
- `screenshot.<capture_id>.bmp` / `screenshot_status.json` - Correlated pixels and frame metadata
- `ui_pointer_command.json` / `ui_pointer_result.<command_id>.json` - Atomic interaction requests and delivery acknowledgments
- `runtime_status.json` - Current frame status
- `ui_flicker_status.json` - UI-only continuity and temporal status
- `ui_flicker_incidents\` - UI-rectangle pre/post evidence packets

Set `SIMXR_DATA_DIR` to the same directory in the app and MCP environments to
isolate a test or multiple app processes. Do not share an IPC directory between
independent runtime processes. Restart the MCP and launch the target app with the
updated runtime DLL to use the new tools.

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
