# Explicit session focus testing

`set_session_focus({"focused": false})` asks the live simulator to move its
running application from `FOCUSED` to `VISIBLE`. Rendering and head tracking
continue; action states become inactive and existing pointer leases release.
`set_session_focus({"focused": true})` restores input focus. Restore it in test
cleanup. A new session starts with the normal focused behavior.

This is an explicit test command. Activating or deactivating the desktop preview
continues to affect only preview keyboard/mouse input. The command does not
reproduce SteamVR dashboard composition, motion smoothing or hardware timing.

The command is queued, so wait for `get_session_state` to report the requested
state and take a fresh screenshot before checking rendered results. An isolated
runtime without diagnostic logs needs a fresh capture to establish ownership
before control commands can be queued. No command is written for a dead runtime.

The runtime consumes file IPC on the frame thread and applies the state transition
on the `xrPollEvent` thread. It does not enqueue state events concurrently from
`xrWaitFrame`. Only a running `VISIBLE`/`FOCUSED` session can change focus; a
stopping or ended session is not restarted by this command.

Control-command wakeups retain their own generation. A screenshot/render stage
cannot erase a head-pose or focus command arriving after the control stage has
already run. `command_epochs_tests` covers the lost-wakeup ordering in Release
as well as notifications arriving during processing.

The boundary follows the [OpenXR session-state rules](https://registry.khronos.org/OpenXR/specs/1.0-khr/html/xrspec.html#session-lifecycle).
Use `tests/flatout_dashboard_focus_e2e.py` on an owned disposable game session
at its initial welcome screen. It checks repeated focus loss/return, inactive
held triggers, continued rendering, stable upright popup placement, opaque
padding in both eyes and one final OK activation. Actual SteamVR dashboard
testing remains a separate requirement.
After that test reveals Main, run it again with `--parent-dialog` to exercise
the same boundary on an owned diagnostic confirmation over the spatial menu.
The diagnostic uses Cancel and has no original game action attached.
