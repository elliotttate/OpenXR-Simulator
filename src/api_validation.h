#pragma once

#include <openxr/openxr.h>

#include <cstdint>
#include <cstring>

namespace api_validation {

// D3D11/12 require feature level 11.0, whose Texture2D limit is 16384. Any other
// enabled graphics binding keeps the conservative 4096 cap.
inline uint32_t MaxSwapchainDimension(bool direct3D, bool otherGraphics) {
    return direct3D && !otherGraphics ? 16384u : 4096u;
}
inline bool SwapchainExtentValid(uint32_t width, uint32_t height, uint32_t limit) {
    return width > 0 && height > 0 && width <= limit && height <= limit;
}

inline XrResult ValidateApiVersion(XrVersion requested, XrVersion supported) {
    // Patch revisions are fully compatible in OpenXR. Compare the interface-bearing
    // major/minor pair and deliberately ignore patch.
    if (requested == 0 || XR_VERSION_MAJOR(requested) != XR_VERSION_MAJOR(supported) ||
        XR_VERSION_MINOR(requested) > XR_VERSION_MINOR(supported)) {
        return XR_ERROR_API_VERSION_UNSUPPORTED;
    }
    return XR_SUCCESS;
}

// The newest API version the runtime implements that the loader's range admits:
// 1.1, or 1.0 for a loader whose range stops short of 1.1; 0 when neither fits.
// Like ValidateApiVersion, this compares major/minor and ignores patch.
inline XrVersion NegotiateApiVersion(XrVersion loaderMin, XrVersion loaderMax) {
    const auto majorMinor = [](XrVersion version) {
        return (static_cast<uint64_t>(XR_VERSION_MAJOR(version)) << 16) | XR_VERSION_MINOR(version);
    };
    static const XrVersion candidates[] = {XR_API_VERSION_1_1, XR_API_VERSION_1_0};
    for (const XrVersion candidate : candidates) {
        if (majorMinor(loaderMin) <= majorMinor(candidate) &&
            majorMinor(candidate) <= majorMinor(loaderMax)) {
            return candidate;
        }
    }
    return 0;
}

inline bool IsWellFormedPath(const char* path) {
    if (!path) return false;
    const size_t length = strnlen(path, XR_MAX_PATH_LENGTH);
    if (length < 2 || length == XR_MAX_PATH_LENGTH || path[0] != '/' ||
        path[length - 1] == '/') {
        return false;
    }
    size_t segmentStart = 1;
    for (size_t index = 1; index <= length; ++index) {
        const char value = index == length ? '/' : path[index];
        const bool allowed = (value >= 'a' && value <= 'z') ||
                             (value >= '0' && value <= '9') || value == '-' ||
                             value == '_' || value == '.' || value == '/';
        if (!allowed) return false;
        if (value != '/') continue;
        if (index == segmentStart) return false;
        bool onlyPeriods = true;
        for (size_t character = segmentStart; character < index; ++character) {
            if (path[character] != '.') {
                onlyPeriods = false;
                break;
            }
        }
        if (onlyPeriods) return false;
        segmentStart = index + 1;
    }
    return true;
}

inline bool IsWellFormedPathLevel(const char* name, size_t capacity) {
    if (!name || capacity == 0) return false;
    const size_t length = strnlen(name, capacity);
    if (length == 0 || length == capacity) return false;
    bool onlyPeriods = true;
    for (size_t index = 0; index < length; ++index) {
        const char value = name[index];
        const bool allowed = (value >= 'a' && value <= 'z') ||
                             (value >= '0' && value <= '9') || value == '-' ||
                             value == '_' || value == '.';
        if (!allowed) return false;
        if (value != '.') onlyPeriods = false;
    }
    return !onlyPeriods;
}

template <typename T>
inline XrResult ValidateEnumeration(uint32_t capacityInput, uint32_t* countOutput,
                                    T* values, uint32_t requiredCount) {
    if (!countOutput) return XR_ERROR_VALIDATION_FAILURE;
    *countOutput = requiredCount;
    if (capacityInput == 0) return XR_SUCCESS;
    if (!values) return XR_ERROR_VALIDATION_FAILURE;
    if (capacityInput < requiredCount) return XR_ERROR_SIZE_INSUFFICIENT;
    return XR_SUCCESS;
}

template <typename T>
inline XrResult ValidateOutputStruct(const T* value, XrStructureType expectedType) {
    if (!value || value->type != expectedType) return XR_ERROR_VALIDATION_FAILURE;
    return XR_SUCCESS;
}

} // namespace api_validation
