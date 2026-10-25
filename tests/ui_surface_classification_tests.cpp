#include "ui_automation.h"
#include <cstdio>

int main() {
    int failures = 0;
    auto check = [&](bool condition, const char* message) {
        if (!condition) { std::fprintf(stderr,"FAIL: %s\n",message); ++failures; }
    };
    ui_auto::Frame frame;
    XrCompositionLayerQuad q{XR_TYPE_COMPOSITION_LAYER_QUAD};
    q.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
    q.subImage.imageRect = {{1,1},{1,1}};
    q.size = {.002f,1.2f};
    ui_auto::AddQuadSurface(frame,3,q,ui_auto::Identity());
    check(frame.surfaces.empty(), "a thin one-texel ray cannot become an automatic UI target");
    check(frame.decorations.size()==1 && frame.decorations[0].id==3,
          "the ray's real layer id and geometry remain available as decoration metadata");
    q.size = {2.f,1.f};
    ui_auto::AddQuadSurface(frame,1,q,ui_auto::Identity());
    check(frame.surfaces.size()==1 && frame.surfaces.back().id==1,
          "a large one-texel solid UI panel remains targetable");
    q.size = {.002f,1.2f}; q.subImage.imageRect.extent = {128,1024};
    ui_auto::AddQuadSurface(frame,2,q,ui_auto::Identity());
    check(frame.surfaces.size()==2 && frame.surfaces.back().id==2,
          "a thin textured UI panel remains targetable");
    q.subImage.imageRect.extent = {1,1}; q.size = {.002f,.002f};
    ui_auto::AddQuadSurface(frame,4,q,ui_auto::Identity());
    check(frame.surfaces.size()==3, "a small square one-texel button is not a ribbon");
    q.size = {1.2f,.002f};
    ui_auto::AddQuadSurface(frame,5,q,ui_auto::Identity());
    check(frame.decorations.size()==2, "horizontal ribbon geometry is handled too");
    q.size = {.002f,.03f};
    ui_auto::AddQuadSurface(frame,6,q,ui_auto::Identity());
    check(frame.surfaces.size()==4,
          "short strips are not silently classified; the ray producer must suppress near-hand ribbons");
    return failures ? 1 : 0;
}
