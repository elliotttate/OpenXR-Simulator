#pragma once

#include <openxr/openxr.h>

#include <algorithm>
#include <cctype>
#include <string>

namespace interaction_query {

constexpr int kLeftHandSource = 1;
constexpr int kRightHandSource = 2;
constexpr int kHeadSource = 4;
constexpr int kGamepadSource = 8;
constexpr int kAllActionSources =
    kLeftHandSource | kRightHandSource | kHeadSource | kGamepadSource;

inline int ActionSourceMask(const std::string& path) {
    if (path == "/user/hand/left") return kLeftHandSource;
    if (path == "/user/hand/right") return kRightHandSource;
    if (path == "/user/head") return kHeadSource;
    if (path == "/user/gamepad") return kGamepadSource;
    return 0;
}

inline size_t ActionSlotForSourceMask(int sourceMask) {
    if (sourceMask == kLeftHandSource) return 1;
    if (sourceMask == kRightHandSource) return 2;
    if (sourceMask == kHeadSource) return 3;
    if (sourceMask == kGamepadSource) return 4;
    return 0;
}

inline bool IsSimulatedHandSource(int sourceMask) {
    return sourceMask == kLeftHandSource || sourceMask == kRightHandSource;
}

inline bool IsCoreTopLevelUserPath(const std::string& path) {
    return path == "/user/hand/left" || path == "/user/hand/right" ||
           path == "/user/head" || path == "/user/gamepad" ||
           path == "/user/treadmill";
}

inline std::string TopLevelForSourcePath(const std::string& path) {
    static const char* const paths[] = {
        "/user/hand/left", "/user/hand/right", "/user/head",
        "/user/gamepad", "/user/treadmill",
    };
    for (const char* topLevel : paths) {
        const size_t length = std::char_traits<char>::length(topLevel);
        if (path.compare(0, length, topLevel) == 0 &&
            (path.size() == length || path[length] == '/')) {
            return topLevel;
        }
    }
    return {};
}

inline bool ProfileSupportsTopLevel(const std::string& profile,
                                    const std::string& topLevel) {
    if (profile == "/interaction_profiles/htc/vive_pro") {
        return topLevel == "/user/head";
    }
    if (profile == "/interaction_profiles/microsoft/xbox_controller") {
        return topLevel == "/user/gamepad";
    }
    const bool handProfile =
        profile == "/interaction_profiles/khr/simple_controller" ||
        profile == "/interaction_profiles/google/daydream_controller" ||
        profile == "/interaction_profiles/htc/vive_controller" ||
        profile == "/interaction_profiles/microsoft/motion_controller" ||
        profile == "/interaction_profiles/oculus/go_controller" ||
        profile == "/interaction_profiles/oculus/touch_controller" ||
        profile == "/interaction_profiles/valve/index_controller";
    return handProfile &&
           (topLevel == "/user/hand/left" || topLevel == "/user/hand/right");
}

inline std::string UserPathName(const std::string& topLevel) {
    if (topLevel == "/user/hand/left") return "Left Hand";
    if (topLevel == "/user/hand/right") return "Right Hand";
    if (topLevel == "/user/head") return "Head";
    if (topLevel == "/user/gamepad") return "Gamepad";
    if (topLevel == "/user/treadmill") return "Treadmill";
    return {};
}

inline std::string InteractionProfileName(const std::string& profile) {
    if (profile == "/interaction_profiles/khr/simple_controller") return "Simple Controller";
    if (profile == "/interaction_profiles/google/daydream_controller") return "Daydream Controller";
    if (profile == "/interaction_profiles/htc/vive_controller") return "Vive Controller";
    if (profile == "/interaction_profiles/htc/vive_pro") return "Vive Pro";
    if (profile == "/interaction_profiles/microsoft/motion_controller") return "Motion Controller";
    if (profile == "/interaction_profiles/microsoft/xbox_controller") return "Xbox Controller";
    if (profile == "/interaction_profiles/oculus/go_controller") return "Oculus Go Controller";
    if (profile == "/interaction_profiles/oculus/touch_controller") return "Oculus Touch Controller";
    if (profile == "/interaction_profiles/valve/index_controller") return "Valve Index Controller";
    return {};
}

inline std::string TitleCaseIdentifier(std::string identifier) {
    bool capitalize = true;
    for (char& character : identifier) {
        if (character == '_') {
            character = ' ';
            capitalize = true;
        } else if (capitalize) {
            character = static_cast<char>(std::toupper(static_cast<unsigned char>(character)));
            capitalize = false;
        }
    }
    return identifier;
}

inline std::string ComponentName(const std::string& sourcePath) {
    size_t start = sourcePath.find("/input/");
    if (start == std::string::npos) start = sourcePath.find("/output/");
    if (start == std::string::npos) return {};
    start = sourcePath.find('/', start + 1) + 1;
    const size_t end = sourcePath.find('/', start);
    std::string identifier = sourcePath.substr(start, end - start);
    const bool button = identifier == "a" || identifier == "b" ||
                        identifier == "x" || identifier == "y";
    std::string name = TitleCaseIdentifier(std::move(identifier));
    if (button) name += " Button";
    if (end != std::string::npos && sourcePath.substr(end + 1) == "pose") name += " Pose";
    return name;
}

inline std::string LocalizedSourceName(const std::string& sourcePath,
                                       const std::string& profile,
                                       XrInputSourceLocalizedNameFlags components) {
    std::string result;
    const auto append = [&](const std::string& part) {
        if (part.empty()) return;
        if (!result.empty()) result += " ";
        result += part;
    };
    if (components & XR_INPUT_SOURCE_LOCALIZED_NAME_USER_PATH_BIT) {
        append(UserPathName(TopLevelForSourcePath(sourcePath)));
    }
    if (components & XR_INPUT_SOURCE_LOCALIZED_NAME_INTERACTION_PROFILE_BIT) {
        append(InteractionProfileName(profile));
    }
    if (components & XR_INPUT_SOURCE_LOCALIZED_NAME_COMPONENT_BIT) {
        append(ComponentName(sourcePath));
    }
    return result;
}

} // namespace interaction_query
