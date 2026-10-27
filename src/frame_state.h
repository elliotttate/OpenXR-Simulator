#pragma once

#include <openxr/openxr.h>

namespace frame_state {

class Lifecycle {
public:
    void Reset() {
        waitPending_ = false;
        frameBegun_ = false;
    }

    bool CanWait() const { return !waitPending_; }

    XrResult MarkWaited() {
        if (waitPending_) return XR_ERROR_CALL_ORDER_INVALID;
        waitPending_ = true;
        return XR_SUCCESS;
    }

    XrResult Begin() {
        if (!waitPending_) return XR_ERROR_CALL_ORDER_INVALID;
        waitPending_ = false;
        if (frameBegun_) return XR_FRAME_DISCARDED;
        frameBegun_ = true;
        return XR_SUCCESS;
    }

    bool CanEnd() const { return frameBegun_; }

    XrResult End() {
        if (!frameBegun_) return XR_ERROR_CALL_ORDER_INVALID;
        frameBegun_ = false;
        return XR_SUCCESS;
    }

private:
    bool waitPending_{false};
    bool frameBegun_{false};
};

} // namespace frame_state
