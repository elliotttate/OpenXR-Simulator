#pragma once

#include <openxr/openxr.h>
#include <openxr/openxr_reflection.h>

#include <cstdint>
#include <string>

namespace enum_strings {

inline std::string Result(XrResult value) {
#define SIMXR_RESULT_NAME(name, number) \
    if (value == static_cast<XrResult>(number)) return #name;
    XR_LIST_ENUM_XrResult(SIMXR_RESULT_NAME)
#undef SIMXR_RESULT_NAME
    const char* prefix = value >= 0 ? "XR_UNKNOWN_SUCCESS_" : "XR_UNKNOWN_FAILURE_";
    return std::string(prefix) + std::to_string(static_cast<int64_t>(value));
}

inline std::string StructureType(XrStructureType value) {
#define SIMXR_STRUCTURE_NAME(name, number) \
    if (value == static_cast<XrStructureType>(number)) return #name;
    XR_LIST_ENUM_XrStructureType(SIMXR_STRUCTURE_NAME)
#undef SIMXR_STRUCTURE_NAME
    return std::string("XR_UNKNOWN_STRUCTURE_TYPE_") +
           std::to_string(static_cast<int64_t>(value));
}

} // namespace enum_strings
