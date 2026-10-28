#include "controller_poses.h"

#include <cmath>
#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

bool Near(float a, float b, float tolerance = 1e-4f) {
    return std::fabs(a - b) <= tolerance;
}

bool Near(const XrVector3f& a, const XrVector3f& b, float tolerance = 1e-4f) {
    return Near(a.x, b.x, tolerance) && Near(a.y, b.y, tolerance) && Near(a.z, b.z, tolerance);
}

float AngleDegrees(const XrQuaternionf& q) {
    return 2.0f * std::acos(std::fmin(1.0f, std::fabs(q.w))) * 57.29578f;
}

const char* const kTouch = "/interaction_profiles/oculus/touch_controller";
const char* const kIndex = "/interaction_profiles/valve/index_controller";

} // namespace

int main() {
    for (const bool left : {true, false}) {
        const XrPosef touch = controller_poses::AimFromGrip(kTouch, left);
        Check(pose_math::IsNormalized(touch.orientation), "Touch grip rotation is normalized");
        // Grip -Z runs along the handle: 60 degrees above the aim ray, toward the thumb.
        Check(Near(pose_math::Rotate(touch.orientation, {0.0f, 0.0f, -1.0f}), {0.0f, 0.8660254f, -0.5f}),
              "Touch grip forward is the aim forward pitched up 60 degrees");
        Check(Near(touch.position, {0.0f, -0.03f, 0.095f}),
              "Touch grip origin sits 3 cm below and 9.5 cm behind the aim origin");
    }

    const XrPosef indexLeft = controller_poses::AimFromGrip(kIndex, true);
    const XrPosef indexRight = controller_poses::AimFromGrip(kIndex, false);
    Check(Near(AngleDegrees(indexLeft.orientation), 55.48f, 0.05f), "Index grip is 55.5 degrees from aim");
    Check(Near(indexLeft.position.x, -indexRight.position.x) && Near(indexLeft.position.y, indexRight.position.y) &&
          Near(indexLeft.position.z, indexRight.position.z), "Index grip offsets mirror across hands");
    Check(Near(indexLeft.orientation.x, indexRight.orientation.x) &&
          Near(indexLeft.orientation.y, -indexRight.orientation.y) &&
          Near(indexLeft.orientation.z, -indexRight.orientation.z), "Index grip rotations mirror across hands");

    const XrPosef simple = controller_poses::AimFromGrip("/interaction_profiles/khr/simple_controller", true);
    Check(Near(simple.position, {0.0f, 0.0f, 0.0f}) && Near(simple.orientation.w, 1.0f),
          "profiles without vendor poses keep grip at the aim pose");

    // A grip space located from a pitched, translated aim pose keeps the vendor offset.
    const XrPosef aim{{0.0f, 0.3826834f, 0.0f, 0.9238795f}, {0.2f, 1.1f, -0.4f}};
    const XrPosef grip = pose_math::Compose(aim, controller_poses::AimFromGrip(kTouch, false));
    const XrPosef gripInAim = pose_math::Relative(grip, aim);
    Check(Near(gripInAim.position, {0.0f, -0.03f, 0.095f}) &&
          Near(AngleDegrees(gripInAim.orientation), 60.0f, 0.05f),
          "composed grip pose stays at the vendor offset from aim");

    if (failures == 0) std::printf("controller_poses_tests: all checks passed\n");
    return failures == 0 ? 0 : 1;
}
