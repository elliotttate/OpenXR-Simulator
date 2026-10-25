// Frame-coherent metadata and controller leases shared by capture and input.
#pragma once
#include <openxr/openxr.h>
#include <windows.h>
#include <array>
#include <cmath>
#include <cstdio>
#include <string>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace ui_auto {
// Hosts may sync actions on a different thread from frame submission. Recursive
// because frame capture, pose location and Win32 dispatch share these helpers.
inline std::recursive_mutex g_mutex;

inline XrPosef Identity() { return {{0, 0, 0, 1}, {0, 0, 0}}; }
inline XrVector3f Rotate(XrQuaternionf q, XrVector3f v) {
    const XrVector3f t{2*(q.y*v.z-q.z*v.y), 2*(q.z*v.x-q.x*v.z), 2*(q.x*v.y-q.y*v.x)};
    return {v.x+q.w*t.x+q.y*t.z-q.z*t.y, v.y+q.w*t.y+q.z*t.x-q.x*t.z,
            v.z+q.w*t.z+q.x*t.y-q.y*t.x};
}
inline XrQuaternionf Multiply(XrQuaternionf a, XrQuaternionf b) {
    return {a.w*b.x+a.x*b.w+a.y*b.z-a.z*b.y, a.w*b.y-a.x*b.z+a.y*b.w+a.z*b.x,
            a.w*b.z+a.x*b.y-a.y*b.x+a.z*b.w, a.w*b.w-a.x*b.x-a.y*b.y-a.z*b.z};
}
inline XrPosef Compose(XrPosef a, XrPosef b) {
    auto p = Rotate(a.orientation, b.position);
    return {Multiply(a.orientation, b.orientation), {a.position.x+p.x, a.position.y+p.y, a.position.z+p.z}};
}
inline XrPosef Inverse(XrPosef p) {
    XrQuaternionf q{-p.orientation.x, -p.orientation.y, -p.orientation.z, p.orientation.w};
    return {q, Rotate(q, {-p.position.x, -p.position.y, -p.position.z})};
}
inline bool NearPose(XrPosef a, XrPosef b, float tolerance = 0.0001f) {
    const auto& x=a.orientation; const auto& y=b.orientation;
    const float minus=(x.x-y.x)*(x.x-y.x)+(x.y-y.y)*(x.y-y.y)+(x.z-y.z)*(x.z-y.z)+(x.w-y.w)*(x.w-y.w);
    const float plus=(x.x+y.x)*(x.x+y.x)+(x.y+y.y)*(x.y+y.y)+(x.z+y.z)*(x.z+y.z)+(x.w+y.w)*(x.w+y.w);
    return std::fabs(a.position.x-b.position.x) < tolerance &&
           std::fabs(a.position.y-b.position.y) < tolerance &&
           std::fabs(a.position.z-b.position.z) < tolerance && (std::min)(minus,plus) < tolerance*tolerance;
}

struct Eye {
    int eye = 0;
    float x = 0, y = 0, w = 0, h = 0;
    // source UV range (full-render inspection can expose pixels outside imageRect).
    float u0 = 0, v0 = 0, u1 = 1, v1 = 1;
    XrPosef pose = Identity();
    XrFovf fov{};
};
struct Surface {
    int id = 0;
    XrEyeVisibility visibility = XR_EYE_VISIBILITY_BOTH;
    XrPosef pose = Identity();
    float width = 0, height = 0;
};
struct Frame {
    std::string session;
    uint64_t frame = 0, sync = 0, tick = 0;
    int width = 0, height = 0;
    std::string layout;
    bool supported = true;
    XrPosef head = Identity();
    XrVector3f hands[2]{};
    std::vector<Eye> eyes, projectionEyes;
    std::vector<Surface> surfaces;
    std::vector<Surface> decorations;
};
// Keep the actual compositor geometry for diagnostics without allowing a
// one-texel controller ribbon to intercept screenshot-based UI targeting.
inline void AddQuadSurface(Frame& frame, int id, const XrCompositionLayerQuad& quad, XrPosef worldPose) {
    const float narrow = (std::min)(quad.size.width,quad.size.height);
    const float length = (std::max)(quad.size.width,quad.size.height);
    const bool ribbon = quad.subImage.imageRect.extent.width==1 && quad.subImage.imageRect.extent.height==1 &&
                        narrow>0 && narrow<=.004f && length>=.05f && length/narrow>=12.f;
    auto& destination = ribbon ? frame.decorations : frame.surfaces;
    destination.push_back({id,quad.eyeVisibility,worldPose,quad.size.width,quad.size.height});
}
struct SavedCapture { Frame geometry; ULONGLONG savedAt = 0; };
inline std::string g_session;
inline uint64_t g_sync = 0;
inline Frame g_frame;
inline std::unordered_map<std::string, SavedCapture> g_captures;

