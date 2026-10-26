#include "composition_render.h"

#include <cstdio>

namespace {

int failures = 0;

void Check(bool condition, const char* message) {
    if (condition) return;
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++failures;
}

XrRect2Di Rect(int32_t x, int32_t y, int32_t width, int32_t height) {
    return {{x, y}, {width, height}};
}

} // namespace

int main() {
    using composition_render::ResolveSubImageRect;

    Check(composition_render::BlendForLayerFlags(0) ==
              composition_render::LayerBlend::Opaque,
          "source alpha is ignored unless explicitly enabled");
    Check(composition_render::BlendForLayerFlags(
              XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT) ==
              composition_render::LayerBlend::Premultiplied,
          "source alpha defaults to premultiplied color");
    Check(composition_render::BlendForLayerFlags(
              XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT |
              XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT) ==
              composition_render::LayerBlend::Unpremultiplied,
          "the unpremultiplied flag selects straight-alpha blending");
    Check(composition_render::BlendForLayerFlags(
              XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT) ==
              composition_render::LayerBlend::Opaque,
          "the unpremultiplied flag alone does not enable source alpha");

    const auto full = ResolveSubImageRect(Rect(0, 0, 1024, 512), 1024, 512, false);
    Check(full.x == 0 && full.y == 0 && full.w == 1024 && full.h == 512,
          "a full-image rectangle remains unchanged");

    const auto cropped = ResolveSubImageRect(Rect(10, 20, 300, 200), 1024, 512, false);
    Check(cropped.x == 10 && cropped.y == 20 && cropped.w == 300 && cropped.h == 200,
          "a valid crop remains unchanged");

    const auto empty = ResolveSubImageRect(Rect(1024, 512, 0, 0), 1024, 512, false);
    Check(empty.x == 1024 && empty.y == 512 && empty.w == 0 && empty.h == 0,
          "a zero-area boundary rectangle stays empty");

    const auto diagnosticEmpty = ResolveSubImageRect(Rect(20, 30, 0, 100), 1024, 512, true);
    Check(diagnosticEmpty.w == 0 && diagnosticEmpty.h == 0,
          "the full-render diagnostic does not expose an empty subimage");

    const auto diagnosticFull = ResolveSubImageRect(Rect(10, 20, 300, 200), 1024, 512, true);
    Check(diagnosticFull.x == 0 && diagnosticFull.y == 0 &&
              diagnosticFull.w == 1024 && diagnosticFull.h == 512,
          "the full-render diagnostic still expands a non-empty crop");

    const auto clipped = ResolveSubImageRect(Rect(-10, -20, 100, 80), 1024, 512, false);
    Check(clipped.x == 0 && clipped.y == 0 && clipped.w == 90 && clipped.h == 60,
          "defensive resolution clips malformed rectangles safely");

    XrCompositionLayerProjectionView views[2]{};
    views[0].subImage.imageRect = Rect(0, 0, 0, 512);
    views[1].subImage.imageRect = Rect(0, 0, 1024, 512);
    XrCompositionLayerProjection projection{};
    projection.viewCount = 2;
    projection.views = views;
    Check(composition_render::HasPixels(projection),
          "a projection contributes when either submitted view has pixels");
    views[1].subImage.imageRect.extent.width = 0;
    Check(!composition_render::HasPixels(projection),
          "a projection with two empty views contributes no pixels");

    XrCompositionLayerQuad quad{};
    quad.subImage.imageRect = Rect(0, 0, 256, 256);
    quad.size = {1.0f, 1.0f};
    Check(composition_render::HasPixels(quad), "a positive-size quad contributes pixels");
    quad.size.width = 0.0f;
    Check(!composition_render::HasPixels(quad), "a zero-width quad contributes no pixels");

    // Two 3x2 RGBA layers with the first byte identifying each pixel.
    std::vector<uint8_t> source(3 * 2 * 2 * 4, 0);
    for (size_t pixel = 0; pixel < source.size() / 4; ++pixel) source[pixel * 4] = (uint8_t)pixel;
    std::vector<uint8_t> copied;
    Check(composition_render::CopyRgbaSubImage(
              source.data(), source.size(), 3, 2, 2, 1, {1, 0, 2, 2}, 3, 2, copied),
          "an array-layer crop is copied successfully");
    Check(copied.size() == 3 * 2 * 4 && copied[0] == 7 && copied[4] == 8 &&
              copied[8] == 0 && copied[12] == 10 && copied[16] == 11 && copied[20] == 0,
          "the requested layer and crop are copied with zero padding");
    Check(!composition_render::CopyRgbaSubImage(
              source.data(), source.size() - 1, 3, 2, 2, 1, {0, 0, 3, 2}, 3, 2, copied),
          "a truncated full-texture readback is rejected");

    XrCompositionLayerCylinderKHR cylinder{};
    cylinder.subImage.imageRect = Rect(10, 20, 360, 180);
    cylinder.radius = 2.0f;
    cylinder.centralAngle = 1.5707963268f;
    cylinder.aspectRatio = 2.0f;
    const auto segments = composition_render::BuildCylinderSegments(cylinder);
    Check(segments.size() == 18, "a ninety-degree cylinder uses five-degree segments");
    Check(segments.front().imageRect.offset.x == 10 &&
              segments.back().imageRect.offset.x + segments.back().imageRect.extent.width == 370,
          "cylinder segments cover the complete source rectangle");
    Check(std::abs(segments.front().height - 1.5707963268f) < 0.0001f,
          "cylinder height is arc length divided by aspect ratio");
    const auto& left = segments.front();
    const auto& right = segments.back();
    Check(std::abs(left.position.x + right.position.x) < 0.0001f &&
              std::abs(left.position.z - right.position.z) < 0.0001f,
          "the tessellation is symmetric around local negative Z");
    const auto& next = segments[1];
    const float leftRightX = left.position.x + left.width * 0.5f * std::cos(left.centerAngle);
    const float leftRightZ = left.position.z + left.width * 0.5f * std::sin(left.centerAngle);
    const float nextLeftX = next.position.x - next.width * 0.5f * std::cos(next.centerAngle);
    const float nextLeftZ = next.position.z - next.width * 0.5f * std::sin(next.centerAngle);
    Check(std::abs(leftRightX - nextLeftX) < 0.0001f &&
              std::abs(leftRightZ - nextLeftZ) < 0.0001f,
          "adjacent cylinder chords share exact endpoints");
    cylinder.radius = 0.0f;
    const auto infiniteSegments = composition_render::BuildCylinderSegments(cylinder);
    Check(!infiniteSegments.empty() && std::isfinite(infiniteSegments[0].position.z),
          "an infinite cylinder receives a finite angular approximation");

    if (failures) return 1;
    std::puts("composition render tests passed");
    return 0;
}
