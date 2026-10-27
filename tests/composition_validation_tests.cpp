#include "composition_validation.h"

#include <cmath>
#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

XrSwapchainSubImage SubImage(int32_t x, int32_t y, int32_t width, int32_t height,
                             uint32_t arrayIndex = 0) {
    XrSwapchainSubImage image{};
    image.imageRect = {{x, y}, {width, height}};
    image.imageArrayIndex = arrayIndex;
    return image;
}

} // namespace

int main() {
    Check(composition_validation::IsValidFov({-0.8f, 0.9f, 0.7f, -0.6f}),
          "a normal asymmetric field of view is valid");
    Check(composition_validation::IsValidFov({0.8f, -0.8f, -0.7f, 0.7f}),
          "reversed angles are valid and request axis flipping");
    Check(!composition_validation::IsValidFov({-1.57079632679489661923f, 0.8f, 0.7f, -0.7f}),
          "field-of-view angles exclude negative pi over two");
    Check(!composition_validation::IsValidFov({-0.8f, 1.57079632679489661923f, 0.7f, -0.7f}),
          "field-of-view angles exclude positive pi over two");
    Check(!composition_validation::IsValidFov({-0.8f, 0.8f, NAN, -0.7f}),
          "field-of-view angles must be finite");

    const composition_validation::SwapchainInfo valid{1024, 512, 2, 1, true};
    Check(composition_validation::ValidateSubImage(SubImage(0, 0, 1024, 512), valid) == XR_SUCCESS,
          "a full-image rectangle is valid");
    Check(composition_validation::ValidateSubImage(SubImage(1024, 512, 0, 0), valid) == XR_SUCCESS,
          "a zero extent at the image boundary is non-negative and in bounds");
    Check(composition_validation::ValidateSubImage(SubImage(-1, 0, 100, 100), valid) ==
              XR_ERROR_SWAPCHAIN_RECT_INVALID,
          "a negative offset is outside the image");
    Check(composition_validation::ValidateSubImage(SubImage(1000, 0, 25, 100), valid) ==
              XR_ERROR_SWAPCHAIN_RECT_INVALID,
          "a rectangle extending past the right edge is rejected");
    Check(composition_validation::ValidateSubImage(SubImage(0, 0, -1, 100), valid) ==
              XR_ERROR_SWAPCHAIN_RECT_INVALID,
          "a negative extent is rejected");
    Check(composition_validation::ValidateSubImage(SubImage(0, 0, 100, 100, 2), valid) ==
              XR_ERROR_VALIDATION_FAILURE,
          "an out-of-range array index is rejected");

    auto unreleased = valid;
    unreleased.hasReleasedImage = false;
    Check(composition_validation::ValidateSubImage(SubImage(0, 0, 100, 100), unreleased) ==
              XR_ERROR_LAYER_INVALID,
          "a layer cannot reference a swapchain without a released image");

    auto cubemap = valid;
    cubemap.faceCount = 6;
    Check(composition_validation::ValidateSubImage(SubImage(0, 0, 100, 100), cubemap) ==
              XR_ERROR_VALIDATION_FAILURE,
          "projection and quad sub-images require a non-cubemap swapchain");

    if (failures) return 1;
    std::puts("composition validation tests passed");
    return 0;
}
