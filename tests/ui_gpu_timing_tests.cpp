#include "ui_enhancements.h"

#include <cstdio>
#include <string>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

} // namespace

int main() {
    Check(ui::g_gpuTiming.load(), "GPU timing defaults to on");

    // Saved and restored with the other settings.
    ui::ApplySettingsJson("{\"gpu_timing\": false}");
    Check(!ui::g_gpuTiming.load(), "gpu_timing loads from the settings file");
    Check(ui::SerializeSettings().find("\"gpu_timing\": false") != std::string::npos, "gpu_timing is saved");
    ui::ApplySettingsJson("{}");
    Check(!ui::g_gpuTiming.load(), "a settings file without gpu_timing leaves it alone");

    // Tools > GPU Timing flips it, unless OPENXR_SIM_GPU_TIMING pins it.
    ui::HandleMenuCommand(nullptr, ui::ID_TOOLS_GPU_TIMING);
    if (ui::GpuTimingForced() < 0) {
        Check(ui::g_gpuTiming.load() && ui::GpuTimingOn(), "the menu switches it back on");
        Check(ui::SerializeSettings().find("\"gpu_timing\": true") != std::string::npos,
              "the toggled value is what gets saved");
    } else {
        Check(!ui::g_gpuTiming.load(), "the menu leaves it alone while the environment pins it");
        Check(ui::GpuTimingOn() == (ui::GpuTimingForced() != 0), "the environment decides");
    }

    if (failures) return 1;
    std::puts("ui_gpu_timing_tests: PASS");
    return 0;
}
