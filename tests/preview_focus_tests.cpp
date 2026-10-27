#include "preview_focus.h"

#include <array>
#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

} // namespace

int main() {
    constexpr std::array states{
        XR_SESSION_STATE_READY,
        XR_SESSION_STATE_SYNCHRONIZED,
        XR_SESSION_STATE_VISIBLE,
        XR_SESSION_STATE_FOCUSED,
        XR_SESSION_STATE_STOPPING,
    };

    for (const XrSessionState state : states) {
        const preview_focus::ActivationEffects activated =
            preview_focus::OnActivation(true, state);
        Check(activated.inputFocused, "activation enables preview input");
        Check(!activated.releaseMouseCapture,
              "activation does not release preview mouse capture");
        Check(activated.sessionState == state,
              "preview activation preserves the OpenXR session state");

        const preview_focus::ActivationEffects deactivated =
            preview_focus::OnActivation(false, state);
        Check(!deactivated.inputFocused, "deactivation disables preview input");
        Check(deactivated.releaseMouseCapture,
              "deactivation releases preview mouse capture");
        Check(deactivated.sessionState == state,
              "preview deactivation preserves the OpenXR session state");
    }

    if (failures) return 1;
    std::puts("preview focus tests passed");
    return 0;
}