struct Pointer {
    bool owned = false, pending = false;
    XrPosef pose = Identity();
    std::string id, session, captureId, requestDigest, action, button = "trigger";
    int surfaceId = -1;
    bool down = false;
    // 0=dwell, 1=pressed, 2=released/settling; advanced after action snapshots.
    int phase = 0, cycles = 0, dwell = 2, hold = 2;
    uint64_t appliedFrame = 0;
    ULONGLONG expires = 0;
};
inline Pointer g_pointer[2];

inline bool NeedsPrepressValidation(const Pointer& p) {
    // Hold publishes button-down immediately, unlike a click's unpressed
    // dwell. Once a bound action snapshot observed it, closing its carrier is
    // valid application feedback and must not turn delivery into an error.
    return p.pending && p.phase==0 && !(p.action=="hold" && p.cycles>0);
}

// Called only after action snapshots are published. An unobserved sync must
// neither advance delivery nor imply that the application saw a held press.
inline bool AdvancePointerAfterSync(Pointer& p, bool observed) {
    if (!p.owned || !p.pending || !observed) return false;
    if (++p.cycles < (p.phase==0 ? p.dwell : p.phase==1 ? p.hold : 2)) return false;
    p.cycles=0;
    if (p.action=="hover" || p.action=="hold") return true;
    if (p.phase==0) { p.down=true; p.phase=1; }
    else if (p.phase==1) { p.down=false; p.phase=2; }
    else return true;
    return false;
}

inline bool ValidId(const std::string& s) {
    if (s.empty() || s.size() > 80) return false;
    for (char c : s) if (!(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z') &&
                        !(c >= '0' && c <= '9') && c != '-') return false;
    return true;
}
inline void WritePose(FILE* f, XrPosef p) {
    fprintf(f, "{\"position\":[%.9g,%.9g,%.9g],\"orientation\":[%.9g,%.9g,%.9g,%.9g]}",
            p.position.x,p.position.y,p.position.z,p.orientation.x,p.orientation.y,p.orientation.z,p.orientation.w);
}
inline void WriteEyes(FILE* f, const std::vector<Eye>& eyes) {
    fprintf(f, "[");
    for (size_t i=0; i<eyes.size(); ++i) {
        const auto& e=eyes[i];
        fprintf(f, "%s{\"eye\":\"%s\",\"viewport\":[%.9g,%.9g,%.9g,%.9g],\"uv\":[%.9g,%.9g,%.9g,%.9g],\"pose\":",
                i ? "," : "", e.eye ? "right" : "left", e.x,e.y,e.w,e.h,e.u0,e.v0,e.u1,e.v1);
        WritePose(f,e.pose);
        fprintf(f, ",\"fov\":[%.9g,%.9g,%.9g,%.9g]}", e.fov.angleLeft,e.fov.angleRight,e.fov.angleUp,e.fov.angleDown);
    }
    fprintf(f, "]");
}
inline void WriteGeometry(FILE* f, const Frame& g) {
    fprintf(f, ",\n\"geometry\":{\"session_id\":\"%s\",\"frame_id\":%llu,\"sync_id\":%llu,\"supported\":%s,\"layout\":\"%s\",\"head\":",
            g.session.c_str(),g.frame,g.sync,g.supported ? "true" : "false",g.layout.c_str());
    WritePose(f,g.head);
    fprintf(f, ",\"hands\":[[%.9g,%.9g,%.9g],[%.9g,%.9g,%.9g]],\"eyes\":",
            g.hands[0].x,g.hands[0].y,g.hands[0].z,g.hands[1].x,g.hands[1].y,g.hands[1].z);
    WriteEyes(f,g.eyes);
    fprintf(f, ",\"projection_eyes\":"); WriteEyes(f,g.projectionEyes);
    fprintf(f, ",\"surfaces\":[");
    for (size_t i=0; i<g.surfaces.size(); ++i) {
        const auto& s=g.surfaces[i];
        fprintf(f, "%s{\"id\":%d,\"visibility\":%d,\"size\":[%.9g,%.9g],\"pose\":",i ? "," : "",s.id,(int)s.visibility,s.width,s.height);
        WritePose(f,s.pose); fprintf(f,"}");
    }
    fprintf(f,"],\"decorations\":[");
    for (size_t i=0; i<g.decorations.size(); ++i) {
        const auto& s=g.decorations[i];
        fprintf(f, "%s{\"id\":%d,\"role\":\"one_texel_ribbon\",\"visibility\":%d,\"size\":[%.9g,%.9g],\"pose\":",i ? "," : "",s.id,(int)s.visibility,s.width,s.height);
        WritePose(f,s.pose); fprintf(f,"}");
    }
    fprintf(f,"]}");
}

} // namespace ui_auto
