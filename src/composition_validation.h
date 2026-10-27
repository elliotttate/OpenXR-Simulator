#pragma once

#include <openxr/openxr.h>

#include <cmath>
#include <cstdint>

namespace composition_validation {

struct SwapchainInfo {
    uint32_t width{0};
    uint32_t height{0};
    uint32_t arraySize{0};
    uint32_t faceCount{0};
    bool hasReleasedImage{false};
};

inline bool IsValidFov(const XrFovf& fov) {
    constexpr float halfPi = 1.57079632679489661923f;
    return std::isfinite(fov.angleLeft) && std::isfinite(fov.angleRight) &&
           std::isfinite(fov.angleUp) && std::isfinite(fov.angleDown) &&
           fov.angleLeft > -halfPi && fov.angleLeft < halfPi &&
           fov.angleRight > -halfPi && fov.angleRight < halfPi &&
           fov.angleUp > -halfPi && fov.angleUp < halfPi &&
           fov.angleDown > -halfPi && fov.angleDown < halfPi;
}

inline XrResult ValidateSubImage(const XrSwapchainSubImage& subImage,
                                 const SwapchainInfo& swapchain) {
    if (!swapchain.hasReleasedImage) return XR_ERROR_LAYER_INVALID;
    if (swapchain.faceCount != 1 || subImage.imageArrayIndex >= swapchain.arraySize) {
        return XR_ERROR_VALIDATION_FAILURE;
    }

    const XrRect2Di& rect = subImage.imageRect;
    if (rect.extent.width < 0 || rect.extent.height < 0) {
        return XR_ERROR_SWAPCHAIN_RECT_INVALID;
    }
    const int64_t right = static_cast<int64_t>(rect.offset.x) + rect.extent.width;
    const int64_t bottom = static_cast<int64_t>(rect.offset.y) + rect.extent.height;
    if (rect.offset.x < 0 || rect.offset.y < 0 ||
        right > swapchain.width || bottom > swapchain.height) {
        return XR_ERROR_SWAPCHAIN_RECT_INVALID;
    }
    return XR_SUCCESS;
}

} // namespace composition_validation
