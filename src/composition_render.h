#pragma once

#include <openxr/openxr.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

namespace composition_render {

enum class LayerBlend { Opaque, Premultiplied, Unpremultiplied };

inline LayerBlend BlendForLayerFlags(XrCompositionLayerFlags flags) {
    if (!(flags & XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT)) {
        return LayerBlend::Opaque;
    }
    return (flags & XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT)
        ? LayerBlend::Unpremultiplied : LayerBlend::Premultiplied;
}

struct SubImageRect {
    uint32_t x{0};
    uint32_t y{0};
    uint32_t w{0};
    uint32_t h{0};
};

// Resolve an application-supplied image rectangle defensively. Validation normally
// guarantees that it lies inside the swapchain, but keeping the clamp here prevents a
// malformed rectangle from reaching a graphics API copy operation. A zero extent is a
// legal empty rectangle in OpenXR and must stay empty rather than exposing the full image.
inline SubImageRect ResolveSubImageRect(const XrRect2Di& rect, uint32_t textureWidth,
                                        uint32_t textureHeight, bool showFullRender) {
    if (rect.extent.width == 0 || rect.extent.height == 0) {
        return {
            static_cast<uint32_t>((std::max)(0, (std::min)(rect.offset.x,
                                                          static_cast<int32_t>(textureWidth)))),
            static_cast<uint32_t>((std::max)(0, (std::min)(rect.offset.y,
                                                          static_cast<int32_t>(textureHeight)))),
            0,
            0,
        };
    }

    const SubImageRect full{0, 0, textureWidth, textureHeight};
    if (showFullRender) return full;
    if (rect.extent.width < 0 || rect.extent.height < 0) return {};

    int64_t x = rect.offset.x;
    int64_t y = rect.offset.y;
    int64_t width = rect.extent.width;
    int64_t height = rect.extent.height;
    if (x < 0) {
        width += x;
        x = 0;
    }
    if (y < 0) {
        height += y;
        y = 0;
    }
    if (x >= textureWidth || y >= textureHeight) return {};
    width = (std::min)(width, static_cast<int64_t>(textureWidth) - x);
    height = (std::min)(height, static_cast<int64_t>(textureHeight) - y);
    if (width <= 0 || height <= 0) return {};
    return {static_cast<uint32_t>(x), static_cast<uint32_t>(y),
            static_cast<uint32_t>(width), static_cast<uint32_t>(height)};
}

inline bool HasPixels(const XrRect2Di& rect) {
    return rect.extent.width > 0 && rect.extent.height > 0;
}

inline bool HasPixels(const XrCompositionLayerProjection& projection) {
    if (!projection.views) return false;
    for (uint32_t view = 0; view < projection.viewCount; ++view) {
        if (HasPixels(projection.views[view].subImage.imageRect)) return true;
    }
    return false;
}

inline bool HasPixels(const XrCompositionLayerQuad& quad) {
    return quad.size.width > 0.0f && quad.size.height > 0.0f &&
           HasPixels(quad.subImage.imageRect);
}

inline bool HasPixels(const XrCompositionLayerCylinderKHR& cylinder) {
    return cylinder.centralAngle > 0.0f && cylinder.aspectRatio > 0.0f &&
           HasPixels(cylinder.subImage.imageRect);
}

struct CylinderSegment {
    float centerAngle{0.0f};
    XrVector3f position{};
    float width{0.0f};
    float height{0.0f};
    XrRect2Di imageRect{};
};

inline std::vector<CylinderSegment> BuildCylinderSegments(
    const XrCompositionLayerCylinderKHR& cylinder,
    float maximumSegmentAngle = 0.0872664626f) { // five degrees
    std::vector<CylinderSegment> result;
    if (!HasPixels(cylinder) || !std::isfinite(maximumSegmentAngle) ||
        maximumSegmentAngle <= 0.0f) {
        return result;
    }

    // Zero and positive infinity designate an infinite cylinder. A large finite radius
    // preserves its angular dimensions while making translation parallax negligible in
    // the desktop compositor, whose projection has no finite far plane.
    const float radius = (cylinder.radius > 0.0f && std::isfinite(cylinder.radius))
        ? cylinder.radius : 1000.0f;
    const float exactCount = cylinder.centralAngle / maximumSegmentAngle;
    uint32_t count = (uint32_t)std::ceil(exactCount - 1e-5f);
    count = (std::max)(1u, (std::min)(72u, count));
    count = (std::min)(count, (uint32_t)cylinder.subImage.imageRect.extent.width);
    if (count == 0) return result;

    result.reserve(count);
    const float angleStep = cylinder.centralAngle / count;
    const float chordRadius = radius * std::cos(angleStep * 0.5f);
    const float chordWidth = 2.0f * radius * std::sin(angleStep * 0.5f);
    const float height = radius * cylinder.centralAngle / cylinder.aspectRatio;
    const int32_t sourceX = cylinder.subImage.imageRect.offset.x;
    const int32_t sourceWidth = cylinder.subImage.imageRect.extent.width;
    for (uint32_t index = 0; index < count; ++index) {
        const float centerAngle = -cylinder.centralAngle * 0.5f +
                                  (index + 0.5f) * angleStep;
        const int32_t x0 = sourceX + (int32_t)((int64_t)sourceWidth * index / count);
        const int32_t x1 = sourceX + (int32_t)((int64_t)sourceWidth * (index + 1) / count);
        CylinderSegment segment{};
        segment.centerAngle = centerAngle;
        segment.position = {chordRadius * std::sin(centerAngle), 0.0f,
                            -chordRadius * std::cos(centerAngle)};
        segment.width = chordWidth;
        segment.height = height;
        segment.imageRect = {{x0, cylinder.subImage.imageRect.offset.y},
                             {x1 - x0, cylinder.subImage.imageRect.extent.height}};
        result.push_back(segment);
    }
    return result;
}

inline bool CopyRgbaSubImage(const uint8_t* source, size_t sourceBytes,
                             uint32_t textureWidth, uint32_t textureHeight,
                             uint32_t arraySize, uint32_t arrayIndex,
                             const SubImageRect& rect, uint32_t outputWidth,
                             uint32_t outputHeight, std::vector<uint8_t>& output) {
    const uint64_t outputBytes64 = (uint64_t)outputWidth * outputHeight * 4;
    if (outputBytes64 > std::numeric_limits<size_t>::max()) return false;
    output.assign((size_t)outputBytes64, 0);

    const uint64_t layerBytes64 = (uint64_t)textureWidth * textureHeight * 4;
    const uint32_t layers = arraySize ? arraySize : 1;
    if (!source || layerBytes64 == 0 || layerBytes64 > std::numeric_limits<size_t>::max() ||
        layers > std::numeric_limits<size_t>::max() / (size_t)layerBytes64 ||
        sourceBytes < (size_t)layerBytes64 * layers || arrayIndex >= layers ||
        rect.w == 0 || rect.h == 0 || outputWidth < rect.w || outputHeight < rect.h ||
        (uint64_t)rect.x + rect.w > textureWidth ||
        (uint64_t)rect.y + rect.h > textureHeight) {
        return false;
    }

    const size_t layerBytes = (size_t)layerBytes64;
    const uint8_t* layer = source + layerBytes * arrayIndex;
    const size_t destinationPitch = (size_t)outputWidth * 4;
    for (uint32_t row = 0; row < rect.h; ++row) {
        const uint8_t* sourceRow =
            layer + ((size_t)(rect.y + row) * textureWidth + rect.x) * 4;
        uint8_t* destinationRow = output.data() + (size_t)row * destinationPitch;
        std::memcpy(destinationRow, sourceRow, (size_t)rect.w * 4);
    }
    return true;
}

} // namespace composition_render
