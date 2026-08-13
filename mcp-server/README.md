# OpenXR Simulator MCP Server

A Model Context Protocol (MCP) server that provides tools for diagnosing OpenXR issues and capturing screenshots from the OpenXR Simulator runtime.

## Features

- **Screenshot Capture**: Capture the current XR frame being rendered (stereo view)
- **Frame Diagnostics**: Get detailed frame timing, resolution, and format information
- **Log Analysis**: Read and analyze simulator logs for debugging
- **Issue Diagnosis**: Automated analysis of common OpenXR problems
- **Session Monitoring**: Track session state and head tracking
- **UI Flicker Detection**: Inspect quad-layer continuity independently from world/projection motion

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
pip install mcp
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
- `eye`: Which eye view to capture ("both", "left", "right") - default: "both"
- `timeout`: Timeout in seconds - default: 5.0

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
- `runtime_status.json` - Current frame status
- `ui_flicker_status.json` - UI-only continuity and temporal status
- `ui_flicker_incidents\` - UI-rectangle pre/post evidence packets

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
