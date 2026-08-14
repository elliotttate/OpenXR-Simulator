#include "ui_enhancements.h"

#include <cmath>
#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

bool Near(float a, float b) {
    return std::fabs(a - b) < 0.001f;
}

} // namespace

int main() {
    ui::g_uiState = ui::UIState{};

    uint32_t width = 0, height = 0;
    ui::GetRenderResolution(width, height);
    Check(width == 1280 && height == 1400,
          "performance resolution is the default");

    ui::SetHeadsetProfile(ui::HeadsetProfile::Quest3);
    ui::SetRenderResolution(0, 0);
    ui::GetRenderResolution(width, height);
    Check(width == 2064 && height == 2208,
          "native mode follows the active headset panel");

    ui::SetHeadsetProfile(ui::HeadsetProfile::ValveIndex);
    ui::GetRenderResolution(width, height);
    Check(width == 1440 && height == 1600,
          "native mode updates when the headset profile changes");

    ui::SetRenderResolution(1280, 1400);
    ui::SetHeadsetProfile(ui::HeadsetProfile::Quest3);
    ui::GetRenderResolution(width, height);
    Check(width == 1280 && height == 1400,
          "explicit render resolution remains independent of headset geometry");

    ui::SetRenderResolution(1, 9000);
    ui::GetRenderResolution(width, height);
    Check(width == 320 && height == 4096,
          "custom render resolution is clamped to runtime limits");

    ui::g_previewGeom = { 1920, 1080, 4128, 2208 };
    ui::SetFitToWindow();
    const ui::PreviewRect filled = ui::ComputePreviewRect();
    Check(Near(filled.x, 0.0f) && Near(filled.y, 0.0f) &&
          Near(filled.w, 1920.0f) && Near(filled.h, 1080.0f),
          "fill mode covers the entire client area at a different aspect");

    ui::g_previewGeom = { 640, 360, 1920, 1080 };
    const ui::PreviewRect downscaled = ui::ComputePreviewRect();
    Check(Near(downscaled.w, 640.0f) && Near(downscaled.h, 360.0f),
          "fill mode downscales a larger render to the client area");

    ui::g_previewGeom = { 3840, 2160, 960, 540 };
    const ui::PreviewRect upscaled = ui::ComputePreviewRect();
    Check(Near(upscaled.w, 3840.0f) && Near(upscaled.h, 2160.0f),
          "fill mode upscales a smaller render to the client area");

    ui::ApplySettingsJson(
        "{\"render_width\": 1500, \"render_height\": 1600, \"zoom_mode\": \"fill\"}");
    ui::GetRenderResolution(width, height);
    Check(width == 1500 && height == 1600 && ui::g_uiState.fitToWindow,
          "custom settings restore resolution and fill mode");

    ui::g_uiState = ui::UIState{};
    ui::ApplySettingsJson("{\"zoom_mode\": \"scale\", \"zoom_scale\": 1.0}");
    ui::GetRenderResolution(width, height);
    Check(width == 1280 && height == 1400 && ui::g_uiState.fitToWindow,
          "older settings migrate to the performance resolution and full-window fill");

    if (failures != 0) return 1;
    std::puts("ui_resolution_tests: PASS");
    return 0;
}
