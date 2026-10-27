#include "api_validation.h"
#include "enum_strings.h"

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
    const XrVersion supported = XR_MAKE_VERSION(1, 0, 34);
    Check(api_validation::ValidateApiVersion(XR_MAKE_VERSION(1, 0, 34), supported) == XR_SUCCESS,
          "the advertised API version is accepted");
    Check(api_validation::ValidateApiVersion(XR_MAKE_VERSION(1, 0, 35), supported) == XR_SUCCESS,
          "patch revisions remain fully compatible");
    Check(api_validation::ValidateApiVersion(XR_MAKE_VERSION(1, 1, 0), supported) ==
              XR_ERROR_API_VERSION_UNSUPPORTED,
          "a newer minor version is rejected");
    Check(api_validation::IsWellFormedPath("/user/hand/left"),
          "a normal semantic path is well formed");
    Check(!api_validation::IsWellFormedPath("user/hand/left"),
          "a path must begin with a slash");
    Check(!api_validation::IsWellFormedPath("/user//left"),
          "a path cannot contain an empty level");
    Check(!api_validation::IsWellFormedPath("/user/../left"),
          "a path level cannot contain only periods");
    Check(!api_validation::IsWellFormedPath("/User/hand"),
          "a path cannot contain uppercase characters");
    Check(api_validation::IsWellFormedPathLevel("game_play-1.0", 32),
          "an action name may contain path-level characters");
    Check(!api_validation::IsWellFormedPathLevel("game/play", 32),
          "an action name cannot contain a path separator");
    Check(!api_validation::IsWellFormedPathLevel("..", 32),
          "an action name cannot consist only of periods");
    Check(!api_validation::IsWellFormedPathLevel("GamePlay", 32),
          "an action name cannot contain uppercase characters");

    uint32_t count = 0;
    int values[2]{};
    Check(api_validation::ValidateEnumeration(0, &count, (int*)nullptr, 2) == XR_SUCCESS &&
              count == 2,
          "a zero-capacity query reports the required count");
    Check(api_validation::ValidateEnumeration(1, &count, values, 2) ==
              XR_ERROR_SIZE_INSUFFICIENT && count == 2,
          "an undersized enumeration reports size insufficient");
    Check(api_validation::ValidateEnumeration(2, &count, values, 2) == XR_SUCCESS,
          "an exact-capacity enumeration succeeds");
    Check(api_validation::ValidateEnumeration(2, (uint32_t*)nullptr, values, 2) ==
              XR_ERROR_VALIDATION_FAILURE,
          "an enumeration requires a count output");
    Check(api_validation::ValidateEnumeration(2, &count, (int*)nullptr, 2) ==
              XR_ERROR_VALIDATION_FAILURE,
          "a nonzero capacity requires output storage");

    XrViewConfigurationView view{XR_TYPE_VIEW_CONFIGURATION_VIEW};
    Check(api_validation::ValidateOutputStruct(&view, XR_TYPE_VIEW_CONFIGURATION_VIEW) == XR_SUCCESS,
          "an output struct with the expected type succeeds");
    view.type = XR_TYPE_UNKNOWN;
    Check(api_validation::ValidateOutputStruct(&view, XR_TYPE_VIEW_CONFIGURATION_VIEW) ==
              XR_ERROR_VALIDATION_FAILURE,
          "an output struct with the wrong type is rejected");

    Check(enum_strings::Result(XR_ERROR_SESSION_NOT_RUNNING) ==
              "XR_ERROR_SESSION_NOT_RUNNING",
          "a known result uses its exact OpenXR token");
    Check(enum_strings::Result(XR_ERROR_MARKER_DETECTOR_PERMISSION_DENIED_ML) ==
              "XR_ERROR_MARKER_DETECTOR_PERMISSION_DENIED_ML",
          "a result added by OpenXR 1.0.34 is recognized");
    Check(enum_strings::Result(static_cast<XrResult>(12345)) ==
              "XR_UNKNOWN_SUCCESS_12345",
          "an unknown positive result uses the required success form");
    Check(enum_strings::Result(static_cast<XrResult>(-12345)) ==
              "XR_UNKNOWN_FAILURE_-12345",
          "an unknown negative result uses the required failure form");
    Check(enum_strings::StructureType(XR_TYPE_FRAME_STATE) == "XR_TYPE_FRAME_STATE",
          "a known structure type uses its exact OpenXR token");
    Check(enum_strings::StructureType(XR_TYPE_SYSTEM_MARKER_UNDERSTANDING_PROPERTIES_ML) ==
              "XR_TYPE_SYSTEM_MARKER_UNDERSTANDING_PROPERTIES_ML",
          "a structure type added by OpenXR 1.0.34 is recognized");
    Check(enum_strings::StructureType(static_cast<XrStructureType>(12345)) ==
              "XR_UNKNOWN_STRUCTURE_TYPE_12345",
          "an unknown structure type includes its decimal value");

    if (failures) return 1;
    std::puts("API validation tests passed");
    return 0;
}
