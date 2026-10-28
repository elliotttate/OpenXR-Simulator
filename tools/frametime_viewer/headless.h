// Command-line modes of openxr_sim_frametime.exe that run without a window,
// for scripts and the MCP server. They share every definition with the viewer,
// so a number from a script means what it means on screen.
//
//   --list-sessions                     processes publishing a frame timeline
//   --live-summary --pid N [--seconds S] the last S seconds of a running app
//   --record --pid N [--out F] [--label L] [--stop-file P] [--seconds S]
//                                       record until P exists, S passes or the app exits
//   --analyze F                         summarize a capture
//   --export-csv F --out C              one CSV row per frame
//
// Common: --json OUT (write the result there, else stdout), --hz H (score
// against another refresh rate), --hitch-budgets X, --top N (worst frames listed).
#pragma once
#include <windows.h>

namespace viewer {

// True if argv names a headless mode; `exitCode` is then the process result.
bool RunHeadless(int argc, wchar_t** argv, int& exitCode);

} // namespace viewer
