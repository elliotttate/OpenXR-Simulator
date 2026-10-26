#include "pose_math.h"

#include <cmath>
#include <cstdio>

namespace {

int failures = 0;

bool Near(float left, float right) {
    return std::fabs(left - right) < 0.0001f;
}

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

void CheckPosition(const XrPosef& pose, float x, float y, float z, const char* message) {
    Check(Near(pose.position.x, x) && Near(pose.position.y, y) && Near(pose.position.z, z), message);
}

void CheckOrientation(const XrPosef& pose, float x, float y, float z, float w, const char* message) {
    Check(Near(pose.orientation.x, x) && Near(pose.orientation.y, y) &&
          Near(pose.orientation.z, z) && Near(pose.orientation.w, w), message);
}

} // namespace

int main() {
    Check(pose_math::IsNormalized({0.0f, 0.0f, 0.0f, 1.0f}),
          "an identity quaternion is normalized");
    Check(pose_math::IsNormalized({0.0f, 0.0f, 0.0f, 1.009f}),
          "the specification's one-percent norm tolerance is accepted");
    Check(!pose_math::IsNormalized({0.0f, 0.0f, 0.0f, 1.011f}),
          "a quaternion outside the one-percent norm tolerance is rejected");
    Check(!pose_math::IsNormalized({0.0f, 0.0f, 0.0f, NAN}),
          "a non-finite quaternion is rejected");
    const XrQuaternionf normalized = pose_math::Normalize({0.0f, 0.0f, 0.0f, 1.009f});
    Check(Near(normalized.w, 1.0f),
          "an accepted near-unit quaternion is normalized for pose math");
    const XrVector3f leverArmVelocity =
        pose_math::Cross({0.0f, 1.0f, 0.0f}, {2.0f, 0.0f, 0.0f});
    Check(Near(leverArmVelocity.x, 0.0f) && Near(leverArmVelocity.y, 0.0f) &&
              Near(leverArmVelocity.z, -2.0f),
          "angular velocity contributes the expected lever-arm velocity");

    const XrPosef identity = pose_math::Identity();
    const XrPosef worldSpace{{0.0f, 0.0f, 0.0f, 1.0f}, {4.0f, 5.0f, 6.0f}};
    CheckPosition(pose_math::Relative(worldSpace, identity), 4.0f, 5.0f, 6.0f,
                  "identity base preserves a world pose");

    const XrPosef translatedBase{{0.0f, 0.0f, 0.0f, 1.0f}, {1.0f, 2.0f, 3.0f}};
    CheckPosition(pose_math::Relative(worldSpace, translatedBase), 3.0f, 3.0f, 3.0f,
                  "base-space origin is subtracted");

    constexpr float halfSqrt = 0.70710678f;
    const XrPosef yawedBase{{0.0f, halfSqrt, 0.0f, halfSqrt}, {0.0f, 0.0f, 0.0f}};
    const XrPosef pointAhead{{0.0f, 0.0f, 0.0f, 1.0f}, {-1.0f, 0.0f, 0.0f}};
    const XrPosef relativeToYawedBase = pose_math::Relative(pointAhead, yawedBase);
    CheckPosition(relativeToYawedBase, 0.0f, 0.0f, -1.0f,
                  "base-space orientation rotates the relative position");
    CheckOrientation(relativeToYawedBase, 0.0f, -halfSqrt, 0.0f, halfSqrt,
                     "base-space orientation is removed from the relative orientation");

    const XrPosef originOffset{{0.0f, 0.0f, 0.0f, 1.0f}, {0.0f, 1.0f, -2.0f}};
    const XrPosef child{{0.0f, 0.0f, 0.0f, 1.0f}, {0.25f, 0.0f, 0.0f}};
    CheckPosition(pose_math::Compose(originOffset, child), 0.25f, 1.0f, -2.0f,
                  "space origin offsets compose with child poses");

    const XrPosef roundTrip = pose_math::Compose(pose_math::Inverse(yawedBase), yawedBase);
    CheckPosition(roundTrip, 0.0f, 0.0f, 0.0f, "pose inverse cancels translation");
    Check(Near(roundTrip.orientation.x, 0.0f) && Near(roundTrip.orientation.y, 0.0f) &&
          Near(roundTrip.orientation.z, 0.0f) && Near(roundTrip.orientation.w, 1.0f),
          "pose inverse cancels orientation");

    if (failures) return 1;
    std::puts("pose math tests passed");
    return 0;
}
