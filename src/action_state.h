#pragma once

#include <openxr/openxr.h>

#include <cmath>
#include <string>
#include <unordered_map>

namespace input {

inline XrResult ValidateHapticVibration(const XrHapticVibration& vibration) {
    if ((vibration.duration != XR_MIN_HAPTIC_DURATION && vibration.duration <= 0) ||
        !std::isfinite(vibration.frequency) || vibration.frequency < 0.0f ||
        !std::isfinite(vibration.amplitude) ||
        vibration.amplitude < 0.0f || vibration.amplitude > 1.0f) {
        return XR_ERROR_VALIDATION_FAILURE;
    }
    return XR_SUCCESS;
}

template <size_t N>
inline bool BindingPathAllowed(const std::string& value, const char* const (&leafPaths)[N]) {
    for (const char* leaf : leafPaths) {
        if (value == leaf) return true;
    }
    if (value.compare(0, 6, "input/") != 0) return false;
    const std::string childPrefix = value + "/";
    for (const char* leaf : leafPaths) {
        if (std::string(leaf).compare(0, childPrefix.size(), childPrefix) == 0) return true;
    }
    return false;
}

template <typename T>
inline bool ValuesEqual(const T& left, const T& right) {
    return left == right;
}

template <>
inline bool ValuesEqual<XrVector2f>(const XrVector2f& left, const XrVector2f& right) {
    return left.x == right.x && left.y == right.y;
}

// OpenXR action values are sampled by xrSyncActions and remain stable until the
// next successful sync. This helper owns that two-snapshot history and applies
// the inactive-state rules shared by boolean, float, and vector actions.
template <typename T>
struct LatchedValue {
    T current{};
    XrBool32 active{XR_FALSE};
    XrBool32 changed{XR_FALSE};
    XrTime lastChangeTime{0};
    bool hasPreviousSync{false};

    void Sync(const T& next, bool nextActive, XrTime syncTime) {
        const bool wasActive = active == XR_TRUE;
        const T previous = current;

        if (!nextActive) {
            current = T{};
            active = XR_FALSE;
            changed = XR_FALSE;
            lastChangeTime = 0;
            hasPreviousSync = true;
            return;
        }

        current = next;
        active = XR_TRUE;
        changed = hasPreviousSync && wasActive && !ValuesEqual(previous, next)
            ? XR_TRUE : XR_FALSE;

        // A newly active source has no prior comparable sync, but its physical
        // state still has a meaningful best-known change time.
        if (!hasPreviousSync || !wasActive || changed == XR_TRUE) {
            lastChangeTime = syncTime;
        }
        hasPreviousSync = true;
    }
};

struct ActionSlot {
    LatchedValue<XrBool32> boolean;
    LatchedValue<float> scalar;
    LatchedValue<XrVector2f> vector;
    XrBool32 poseActive{XR_FALSE};

    void SetInactive(XrTime syncTime) {
        boolean.Sync(XR_FALSE, false, syncTime);
        scalar.Sync(0.0f, false, syncTime);
        vector.Sync(XrVector2f{0.0f, 0.0f}, false, syncTime);
        poseActive = XR_FALSE;
    }
};

class BindingPriorities {
public:
    void Observe(const std::string& source, uint32_t priority) {
        auto inserted = highest_.emplace(source, priority);
        if (!inserted.second && inserted.first->second < priority) {
            inserted.first->second = priority;
        }
    }

    bool Allows(const std::string& source, uint32_t priority) const {
        auto it = highest_.find(source);
        return it == highest_.end() || priority >= it->second;
    }

private:
    std::unordered_map<std::string, uint32_t> highest_;
};

inline std::string BindingCollisionKey(const std::string& path) {
    const size_t slash = path.rfind('/');
    if (slash == std::string::npos) return path;
    const std::string component = path.substr(slash + 1);
    if (component == "click" || component == "touch" || component == "value" ||
        component == "force" || component == "x" || component == "y" || component == "pose") {
        return path.substr(0, slash);
    }
    return path;
}

} // namespace input
