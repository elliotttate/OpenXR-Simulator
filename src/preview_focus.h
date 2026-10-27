#pragma once

#include <openxr/openxr.h>

namespace preview_focus {

struct ActivationEffects {
    bool inputFocused{false};
    bool releaseMouseCapture{false};
    XrSessionState sessionState{XR_SESSION_STATE_UNKNOWN};
};

constexpr ActivationEffects OnActivation(
    const bool active,
    const XrSessionState sessionState) noexcept {
    // The desktop preview is a runtime-owned diagnostic surface. Its Win32
    // activation controls preview input only; it is not the user's OpenXR
    // focus authority and must never demote or promote the application session.
    return ActivationEffects{active, !active, sessionState};
}

} // namespace preview_focus
