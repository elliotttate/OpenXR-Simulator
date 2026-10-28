#pragma once

#include "pose_math.h"

#include <string>

// The simulator drives one pose per hand and reports it as the aim pose. Real
// runtimes report the grip pose at a vendor offset from aim -- 60 degrees for
// every Touch controller -- so an app that draws controller models on the grip
// must see that offset here too, or a model registered for hardware looks wrong.
// Values are SteamVR's render-model components: openxr_grip/openxr_aim, else
// grip/tip, the order SteamVR's runtime uses. Other profiles keep grip at aim.
namespace controller_poses {

// grip = Compose(aim, AimFromGrip(profile, leftHand)).
inline XrPosef AimFromGrip(const std::string& profile, bool leftHand) {
    if (profile == "/interaction_profiles/oculus/touch_controller") {
        return {{0.5f, 0.0f, 0.0f, 0.8660254f}, {0.0f, -0.0299991f, 0.0950003f}};
    }
    if (profile == "/interaction_profiles/valve/index_controller") {
        const float side = leftHand ? 1.0f : -1.0f;
        return {{0.4647712f, 0.0244080f * side, 0.0080248f * side, 0.8850580f},
                {0.0036100f * side, -0.0707737f, 0.0843448f}};
    }
    return pose_math::Identity();
}

} // namespace controller_poses
