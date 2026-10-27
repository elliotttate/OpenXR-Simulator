#include "action_state.h"
#include "interaction_query.h"

#include <cmath>
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
    XrHapticVibration vibration{XR_TYPE_HAPTIC_VIBRATION};
    vibration.duration = XR_MIN_HAPTIC_DURATION;
    vibration.frequency = XR_FREQUENCY_UNSPECIFIED;
    vibration.amplitude = 1.0f;
    Check(input::ValidateHapticVibration(vibration) == XR_SUCCESS,
          "minimum-duration vibration with unspecified frequency is valid");
    vibration.duration = 1;
    vibration.amplitude = 0.0f;
    Check(input::ValidateHapticVibration(vibration) == XR_SUCCESS,
          "finite positive duration and zero amplitude are valid");
    vibration.duration = 0;
    Check(input::ValidateHapticVibration(vibration) == XR_ERROR_VALIDATION_FAILURE,
          "zero haptic duration is invalid");
    vibration.duration = 1;
    vibration.frequency = -1.0f;
    Check(input::ValidateHapticVibration(vibration) == XR_ERROR_VALIDATION_FAILURE,
          "negative haptic frequency is invalid");
    vibration.frequency = NAN;
    Check(input::ValidateHapticVibration(vibration) == XR_ERROR_VALIDATION_FAILURE,
          "non-finite haptic frequency is invalid");
    vibration.frequency = 1.0f;
    vibration.amplitude = 1.01f;
    Check(input::ValidateHapticVibration(vibration) == XR_ERROR_VALIDATION_FAILURE,
          "haptic amplitude above one is invalid");
    vibration.amplitude = NAN;
    Check(input::ValidateHapticVibration(vibration) == XR_ERROR_VALIDATION_FAILURE,
          "non-finite haptic amplitude is invalid");

    input::LatchedValue<XrBool32> button;

    button.Sync(XR_FALSE, true, 100);
    Check(button.active == XR_TRUE, "first bound sync is active");
    Check(button.changed == XR_FALSE, "first sync does not report a change");
    Check(button.lastChangeTime == 100, "first active sync gets a valid change time");

    button.Sync(XR_TRUE, true, 200);
    Check(button.current == XR_TRUE, "new button value is latched");
    Check(button.changed == XR_TRUE, "a value transition is reported");
    Check(button.lastChangeTime == 200, "transition records its sync time");

    button.Sync(XR_TRUE, true, 300);
    Check(button.changed == XR_FALSE, "unchanged consecutive sync is not reported as changed");
    Check(button.lastChangeTime == 200, "unchanged state preserves its change time");

    button.Sync(XR_TRUE, false, 400);
    Check(button.active == XR_FALSE, "unavailable input becomes inactive");
    Check(button.current == XR_FALSE, "inactive input returns zero state");
    Check(button.changed == XR_FALSE, "inactive input never reports a change");
    Check(button.lastChangeTime == 0, "inactive input returns zero change time");

    button.Sync(XR_TRUE, true, 500);
    Check(button.active == XR_TRUE, "input can reactivate");
    Check(button.changed == XR_FALSE, "reactivation has no comparable previous active sync");
    Check(button.lastChangeTime == 500, "reactivation gets a fresh best-known change time");

    input::LatchedValue<XrVector2f> stick;
    stick.Sync(XrVector2f{0.0f, 0.0f}, true, 600);
    stick.Sync(XrVector2f{0.25f, -0.5f}, true, 700);
    Check(stick.changed == XR_TRUE, "vector component changes are detected");
    Check(stick.current.x == 0.25f && stick.current.y == -0.5f,
          "vector state is latched exactly");

    input::BindingPriorities priorities;
    const std::string triggerSource = input::BindingCollisionKey(
        "/user/hand/right/input/trigger/click");
    Check(triggerSource == input::BindingCollisionKey("/user/hand/right/input/trigger/value"),
          "different components of one input identifier share a priority key");
    priorities.Observe(triggerSource, 1);
    priorities.Observe(triggerSource, 10);
    Check(!priorities.Allows(triggerSource, 1), "lower-priority binding is suppressed");
    Check(priorities.Allows(triggerSource, 10), "highest-priority binding remains active");
    Check(priorities.Allows(triggerSource, 11), "a newly higher priority is not suppressed");
    Check(priorities.Allows(input::BindingCollisionKey("/user/hand/right/input/squeeze/value"), 0),
          "an uncontested source remains active");

    static const char* const touchStickPaths[] = {
        "input/thumbstick/x", "input/thumbstick/y", "input/thumbstick/click", "input/thumbstick/touch",
    };
    Check(input::BindingPathAllowed("input/thumbstick", touchStickPaths),
          "a vector action may bind to the parent of x/y components");
    Check(input::BindingPathAllowed("input/thumbstick/click", touchStickPaths),
          "an explicitly allowed leaf remains valid");
    Check(!input::BindingPathAllowed("input/not_a_real_source", touchStickPaths),
          "an unknown parent source remains unsupported");

    Check(interaction_query::IsCoreTopLevelUserPath("/user/treadmill"),
          "all core top-level user paths are recognized");
    Check(!interaction_query::IsCoreTopLevelUserPath("/user/hand/left/input/trigger"),
          "an input component is not a top-level user path");
    Check(interaction_query::ActionSourceMask("/user/head") ==
              interaction_query::kHeadSource,
          "head is a core action subaction path");
    Check(interaction_query::ActionSourceMask("/user/gamepad") ==
              interaction_query::kGamepadSource,
          "gamepad is a core action subaction path");
    Check(interaction_query::ActionSourceMask("/user/treadmill") == 0,
          "treadmill is not a core action subaction path");
    Check(interaction_query::ActionSlotForSourceMask(interaction_query::kGamepadSource) == 4,
          "each core action source has a stable state slot");
    Check(interaction_query::IsSimulatedHandSource(interaction_query::kLeftHandSource) &&
              !interaction_query::IsSimulatedHandSource(interaction_query::kHeadSource),
          "an unsimulated head pose cannot alias a hand controller");
    Check(interaction_query::ProfileSupportsTopLevel(
              "/interaction_profiles/oculus/touch_controller", "/user/hand/right"),
          "the Touch profile is active on hand paths");
    Check(!interaction_query::ProfileSupportsTopLevel(
              "/interaction_profiles/oculus/touch_controller", "/user/gamepad"),
          "a hand profile is not reported for the gamepad");
    const XrInputSourceLocalizedNameFlags allNameParts =
        XR_INPUT_SOURCE_LOCALIZED_NAME_USER_PATH_BIT |
        XR_INPUT_SOURCE_LOCALIZED_NAME_INTERACTION_PROFILE_BIT |
        XR_INPUT_SOURCE_LOCALIZED_NAME_COMPONENT_BIT;
    Check(interaction_query::LocalizedSourceName(
              "/user/hand/right/input/trigger/value",
              "/interaction_profiles/oculus/touch_controller", allNameParts) ==
              "Right Hand Oculus Touch Controller Trigger",
          "localized source names honor all requested components");
    Check(interaction_query::LocalizedSourceName(
              "/user/hand/left/input/grip/pose", "",
              XR_INPUT_SOURCE_LOCALIZED_NAME_COMPONENT_BIT) == "Grip Pose",
          "component-only names omit unrequested path and profile text");

    if (failures) return 1;
    std::puts("action state tests passed");
    return 0;
}
