#include "ui_enhancements.h"

#include <cstdio>
#include <cstring>
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
    ui::g_uiState = ui::UIState{};

    Check(ui::g_uiState.inputPolling == ui::InputPolling::AppForeground,
          "input polling defaults to accepting the application's own window");

    // A reordered table would silently rename every saved setting.
    Check(ui::kInputPollingPresetCount == 3, "three input polling modes");
    Check(std::strcmp(ui::InputPollingName(ui::InputPolling::PreviewFocused), "preview") == 0 &&
          std::strcmp(ui::InputPollingName(ui::InputPolling::AppForeground), "app") == 0 &&
          std::strcmp(ui::InputPollingName(ui::InputPolling::Background), "background") == 0,
          "each enum value names its own table row");

    for (int i = 0; i < ui::kInputPollingPresetCount; ++i) {
        const ui::InputPolling mode = (ui::InputPolling)i;
        Check(ui::InputPollingFromName(ui::InputPollingName(mode),
                                       ui::InputPolling::PreviewFocused) == mode,
              "every mode survives a name round trip");
    }

    Check(ui::InputPollingFromName("nonsense", ui::InputPolling::Background) ==
              ui::InputPolling::Background,
          "an unreadable name leaves the current mode alone");

    Check(ui::IsInputPollingCommand(ui::ID_INPUT_POLLING_FIRST) &&
          ui::IsInputPollingCommand(ui::ID_INPUT_POLLING_FIRST +
                                    ui::kInputPollingPresetCount - 1) &&
          !ui::IsInputPollingCommand(ui::ID_INPUT_POLLING_FIRST +
                                     ui::kInputPollingPresetCount),
          "only ids backed by a table row are input polling commands");

    ui::g_uiState.inputPolling = ui::InputPolling::Background;
    const std::string json = ui::SerializeSettings();
    Check(json.find("\"input_polling\": \"background\"") != std::string::npos,
          "the chosen mode is written to settings");

    ui::g_uiState = ui::UIState{};
    ui::ApplySettingsJson(json.c_str());
    Check(ui::g_uiState.inputPolling == ui::InputPolling::Background,
          "the mode is restored from settings");

    ui::g_uiState = ui::UIState{};
    ui::ApplySettingsJson("{\"view_mode\": \"both\"}");
    Check(ui::g_uiState.inputPolling == ui::InputPolling::AppForeground,
          "an older settings file keeps the default mode");

    if (failures != 0) return 1;
    std::puts("ui_input_polling_tests: PASS");
    return 0;
}
