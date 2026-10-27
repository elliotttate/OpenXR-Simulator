#pragma once

#include <openxr/openxr.h>

#include <cstdint>
#include <deque>
#include <vector>

namespace swapchain_state {

enum class ImageState {
    Available,
    Acquired,
    Waited,
};

class Lifecycle {
public:
    void Reset(uint32_t imageCount, bool staticImage) {
        states_.assign(imageCount, ImageState::Available);
        acquired_.clear();
        nextIndex_ = 0;
        staticImage_ = staticImage;
        staticImageAcquired_ = false;
    }

    XrResult Acquire(uint32_t& index) {
        if (states_.empty() || (staticImage_ && staticImageAcquired_)) {
            return XR_ERROR_CALL_ORDER_INVALID;
        }

        for (uint32_t offset = 0; offset < states_.size(); ++offset) {
            const uint32_t candidate = (nextIndex_ + offset) % static_cast<uint32_t>(states_.size());
            if (states_[candidate] != ImageState::Available) continue;

            states_[candidate] = ImageState::Acquired;
            acquired_.push_back(candidate);
            nextIndex_ = (candidate + 1) % static_cast<uint32_t>(states_.size());
            staticImageAcquired_ = true;
            index = candidate;
            return XR_SUCCESS;
        }
        return XR_ERROR_CALL_ORDER_INVALID;
    }

    XrResult Wait(uint32_t& index) {
        if (acquired_.empty()) return XR_ERROR_CALL_ORDER_INVALID;
        index = acquired_.front();
        if (states_[index] != ImageState::Acquired) return XR_ERROR_CALL_ORDER_INVALID;
        states_[index] = ImageState::Waited;
        return XR_SUCCESS;
    }

    XrResult Release(uint32_t& index) {
        if (acquired_.empty()) return XR_ERROR_CALL_ORDER_INVALID;
        index = acquired_.front();
        if (states_[index] != ImageState::Waited) return XR_ERROR_CALL_ORDER_INVALID;
        states_[index] = ImageState::Available;
        acquired_.pop_front();
        return XR_SUCCESS;
    }

    bool IsAppOwned(uint32_t index) const {
        return index < states_.size() && states_[index] != ImageState::Available;
    }

private:
    std::vector<ImageState> states_;
    std::deque<uint32_t> acquired_;
    uint32_t nextIndex_{0};
    bool staticImage_{false};
    bool staticImageAcquired_{false};
};

} // namespace swapchain_state
