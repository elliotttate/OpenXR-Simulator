// Minimal OpenXR Simulator Runtime (D3D11/D3D12/OpenGL)
// - Implements enough of the runtime interface to let OpenXR apps start and render into runtime-owned swapchains
// - Opens a desktop window and presents the app's submitted images side-by-side
// - Supports D3D11, D3D12, and OpenGL graphics APIs

#define XR_USE_PLATFORM_WIN32
#define XR_USE_GRAPHICS_API_D3D11
#define XR_USE_GRAPHICS_API_D3D12
#define XR_USE_GRAPHICS_API_OPENGL
#define XR_USE_GRAPHICS_API_VULKAN

#include <windows.h>
#include <windowsx.h>
#include <wrl/client.h>
#include <d3d11.h>
#include <d3d12.h>
#include <d3d11on12.h>
#include <d3dcompiler.h>
#include <dxgi.h>
#include <dxgi1_6.h>

#define VK_USE_PLATFORM_WIN32_KHR
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>

#include "flicker_detector.h"

// OpenGL headers - minimal definitions for what we need
#include <GL/gl.h>

// OpenGL extension constants and types (from glext.h / wglext.h)
// We define these inline since Windows doesn't ship with glext.h
#ifndef GL_SRGB8_ALPHA8
#define GL_SRGB8_ALPHA8                   0x8C43
#endif
#ifndef GL_RGBA8
#define GL_RGBA8                          0x8058
#endif
#ifndef GL_BGRA
#define GL_BGRA                           0x80E1
#endif
#ifndef GL_RGBA16F
#define GL_RGBA16F                        0x881A
#endif
#ifndef GL_RGBA32F
#define GL_RGBA32F                        0x8814
#endif
#ifndef GL_RGB10_A2
#define GL_RGB10_A2                       0x8059
#endif
#ifndef GL_DEPTH_COMPONENT32F
#define GL_DEPTH_COMPONENT32F             0x8CAC
#endif
#ifndef GL_DEPTH24_STENCIL8
#define GL_DEPTH24_STENCIL8               0x88F0
#endif
#ifndef GL_DEPTH_COMPONENT16
#define GL_DEPTH_COMPONENT16              0x81A5
#endif
#ifndef GL_DEPTH_STENCIL
#define GL_DEPTH_STENCIL                  0x84F9
#endif
#ifndef GL_UNSIGNED_INT_24_8
#define GL_UNSIGNED_INT_24_8              0x84FA
#endif
#ifndef GL_TEXTURE_2D_ARRAY
#define GL_TEXTURE_2D_ARRAY               0x8C1A
#endif
#ifndef GL_FRAMEBUFFER
#define GL_FRAMEBUFFER                    0x8D40
#endif
#ifndef GL_COLOR_ATTACHMENT0
#define GL_COLOR_ATTACHMENT0              0x8CE0
#endif
#ifndef GL_FRAMEBUFFER_COMPLETE
#define GL_FRAMEBUFFER_COMPLETE           0x8CD5
#endif

// Function pointer types for GL extension functions
typedef void (APIENTRY *PFNGLTEXIMAGE3DPROC)(GLenum target, GLint level, GLint internalformat, GLsizei width, GLsizei height, GLsizei depth, GLint border, GLenum format, GLenum type, const void* pixels);
typedef void (APIENTRY *PFNGLGENFRAMEBUFFERSPROC)(GLsizei n, GLuint* framebuffers);
typedef void (APIENTRY *PFNGLDELETEFRAMEBUFFERSPROC)(GLsizei n, const GLuint* framebuffers);
typedef void (APIENTRY *PFNGLBINDFRAMEBUFFERPROC)(GLenum target, GLuint framebuffer);
typedef void (APIENTRY *PFNGLFRAMEBUFFERTEXTURE2DPROC)(GLenum target, GLenum attachment, GLenum textarget, GLuint texture, GLint level);
typedef GLenum (APIENTRY *PFNGLCHECKFRAMEBUFFERSTATUSPROC)(GLenum target);
typedef void (APIENTRY *PFNGLREADPIXELSPROC)(GLint x, GLint y, GLsizei width, GLsizei height, GLenum format, GLenum type, void* pixels);

// GL function pointers (loaded at runtime)
static PFNGLTEXIMAGE3DPROC g_glTexImage3D = nullptr;
static PFNGLGENFRAMEBUFFERSPROC g_glGenFramebuffers = nullptr;
static PFNGLDELETEFRAMEBUFFERSPROC g_glDeleteFramebuffers = nullptr;
static PFNGLBINDFRAMEBUFFERPROC g_glBindFramebuffer = nullptr;
static PFNGLFRAMEBUFFERTEXTURE2DPROC g_glFramebufferTexture2D = nullptr;
static PFNGLCHECKFRAMEBUFFERSTATUSPROC g_glCheckFramebufferStatus = nullptr;
#include <string>
#include <vector>
#include <unordered_map>
#include <optional>
#include <cstring>
#include <cstdlib>
#include <thread>
#include <mutex>
#include <atomic>
#include <deque>
#include <algorithm>
#include <cmath>
#include <cfloat>
#include <cstdarg>
#include <cstdio>
#include <share.h>
#include <chrono>

#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <loader_interfaces.h>
#include "mcp_integration.h"
#include "projection_timing.h"
#include "ui_enhancements.h"

using Microsoft::WRL::ComPtr;

#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "opengl32.lib")

// D3D12 helper - calculates subresource index (from d3dx12.h)
inline UINT D3D12CalcSubresource(UINT MipSlice, UINT ArraySlice, UINT PlaneSlice, UINT MipLevels, UINT ArraySize) {
    return MipSlice + ArraySlice * MipLevels + PlaneSlice * MipLevels * ArraySize;
}


// Simple logging (debug output + file log)
static FILE* g_LogFile = nullptr;
static std::mutex g_LogMutex;

// Log() is deliberately expensive: OutputDebugStringA takes a process-global lock and
// the fflush makes every line survive a crash, which is what makes the log worth having.
// Neither is affordable per frame -- a handful of calls in the frame loop cost more than
// the compositing does. Anything on the frame path goes through LogV/LogVf, which cost a
// predicted-taken branch unless SIMXR_VERBOSE is set to something other than 0.
static const bool g_logVerbose = []() {
    char v[16]{};
    const DWORD n = GetEnvironmentVariableA("SIMXR_VERBOSE", v, (DWORD)sizeof(v));
    return n > 0 && n < sizeof(v) && v[0] != '0';
}();

static void EnsureLogFile() {
    if (g_LogFile) return;
    char base[MAX_PATH]{};
    DWORD len = GetEnvironmentVariableA("LOCALAPPDATA", base, (DWORD)sizeof(base));
    char path[MAX_PATH]{};
    if (len > 0 && len < sizeof(base)) {
        snprintf(path, sizeof(path), "%s\\OpenXR-Simulator", base);
        CreateDirectoryA(path, nullptr);
        snprintf(path, sizeof(path), "%s\\OpenXR-Simulator\\openxr_simulator.%lu.log", base, GetCurrentProcessId());
    } else {
        snprintf(path, sizeof(path), ".\\openxr_simulator.%lu.log", GetCurrentProcessId());
    }
    // Permit diagnostics readers and sibling host processes to open the log while
    // keeping frame-path logging buffered instead of flushing on every line.
    g_LogFile = _fsopen(path, "a", _SH_DENYNO);
    if (g_LogFile) setvbuf(g_LogFile, nullptr, _IOFBF, 64 * 1024);
}
static void Log(const char* msg) {
    OutputDebugStringA(msg);
    std::lock_guard<std::mutex> guard(g_LogMutex);
    EnsureLogFile();
    if (g_LogFile) {
        fputs(msg, g_LogFile);
        if (msg[0] && msg[strlen(msg)-1] != '\n') fputc('\n', g_LogFile);
        static ULONGLONG lastFlushMs = 0;
        const ULONGLONG nowMs = GetTickCount64();
        if (lastFlushMs == 0 || nowMs - lastFlushMs >= 1000) {
            fflush(g_LogFile);
            lastFlushMs = nowMs;
        }
    }
}
static void Log(const std::string& msg) { Log(msg.c_str()); }
static void Logf(const char* fmt, ...) {
    char buf[2048];
    va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    Log(buf);
}
static void LogV(const char* msg) { if (g_logVerbose) Log(msg); }
static void LogVf(const char* fmt, ...) {
    if (!g_logVerbose) return;
    char buf[2048];
    va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    Log(buf);
}

// Helper to load glTexImage3D (OpenGL 1.2+ function not in Windows GL headers)
static bool EnsureGLTexImage3D() {
    if (g_glTexImage3D) return true;
    g_glTexImage3D = (PFNGLTEXIMAGE3DPROC)wglGetProcAddress("glTexImage3D");
    if (!g_glTexImage3D) {
        Log("[SimXR] Failed to load glTexImage3D");
        return false;
    }
    return true;
}

static bool EnsureGLFramebufferFuncs() {
    if (g_glGenFramebuffers) return true;
    g_glGenFramebuffers = (PFNGLGENFRAMEBUFFERSPROC)wglGetProcAddress("glGenFramebuffers");
    g_glDeleteFramebuffers = (PFNGLDELETEFRAMEBUFFERSPROC)wglGetProcAddress("glDeleteFramebuffers");
    g_glBindFramebuffer = (PFNGLBINDFRAMEBUFFERPROC)wglGetProcAddress("glBindFramebuffer");
    g_glFramebufferTexture2D = (PFNGLFRAMEBUFFERTEXTURE2DPROC)wglGetProcAddress("glFramebufferTexture2D");
    g_glCheckFramebufferStatus = (PFNGLCHECKFRAMEBUFFERSTATUSPROC)wglGetProcAddress("glCheckFramebufferStatus");
    if (!g_glGenFramebuffers || !g_glDeleteFramebuffers || !g_glBindFramebuffer ||
        !g_glFramebufferTexture2D || !g_glCheckFramebufferStatus) {
        Log("[SimXR] Failed to load GL framebuffer functions");
        return false;
    }
    return true;
}

static XrQuaternionf QuatFromYawPitch(float yaw, float pitch) {
    const float cy = cosf(yaw * 0.5f);
    const float sy = sinf(yaw * 0.5f);
    const float cp = cosf(pitch * 0.5f);
    const float sp = sinf(pitch * 0.5f);
    XrQuaternionf q{};
    q.x = sp * cy;
    q.y = cp * sy;
    q.z = -sp * sy;
    q.w = cp * cy;
    return q;
}

// Yaw / Pitch / Roll head orientation (Y-X-Z intrinsic Euler).
// Roll is rotation about the head's forward axis, used by the diagnostic
// pose-injection paths so app coordinate-system bugs that only manifest
// off-axis (e.g. inverted-roll quaternion handedness) become visible.
static XrQuaternionf QuatFromYawPitchRoll(float yaw, float pitch, float roll) {
    // Build q_yaw, q_pitch, q_roll separately and compose: q = q_yaw * q_pitch * q_roll
    // (apply roll first in head-local frame, then pitch, then yaw).
    const float hy = yaw   * 0.5f, cy = cosf(hy), sy = sinf(hy);
    const float hp = pitch * 0.5f, cp = cosf(hp), sp = sinf(hp);
    const float hr = roll  * 0.5f, cr = cosf(hr), sr = sinf(hr);

    // q_yaw  = (0, sy, 0, cy)   -- about +Y
    // q_pitch= (sp, 0, 0, cp)   -- about +X
    // q_roll = (0, 0, sr, cr)   -- about +Z (head forward = -Z, so roll feels intuitive)
    // q = q_yaw * q_pitch * q_roll
    XrQuaternionf qyp;  // q_yaw * q_pitch
    qyp.x = cy * sp;
    qyp.y = sy * cp;
    qyp.z = -sy * sp;
    qyp.w = cy * cp;

    XrQuaternionf q;  // qyp * q_roll
    q.x = qyp.x * cr + qyp.y * sr;
    q.y = qyp.y * cr - qyp.x * sr;
    q.z = qyp.z * cr + qyp.w * sr;
    q.w = qyp.w * cr - qyp.z * sr;
    return q;
}


// Helper function to convert a typed format to typeless
static DXGI_FORMAT ToTypeless(DXGI_FORMAT format) {
    switch (format) {
        // R8G8B8A8 family
        case DXGI_FORMAT_R8G8B8A8_UNORM:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        case DXGI_FORMAT_R8G8B8A8_UINT:
        case DXGI_FORMAT_R8G8B8A8_SINT:
        case DXGI_FORMAT_R8G8B8A8_SNORM:
            return DXGI_FORMAT_R8G8B8A8_TYPELESS;
            
        // B8G8R8A8 family
        case DXGI_FORMAT_B8G8R8A8_UNORM:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
            return DXGI_FORMAT_B8G8R8A8_TYPELESS;
            
        // R16G16B16A16 family
        case DXGI_FORMAT_R16G16B16A16_FLOAT:
        case DXGI_FORMAT_R16G16B16A16_UNORM:
        case DXGI_FORMAT_R16G16B16A16_UINT:
        case DXGI_FORMAT_R16G16B16A16_SNORM:
        case DXGI_FORMAT_R16G16B16A16_SINT:
            return DXGI_FORMAT_R16G16B16A16_TYPELESS;
            
        // R32G32B32A32 family
        case DXGI_FORMAT_R32G32B32A32_FLOAT:
        case DXGI_FORMAT_R32G32B32A32_UINT:
        case DXGI_FORMAT_R32G32B32A32_SINT:
            return DXGI_FORMAT_R32G32B32A32_TYPELESS;
            
        // R10G10B10A2 family
        case DXGI_FORMAT_R10G10B10A2_UNORM:
        case DXGI_FORMAT_R10G10B10A2_UINT:
            return DXGI_FORMAT_R10G10B10A2_TYPELESS;
            
        // Already typeless or depth formats - return as-is
        default:
            return format;
    }
}

// Released from WndProc and instance teardown, both of which run long before the D3D12
// preview path where it is defined.
namespace rt { struct Session; }
// Runtime state
namespace rt {

struct Swapchain;

// Forward declarations
void PushState(XrSession s, XrSessionState newState);

// Global adapter LUID that we'll use consistently
static LUID g_adapterLuid = {};
static bool g_adapterLuidSet = false;

// Global persistent window that survives session creation/destruction
static HWND g_persistentWindow = nullptr;
static std::mutex g_windowMutex;
static ComPtr<IDXGISwapChain1> g_persistentSwapchain;
static UINT g_persistentWidth = 1920;
static UINT g_persistentHeight = 540;
static bool g_windowClassRegistered = false;

// Latest XR swapchain (per-eye) source dimensions, populated by presentProjection.
// Used by menu/keyboard resize callbacks to compute a target window size.
static std::atomic<UINT> g_sourceWidth{0};
static std::atomic<UINT> g_sourceHeight{0};

struct Instance {
    XrInstance handle{(XrInstance)1};
    std::vector<std::string> enabledExtensions;
};

// The D3D12 preview back buffer is always BGRA8, whatever the app submits: that is the
// byte layout of the GDI DIB section it is copied into, so the readback needs no swizzle.
static constexpr DXGI_FORMAT kPreviewRTFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
// The eyes are written through kPreviewRTFormat, which passes their already-encoded bytes
// straight through. Quad layers are blended through this view of the same resource instead,
// so the hardware decodes the destination to linear, blends, and re-encodes. The resource
// itself is TYPELESS to allow both views.
static constexpr DXGI_FORMAT kPreviewRTFormatTypeless = DXGI_FORMAT_B8G8R8A8_TYPELESS;
static constexpr DXGI_FORMAT kPreviewRTFormatSrgb = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
// Shader-visible SRVs, ringed so a descriptor is not rewritten while a command list that
// reads it is still in flight. A frame takes one per eye plus one per quad layer, and
// kPreviewFrames of them can be queued at once, so this has to cover the busiest frame an
// app submits times that. 64 is three frames of twenty layers and costs a few kilobytes;
// an app with more layers than that in one frame only wraps within its own frame.
static constexpr UINT kPreviewSrvSlots = 64;
// c0..c3, tans, uvRect, opts - see kPreviewQuadHLSL.
static constexpr UINT kQuadConstantCount = 28;

// Preview frames in flight. The preview used to submit its blit and then block the
// calling thread on a fence until the GPU had finished it, so the readback could be
// mapped in the same call. That thread is the app's render thread, inside xrEndFrame,
// and the fence sits behind the app's whole frame (the cross-queue wait in
// presentProjection puts it there) — so the wait did not cost the blit, it cost the
// entire GPU frame, every frame, and CPU/GPU overlap stopped existing. Three slots let
// the composite be submitted and picked up a frame or two later instead, with the CPU
// never waiting on the GPU at all.
static constexpr UINT kPreviewFrames = 3;
// The desktop window can be maximized on a 4K display, but reading and scanning
// a full 4K GDI surface on the application's xrEndFrame thread costs more than
// an entire 90 Hz frame. Keep the mirror's internal surface at a diagnostic-
// quality 1080p envelope and let the final StretchDIBits scale it to the window.
// Eye swapchain resolution is untouched; this affects only the desktop mirror.
static constexpr UINT kPreviewMaxWidth = 1920;
static constexpr UINT kPreviewMaxHeight = 1080;

struct PreviewFrame12 {
    ComPtr<ID3D12CommandAllocator> alloc;
    ComPtr<ID3D12Resource> readback;   // composited frame, kPreviewRTFormat rows at previewReadbackPitch
    UINT rtWidth{0}, rtHeight{0};      // size the composite in `readback` was rendered at
    UINT64 fenceValue{0};              // last value signalled for this slot; 0 = never submitted
    bool pending{false};               // submitted, not yet painted into the back buffer
    bool recording{false};             // the RT is open and this frame has layers in it

    // What the frame in this slot was, captured when it was recorded rather than when it
    // is painted. The painter runs a frame or two later, so anything it read live would
    // label the image with a pose the image was not rendered with -- which is precisely
    // the question a burst exists to answer.
    uint32_t frame{0};
    bool hasProjection{false};
    uint32_t quadLayers{0};
    bool quadComposed{false};
    int32_t quadRects[2][4]{};
    float quadSourceAlphaCoverage{0.0f};
    float headYaw{0}, headPitch{0}, headRoll{0};
    float headX{0}, headY{0}, headZ{0};
    mcp::ProjLogEntry proj{};          // pose the app submitted for this frame
};

// Everything a Vulkan session needs on top of the D3D12 compositor it shares with a
// D3D12 session. The swapchain images are D3D12 resources either way; a Vulkan session
// creates them shared and hands the app imported VkImages over the same memory, so the
// preview, screenshot and quad paths never learn there is a second API involved.
struct VulkanSession {
    VkPhysicalDevice physicalDevice{VK_NULL_HANDLE};
    VkDevice device{VK_NULL_HANDLE};
    VkQueue queue{VK_NULL_HANDLE};
    VkCommandPool cmdPool{VK_NULL_HANDLE};
    VkCommandBuffer cmdBuffer{VK_NULL_HANDLE};
    // One shared D3D12 fence imported as a timeline VkSemaphore orders the app's queue
    // against the preview queue. Values are a single monotonic counter (Vulkan signals
    // one, D3D12 signals the next) rather than a two-state ping-pong: AMD rejects a
    // timeline signal that does not strictly increase.
    ComPtr<ID3D12Fence> fence;
    HANDLE fenceHandle{nullptr};
    VkSemaphore semaphore{VK_NULL_HANDLE};
    uint64_t counter{0};
    uint64_t appSignalled{0};       // last value the app's queue signalled, 0 = never
    uint64_t previewSignalled{0};   // last value the preview queue signalled, 0 = never
    uint64_t previewWaited{0};      // last previewSignalled the app's queue was made to wait on
    bool timeline{false};           // false: fall back to CPU waits around the composite
};

struct Session {
    XrSession handle{(XrSession)1};
    XrSessionState state{XR_SESSION_STATE_IDLE};
    ComPtr<ID3D11Device> d3d11Device;
    ComPtr<ID3D11DeviceContext> d3d11Context;
    // DX12 support
    ComPtr<ID3D12Device> d3d12Device;
    ComPtr<ID3D12CommandQueue> d3d12Queue;
    bool usesD3D12{false};
    // Vulkan support. usesD3D12 is set alongside this: the compositor is the D3D12 one.
    bool usesVulkan{false};
    VulkanSession vk;
    // OpenGL support
    HDC glDC{nullptr};
    HGLRC glRC{nullptr};
    bool usesOpenGL{false};

    // DX12 preview resources (GDI-based to avoid DXGI Present hook conflicts with Steam overlay / UEVR)
    ComPtr<ID3D12CommandQueue> previewQueue12;
    ComPtr<ID3D12Fence> crossQueueFence;
    UINT64 crossQueueFenceValue{0};
    ComPtr<ID3D12Resource> previewRT12;         // offscreen render target (replaces swapchain backbuffer)
    UINT previewReadbackPitch{0};               // row pitch aligned to D3D12_TEXTURE_DATA_PITCH_ALIGNMENT
    // Frames in flight. The composite of each one hangs off its own slot, so nothing is
    // mapped while the GPU may still be writing it and nothing has to be waited for to
    // be reused.
    PreviewFrame12 previewFrames[kPreviewFrames];
    UINT previewSlot{0};                        // slot this frame records into
    bool previewSlotOpen{false};                // previewSlot's allocator is reset and being recorded
    // Preview back buffer (D3D12/GDI): every layer paints into this memory DC, which is
    // blitted to the window once per frame. Painting layers straight to the window makes
    // the eye blit erase overlays for the length of the quad readback that follows it,
    // which shows up as a flickering 2D layer.
    ComPtr<ID3D12GraphicsCommandList> previewCmdList;
    ComPtr<ID3D12Fence> previewFence;
    HANDLE previewFenceEvent{nullptr};
    UINT64 previewFenceValue{0};                // last value signalled on previewQueue12
    // Scaling pipeline for the D3D12 preview. The eyes are drawn into the RT at the size
    // the window shows them, so the readback that follows is the window's pixel count and
    // not the stereo render's - a 5120x1440 submission mirrored into a 1280x360 fit rect
    // is 30MB a frame off the GPU and a CPU-side resample either way without this.
    ComPtr<ID3D12RootSignature> previewRootSig;
    ComPtr<ID3D12PipelineState> previewPSO;
    // Quad layers are rasterised into the same RT through an sRGB view; one PSO per
    // rt::LayerBlend mode.
    ComPtr<ID3D12RootSignature> previewQuadRootSig;
    ComPtr<ID3D12PipelineState> previewQuadPSO[3];
    ComPtr<ID3D12DescriptorHeap> previewSrvHeap;
    // Two RTVs over previewRT12: [0] plain, for the eye pass; [1] sRGB, for quad blending.
    ComPtr<ID3D12DescriptorHeap> previewRtvHeap;
    UINT previewRtvStride{0};
    UINT previewSrvStride{0};
    UINT previewSrvSlot{0};

    // Blit resources
    ComPtr<ID3D11VertexShader> blitVS;
    ComPtr<ID3D11PixelShader> blitPS;
    ComPtr<ID3D11SamplerState> samplerState;
    ComPtr<ID3D11RasterizerState> noCullRS;  // Rasterizer state with culling disabled
    ComPtr<ID3D11BlendState> anaglyphRedBS;
    ComPtr<ID3D11BlendState> anaglyphCyanBS;
    // Quad layers are positioned by their four projected corners rather than by the
    // viewport, so they need their own VS and a constant buffer to carry the corners.
    ComPtr<ID3D11VertexShader> quadVS;
    ComPtr<ID3D11Buffer> quadCB;
    // Keyed by (mode << 8) | writeMask; see GetLayerBlendState.
    std::unordered_map<uint32_t, ComPtr<ID3D11BlendState>> layerBlendStates;

    // Reusable staging for the D3D11 mirror. blitViewToHalf and renderQuadLayer used
    // to create a texture and an SRV per eye/quad on every mirrored frame; these small
    // caches (two entries rotating, so the two eyes each keep their own) turn that into
    // a plain copy into an existing texture. A size or format change just recreates.
    struct TempTexEntry {
        UINT width = 0, height = 0;
        DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
        ComPtr<ID3D11Texture2D> texture;
        ComPtr<ID3D11ShaderResourceView> srv;
    };
    TempTexEntry blitTempCache[2];
    UINT blitTempNext{0};
    TempTexEntry quadTempCache[2];
    UINT quadTempNext{0};

    // OpenGL preview: readback buffers and upload textures reused across frames
    // instead of being reallocated per frame.
    std::vector<uint8_t> glEyePixels[2];
    std::vector<uint8_t> glQuadPixels;
    ComPtr<ID3D11Texture2D> glEyeTex[2];
    ComPtr<ID3D11ShaderResourceView> glEyeSrv[2];
    UINT glEyeTexW{0}, glEyeTexH{0};

    // Desktop preview window (no thread - handled on main thread)
    HWND hwnd{nullptr};
    std::atomic<bool> isFocused{false};
    ComPtr<IDXGISwapChain1> previewSwapchain;
    UINT previewWidth{1920};
    UINT previewHeight{540};
    DXGI_FORMAT previewFormat{DXGI_FORMAT_UNKNOWN};  // Track format for matching
    // Current client area of the preview window (updated on WM_SIZE).
    // 0 means "not yet known" — use an initial sensible size for the first frame.
    std::atomic<UINT> clientWidth{0};
    std::atomic<UINT> clientHeight{0};
    std::mutex previewMutex;
};

struct Swapchain {
    XrSwapchain handle{(XrSwapchain)1};
    DXGI_FORMAT format{DXGI_FORMAT_R8G8B8A8_UNORM};
    uint32_t width{0}, height{0}, arraySize{2};
    uint32_t mipCount{1};
    // Backend type and images
    enum class Backend { D3D11, D3D12, OpenGL } backend{Backend::D3D11};
    std::vector<ComPtr<ID3D11Texture2D>> images;      // D3D11 path
    std::vector<ComPtr<ID3D12Resource>> images12;     // D3D12 path
    std::vector<D3D12_RESOURCE_STATES> imageStates12;
    // State the app is required to release images in, per XR_KHR_D3D12_enable:
    // RENDER_TARGET for colour, DEPTH_WRITE for depth.
    D3D12_RESOURCE_STATES releaseState12{D3D12_RESOURCE_STATE_COMMON};
    // Vulkan path: images12 still holds the shared D3D12 resources the compositor reads,
    // imagesVk the VkImages bound over the same memory that the app is handed.
    bool isVulkan{false};
    std::vector<VkImage> imagesVk;
    std::vector<VkDeviceMemory> memoryVk;
    std::vector<HANDLE> sharedHandles;
    std::vector<GLuint> imagesGL;                     // OpenGL path
    GLenum glInternalFormat{GL_RGBA8};                // OpenGL internal format
    uint32_t nextIndex{0};
    uint32_t lastAcquired{UINT32_MAX};  // Initialize to invalid
    uint32_t lastReleased{UINT32_MAX};  // Initialize to invalid
    uint32_t imageCount{3};
};

static Instance g_instance{};
static Session g_session{};
static std::unordered_map<XrSwapchain, Swapchain> g_swapchains;
uintptr_t g_nextSwapchainHandle = 1;

// Head tracking state for mouse look and WASD movement
static XrVector3f g_headPos = {0.0f, 1.7f, 0.0f};  // Start at standing eye height
static float g_headYaw = 0.0f;    // Rotation around Y axis (left/right)
static float g_headPitch = 0.0f;  // Rotation around X axis (up/down)
static float g_headRoll = 0.0f;   // Rotation around forward axis (head tilt). MCP-only — no keyboard binding.

// ---------- Diagnostic state: settable IPD and asymmetric per-eye FOV ----------
//
// By default the simulator publishes a UI-selected headset profile with
// asymmetric per-eye FOV and nonzero IPD. Symmetric FOV is still available
// from the FOV menu for sanity checks.
//
// When g_useCustomFov / g_useCustomIpd are set (via MCP set_fov / set_ipd
// / set_headset_profile), xrLocateViews emits the configured values
// instead. Headset profiles (quest3, index, etc.) preset both at once.
//
// FOV is stored in radians per OpenXR XrFovf convention:
//   angleLeft  < 0,  angleRight > 0
//   angleDown  < 0,  angleUp    > 0
// Per eye: index 0 = left eye, 1 = right eye.
static bool  g_useCustomFov = false;
static float g_eyeFovL[2] = { 0, 0 };
static float g_eyeFovR[2] = { 0, 0 };
static float g_eyeFovU[2] = { 0, 0 };
static float g_eyeFovD[2] = { 0, 0 };

static bool  g_useCustomIpd = false;
static float g_customIpd = 0.064f;  // meters

static XrFovf MakeFovDeg(float angleLeft, float angleRight, float angleUp, float angleDown) {
    const float DEG2RAD = 3.14159265f / 180.0f;
    return XrFovf{
        angleLeft * DEG2RAD,
        angleRight * DEG2RAD,
        angleUp * DEG2RAD,
        angleDown * DEG2RAD
    };
}

static XrFovf GetUiFov(uint32_t eyeIndex) {
    // The generic profile has no geometry of its own, so it stays symmetric even
    // if a stale settings file asks for asymmetry.
    if (!ui::g_uiState.useAsymmetricFov ||
        ui::g_uiState.headsetProfile == ui::HeadsetProfile::GenericSymmetric) {
        int fovDeg = ui::g_uiState.fovDegrees;
        if (fovDeg <= 0 || fovDeg > 180) fovDeg = 90;
        float fovRadians = fovDeg * 0.5f * 3.14159265f / 180.0f;
        // XrFovf fields are angles in radians, not tangent-space extents.
        return XrFovf{ -fovRadians, fovRadians, fovRadians, -fovRadians };
    }

    const ui::EyeFov& e = ui::GetActiveHeadsetSpec().eye[eyeIndex == 0 ? 0 : 1];
    return MakeFovDeg(e.angleLeft, e.angleRight, e.angleUp, e.angleDown);
}

static float GetUiIpdMeters() {
    return (std::max)(0.0f, (std::min)(0.2f, ui::g_uiState.ipdMeters));
}

static void HandleUiSettingsCommand(int cmd) {
    if (ui::IsFovSettingsCommand(cmd)) {
        g_useCustomFov = false;
    }
    if (ui::IsIpdSettingsCommand(cmd)) {
        g_useCustomIpd = false;
    }
}

// Anaglyph preview overlay: when enabled, the simulator's preview window
// composites left+right eyes into a red/cyan stereo image instead of
// side-by-side. Reveals IPD bugs (eyes don't converge -> red/cyan ghost)
// at a glance.
static bool g_anaglyphPreview = false;

// Pose sweep: auto-oscillate yaw / pitch / roll on a sine wave so the
// app sees a continuous range of head orientations. Catches off-axis
// quaternion bugs in seconds — anything mis-converted from OpenXR's
// right-handed frame to a left-handed game frame becomes a "world
// wobbles wrong direction" symptom that's hard to miss.
static bool  g_poseSweepEnabled  = false;
static float g_poseSweepStartT   = 0.0f;
static float g_poseSweepYawAmp   = 0.5f;   // radians (~28 deg)
static float g_poseSweepPitchAmp = 0.3f;   // radians (~17 deg)
static float g_poseSweepRollAmp  = 0.3f;   // radians (~17 deg)
static float g_poseSweepFreq     = 0.25f;  // Hz
static bool g_mouseCapture = false;
static POINT g_lastMousePos = {0, 0};

// Middle-button drag panning the zoomed preview, in client coordinates.
static bool g_panDrag = false;
static POINT g_panLastPos = {0, 0};

// Inside a WM_ENTERSIZEMOVE/WM_EXITSIZEMOVE pair: the user is dragging the window's
// frame, and every step of it would otherwise rewrite settings.json.
static bool g_sizingDrag = false;

// Controller tracking state for motion controller emulation
// Positions are relative to head position, orientation follows head by default
struct ControllerState {
    XrVector3f posOffset;  // Offset from head position (in head-local space)
    float yawOffset;       // Additional yaw relative to head
    float pitchOffset;     // Additional pitch relative to head
    bool isTracking;       // Whether controller is "tracked"

    // Input state for button/trigger emulation
    bool triggerPressed;   // Primary trigger (fire)
    bool gripPressed;      // Grip button
    bool menuPressed;      // Menu button
    bool primaryPressed;   // Primary button (A/X)
    bool secondaryPressed; // Secondary button (B/Y)
    bool thumbstickPressed;// Thumbstick click
    float triggerValue;    // 0.0-1.0 trigger analog value
    float gripValue;       // 0.0-1.0 grip analog value
    XrVector2f thumbstick; // -1.0 to 1.0 thumbstick position

    // Velocity tracking for motion detection
    XrVector3f prevPosWorld;    // Previous frame world position
    XrVector3f linearVelocity;  // m/s in world space
    XrVector3f angularVelocity; // rad/s
    float prevYaw;              // Previous yaw for angular velocity
    float prevPitch;            // Previous pitch for angular velocity
};
static ControllerState g_leftController = {
    {-0.2f, -0.3f, -0.4f}, 0.0f, -0.3f, true,  // Position/orientation
    false, false, false, false, false, false,   // Button states
    0.0f, 0.0f, {0.0f, 0.0f},                   // Analog values
    {0.0f, 0.0f, 0.0f}, {0.0f, 0.0f, 0.0f}, {0.0f, 0.0f, 0.0f}, 0.0f, 0.0f  // Velocity tracking
};
static ControllerState g_rightController = {
    {0.2f, -0.3f, -0.4f}, 0.0f, -0.3f, true,   // Position/orientation
    false, false, false, false, false, false,   // Button states
    0.0f, 0.0f, {0.0f, 0.0f},                   // Analog values
    {0.0f, 0.0f, 0.0f}, {0.0f, 0.0f, 0.0f}, {0.0f, 0.0f, 0.0f}, 0.0f, 0.0f  // Velocity tracking
};

// Map XrSpace handles to controller type (0=none, 1=left grip, 2=left aim, 3=right grip, 4=right aim)
static std::unordered_map<XrSpace, int> g_controllerSpaces;

// Composition layers carry a pose plus the space it is in, and VIEW (head-locked)
// against STAGE (world) changes where the layer belongs entirely.
struct RefSpace {
    XrReferenceSpaceType type;
    XrPosef poseInRef;   // space origin expressed in that reference space
};
static std::unordered_map<XrSpace, RefSpace> g_referenceSpaces;

// Map XrPath to path string for controller detection
static std::unordered_map<XrPath, std::string> g_pathStrings;

// Interaction profiles the app suggested bindings for, in suggestion order, and
// the one xrGetCurrentInteractionProfile reports back once action sets attach.
static std::vector<XrPath> g_suggestedProfiles;
static XrPath g_activeProfile = XR_NULL_PATH;

// Map XrAction to action name for input mapping
static std::unordered_map<XrAction, std::string> g_actionNames;

// Map XrAction to which hand it's bound to (0=both/any, 1=left, 2=right)
static std::unordered_map<XrAction, int> g_actionHand;

// Time tracking for velocity calculation
static XrTime g_lastFrameTime = 0;

// Get controller world pose (combines head pose with controller offset)
static void GetControllerPose(const ControllerState& ctrl, XrPosef* outPose) {
    // Controller orientation = head orientation + controller offsets
    float totalYaw = g_headYaw + ctrl.yawOffset;
    float totalPitch = g_headPitch + ctrl.pitchOffset;
    outPose->orientation = QuatFromYawPitch(totalYaw, totalPitch);

    // Controller position = head position + rotated offset
    // Rotate the offset by head yaw only (not pitch) for natural hand movement
    XrQuaternionf headYawQ = QuatFromYawPitch(g_headYaw, 0.0f);

    // Simple rotation of offset by head yaw
    float cosY = cosf(g_headYaw);
    float sinY = sinf(g_headYaw);
    outPose->position.x = g_headPos.x + ctrl.posOffset.x * cosY - ctrl.posOffset.z * sinY;
    outPose->position.y = g_headPos.y + ctrl.posOffset.y;
    outPose->position.z = g_headPos.z + ctrl.posOffset.x * sinY + ctrl.posOffset.z * cosY;
}

// Helper function to create quaternion from yaw and pitch
XrQuaternionf QuatFromYawPitch(float yaw, float pitch) {
    // Create rotation: first yaw around Y, then pitch around X
    float cy = cosf(yaw * 0.5f);
    float sy = sinf(yaw * 0.5f);
    float cp = cosf(pitch * 0.5f);
    float sp = sinf(pitch * 0.5f);

    // Combine rotations (yaw * pitch)
    XrQuaternionf q;
    q.w = cy * cp;
    q.x = cy * sp;
    q.y = sy * cp;
    q.z = -sy * sp;
    return q;
}

// Yaw / Pitch / Roll: q = q_yaw * q_pitch * q_roll. Roll is rotation
// about the head's local forward axis (head tilt), used by the
// MCP-injected diagnostic poses so off-axis quaternion-handedness
// bugs surface in the simulator.
XrQuaternionf QuatFromYawPitchRoll(float yaw, float pitch, float roll) {
    const float hy = yaw   * 0.5f, cy = cosf(hy), sy = sinf(hy);
    const float hp = pitch * 0.5f, cp = cosf(hp), sp = sinf(hp);
    const float hr = roll  * 0.5f, cr = cosf(hr), sr = sinf(hr);
    XrQuaternionf qyp;  // q_yaw * q_pitch (matches QuatFromYawPitch above)
    qyp.x = cy * sp;
    qyp.y = sy * cp;
    qyp.z = -sy * sp;
    qyp.w = cy * cp;
    XrQuaternionf q;  // qyp * q_roll where q_roll = (0, 0, sr, cr)
    q.x = qyp.x * cr + qyp.y * sr;
    q.y = qyp.y * cr - qyp.x * sr;
    q.z = qyp.z * cr + qyp.w * sr;
    q.w = qyp.w * cr - qyp.z * sr;
    return q;
}

static inline XrQuaternionf MultiplyQuaternions(const XrQuaternionf& a, const XrQuaternionf& b) {
    return XrQuaternionf{
        a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y,
        a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x,
        a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w,
        a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z
    };
}

// Rotate a vector by a quaternion (q * v * q^-1)
static inline XrVector3f RotateVectorByQuaternion(const XrQuaternionf& q, const XrVector3f& v) {
    const XrQuaternionf qv{ v.x, v.y, v.z, 0.0f };
    const XrQuaternionf qinv{ -q.x, -q.y, -q.z, q.w };
    const XrQuaternionf r = MultiplyQuaternions(MultiplyQuaternions(q, qv), qinv);
    return XrVector3f{ r.x, r.y, r.z };
}

static inline XrPosef ComposePose(const XrPosef& parent, const XrPosef& child) {
    const XrVector3f r = RotateVectorByQuaternion(parent.orientation, child.position);
    XrPosef out;
    out.orientation = MultiplyQuaternions(parent.orientation, child.orientation);
    out.position = { parent.position.x + r.x, parent.position.y + r.y, parent.position.z + r.z };
    return out;
}

static void GetEffectiveHeadAngles(float& yaw, float& pitch, float& roll) {
    yaw = g_headYaw; pitch = g_headPitch; roll = g_headRoll;
    if (!g_poseSweepEnabled) return;
    const float t = (float)GetTickCount64() * 0.001f - g_poseSweepStartT;
    const float TWO_PI = 6.2831853f;
    const float w = TWO_PI * g_poseSweepFreq;
    yaw   = g_poseSweepYawAmp   * sinf(w * t);
    pitch = g_poseSweepPitchAmp * sinf(w * t + 1.0f);
    roll  = g_poseSweepRollAmp  * sinf(w * t + 2.0f);
}

// Angles are passed in, not sampled here, so both eyes of a frame come from one
// instant of the pose sweep.
static XrPosef ViewPoseFromAngles(uint32_t eye, float yaw, float pitch, float roll) {
    XrPosef pose{};
    pose.orientation = QuatFromYawPitchRoll(yaw, pitch, roll);
    const float ipd = g_useCustomIpd ? g_customIpd : GetUiIpdMeters();
    const float offset = (eye == 0) ? -ipd * 0.5f : ipd * 0.5f;
    const XrVector3f rotated = RotateVectorByQuaternion(pose.orientation, XrVector3f{ offset, 0.0f, 0.0f });
    pose.position = { g_headPos.x + rotated.x, g_headPos.y + rotated.y, g_headPos.z + rotated.z };
    return pose;
}

static XrFovf GetViewFov(uint32_t eye) {
    if (g_useCustomFov) return XrFovf{ g_eyeFovL[eye], g_eyeFovR[eye], g_eyeFovU[eye], g_eyeFovD[eye] };
    return GetUiFov(eye);
}

// --- Composition layer blending -------------------------------------------------------------------
// OpenXR 1.0, XrCompositionLayerFlagBits: the layer's alpha channel is only live when
// BLEND_TEXTURE_SOURCE_ALPHA is set, and is premultiplied into the colour channels unless
// UNPREMULTIPLIED_ALPHA says otherwise. CORRECT_CHROMATIC_ABERRATION is a legitimate no-op
// here - the preview draws no distortion mesh to correct.
enum class LayerBlend { Opaque, Premultiplied, Unpremultiplied };

static inline LayerBlend BlendForLayerFlags(XrCompositionLayerFlags flags) {
    if (!(flags & XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT)) return LayerBlend::Opaque;
    return (flags & XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT) ? LayerBlend::Unpremultiplied
                                                                    : LayerBlend::Premultiplied;
}

// The preview swapchain is created UNORM because FLIP_DISCARD rejects _SRGB, so every render
// target view over it has to opt back into the gamma encode by hand. Without this the GPU
// blends linear values into a buffer the display reads as sRGB and the layer comes out dark.
static inline DXGI_FORMAT SrgbRtvFormat(DXGI_FORMAT backbufferFormat) {
    if (backbufferFormat == DXGI_FORMAT_R8G8B8A8_UNORM) return DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    if (backbufferFormat == DXGI_FORMAT_B8G8R8A8_UNORM) return DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
    return backbufferFormat;
}

static bool CreatePreviewRtv(Session& s, ID3D11Texture2D* backbuffer,
                             ComPtr<ID3D11RenderTargetView>& out) {
    D3D11_RENDER_TARGET_VIEW_DESC desc = {};
    desc.Format = SrgbRtvFormat(s.previewFormat);
    desc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
    desc.Texture2D.MipSlice = 0;
    if (SUCCEEDED(s.d3d11Device->CreateRenderTargetView(backbuffer, &desc, out.ReleaseAndGetAddressOf()))) {
        return true;
    }
    Log("[SimXR] Explicit sRGB RTV failed, falling back to auto format");
    return SUCCEEDED(s.d3d11Device->CreateRenderTargetView(backbuffer, nullptr, out.ReleaseAndGetAddressOf()));
}

// --- Quad layer geometry --------------------------------------------------------------------------
// LOCAL and STAGE are the same world here, VIEW is head-locked, and an unknown handle falls
// back to LOCAL the way xrLocateSpace does.
static XrPosef QuadWorldPose(const XrCompositionLayerQuad& quad, float yaw, float pitch, float roll,
                             bool* outHeadLocked = nullptr) {
    XrPosef spaceOrigin{};
    spaceOrigin.orientation = { 0.0f, 0.0f, 0.0f, 1.0f };
    spaceOrigin.position = { 0.0f, 0.0f, 0.0f };
    bool headLocked = false;
    auto spaceIt = g_referenceSpaces.find(quad.space);
    if (spaceIt != g_referenceSpaces.end()) {
        headLocked = (spaceIt->second.type == XR_REFERENCE_SPACE_TYPE_VIEW);
        spaceOrigin = spaceIt->second.poseInRef;
    }
    if (headLocked) {
        XrPosef head{};
        head.orientation = QuatFromYawPitchRoll(yaw, pitch, roll);
        head.position = g_headPos;
        spaceOrigin = ComposePose(head, spaceOrigin);
    }
    if (outHeadLocked) *outHeadLocked = headLocked;
    return ComposePose(spaceOrigin, quad.pose);
}

// Corners come back in fan order: top-left, top-right, bottom-right, bottom-left. A triangle
// strip wants TL, TR, BL, BR instead, so it has to walk these as {0, 1, 3, 2}.
static void QuadWorldCorners(const XrCompositionLayerQuad& quad, float yaw, float pitch, float roll,
                             XrVector3f outCorners[4], bool* outHeadLocked) {
    const XrPosef quadWorld = QuadWorldPose(quad, yaw, pitch, roll, outHeadLocked);

    const float halfW = quad.size.width * 0.5f;
    const float halfH = quad.size.height * 0.5f;
    const XrVector3f localCorners[4] = {
        { -halfW,  halfH, 0.0f }, {  halfW,  halfH, 0.0f },
        {  halfW, -halfH, 0.0f }, { -halfW, -halfH, 0.0f },
    };
    for (int i = 0; i < 4; ++i) {
        const XrVector3f r = RotateVectorByQuaternion(quadWorld.orientation, localCorners[i]);
        outCorners[i] = { quadWorld.position.x + r.x, quadWorld.position.y + r.y, quadWorld.position.z + r.z };
    }
}

// View space matches xrLocateViews: -Z forward, so a visible point has z < 0.
static inline XrVector3f WorldToView(const XrVector3f& world, const XrPosef& view) {
    const XrVector3f d{ world.x - view.position.x, world.y - view.position.y, world.z - view.position.z };
    const XrQuaternionf toView{ -view.orientation.x, -view.orientation.y,
                                -view.orientation.z,  view.orientation.w };
    return RotateVectorByQuaternion(toView, d);
}

static inline bool QuadVisibleInEye(XrEyeVisibility visibility, uint32_t eye) {
    if (visibility == XR_EYE_VISIBILITY_LEFT)  return eye == 0;
    if (visibility == XR_EYE_VISIBILITY_RIGHT) return eye == 1;
    return true;
}

// Initialize shader resources for blitting
bool InitBlitResources(Session& s) {
    if (s.blitVS && s.blitPS && s.quadVS && s.quadCB && s.samplerState && s.noCullRS &&
        s.anaglyphRedBS && s.anaglyphCyanBS) {
        return true;
    }

    // Compile shaders
    const char* shaderSource = R"(
        Texture2D txDiffuse : register(t0);
        SamplerState samLinear : register(s0);

        struct VS_OUTPUT {
            float4 Pos : SV_POSITION;
            float2 Tex : TEXCOORD;
        };

        // Vertex Shader (generates fullscreen quad with correct UV mapping)
        VS_OUTPUT VSMain(uint vertexId : SV_VertexID) {
            VS_OUTPUT output;
            // Generate (0,0), (2,0), (0,2), (2,2) pattern
            float2 xy = float2((vertexId << 1) & 2, vertexId & 2);

            // Clip-space position: xy goes 0-2, we need -1 to 1
            // x: 0->-1, 2->1  means x_clip = xy.x - 1
            // y: 0->1, 2->-1  means y_clip = 1 - xy.y
            output.Pos = float4(xy.x - 1.0, 1.0 - xy.y, 0.0, 1.0);

            // Normalized UVs (0-1 range, not 0-2)
            output.Tex = xy * 0.5;

            return output;
        }

        // Corners arrive in clip space rather than NDC so that w survives to the rasterizer:
        // that is what keeps texture interpolation perspective-correct on an angled quad, and
        // what lets the GPU clip a quad straddling the near plane instead of dropping it.
        cbuffer QuadCB : register(b0) {
            float4 gCorners[4];
        };

        VS_OUTPUT QuadVSMain(uint vertexId : SV_VertexID) {
            VS_OUTPUT output;
            output.Pos = gCorners[vertexId];
            output.Tex = float2(vertexId & 1, (vertexId >> 1) & 1);
            return output;
        }

        // Pixel Shader - GPU handles sRGB conversion automatically with proper formats
        float4 PSMain(VS_OUTPUT input) : SV_TARGET {
            return txDiffuse.Sample(samLinear, input.Tex);
        }
    )";

    ComPtr<ID3DBlob> vsBlob, psBlob, errorBlob;
    HRESULT hr;
    UINT compileFlags = D3DCOMPILE_OPTIMIZATION_LEVEL3;

    // Compile VS
    hr = D3DCompile(shaderSource, strlen(shaderSource), "BlitShader", nullptr, nullptr, 
                    "VSMain", "vs_5_0", compileFlags, 0, vsBlob.GetAddressOf(), errorBlob.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to compile VS: %s", errorBlob ? (char*)errorBlob->GetBufferPointer() : "Unknown error");
        return false;
    }
    hr = s.d3d11Device->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), 
                                           nullptr, s.blitVS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create VS: 0x%08X", hr); return false; }

    // Compile PS
    hr = D3DCompile(shaderSource, strlen(shaderSource), "BlitShader", nullptr, nullptr, 
                    "PSMain", "ps_5_0", compileFlags, 0, psBlob.GetAddressOf(), errorBlob.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to compile PS: %s", errorBlob ? (char*)errorBlob->GetBufferPointer() : "Unknown error");
        return false;
    }
    hr = s.d3d11Device->CreatePixelShader(psBlob->GetBufferPointer(), psBlob->GetBufferSize(),
                                          nullptr, s.blitPS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create PS: 0x%08X", hr); return false; }

    // Compile the quad VS. It shares the source (and so the PS) with the blit path but is a
    // separate shader because it reads a constant buffer, and the blit call sites bind none.
    ComPtr<ID3DBlob> quadVsBlob;
    hr = D3DCompile(shaderSource, strlen(shaderSource), "BlitShader", nullptr, nullptr,
                    "QuadVSMain", "vs_5_0", compileFlags, 0, quadVsBlob.GetAddressOf(), errorBlob.ReleaseAndGetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] Failed to compile quad VS: %s", errorBlob ? (char*)errorBlob->GetBufferPointer() : "Unknown error");
        return false;
    }
    hr = s.d3d11Device->CreateVertexShader(quadVsBlob->GetBufferPointer(), quadVsBlob->GetBufferSize(),
                                           nullptr, s.quadVS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create quad VS: 0x%08X", hr); return false; }

    D3D11_BUFFER_DESC cbDesc{};
    cbDesc.ByteWidth = sizeof(float) * 4 * 4;   // float4 gCorners[4]
    cbDesc.Usage = D3D11_USAGE_DYNAMIC;
    cbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = s.d3d11Device->CreateBuffer(&cbDesc, nullptr, s.quadCB.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create quad constant buffer: 0x%08X", hr); return false; }

    // Create Sampler State
    D3D11_SAMPLER_DESC sampDesc = {};
    sampDesc.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampDesc.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampDesc.ComparisonFunc = D3D11_COMPARISON_NEVER;
    sampDesc.MinLOD = 0; 
    sampDesc.MaxLOD = D3D11_FLOAT32_MAX;
    hr = s.d3d11Device->CreateSamplerState(&sampDesc, s.samplerState.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create SamplerState: 0x%08X", hr); return false; }

    // Create Rasterizer State with culling disabled
    D3D11_RASTERIZER_DESC rsDesc{};
    rsDesc.FillMode = D3D11_FILL_SOLID;
    rsDesc.CullMode = D3D11_CULL_NONE;  // Disable culling to prevent triangles from being discarded
    rsDesc.FrontCounterClockwise = FALSE;
    rsDesc.DepthClipEnable = TRUE;
    rsDesc.ScissorEnable = FALSE;
    rsDesc.MultisampleEnable = FALSE;
    rsDesc.AntialiasedLineEnable = FALSE;
    hr = s.d3d11Device->CreateRasterizerState(&rsDesc, s.noCullRS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create RasterizerState: 0x%08X", hr); return false; }

    // Create blend states for anaglyph rendering
    D3D11_BLEND_DESC blendDesc{};
    blendDesc.AlphaToCoverageEnable = FALSE;
    blendDesc.IndependentBlendEnable = FALSE;
    blendDesc.RenderTarget[0].BlendEnable = FALSE;
    blendDesc.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED;
    hr = s.d3d11Device->CreateBlendState(&blendDesc, s.anaglyphRedBS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create anaglyph red blend state: 0x%08X", hr); return false; }

    blendDesc.RenderTarget[0].RenderTargetWriteMask =
        D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
    hr = s.d3d11Device->CreateBlendState(&blendDesc, s.anaglyphCyanBS.GetAddressOf());
    if (FAILED(hr)) { Logf("[SimXR] Failed to create anaglyph cyan blend state: 0x%08X", hr); return false; }

    Log("[SimXR] Blit resources initialized successfully.");
    return true;
}

// Three spec blend modes times three anaglyph channel masks is more combinations than are
// worth naming, and an app only ever reaches a couple, so they are built on demand and cached.
static ID3D11BlendState* GetLayerBlendState(Session& s, LayerBlend mode, UINT8 writeMask) {
    const uint32_t key = ((uint32_t)mode << 8) | writeMask;
    auto it = s.layerBlendStates.find(key);
    if (it != s.layerBlendStates.end()) return it->second.Get();

    D3D11_BLEND_DESC desc{};
    desc.AlphaToCoverageEnable = FALSE;
    desc.IndependentBlendEnable = FALSE;
    auto& target = desc.RenderTarget[0];
    target.BlendOp = D3D11_BLEND_OP_ADD;
    target.BlendOpAlpha = D3D11_BLEND_OP_ADD;
    target.RenderTargetWriteMask = writeMask;
    if (mode == LayerBlend::Opaque) {
        // Source alpha is ignored rather than written: dropping it from the write mask leaves
        // the destination at the 1.0 the black clear put there, which is what an OPAQUE
        // environment blend mode expects of the composited result.
        target.BlendEnable = FALSE;
        target.RenderTargetWriteMask &= (UINT8)(D3D11_COLOR_WRITE_ENABLE_RED |
                                                D3D11_COLOR_WRITE_ENABLE_GREEN |
                                                D3D11_COLOR_WRITE_ENABLE_BLUE);
    } else {
        target.BlendEnable = TRUE;
        // Premultiplied content already carries the src.rgb * src.a term.
        target.SrcBlend = (mode == LayerBlend::Unpremultiplied) ? D3D11_BLEND_SRC_ALPHA : D3D11_BLEND_ONE;
        target.DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
        target.SrcBlendAlpha = D3D11_BLEND_ONE;
        target.DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    }

    ComPtr<ID3D11BlendState> state;
    if (FAILED(s.d3d11Device->CreateBlendState(&desc, state.GetAddressOf()))) {
        Logf("[SimXR] Failed to create layer blend state (mode=%d, mask=0x%02X)", (int)mode, writeMask);
        return nullptr;
    }
    return s.layerBlendStates.emplace(key, state).first->second.Get();
}

// Compute the natural canvas size for the current stereo layout, expressed in
// source pixels (i.e. independent of the window/client size). Used to derive
// the content aspect ratio for letterboxing.
static void ComputeContentDims(int srcW, int srcH,
                                ui::ViewMode mode, ui::DisplayLayout layout,
                                int& contentW, int& contentH) {
    if (mode == ui::ViewMode::BothEyes) {
        if (layout == ui::DisplayLayout::SideBySide) {
            contentW = srcW * 2;
            contentH = srcH;
        } else if (layout == ui::DisplayLayout::OverUnder) {
            contentW = srcW;
            contentH = srcH * 2;
        } else { // Anaglyph: both eyes overlap in same frame
            contentW = srcW;
            contentH = srcH;
        }
    } else {
        contentW = srcW;
        contentH = srcH;
    }
    if (contentW <= 0) contentW = 1;
    if (contentH <= 0) contentH = 1;
}

// Largest rect with (contentW : contentH) aspect that fits inside (dstW × dstH),
// centered. Used to letterbox/pillarbox the stereo image into the window.
struct FitRect { float x, y, w, h; };
static FitRect ComputeFitRect(int contentW, int contentH, int dstW, int dstH) {
    FitRect r{0.0f, 0.0f, (float)dstW, (float)dstH};
    if (contentW <= 0 || contentH <= 0 || dstW <= 0 || dstH <= 0) return r;
    double ca = (double)contentW / (double)contentH;
    double da = (double)dstW / (double)dstH;
    if (ca > da) {
        // Content wider than dest → pillar (full width, shorter height)
        r.w = (float)dstW;
        r.h = (float)((double)dstW / ca);
        r.x = 0.0f;
        r.y = ((float)dstH - r.h) * 0.5f;
    } else {
        // Content taller than dest → letterbox (full height, narrower width)
        r.h = (float)dstH;
        r.w = (float)((double)dstH * ca);
        r.x = ((float)dstW - r.w) * 0.5f;
        r.y = 0.0f;
    }
    return r;
}

// Shrink w x h, keeping its aspect, until it fits the desktop work area.
static void ClampToWorkArea(int& w, int& h) {
    if (w <= 0 || h <= 0) return;
    RECT wa{};
    if (!SystemParametersInfoW(SPI_GETWORKAREA, 0, &wa, 0)) return;
    const int maxW = (int)((wa.right - wa.left) * 0.9);
    const int maxH = (int)((wa.bottom - wa.top) * 0.9);
    if (maxW <= 0 || maxH <= 0 || (w <= maxW && h <= maxH)) return;
    const double scale = (std::min)((double)maxW / w, (double)maxH / h);
    w = (std::max)(320, (int)(w * scale));
    h = (std::max)(240, (int)(h * scale));
}

// The shape the preview maps the eyes onto: the headset's panel, laid out for the
// current view mode. This is deliberately NOT the app's buffer size. The app renders
// the frustum we report, which is taller than it is wide; into a 16:9 buffer that is
// non-square pixels, and showing those pixels 1:1 stretches the image ~2x horizontally.
// Scaling the buffer into the panel's shape is what a real compositor does.
static void ComputeDisplayDims(int& contentW, int& contentH) {
    uint32_t panelW = 0, panelH = 0;
    ui::GetHeadsetPanelResolution(panelW, panelH);
    ComputeContentDims((int)panelW, (int)panelH, ui::g_uiState.viewMode, ui::g_uiState.displayLayout,
                       contentW, contentH);
}

// The region of a swapchain image the app declared valid (XrSwapchainSubImage::imageRect),
// clamped to the texture. Apps commonly render a 16:9 eye into a larger or square
// swapchain and describe it with this rect, so everything downstream - the copy, the
// preview size, the window aspect - has to work from it and not from the texture.
// A zero or unusable rect means the whole image, and so does the "show full render" toggle.
struct SubImageRect { uint32_t x, y, w, h; };
static SubImageRect ResolveSubImageRect(const XrRect2Di& rect, uint32_t texW, uint32_t texH,
                                        const char* label) {
    const SubImageRect full{ 0, 0, texW, texH };
    if (ui::g_uiState.showFullRender) return full;
    if (rect.extent.width <= 0 || rect.extent.height <= 0) return full;

    int64_t x = rect.offset.x, y = rect.offset.y;
    int64_t w = rect.extent.width, h = rect.extent.height;
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x >= (int64_t)texW || y >= (int64_t)texH) {
        w = h = 0;
    } else {
        if (x + w > (int64_t)texW) w = (int64_t)texW - x;
        if (y + h > (int64_t)texH) h = (int64_t)texH - y;
    }
    if (w <= 0 || h <= 0) {
        static int badRect = 0;
        if (++badRect % 60 == 1) {
            Logf("[SimXR] %s: unusable imageRect (offset=%d,%d extent=%dx%d, texture %ux%u); using full image",
                 label, rect.offset.x, rect.offset.y, rect.extent.width, rect.extent.height, texW, texH);
        }
        return full;
    }
    return SubImageRect{ (uint32_t)x, (uint32_t)y, (uint32_t)w, (uint32_t)h };
}

static perf::ProjectionTimingTracker g_projectionTiming;

// Build the stats payload for the title bar (used when "Show Statistics" is on).
static ui::StatsInfo BuildStatsInfo(rt::Session& s) {
    ui::StatsInfo si;
    si.sourceW = (int)rt::g_sourceWidth.load();
    si.sourceH = (int)rt::g_sourceHeight.load();
    si.clientW = (int)s.clientWidth.load();
    si.clientH = (int)s.clientHeight.load();
    si.headX = rt::g_headPos.x;
    si.headY = rt::g_headPos.y;
    si.headZ = rt::g_headPos.z;
    constexpr float RAD2DEG = 57.2957795f;
    si.yawDeg   = rt::g_headYaw   * RAD2DEG;
    si.pitchDeg = rt::g_headPitch * RAD2DEG;
    si.rollDeg  = rt::g_headRoll  * RAD2DEG;
    const auto timing = g_projectionTiming.Snapshot(
        perf::ProjectionTimingTracker::Clock::now());
    si.projectionTimingActive = timing.active;
    si.projectionFps = timing.fps;
    si.latestFrameMs = timing.latestFrameMs;
    si.p50FrameMs = timing.p50FrameMs;
    si.p95FrameMs = timing.p95FrameMs;
    si.projectionTimingSamples = (uint32_t)timing.sampleCount;
    return si;
}

// Repaint the title from the message loop, so a toggle that changes what the
// title says lands immediately instead of at the render loop's next 500ms tick
// -- which never arrives at all if the app has stopped submitting frames.
static void RefreshTitleNow(HWND hwnd) {
    if (!hwnd || hwnd != rt::g_session.hwnd) return;
    ui::StatsInfo si = BuildStatsInfo(rt::g_session);
    ui::UpdateWindowTitle(hwnd, &si);
}

// Read the current preview-window client size (set on WM_SIZE). If the user
// hasn't sized it yet, fall back to the menu-zoom-derived target.
static void GetPreviewClientSize(rt::Session& s, int srcW, int srcH, int& outW, int& outH) {
    UINT cw = s.clientWidth.load();
    UINT ch = s.clientHeight.load();
    if (cw > 0 && ch > 0) {
        outW = (int)cw;
        outH = (int)ch;
        return;
    }
    // Initial fallback (first frame, before WM_SIZE has fired)
    uint32_t panelW = 0, panelH = 0;
    ui::GetHeadsetPanelResolution(panelW, panelH);
    int tw = 0, th = 0;
    ui::CalculateWindowSize((int)panelW, (int)panelH, tw, th);
    ClampToWorkArea(tw, th);
    outW = tw;
    outH = th;
}

// Publish what zoom and pan are measured against: the window's client area and the shape
// the eyes are laid out in. The render path refreshes this every frame; the input
// handlers refresh it before acting, so a wheel notch still lands on a stalled app.
static void RefreshPreviewGeometry(int clientW, int clientH) {
    int contentW = 0, contentH = 0;
    ComputeDisplayDims(contentW, contentH);
    ui::g_previewGeom = { clientW, clientH, contentW, contentH };
}

static void RefreshPreviewGeometry(HWND hWnd) {
    RECT cr{};
    if (!hWnd || !GetClientRect(hWnd, &cr)) return;
    RefreshPreviewGeometry(cr.right - cr.left, cr.bottom - cr.top);
}

// Where the stereo image lands inside a dstW x dstH target, in that target's pixels.
// "Fill Window" gives the entire target; any other zoom is an absolute content scale
// placed by the pan offset, which the target then clips.
static FitRect ComputePresentRect(int dstW, int dstH) {
    RefreshPreviewGeometry(dstW, dstH);
    const ui::PreviewRect r = ui::ComputePreviewRect();
    return FitRect{ r.x, r.y, r.w, r.h };
}

// Size of the D3D12 preview's offscreen RT. It follows the window's aspect and
// zoom geometry, but is capped independently from a maximized/high-DPI window
// so desktop debugging cannot consume an extra full frame of CPU time.
static void ComputePreviewRTSize(rt::Session& s, int& outW, int& outH) {
    int clientW = 0, clientH = 0;
    GetPreviewClientSize(s, 0, 0, clientW, clientH);
    const double scale = (std::min)({
        1.0,
        (double)kPreviewMaxWidth / (std::max)(1, clientW),
        (double)kPreviewMaxHeight / (std::max)(1, clientH),
    });
    outW = (std::max)(1, (int)std::lround(clientW * scale));
    outH = (std::max)(1, (int)std::lround(clientH * scale));
}

// Snap the preview window so its client aspect matches the content aspect,
// keeping the current client width. Height is ours to derive; width is whatever
// the user last chose. No-op when the aspect already matches. Called from
// WM_SIZE, and from the render path when the content aspect itself changes.
static void FitWindowToContentAspect(HWND hWnd) {
    if (!hWnd) return;
    // Fill mode deliberately permits any window/monitor aspect. The compositor scales the
    // source to the full client rect, so maximizing must stay maximized instead of being
    // snapped back to an aspect-correct floating window.
    if (ui::g_uiState.fitToWindow) return;

    RECT cr{};
    if (!GetClientRect(hWnd, &cr)) return;
    const int cw = cr.right - cr.left;
    const int ch = cr.bottom - cr.top;
    if (cw <= 0 || ch <= 0) return;

    int contentW = 0, contentH = 0;
    ComputeDisplayDims(contentW, contentH);
    if (contentW <= 0 || contentH <= 0) return;

    const double contentAspect = (double)contentW / (double)contentH;
    const double clientAspect  = (double)cw / (double)ch;
    if (fabs(contentAspect - clientAspect) < 0.005 * (contentAspect + clientAspect)) return;

    int targetCH = (int)((double)cw / contentAspect + 0.5);
    if (targetCH < 1) targetCH = 1;

    RECT rc{0, 0, (LONG)cw, targetCH};
    AdjustWindowRectEx(&rc, (DWORD)GetWindowLongW(hWnd, GWL_STYLE), GetMenu(hWnd) != nullptr,
                       (DWORD)GetWindowLongW(hWnd, GWL_EXSTYLE));
    SetWindowPos(hWnd, nullptr, 0, 0, rc.right - rc.left, rc.bottom - rc.top,
                 SWP_NOMOVE | SWP_NOZORDER);
}

// Re-shape the window for a new content aspect -- a view mode or layout change, which
// turns a side-by-side pair into a single eye and back. Zoom no longer touches the
// window, so this is the only thing that resizes it, and it keeps the client area the
// user had rather than jumping back to a panel-derived size every time.
static void ResizeWindowForContent(HWND hWnd) {
    if (!hWnd) return;

    int contentW = 0, contentH = 0;
    ComputeDisplayDims(contentW, contentH);
    if (contentW <= 0 || contentH <= 0) return;
    const double aspect = (double)contentW / (double)contentH;

    RECT cr{};
    if (!GetClientRect(hWnd, &cr)) return;
    const double area = (double)(cr.right - cr.left) * (double)(cr.bottom - cr.top);
    if (area < 1.0) return;

    int targetW = (int)(sqrt(area * aspect) + 0.5);
    int targetH = (int)(sqrt(area / aspect) + 0.5);
    ClampToWorkArea(targetW, targetH);

    RECT rc{0, 0, targetW, targetH};
    AdjustWindowRectEx(&rc, (DWORD)GetWindowLongW(hWnd, GWL_STYLE), GetMenu(hWnd) != nullptr,
                       (DWORD)GetWindowLongW(hWnd, GWL_EXSTYLE));
    SetWindowPos(hWnd, nullptr, 0, 0, rc.right - rc.left, rc.bottom - rc.top,
                 SWP_NOMOVE | SWP_NOZORDER);
}

// View format for sampling a D3D12 swapchain image in the preview's scaling pass. The
// resource can be typeless (XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT), and sRGB is dropped
// on purpose: the pass filters the bytes in whatever encoding they are stored in and
// writes them through unchanged, which is what the GDI stretch it replaced did.
static DXGI_FORMAT PreviewSrvFormat(DXGI_FORMAT resourceFormat) {
    switch (resourceFormat) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS:
        case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:     return DXGI_FORMAT_R8G8B8A8_UNORM;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS:
        case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:     return DXGI_FORMAT_B8G8R8A8_UNORM;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS:   return DXGI_FORMAT_R16G16B16A16_FLOAT;
        case DXGI_FORMAT_R32G32B32A32_TYPELESS:   return DXGI_FORMAT_R32G32B32A32_FLOAT;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS:    return DXGI_FORMAT_R10G10B10A2_UNORM;
        default:                                  return resourceFormat;
    }
}

// Block until the last composite handed to the preview queue has run. Leaves the pending
// slots alone, so whatever is waiting to be painted still gets painted.
static void WaitForPreviewFence(rt::Session& s) {
    if (!s.previewFence || !s.previewFenceEvent || s.previewFenceValue == 0) return;
    if (s.previewFence->GetCompletedValue() < s.previewFenceValue) {
        s.previewFence->SetEventOnCompletion(s.previewFenceValue, s.previewFenceEvent);
        WaitForSingleObject(s.previewFenceEvent, 1000);
    }
}

// Block until nothing the preview queue has submitted is still running, so the
// resources it reads can be dropped.
static void WaitForPreviewIdle(rt::Session& s) {
    WaitForPreviewFence(s);
    // Nothing is in flight any more, so no slot is waiting to be picked up either.
    for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
        s.previewFrames[i].pending = false;
        s.previewFrames[i].recording = false;
    }
    s.previewSlotOpen = false;
}

// Just the resources that depend on the preview's size. The window is resizable and
// the RT tracks its fit rect, so this runs on every resize step - the queue, fences
// and command list have to survive it. Recreating the cross-queue fence in particular
// would drop a signal the app's own queue is mid-flight on.
static void ResetD3D12PreviewSurfaces(rt::Session& s) {
    WaitForPreviewIdle(s);
    s.previewRT12.Reset();
    s.previewRtvHeap.Reset();
    for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
        s.previewFrames[i].readback.Reset();
        s.previewFrames[i].rtWidth = s.previewFrames[i].rtHeight = 0;
    }
    s.previewReadbackPitch = 0;
}

static void ResetD3D12PreviewResources(rt::Session& s) {
    // Same reason xrDestroySwapchain waits: nothing the preview queue has submitted keeps
    // the resources it reads alive, and it is no longer drained by the frame that queued it.
    WaitForPreviewIdle(s);
    s.previewRT12.Reset();
    for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
        s.previewFrames[i] = rt::PreviewFrame12{};
    }
    s.previewSlot = 0;
    s.previewSlotOpen = false;
    s.previewReadbackPitch = 0;
    s.previewCmdList.Reset();
    s.previewFence.Reset();
    s.previewFenceValue = 0;
    s.previewQueue12.Reset();
    // Device-owned, so they cannot outlive the session that created them.
    s.previewRtvHeap.Reset();
    s.previewSrvHeap.Reset();
    s.previewPSO.Reset();
    s.previewRootSig.Reset();
    s.previewSrvSlot = 0;
    s.crossQueueFence.Reset();
    s.crossQueueFenceValue = 0;
    if (s.previewFenceEvent) {
        CloseHandle(s.previewFenceEvent);
        s.previewFenceEvent = nullptr;
    }
}

static LRESULT CALLBACK WndProc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    // Dark menu bar: the WM_UAH* custom-draw messages plus the light seam
    // DefWindowProc leaves under the bar on WM_NCPAINT/WM_NCACTIVATE.
    LRESULT darkMenuResult = 0;
    if (ui::HandleDarkMenuMessage(hWnd, msg, wParam, lParam, &darkMenuResult)) {
        return darkMenuResult;
    }
    switch (msg) {
        case WM_CLOSE:
            if (rt::g_session.handle != XR_NULL_HANDLE) {
                rt::PushState(rt::g_session.handle, XR_SESSION_STATE_EXITING);
            }
            Log("[SimXR] WndProc: WM_CLOSE received");
            DestroyWindow(hWnd);
            return 0;
        case WM_DESTROY:
            // DON'T call PostQuitMessage - we're a DLL, not the main app!
            // PostQuitMessage would tell the host application to exit.
            Log("[SimXR] WndProc: WM_DESTROY received");
            return 0;
        case WM_ACTIVATE:
            if (LOWORD(wParam) != WA_INACTIVE) {
                rt::g_session.isFocused = true;
                Log("[SimXR] WndProc: WM_ACTIVATE -> focused");
                // Push FOCUSED state if we were VISIBLE
                if (rt::g_session.state == XR_SESSION_STATE_VISIBLE) {
                    rt::PushState(rt::g_session.handle, XR_SESSION_STATE_FOCUSED);
                }
            } else {
                rt::g_session.isFocused = false;
                Log("[SimXR] WndProc: WM_ACTIVATE -> unfocused");
                rt::g_mouseCapture = false;  // Release mouse capture when window loses focus
                rt::g_panDrag = false;
                ReleaseCapture();
                // Push VISIBLE state if we were FOCUSED
                if (rt::g_session.state == XR_SESSION_STATE_FOCUSED) {
                    rt::PushState(rt::g_session.handle, XR_SESSION_STATE_VISIBLE);
                }
            }
            return 0;
        case WM_LBUTTONDOWN:
            Logf("[SimXR] WM_LBUTTONDOWN: focused=%d", rt::g_session.isFocused.load());
            if (rt::g_session.isFocused) {
                rt::g_mouseCapture = true;
                SetCapture(hWnd);
                GetCursorPos(&rt::g_lastMousePos);
                ShowCursor(FALSE);
                Log("[SimXR] Mouse captured for look control");
            }
            return 0;
        case WM_LBUTTONUP:
            if (rt::g_mouseCapture) {
                rt::g_mouseCapture = false;
                ReleaseCapture();
                ShowCursor(TRUE);
            }
            return 0;
        case WM_MBUTTONDOWN:
            // Middle drag pans the zoomed image. Left is already mouse look, and this
            // way panning stays available while the head is being aimed.
            rt::g_panDrag = true;
            rt::g_panLastPos = POINT{ GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
            SetCapture(hWnd);
            return 0;
        case WM_MBUTTONUP:
            if (rt::g_panDrag) {
                rt::g_panDrag = false;
                ReleaseCapture();
            }
            return 0;
        case WM_ENTERSIZEMOVE:
            rt::g_sizingDrag = true;
            return 0;
        case WM_EXITSIZEMOVE:
            rt::g_sizingDrag = false;
            ui::SaveSettings();
            return 0;
        case WM_SIZING: {
            // Fill mode accepts arbitrary window shapes and performs the up/downscale in
            // the preview compositor. Manual zoom mode keeps the aspect-locked inspector
            // behavior that makes pixel-scale comparisons predictable.
            if (ui::g_uiState.fitToWindow) break;

            // Constrain interactive resize to the current content aspect ratio
            // so the window itself follows the stereo layout (no letterbox).
            int contentW = 0, contentH = 0;
            rt::ComputeDisplayDims(contentW, contentH);
            if (contentW <= 0 || contentH <= 0) break;
            double aspect = (double)contentW / (double)contentH;

            // Measure the non-client overhead (borders + title bar + menu)
            DWORD style   = (DWORD)GetWindowLongW(hWnd, GWL_STYLE);
            DWORD exStyle = (DWORD)GetWindowLongW(hWnd, GWL_EXSTYLE);
            BOOL  hasMenu = GetMenu(hWnd) != nullptr;
            RECT probe{0, 0, 100, 100};
            AdjustWindowRectEx(&probe, style, hasMenu, exStyle);
            int ncW = (probe.right - probe.left) - 100;
            int ncH = (probe.bottom - probe.top) - 100;

            RECT* r = (RECT*)lParam;
            int clientW = (r->right - r->left) - ncW;
            int clientH = (r->bottom - r->top) - ncH;
            if (clientW < 1) clientW = 1;
            if (clientH < 1) clientH = 1;

            UINT edge = (UINT)wParam;
            // Decide which axis drives the constraint:
            //   left/right edge → width is canonical
            //   top/bottom edge → height is canonical
            //   corner → use whichever side the user pulled "further" so
            //            dragging outward grows the window in both axes
            bool widthCanonical;
            switch (edge) {
                case WMSZ_LEFT:
                case WMSZ_RIGHT:
                    widthCanonical = true;
                    break;
                case WMSZ_TOP:
                case WMSZ_BOTTOM:
                    widthCanonical = false;
                    break;
                default:
                    widthCanonical = ((double)clientW / aspect) >= (double)clientH;
                    break;
            }

            if (widthCanonical) {
                clientH = (int)((double)clientW / aspect + 0.5);
            } else {
                clientW = (int)((double)clientH * aspect + 0.5);
            }
            if (clientW < 1) clientW = 1;
            if (clientH < 1) clientH = 1;

            int outerW = clientW + ncW;
            int outerH = clientH + ncH;

            // Anchor the rect to whichever corner the user is NOT dragging
            switch (edge) {
                case WMSZ_LEFT:
                    r->left   = r->right  - outerW;
                    r->bottom = r->top    + outerH;
                    break;
                case WMSZ_RIGHT:
                    r->right  = r->left   + outerW;
                    r->bottom = r->top    + outerH;
                    break;
                case WMSZ_TOP:
                    r->top    = r->bottom - outerH;
                    r->right  = r->left   + outerW;
                    break;
                case WMSZ_BOTTOM:
                    r->bottom = r->top    + outerH;
                    r->right  = r->left   + outerW;
                    break;
                case WMSZ_TOPLEFT:
                    r->left   = r->right  - outerW;
                    r->top    = r->bottom - outerH;
                    break;
                case WMSZ_TOPRIGHT:
                    r->right  = r->left   + outerW;
                    r->top    = r->bottom - outerH;
                    break;
                case WMSZ_BOTTOMLEFT:
                    r->left   = r->right  - outerW;
                    r->bottom = r->top    + outerH;
                    break;
                case WMSZ_BOTTOMRIGHT:
                default:
                    r->right  = r->left   + outerW;
                    r->bottom = r->top    + outerH;
                    break;
            }
            return TRUE;
        }
        case WM_SIZE: {
            // Capture the new client area. The render path picks this up next
            // frame and resizes the swapchain (D3D11/GL) or recomputes the
            // letterbox destination (D3D12 GDI). Ignore minimize (0×0).
            UINT cw = LOWORD(lParam);
            UINT ch = HIWORD(lParam);
            if (cw == 0 || ch == 0) return 0;
            rt::g_session.clientWidth.store(cw);
            rt::g_session.clientHeight.store(ch);

            // Remember the size for the next run. Recorded ahead of the aspect snap
            // below, which resizes again and lands here a second time -- so the
            // corrected size is what the last write leaves in the file. Skipped while
            // a drag is in progress; WM_EXITSIZEMOVE writes once at the end of it.
            ui::g_uiState.windowWidth = (int)cw;
            ui::g_uiState.windowHeight = (int)ch;
            if (!rt::g_sizingDrag) ui::SaveSettings();

            // Fill mode means exactly that: keep the dimensions Windows assigned, including
            // a maximized client area, and let ComputePreviewRect cover every pixel.
            if (ui::g_uiState.fitToWindow) return 0;

            // WM_SIZING constrains interactive drags, but maximize, snap, and
            // programmatic resizes bypass it. If the resulting client aspect
            // doesn't match content aspect, snap the window so we never need
            // to fall back to the letterbox bars at runtime.
            static thread_local bool inFix = false;
            if (inFix) return 0;

            int contentW = 0, contentH = 0;
            rt::ComputeDisplayDims(contentW, contentH);
            if (contentW <= 0 || contentH <= 0) return 0;
            double contentAspect = (double)contentW / (double)contentH;
            double clientAspect  = (double)cw / (double)ch;
            // Tolerance: don't fight ourselves over sub-pixel rounding.
            if (fabs(contentAspect - clientAspect) <
                0.005 * (contentAspect + clientAspect)) {
                return 0;
            }

            DWORD style   = (DWORD)GetWindowLongW(hWnd, GWL_STYLE);
            DWORD exStyle = (DWORD)GetWindowLongW(hWnd, GWL_EXSTYLE);
            BOOL  hasMenu = GetMenu(hWnd) != nullptr;

            if (wParam == SIZE_MAXIMIZED) {
                // Restore out of maximized state, then size to the largest
                // aspect-correct rect that fits in the monitor's work area.
                HMONITOR hmon = MonitorFromWindow(hWnd, MONITOR_DEFAULTTONEAREST);
                MONITORINFO mi{}; mi.cbSize = sizeof(mi);
                if (!GetMonitorInfoW(hmon, &mi)) return 0;
                int availW = mi.rcWork.right - mi.rcWork.left;
                int availH = mi.rcWork.bottom - mi.rcWork.top;

                RECT probe{0, 0, 100, 100};
                AdjustWindowRectEx(&probe, style & ~WS_MAXIMIZE, hasMenu, exStyle);
                int ncW = (probe.right - probe.left) - 100;
                int ncH = (probe.bottom - probe.top) - 100;
                int maxCW = availW - ncW; if (maxCW < 1) maxCW = 1;
                int maxCH = availH - ncH; if (maxCH < 1) maxCH = 1;

                rt::FitRect fit = rt::ComputeFitRect(contentW, contentH, maxCW, maxCH);
                int targetCW = (int)fit.w; if (targetCW < 1) targetCW = 1;
                int targetCH = (int)fit.h; if (targetCH < 1) targetCH = 1;

                RECT rc{0, 0, targetCW, targetCH};
                AdjustWindowRectEx(&rc, style & ~WS_MAXIMIZE, hasMenu, exStyle);
                int outerW = rc.right - rc.left;
                int outerH = rc.bottom - rc.top;
                int x = mi.rcWork.left + (availW - outerW) / 2;
                int y = mi.rcWork.top  + (availH - outerH) / 2;

                inFix = true;
                ShowWindow(hWnd, SW_RESTORE);
                SetWindowPos(hWnd, nullptr, x, y, outerW, outerH, SWP_NOZORDER);
                inFix = false;
            } else {
                // Anything else (DPI change, programmatic, Aero snap, etc.):
                // preserve the new width, recompute height to match aspect.
                inFix = true;
                rt::FitWindowToContentAspect(hWnd);
                inFix = false;
            }
            return 0;
        }
        case WM_MOUSEMOVE:
            if (rt::g_panDrag) {
                const POINT pos{ GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
                ui::g_uiState.panX += (float)(pos.x - rt::g_panLastPos.x);
                ui::g_uiState.panY += (float)(pos.y - rt::g_panLastPos.y);
                rt::g_panLastPos = pos;
                rt::RefreshPreviewGeometry(hWnd);
                ui::ClampPan();
                return 0;
            }
            if (rt::g_mouseCapture) {
                POINT currentPos;
                GetCursorPos(&currentPos);
                
                // Calculate delta
                int deltaX = currentPos.x - rt::g_lastMousePos.x;
                int deltaY = currentPos.y - rt::g_lastMousePos.y;
                
                // Update yaw and pitch (with sensitivity)
                const float sensitivity = 0.002f;
                rt::g_headYaw -= deltaX * sensitivity;
                rt::g_headPitch -= deltaY * sensitivity;  // Inverted for natural feel
                
                // Clamp pitch to avoid gimbal lock
                const float maxPitch = 1.5f;  // ~85 degrees
                if (rt::g_headPitch > maxPitch) rt::g_headPitch = maxPitch;
                if (rt::g_headPitch < -maxPitch) rt::g_headPitch = -maxPitch;
                
                // Reset cursor to center of window to avoid hitting screen edges
                RECT rect;
                GetWindowRect(hWnd, &rect);
                int centerX = (rect.left + rect.right) / 2;
                int centerY = (rect.top + rect.bottom) / 2;
                SetCursorPos(centerX, centerY);
                rt::g_lastMousePos.x = centerX;
                rt::g_lastMousePos.y = centerY;
            }
            return 0;
        case WM_COMMAND:
            // The zoom items anchor on the middle of the window, so they need the
            // geometry to be current even if no frame has been presented since a resize.
            rt::RefreshPreviewGeometry(hWnd);
            if (ui::HandleMenuCommand(hWnd, wParam,
                []() { rt::ResizeWindowForContent(rt::g_session.hwnd); },
                []() { mcp::g_screenshotRequested = true; },
                []() {
                    rt::g_headPos = {0, 1.7f, 0}; rt::g_headYaw = 0; rt::g_headPitch = 0; rt::g_headRoll = 0;
                    ui::SetFitToWindow();
                    ui::SaveSettings();
                },
                [](int cmd) { rt::HandleUiSettingsCommand(cmd); }
            )) {
                rt::RefreshTitleNow(hWnd);
                return 0;
            }
            break;
        case WM_KEYDOWN:
            if (!rt::g_mouseCapture) {
                rt::RefreshPreviewGeometry(hWnd);
                if (ui::HandleKeyboardShortcut(hWnd, wParam,
                    []() { rt::ResizeWindowForContent(rt::g_session.hwnd); },
                    []() { mcp::g_screenshotRequested = true; },
                    []() {
                        rt::g_headPos = {0, 1.7f, 0}; rt::g_headYaw = 0; rt::g_headPitch = 0; rt::g_headRoll = 0;
                        ui::SetFitToWindow();
                        ui::SaveSettings();
                    },
                    [](int cmd) { rt::HandleUiSettingsCommand(cmd); }
                )) {
                    rt::RefreshTitleNow(hWnd);
                    return 0;
                }
            }
            break;
        case WM_MOUSEWHEEL: {
            // Wheel coordinates are on the screen, unlike every other mouse message.
            POINT pt{ GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
            ScreenToClient(hWnd, &pt);
            rt::RefreshPreviewGeometry(hWnd);
            ui::HandleMouseWheel(hWnd, GET_WHEEL_DELTA_WPARAM(wParam), (float)pt.x, (float)pt.y);
            rt::RefreshTitleNow(hWnd);
            return 0;
        }
        default:
            break;
    }
    return DefWindowProcW(hWnd, msg, wParam, lParam);
}

static void ensurePreview(Session& s) {
    if (s.hwnd) return;
    WNDCLASSW wc{}; wc.lpfnWndProc = WndProc; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"OpenXR Simulator";
    RegisterClassW(&wc);
    s.hwnd = CreateWindowExW(0, wc.lpszClassName, L"OpenXR Simulator", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                             CW_USEDEFAULT, CW_USEDEFAULT, (int)s.previewWidth, (int)s.previewHeight, nullptr, nullptr, wc.hInstance, nullptr);
    Logf("[SimXR] ensurePreview: hwnd=%p size=%ux%u usesD3D12=%d", s.hwnd, s.previewWidth, s.previewHeight, s.usesD3D12);

    // Make sure window is shown and updated
    if (s.hwnd) {
        ShowWindow(s.hwnd, SW_SHOW);
        UpdateWindow(s.hwnd);

        // Check if window has focus
        if (GetForegroundWindow() == s.hwnd) {
            s.isFocused = true;
            Log("[SimXR] Window created with focus");
        } else {
            s.isFocused = false;
            Log("[SimXR] Window created without focus");
        }
    }

    // Swapchain creation is now handled in ensurePreviewSized for both D3D11 and D3D12
}

// Window thread functions removed - window now handled on main thread

} // namespace rt

// ============ Vulkan sessions (XR_KHR_vulkan_enable / XR_KHR_vulkan_enable2) ============
//
// The compositor stays D3D12. A Vulkan session's swapchain images are D3D12 committed
// resources created with D3D12_HEAP_FLAG_SHARED and imported into the app's VkDevice
// through VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_RESOURCE_BIT, so the app renders into
// VkImages and the preview, quad, screenshot and burst-capture paths read the very same
// pixels as ID3D12Resources without knowing a second API exists.
//
// The interop rules here are the ones BetterVR's own Vulkan/D3D12 bridge arrived at
// against real AMD and NVIDIA drivers: ALLOW_SIMULTANEOUS_ACCESS on colour so the D3D12
// side is uncompressed, a typeless format for depth, an explicit
// D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT, and a timeline semaphore driven by one
// strictly increasing counter rather than a two-value ping-pong.
namespace vkrt {

struct Dispatch {
    PFN_vkGetInstanceProcAddr GetInstanceProcAddr{nullptr};
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr{nullptr};
    PFN_vkEnumeratePhysicalDevices EnumeratePhysicalDevices{nullptr};
    PFN_vkGetPhysicalDeviceProperties2 GetPhysicalDeviceProperties2{nullptr};
    PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties{nullptr};
    PFN_vkGetDeviceQueue GetDeviceQueue{nullptr};
    PFN_vkDeviceWaitIdle DeviceWaitIdle{nullptr};
    PFN_vkQueueWaitIdle QueueWaitIdle{nullptr};
    PFN_vkQueueSubmit QueueSubmit{nullptr};
    PFN_vkCreateImage CreateImage{nullptr};
    PFN_vkDestroyImage DestroyImage{nullptr};
    PFN_vkGetImageMemoryRequirements2 GetImageMemoryRequirements2{nullptr};
    PFN_vkBindImageMemory2 BindImageMemory2{nullptr};
    PFN_vkAllocateMemory AllocateMemory{nullptr};
    PFN_vkFreeMemory FreeMemory{nullptr};
    PFN_vkGetMemoryWin32HandlePropertiesKHR GetMemoryWin32HandlePropertiesKHR{nullptr};
    PFN_vkCreateSemaphore CreateSemaphore{nullptr};
    PFN_vkDestroySemaphore DestroySemaphore{nullptr};
    PFN_vkImportSemaphoreWin32HandleKHR ImportSemaphoreWin32HandleKHR{nullptr};
    PFN_vkCreateCommandPool CreateCommandPool{nullptr};
    PFN_vkDestroyCommandPool DestroyCommandPool{nullptr};
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers{nullptr};
    PFN_vkResetCommandPool ResetCommandPool{nullptr};
    PFN_vkBeginCommandBuffer BeginCommandBuffer{nullptr};
    PFN_vkEndCommandBuffer EndCommandBuffer{nullptr};
    PFN_vkCmdPipelineBarrier CmdPipelineBarrier{nullptr};
};

static Dispatch g_d{};

// The app hands one over with XR_KHR_vulkan_enable2; a v1 app never does, so fall back to
// whatever vulkan-1.dll the process already has. GetModuleHandle first: an app that ships
// a proxy loader next to its executable would have LoadLibrary resolve to the proxy
// anyway, and this avoids a second reference on it.
static PFN_vkGetInstanceProcAddr g_appGipa = nullptr;

// vkCreateDevice and vkEnumerateDeviceExtensionProperties are instance-level: a null
// VkInstance only resolves the four global entry points. xrCreateVulkanDeviceKHR is not
// handed an instance, so remember the one the app created or asked about.
static VkInstance g_lastInstance = VK_NULL_HANDLE;

static PFN_vkGetInstanceProcAddr Gipa() {
    if (g_appGipa) return g_appGipa;
    static PFN_vkGetInstanceProcAddr cached = []() -> PFN_vkGetInstanceProcAddr {
        HMODULE m = GetModuleHandleA("vulkan-1.dll");
        if (!m) m = LoadLibraryA("vulkan-1.dll");
        if (!m) { Log("[SimXR][VK] vulkan-1.dll is not loadable"); return nullptr; }
        char path[MAX_PATH]{};
        GetModuleFileNameA(m, path, (DWORD)sizeof(path));
        Logf("[SimXR][VK] loader module: %s", path);
        return (PFN_vkGetInstanceProcAddr)GetProcAddress(m, "vkGetInstanceProcAddr");
    }();
    return cached;
}

template <typename T>
static T InstFn(VkInstance inst, const char* name, const char* altName = nullptr) {
    PFN_vkGetInstanceProcAddr gipa = Gipa();
    if (!gipa) return nullptr;
    T fn = (T)gipa(inst, name);
    if (!fn && altName) fn = (T)gipa(inst, altName);
    return fn;
}

template <typename T>
static T DevFn(VkDevice dev, const char* name, const char* altName = nullptr) {
    if (!g_d.GetDeviceProcAddr) return nullptr;
    T fn = (T)g_d.GetDeviceProcAddr(dev, name);
    if (!fn && altName) fn = (T)g_d.GetDeviceProcAddr(dev, altName);
    return fn;
}

static bool LoadInstanceLevel(VkInstance inst) {
    g_d.GetInstanceProcAddr = Gipa();
    if (!g_d.GetInstanceProcAddr) return false;
    g_d.GetDeviceProcAddr = InstFn<PFN_vkGetDeviceProcAddr>(inst, "vkGetDeviceProcAddr");
    g_d.EnumeratePhysicalDevices = InstFn<PFN_vkEnumeratePhysicalDevices>(inst, "vkEnumeratePhysicalDevices");
    g_d.GetPhysicalDeviceProperties2 = InstFn<PFN_vkGetPhysicalDeviceProperties2>(
        inst, "vkGetPhysicalDeviceProperties2", "vkGetPhysicalDeviceProperties2KHR");
    g_d.GetPhysicalDeviceMemoryProperties = InstFn<PFN_vkGetPhysicalDeviceMemoryProperties>(inst, "vkGetPhysicalDeviceMemoryProperties");
    return g_d.GetDeviceProcAddr && g_d.EnumeratePhysicalDevices && g_d.GetPhysicalDeviceProperties2;
}

static bool LoadDeviceLevel(VkDevice dev) {
    g_d.GetDeviceQueue = DevFn<PFN_vkGetDeviceQueue>(dev, "vkGetDeviceQueue");
    g_d.DeviceWaitIdle = DevFn<PFN_vkDeviceWaitIdle>(dev, "vkDeviceWaitIdle");
    g_d.QueueWaitIdle = DevFn<PFN_vkQueueWaitIdle>(dev, "vkQueueWaitIdle");
    g_d.QueueSubmit = DevFn<PFN_vkQueueSubmit>(dev, "vkQueueSubmit");
    g_d.CreateImage = DevFn<PFN_vkCreateImage>(dev, "vkCreateImage");
    g_d.DestroyImage = DevFn<PFN_vkDestroyImage>(dev, "vkDestroyImage");
    g_d.GetImageMemoryRequirements2 = DevFn<PFN_vkGetImageMemoryRequirements2>(
        dev, "vkGetImageMemoryRequirements2", "vkGetImageMemoryRequirements2KHR");
    g_d.BindImageMemory2 = DevFn<PFN_vkBindImageMemory2>(dev, "vkBindImageMemory2", "vkBindImageMemory2KHR");
    g_d.AllocateMemory = DevFn<PFN_vkAllocateMemory>(dev, "vkAllocateMemory");
    g_d.FreeMemory = DevFn<PFN_vkFreeMemory>(dev, "vkFreeMemory");
    g_d.GetMemoryWin32HandlePropertiesKHR = DevFn<PFN_vkGetMemoryWin32HandlePropertiesKHR>(dev, "vkGetMemoryWin32HandlePropertiesKHR");
    g_d.CreateSemaphore = DevFn<PFN_vkCreateSemaphore>(dev, "vkCreateSemaphore");
    g_d.DestroySemaphore = DevFn<PFN_vkDestroySemaphore>(dev, "vkDestroySemaphore");
    g_d.ImportSemaphoreWin32HandleKHR = DevFn<PFN_vkImportSemaphoreWin32HandleKHR>(dev, "vkImportSemaphoreWin32HandleKHR");
    g_d.CreateCommandPool = DevFn<PFN_vkCreateCommandPool>(dev, "vkCreateCommandPool");
    g_d.DestroyCommandPool = DevFn<PFN_vkDestroyCommandPool>(dev, "vkDestroyCommandPool");
    g_d.AllocateCommandBuffers = DevFn<PFN_vkAllocateCommandBuffers>(dev, "vkAllocateCommandBuffers");
    g_d.ResetCommandPool = DevFn<PFN_vkResetCommandPool>(dev, "vkResetCommandPool");
    g_d.BeginCommandBuffer = DevFn<PFN_vkBeginCommandBuffer>(dev, "vkBeginCommandBuffer");
    g_d.EndCommandBuffer = DevFn<PFN_vkEndCommandBuffer>(dev, "vkEndCommandBuffer");
    g_d.CmdPipelineBarrier = DevFn<PFN_vkCmdPipelineBarrier>(dev, "vkCmdPipelineBarrier");

    const void* required[] = {
        (const void*)g_d.GetDeviceQueue, (const void*)g_d.QueueSubmit, (const void*)g_d.CreateImage,
        (const void*)g_d.DestroyImage, (const void*)g_d.GetImageMemoryRequirements2,
        (const void*)g_d.BindImageMemory2, (const void*)g_d.AllocateMemory, (const void*)g_d.FreeMemory,
        (const void*)g_d.GetMemoryWin32HandlePropertiesKHR, (const void*)g_d.CreateCommandPool,
        (const void*)g_d.AllocateCommandBuffers, (const void*)g_d.BeginCommandBuffer,
        (const void*)g_d.EndCommandBuffer, (const void*)g_d.CmdPipelineBarrier,
    };
    for (const void* p : required) if (!p) return false;
    return true;
}

// --- formats ------------------------------------------------------------------------------
// The pairing is the DXGI format the shared resource is created with. Colour keeps the
// typed sRGB/UNORM format (PreviewSrvFormat casts it to a UNORM view for the eye pass, so
// the encoded bytes pass through unchanged); depth goes typeless, which is both what a DSV
// wants and what BetterVR's own shared depth textures use.
struct FormatPair { int64_t vk; DXGI_FORMAT typed; DXGI_FORMAT resource; bool depth; };

static const FormatPair kFormats[] = {
    { VK_FORMAT_R8G8B8A8_SRGB,            DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, false },
    { VK_FORMAT_B8G8R8A8_SRGB,            DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, false },
    { VK_FORMAT_R8G8B8A8_UNORM,           DXGI_FORMAT_R8G8B8A8_UNORM,      DXGI_FORMAT_R8G8B8A8_UNORM,      false },
    { VK_FORMAT_B8G8R8A8_UNORM,           DXGI_FORMAT_B8G8R8A8_UNORM,      DXGI_FORMAT_B8G8R8A8_UNORM,      false },
    { VK_FORMAT_A2B10G10R10_UNORM_PACK32, DXGI_FORMAT_R10G10B10A2_UNORM,   DXGI_FORMAT_R10G10B10A2_UNORM,   false },
    { VK_FORMAT_R16G16B16A16_SFLOAT,      DXGI_FORMAT_R16G16B16A16_FLOAT,  DXGI_FORMAT_R16G16B16A16_FLOAT,  false },
    { VK_FORMAT_D32_SFLOAT,               DXGI_FORMAT_D32_FLOAT,           DXGI_FORMAT_R32_TYPELESS,        true  },
    { VK_FORMAT_D24_UNORM_S8_UINT,        DXGI_FORMAT_D24_UNORM_S8_UINT,   DXGI_FORMAT_R24G8_TYPELESS,      true  },
    { VK_FORMAT_D32_SFLOAT_S8_UINT,       DXGI_FORMAT_D32_FLOAT_S8X24_UINT,DXGI_FORMAT_R32G8X24_TYPELESS,   true  },
    { VK_FORMAT_D16_UNORM,                DXGI_FORMAT_D16_UNORM,           DXGI_FORMAT_R16_TYPELESS,        true  },
};

static const FormatPair* FindFormat(int64_t vkFormat) {
    for (const auto& f : kFormats) if (f.vk == vkFormat) return &f;
    return nullptr;
}

// --- adapter matching ---------------------------------------------------------------------

static bool PreferredAdapterLuid(LUID& out) {
    ComPtr<IDXGIFactory1> f;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(f.GetAddressOf())))) return false;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> a;
        if (f->EnumAdapters1(i, a.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 d{}; a->GetDesc1(&d);
        if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) continue;
        out = d.AdapterLuid;
        return true;
    }
    return false;
}

static bool PhysicalDeviceLuid(VkPhysicalDevice pd, LUID& out) {
    if (!g_d.GetPhysicalDeviceProperties2) return false;
    VkPhysicalDeviceIDProperties id{ VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES };
    VkPhysicalDeviceProperties2 props{ VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 };
    props.pNext = &id;
    g_d.GetPhysicalDeviceProperties2(pd, &props);
    if (!id.deviceLUIDValid) return false;
    memcpy(&out, id.deviceLUID, sizeof(LUID));
    return true;
}

// The VkPhysicalDevice for the adapter the compositor will run on. Matching the LUID is
// what makes the shared-handle import legal: a D3D12 resource can only be opened by a
// Vulkan device on the same physical adapter.
static XrResult PickPhysicalDevice(VkInstance vkInstance, VkPhysicalDevice* out) {
    if (!vkInstance || !out) return XR_ERROR_VALIDATION_FAILURE;
    g_lastInstance = vkInstance;
    if (!LoadInstanceLevel(vkInstance)) {
        Log("[SimXR][VK] could not resolve the instance-level entry points");
        return XR_ERROR_RUNTIME_FAILURE;
    }
    uint32_t count = 0;
    if (g_d.EnumeratePhysicalDevices(vkInstance, &count, nullptr) != VK_SUCCESS || count == 0) {
        Log("[SimXR][VK] vkEnumeratePhysicalDevices returned nothing");
        return XR_ERROR_RUNTIME_FAILURE;
    }
    std::vector<VkPhysicalDevice> devices(count);
    if (g_d.EnumeratePhysicalDevices(vkInstance, &count, devices.data()) != VK_SUCCESS) return XR_ERROR_RUNTIME_FAILURE;

    LUID want{};
    const bool haveWant = PreferredAdapterLuid(want);
    for (VkPhysicalDevice pd : devices) {
        LUID got{};
        if (!PhysicalDeviceLuid(pd, got)) continue;
        Logf("[SimXR][VK] physical device %p LUID=%08lX:%08lX", (void*)pd,
             (unsigned long)got.HighPart, (unsigned long)got.LowPart);
        if (haveWant && got.LowPart == want.LowPart && got.HighPart == want.HighPart) {
            *out = pd;
            Logf("[SimXR][VK] matched the compositor's adapter LUID=%08lX:%08lX",
                 (unsigned long)want.HighPart, (unsigned long)want.LowPart);
            return XR_SUCCESS;
        }
    }
    *out = devices[0];
    Log("[SimXR][VK] no physical device matched the compositor's adapter LUID; using the first one");
    return XR_SUCCESS;
}

// --- extension injection ------------------------------------------------------------------

static const char* kInstanceExtensions[] = {
    VK_KHR_EXTERNAL_MEMORY_CAPABILITIES_EXTENSION_NAME,
    VK_KHR_EXTERNAL_SEMAPHORE_CAPABILITIES_EXTENSION_NAME,
    VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME,
};

static const char* kDeviceExtensions[] = {
    VK_KHR_EXTERNAL_MEMORY_EXTENSION_NAME,
    VK_KHR_EXTERNAL_MEMORY_WIN32_EXTENSION_NAME,
    VK_KHR_EXTERNAL_SEMAPHORE_EXTENSION_NAME,
    VK_KHR_EXTERNAL_SEMAPHORE_WIN32_EXTENSION_NAME,
    VK_KHR_DEDICATED_ALLOCATION_EXTENSION_NAME,
    VK_KHR_GET_MEMORY_REQUIREMENTS_2_EXTENSION_NAME,
    VK_KHR_TIMELINE_SEMAPHORE_EXTENSION_NAME,
};

static std::string JoinExtensions(const char* const* names, size_t count) {
    std::string s;
    for (size_t i = 0; i < count; ++i) { if (i) s += ' '; s += names[i]; }
    return s;
}

// Merge the runtime's requirements into the app's list, skipping anything the app already
// asked for and anything the driver does not advertise -- a promoted extension is core on
// a 1.1+ instance and may be missing from the enumeration, in which case enabling it by
// name is an error rather than a no-op.
static std::vector<const char*> MergeExtensions(const char* const* wanted, size_t wantedCount,
                                                const char* const* appNames, uint32_t appCount,
                                                const std::vector<std::string>& available,
                                                std::vector<std::string>& storage) {
    std::vector<const char*> out;
    for (uint32_t i = 0; i < appCount; ++i) out.push_back(appNames[i]);
    for (size_t i = 0; i < wantedCount; ++i) {
        bool already = false;
        for (const char* n : out) if (strcmp(n, wanted[i]) == 0) { already = true; break; }
        if (already) continue;
        bool supported = false;
        for (const auto& a : available) if (a == wanted[i]) { supported = true; break; }
        if (!supported) { Logf("[SimXR][VK] %s is not advertised; not injecting it", wanted[i]); continue; }
        storage.emplace_back(wanted[i]);
        Logf("[SimXR][VK] injecting %s", wanted[i]);
    }
    for (const auto& s : storage) out.push_back(s.c_str());
    return out;
}

// --- session ------------------------------------------------------------------------------

static VkImageAspectFlags AspectOf(const FormatPair& fp) {
    if (!fp.depth) return VK_IMAGE_ASPECT_COLOR_BIT;
    if (fp.vk == VK_FORMAT_D24_UNORM_S8_UINT || fp.vk == VK_FORMAT_D32_SFLOAT_S8_UINT)
        return VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT;
    return VK_IMAGE_ASPECT_DEPTH_BIT;
}

// The compositor's own D3D12 device. It has to sit on the same physical adapter as the
// app's VkPhysicalDevice or every CreateSharedHandle import fails.
static bool CreateCompositorDevice(rt::Session& s) {
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(factory.GetAddressOf())))) return false;

    LUID want{};
    const bool haveWant = PhysicalDeviceLuid(s.vk.physicalDevice, want);
    ComPtr<IDXGIAdapter1> chosen, firstHardware;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> a;
        if (factory->EnumAdapters1(i, a.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 d{}; a->GetDesc1(&d);
        if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) continue;
        if (!firstHardware) firstHardware = a;
        if (haveWant && d.AdapterLuid.LowPart == want.LowPart && d.AdapterLuid.HighPart == want.HighPart) {
            chosen = a;
            break;
        }
    }
    if (!chosen) {
        chosen = firstHardware;
        if (haveWant) Log("[SimXR][VK] the app's VkPhysicalDevice LUID matched no DXGI adapter; interop may fail");
    }
    if (FAILED(D3D12CreateDevice(chosen.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(s.d3d12Device.GetAddressOf())))) {
        Log("[SimXR][VK] D3D12CreateDevice for the compositor failed");
        return false;
    }
    D3D12_COMMAND_QUEUE_DESC qd{};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(s.d3d12Device->CreateCommandQueue(&qd, IID_PPV_ARGS(s.d3d12Queue.GetAddressOf())))) {
        Log("[SimXR][VK] the compositor's D3D12 command queue could not be created");
        s.d3d12Device.Reset();
        return false;
    }
    if (chosen) {
        DXGI_ADAPTER_DESC1 d{}; chosen->GetDesc1(&d);
        char name[128]{};
        wcstombs(name, d.Description, sizeof(name) - 1);
        Logf("[SimXR][VK] compositor D3D12 device on %s (LUID %08lX:%08lX)", name,
             (unsigned long)d.AdapterLuid.HighPart, (unsigned long)d.AdapterLuid.LowPart);
    }
    return true;
}

// One shared D3D12 fence imported as a timeline VkSemaphore. Without it the session still
// works, on CPU waits instead (see FrameSyncBegin).
static void InitFrameSync(rt::Session& s) {
    s.vk.timeline = false;
    char noTimeline[8]{};
    if (GetEnvironmentVariableA("SIMXR_VK_NO_TIMELINE", noTimeline, (DWORD)sizeof(noTimeline)) > 0 && noTimeline[0] != '0') {
        Log("[SimXR][VK] SIMXR_VK_NO_TIMELINE set; using CPU frame sync");
        return;
    }
    if (!g_d.CreateSemaphore || !g_d.ImportSemaphoreWin32HandleKHR || !g_d.QueueSubmit) {
        Log("[SimXR][VK] no external-semaphore entry points; falling back to CPU frame sync");
        return;
    }
    if (FAILED(s.d3d12Device->CreateFence(0, D3D12_FENCE_FLAG_SHARED, IID_PPV_ARGS(s.vk.fence.GetAddressOf())))) {
        Log("[SimXR][VK] CreateFence(SHARED) failed; falling back to CPU frame sync");
        return;
    }
    if (FAILED(s.d3d12Device->CreateSharedHandle(s.vk.fence.Get(), nullptr, GENERIC_ALL, nullptr, &s.vk.fenceHandle))) {
        Log("[SimXR][VK] CreateSharedHandle(fence) failed; falling back to CPU frame sync");
        s.vk.fence.Reset();
        return;
    }

    VkSemaphoreTypeCreateInfo timelineInfo{ VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO };
    timelineInfo.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
    timelineInfo.initialValue = 0;
    VkSemaphoreCreateInfo semInfo{ VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO };
    semInfo.pNext = &timelineInfo;
    if (g_d.CreateSemaphore(s.vk.device, &semInfo, nullptr, &s.vk.semaphore) != VK_SUCCESS) {
        Log("[SimXR][VK] timeline VkSemaphore creation failed; falling back to CPU frame sync");
        s.vk.semaphore = VK_NULL_HANDLE;
        return;
    }
    VkImportSemaphoreWin32HandleInfoKHR import{ VK_STRUCTURE_TYPE_IMPORT_SEMAPHORE_WIN32_HANDLE_INFO_KHR };
    import.semaphore = s.vk.semaphore;
    import.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_D3D12_FENCE_BIT;
    import.handle = s.vk.fenceHandle;
    const VkResult r = g_d.ImportSemaphoreWin32HandleKHR(s.vk.device, &import);
    if (r != VK_SUCCESS) {
        Logf("[SimXR][VK] ImportSemaphoreWin32HandleKHR failed (%d); falling back to CPU frame sync", (int)r);
        g_d.DestroySemaphore(s.vk.device, s.vk.semaphore, nullptr);
        s.vk.semaphore = VK_NULL_HANDLE;
        return;
    }
    s.vk.timeline = true;
    Log("[SimXR][VK] frame sync: shared ID3D12Fence imported as a timeline VkSemaphore");
}

static bool InitSession(rt::Session& s, const XrGraphicsBindingVulkanKHR& b) {
    s.vk = rt::VulkanSession{};
    s.vk.physicalDevice = b.physicalDevice;
    s.vk.device = b.device;

    if (!LoadInstanceLevel(b.instance)) { Log("[SimXR][VK] instance-level entry points missing"); return false; }
    if (!LoadDeviceLevel(b.device)) { Log("[SimXR][VK] device-level entry points missing (external memory not enabled?)"); return false; }
    g_d.GetDeviceQueue(b.device, b.queueFamilyIndex, b.queueIndex, &s.vk.queue);
    if (!s.vk.queue) { Log("[SimXR][VK] vkGetDeviceQueue returned nothing"); return false; }

    VkCommandPoolCreateInfo pci{ VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO };
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = b.queueFamilyIndex;
    if (g_d.CreateCommandPool(b.device, &pci, nullptr, &s.vk.cmdPool) != VK_SUCCESS) {
        Log("[SimXR][VK] vkCreateCommandPool failed");
        return false;
    }
    VkCommandBufferAllocateInfo cbi{ VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cbi.commandPool = s.vk.cmdPool;
    cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbi.commandBufferCount = 1;
    if (g_d.AllocateCommandBuffers(b.device, &cbi, &s.vk.cmdBuffer) != VK_SUCCESS) {
        Log("[SimXR][VK] vkAllocateCommandBuffers failed");
        return false;
    }
    if (!CreateCompositorDevice(s)) return false;
    InitFrameSync(s);
    Logf("[SimXR][VK] session bound to VkDevice %p, queue family %u index %u",
         (void*)b.device, b.queueFamilyIndex, b.queueIndex);
    return true;
}

static void ShutdownSession(rt::Session& s) {
    if (s.vk.device) {
        if (g_d.DeviceWaitIdle) g_d.DeviceWaitIdle(s.vk.device);
        if (s.vk.semaphore && g_d.DestroySemaphore) g_d.DestroySemaphore(s.vk.device, s.vk.semaphore, nullptr);
        if (s.vk.cmdPool && g_d.DestroyCommandPool) g_d.DestroyCommandPool(s.vk.device, s.vk.cmdPool, nullptr);
    }
    if (s.vk.fenceHandle) CloseHandle(s.vk.fenceHandle);
    s.vk = rt::VulkanSession{};
    s.usesVulkan = false;
}

// --- swapchain images ---------------------------------------------------------------------

static void DestroySwapchainImages(rt::Session& s, rt::Swapchain& chain) {
    if (s.vk.device && g_d.DestroyImage && g_d.FreeMemory) {
        if (g_d.DeviceWaitIdle) g_d.DeviceWaitIdle(s.vk.device);
        for (VkImage img : chain.imagesVk) if (img) g_d.DestroyImage(s.vk.device, img, nullptr);
        for (VkDeviceMemory mem : chain.memoryVk) if (mem) g_d.FreeMemory(s.vk.device, mem, nullptr);
    }
    for (HANDLE h : chain.sharedHandles) if (h) CloseHandle(h);
    chain.imagesVk.clear();
    chain.memoryVk.clear();
    chain.sharedHandles.clear();
}

// Hand the images over in the layouts XR_KHR_vulkan_enable mandates -- colour in
// COLOR_ATTACHMENT_OPTIMAL, depth in DEPTH_STENCIL_ATTACHMENT_OPTIMAL -- and leave them
// there for the rest of the session. A conforming app renders straight into an acquired
// image with no barrier of its own, so anything else is a layout the app never corrects.
static bool TransitionToRequiredLayout(rt::Session& s, rt::Swapchain& chain, const FormatPair& fp) {
    VkCommandBufferBeginInfo bi{ VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (g_d.ResetCommandPool) g_d.ResetCommandPool(s.vk.device, s.vk.cmdPool, 0);
    if (g_d.BeginCommandBuffer(s.vk.cmdBuffer, &bi) != VK_SUCCESS) return false;

    std::vector<VkImageMemoryBarrier> barriers;
    barriers.reserve(chain.imagesVk.size());
    for (VkImage img : chain.imagesVk) {
        VkImageMemoryBarrier b{ VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER };
        b.srcAccessMask = 0;
        b.dstAccessMask = fp.depth ? VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT : VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
        b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        b.newLayout = fp.depth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL
                               : VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = img;
        b.subresourceRange.aspectMask = AspectOf(fp);
        b.subresourceRange.levelCount = chain.mipCount ? chain.mipCount : 1;
        b.subresourceRange.layerCount = chain.arraySize ? chain.arraySize : 1;
        barriers.push_back(b);
    }
    g_d.CmdPipelineBarrier(s.vk.cmdBuffer, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                           fp.depth ? VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT
                                    : VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                           0, 0, nullptr, 0, nullptr, (uint32_t)barriers.size(), barriers.data());
    if (g_d.EndCommandBuffer(s.vk.cmdBuffer) != VK_SUCCESS) return false;

    VkSubmitInfo si{ VK_STRUCTURE_TYPE_SUBMIT_INFO };
    si.commandBufferCount = 1;
    si.pCommandBuffers = &s.vk.cmdBuffer;
    if (g_d.QueueSubmit(s.vk.queue, 1, &si, VK_NULL_HANDLE) != VK_SUCCESS) return false;
    if (g_d.QueueWaitIdle) g_d.QueueWaitIdle(s.vk.queue);
    return true;
}

static XrResult CreateSwapchainImages(rt::Session& s, rt::Swapchain& chain, const XrSwapchainCreateInfo& ci) {
    const FormatPair* fp = FindFormat(ci.format);
    if (!fp) {
        Logf("[SimXR][VK] xrCreateSwapchain: VkFormat %lld is not one this runtime offers", (long long)ci.format);
        return XR_ERROR_SWAPCHAIN_FORMAT_UNSUPPORTED;
    }

    const uint32_t arraySize = chain.arraySize ? chain.arraySize : 1;
    const uint32_t mips = ci.mipCount ? ci.mipCount : 1;
    const uint32_t samples = ci.sampleCount ? ci.sampleCount : 1;
    chain.isVulkan = true;
    chain.format = fp->typed;
    chain.backend = rt::Swapchain::Backend::D3D12;
    chain.mipCount = mips;
    chain.imageCount = 3;

    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    // AMD needs this spelled out on a shared committed resource; 0 (let the runtime pick)
    // produces a resource whose Vulkan import fails.
    rd.Alignment = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    rd.Width = chain.width;
    rd.Height = chain.height;
    rd.DepthOrArraySize = (UINT16)arraySize;
    rd.MipLevels = (UINT16)mips;
    rd.Format = fp->resource;
    rd.SampleDesc.Count = samples;
    rd.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    if (fp->depth) {
        rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
    } else {
        rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
        // ALLOW_SIMULTANEOUS_ACCESS turns off DCC, which is what makes the bytes Vulkan
        // wrote through a COLOR_ATTACHMENT_OPTIMAL layout readable by a D3D12 SRV. It is
        // illegal on depth and on MSAA resources.
        if (samples == 1) rd.Flags |= D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS;
    }

    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;

    VkImageUsageFlags usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    if (ci.usageFlags & XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT) usage |= VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    if (ci.usageFlags & XR_SWAPCHAIN_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT) usage |= VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    if (ci.usageFlags & XR_SWAPCHAIN_USAGE_SAMPLED_BIT) usage |= VK_IMAGE_USAGE_SAMPLED_BIT;
    if (ci.usageFlags & XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT) usage |= VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    if (ci.usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT) usage |= VK_IMAGE_USAGE_STORAGE_BIT;
    if (!(usage & (VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT))) {
        usage |= fp->depth ? VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT : VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    }

    VkPhysicalDeviceMemoryProperties memProps{};
    if (g_d.GetPhysicalDeviceMemoryProperties) g_d.GetPhysicalDeviceMemoryProperties(s.vk.physicalDevice, &memProps);

    for (uint32_t i = 0; i < chain.imageCount; ++i) {
        ComPtr<ID3D12Resource> res;
        HRESULT hr = s.d3d12Device->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_SHARED, &rd,
                                                            D3D12_RESOURCE_STATE_COMMON, nullptr,
                                                            IID_PPV_ARGS(res.GetAddressOf()));
        if (FAILED(hr)) {
            Logf("[SimXR][VK] CreateCommittedResource(SHARED)[%u] failed 0x%08X", i, (unsigned)hr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
        HANDLE shared = nullptr;
        hr = s.d3d12Device->CreateSharedHandle(res.Get(), nullptr, GENERIC_ALL, nullptr, &shared);
        if (FAILED(hr)) {
            Logf("[SimXR][VK] CreateSharedHandle[%u] failed 0x%08X", i, (unsigned)hr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
        chain.images12.push_back(res);
        chain.imageStates12.push_back(D3D12_RESOURCE_STATE_COMMON);
        chain.sharedHandles.push_back(shared);

        VkExternalMemoryImageCreateInfo external{ VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO };
        external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_RESOURCE_BIT;
        VkImageCreateInfo ici{ VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO };
        ici.pNext = &external;
        ici.flags = (ci.usageFlags & XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT) ? VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT : 0;
        ici.imageType = VK_IMAGE_TYPE_2D;
        ici.format = (VkFormat)ci.format;
        ici.extent = { chain.width, chain.height, 1 };
        ici.mipLevels = mips;
        ici.arrayLayers = arraySize;
        ici.samples = (VkSampleCountFlagBits)samples;
        ici.tiling = VK_IMAGE_TILING_OPTIMAL;
        ici.usage = usage;
        ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        VkImage image = VK_NULL_HANDLE;
        VkResult vr = g_d.CreateImage(s.vk.device, &ici, nullptr, &image);
        if (vr != VK_SUCCESS) {
            Logf("[SimXR][VK] vkCreateImage[%u] failed (%d)", i, (int)vr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
        chain.imagesVk.push_back(image);

        VkImageMemoryRequirementsInfo2 reqInfo{ VK_STRUCTURE_TYPE_IMAGE_MEMORY_REQUIREMENTS_INFO_2 };
        reqInfo.image = image;
        VkMemoryRequirements2 req{ VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2 };
        g_d.GetImageMemoryRequirements2(s.vk.device, &reqInfo, &req);

        VkMemoryWin32HandlePropertiesKHR handleProps{ VK_STRUCTURE_TYPE_MEMORY_WIN32_HANDLE_PROPERTIES_KHR };
        vr = g_d.GetMemoryWin32HandlePropertiesKHR(s.vk.device, VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_RESOURCE_BIT,
                                                   shared, &handleProps);
        if (vr != VK_SUCCESS) {
            Logf("[SimXR][VK] vkGetMemoryWin32HandlePropertiesKHR[%u] failed (%d)", i, (int)vr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
        uint32_t typeIndex = UINT32_MAX;
        const uint32_t bits = handleProps.memoryTypeBits & req.memoryRequirements.memoryTypeBits;
        for (uint32_t t = 0; t < memProps.memoryTypeCount; ++t) {
            if (!(bits & (1u << t))) continue;
            if (memProps.memoryTypes[t].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) { typeIndex = t; break; }
        }
        if (typeIndex == UINT32_MAX) {
            for (uint32_t t = 0; t < memProps.memoryTypeCount; ++t) {
                if (handleProps.memoryTypeBits & (1u << t)) { typeIndex = t; break; }
            }
        }
        if (typeIndex == UINT32_MAX) {
            Logf("[SimXR][VK] no importable memory type for image %u", i);
            return XR_ERROR_RUNTIME_FAILURE;
        }

        VkImportMemoryWin32HandleInfoKHR importInfo{ VK_STRUCTURE_TYPE_IMPORT_MEMORY_WIN32_HANDLE_INFO_KHR };
        importInfo.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_RESOURCE_BIT;
        importInfo.handle = shared;
        VkMemoryDedicatedAllocateInfo dedicated{ VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO };
        dedicated.pNext = &importInfo;
        dedicated.image = image;
        VkMemoryAllocateInfo alloc{ VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO };
        alloc.pNext = &dedicated;
        alloc.allocationSize = req.memoryRequirements.size;
        alloc.memoryTypeIndex = typeIndex;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        vr = g_d.AllocateMemory(s.vk.device, &alloc, nullptr, &memory);
        if (vr != VK_SUCCESS) {
            Logf("[SimXR][VK] vkAllocateMemory(import)[%u] failed (%d)", i, (int)vr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
        chain.memoryVk.push_back(memory);

        VkBindImageMemoryInfo bind{ VK_STRUCTURE_TYPE_BIND_IMAGE_MEMORY_INFO };
        bind.image = image;
        bind.memory = memory;
        vr = g_d.BindImageMemory2(s.vk.device, 1, &bind);
        if (vr != VK_SUCCESS) {
            Logf("[SimXR][VK] vkBindImageMemory2[%u] failed (%d)", i, (int)vr);
            return XR_ERROR_RUNTIME_FAILURE;
        }
    }

    // COMMON is where the D3D12 half of a shared resource lives between accesses, and the
    // preview transitions out of and back into it around every read.
    chain.releaseState12 = D3D12_RESOURCE_STATE_COMMON;
    if (!TransitionToRequiredLayout(s, chain, *fp)) {
        Log("[SimXR][VK] could not put the swapchain images in their required layout");
        return XR_ERROR_RUNTIME_FAILURE;
    }
    Logf("[SimXR][VK] swapchain: %u shared images %ux%u array=%u VkFormat=%lld -> DXGI %d, layout %s",
         chain.imageCount, chain.width, chain.height, arraySize, (long long)ci.format, (int)fp->resource,
         fp->depth ? "DEPTH_STENCIL_ATTACHMENT_OPTIMAL" : "COLOR_ATTACHMENT_OPTIMAL");
    return XR_SUCCESS;
}

// --- frame sync ---------------------------------------------------------------------------
// Runs at the top of xrEndFrame: everything the app submitted for this frame is ahead of
// this signal on its queue, so the preview queue waiting on it is waiting on the frame.
static void FrameSyncBegin(rt::Session& s) {
    if (!s.usesVulkan || !s.vk.queue) return;
    if (s.vk.timeline) {
        const uint64_t value = ++s.vk.counter;
        VkTimelineSemaphoreSubmitInfo tsi{ VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO };
        tsi.signalSemaphoreValueCount = 1;
        tsi.pSignalSemaphoreValues = &value;
        VkSubmitInfo si{ VK_STRUCTURE_TYPE_SUBMIT_INFO };
        si.pNext = &tsi;
        si.signalSemaphoreCount = 1;
        si.pSignalSemaphores = &s.vk.semaphore;
        if (g_d.QueueSubmit(s.vk.queue, 1, &si, VK_NULL_HANDLE) == VK_SUCCESS) s.vk.appSignalled = value;
        LogVf("[SimXR][VK] sync: app signals %llu (fence at %llu)", (unsigned long long)value,
              (unsigned long long)(s.vk.fence ? s.vk.fence->GetCompletedValue() : 0));
        return;
    }
    // No timeline semaphore: block instead. Correct, just serialised - the app's frame has
    // to land before the preview reads it, and the previous composite has to be off the
    // GPU before the app reuses those images after this call returns.
    rt::WaitForPreviewFence(s);
    if (g_d.QueueWaitIdle) g_d.QueueWaitIdle(s.vk.queue);
}

// Runs after the composite has been submitted: the app's queue is made to wait for the
// preview's read before anything it submits next can overwrite those images.
static void FrameSyncEnd(rt::Session& s) {
    if (!s.usesVulkan || !s.vk.timeline || !s.vk.queue) return;
    if (s.vk.previewSignalled == s.vk.previewWaited) return;
    const uint64_t value = s.vk.previewSignalled;
    VkPipelineStageFlags stage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    VkTimelineSemaphoreSubmitInfo tsi{ VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO };
    tsi.waitSemaphoreValueCount = 1;
    tsi.pWaitSemaphoreValues = &value;
    VkSubmitInfo si{ VK_STRUCTURE_TYPE_SUBMIT_INFO };
    si.pNext = &tsi;
    si.waitSemaphoreCount = 1;
    si.pWaitSemaphores = &s.vk.semaphore;
    si.pWaitDstStageMask = &stage;
    if (g_d.QueueSubmit(s.vk.queue, 1, &si, VK_NULL_HANDLE) == VK_SUCCESS) s.vk.previewWaited = value;
    LogVf("[SimXR][VK] sync: app waits for %llu (fence at %llu)", (unsigned long long)value,
          (unsigned long long)(s.vk.fence ? s.vk.fence->GetCompletedValue() : 0));
}

} // namespace vkrt

// ----------------- OpenXR runtime exports -----------------

static XrResult XRAPI_PTR xrGetInstanceProcAddr_runtime(XrInstance, const char* name, PFN_xrVoidFunction* fn);

extern "C" __declspec(dllexport) XrResult XRAPI_CALL xrNegotiateLoaderRuntimeInterface(const XrNegotiateLoaderInfo* loaderInfo,
                                                                            XrNegotiateRuntimeRequest* runtimeRequest) {
    try {
        EnsureLogFile();
        Log("\n[SimXR] ========== OpenXR Simulator Runtime Starting ==========\n");
        if (!loaderInfo || !runtimeRequest) {
            Log("[SimXR] xrNegotiateLoaderRuntimeInterface: ERROR - null parameters");
            return XR_ERROR_INITIALIZATION_FAILED;
        }
        
        // The loader FreeLibrary's the runtime after xrDestroyInstance. That drops the last
        // reference this process may hold on d3d12/dxgi/opengl32, and unloading those out
        // from under a live Vulkan driver - the same driver, in a Vulkan session - hangs the
        // process on NVIDIA. Pinning also settles the older hazard of a preview window
        // outliving its WndProc.
        {
            HMODULE self = nullptr;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                               (LPCWSTR)&xrNegotiateLoaderRuntimeInterface, &self);
        }

        Logf("[SimXR] xrNegotiateLoaderRuntimeInterface: loaderInfo=%p, runtimeRequest=%p", loaderInfo, runtimeRequest);
        Logf("[SimXR]   Loader minInterfaceVersion=%u, maxInterfaceVersion=%u, minApiVersion=0x%X, maxApiVersion=0x%X",
             loaderInfo->minInterfaceVersion, loaderInfo->maxInterfaceVersion,
             loaderInfo->minApiVersion, loaderInfo->maxApiVersion);
        
        runtimeRequest->runtimeInterfaceVersion = XR_CURRENT_LOADER_RUNTIME_VERSION;
        runtimeRequest->getInstanceProcAddr = xrGetInstanceProcAddr_runtime;
        runtimeRequest->runtimeApiVersion = XR_CURRENT_API_VERSION;
        
        Logf("[SimXR] xrNegotiateLoaderRuntimeInterface: SUCCESS - runtimeApiVersion=0x%X (%u)", 
             runtimeRequest->runtimeApiVersion, runtimeRequest->runtimeApiVersion);
        return XR_SUCCESS;
    } catch (...) {
        Log("[SimXR] xrNegotiateLoaderRuntimeInterface: EXCEPTION caught!");
        return XR_ERROR_INITIALIZATION_FAILED;
    }
}

// xrGetD3D12GraphicsRequirementsKHR (XR_KHR_D3D12_enable)
static XrResult XRAPI_PTR xrGetD3D12GraphicsRequirementsKHR_runtime(
    XrInstance instance, XrSystemId systemId, XrGraphicsRequirementsD3D12KHR* req) {
    Logf("[SimXR] xrGetD3D12GraphicsRequirementsKHR called: instance=%p, systemId=%llu, req=%p",
         instance, (unsigned long long)systemId, req);
    if (!req) return XR_ERROR_VALIDATION_FAILURE;

    memset(req, 0, sizeof(*req));
    req->type = XR_TYPE_GRAPHICS_REQUIREMENTS_D3D12_KHR;
    req->next = nullptr;

    Microsoft::WRL::ComPtr<IDXGIFactory1> f;
    HRESULT hr = CreateDXGIFactory1(IID_PPV_ARGS(f.GetAddressOf()));
    if (FAILED(hr)) return XR_ERROR_RUNTIME_FAILURE;

    for (UINT i = 0;; ++i) {
        Microsoft::WRL::ComPtr<IDXGIAdapter1> a;
        if (f->EnumAdapters1(i, a.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 d{}; a->GetDesc1(&d);
        if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) continue;
        req->adapterLuid = d.AdapterLuid;
        break;
    }
    req->minFeatureLevel = D3D_FEATURE_LEVEL_11_0;
    Log("[SimXR] xrGetD3D12GraphicsRequirementsKHR: SUCCESS");
    return XR_SUCCESS;
}
// xrGetD3D11GraphicsRequirementsKHR (XR_KHR_D3D11_enable)
static XrResult XRAPI_PTR xrGetD3D11GraphicsRequirementsKHR_runtime(
    XrInstance instance, XrSystemId systemId, XrGraphicsRequirementsD3D11KHR* req) {
    Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR called: instance=%p, systemId=%llu, req=%p",
         instance, (unsigned long long)systemId, req);
    if (!req) {
        Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: ERROR - null req");
        return XR_ERROR_VALIDATION_FAILURE;
    }
    
    Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: req struct size = %zu, expected = %zu",
         sizeof(*req), sizeof(XrGraphicsRequirementsD3D11KHR));
    
    // Check if the struct type is already set (Unity might pre-fill it)
    if (req->type != 0) {
        Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: req->type already set to %d", req->type);
    }
    
    // Zero initialize the entire structure first
    memset(req, 0, sizeof(XrGraphicsRequirementsD3D11KHR));
    req->type = XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR;
    req->next = nullptr;
    
    Microsoft::WRL::ComPtr<IDXGIFactory1> f;
    HRESULT hr = CreateDXGIFactory1(IID_PPV_ARGS(f.GetAddressOf()));
    if (FAILED(hr)) { 
        Logf("[SimXR] CreateDXGIFactory1 failed: 0x%08X", hr); 
        return XR_ERROR_RUNTIME_FAILURE; 
    }
    
    Microsoft::WRL::ComPtr<IDXGIAdapter1> bestAdapter;
    DXGI_ADAPTER_DESC1 bestDesc{};
    bool foundHardware = false;
    
    // Find the best hardware adapter
    for (UINT i = 0; ; ++i) {
        Microsoft::WRL::ComPtr<IDXGIAdapter1> adapt;
        if (f->EnumAdapters1(i, adapt.GetAddressOf()) == DXGI_ERROR_NOT_FOUND)
            break;
            
        DXGI_ADAPTER_DESC1 d{}; 
        adapt->GetDesc1(&d);
        
        // Skip software adapters
        if (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) 
            continue;
            
        // Use the first hardware adapter we find
        if (!foundHardware) {
            bestAdapter = adapt;
            bestDesc = d;
            foundHardware = true;
            
            wchar_t* descStr = d.Description;
            char descAscii[128];
            wcstombs(descAscii, descStr, sizeof(descAscii));
            descAscii[sizeof(descAscii)-1] = '\0';
            Logf("[SimXR] Found hardware adapter: %s", descAscii);
            Logf("[SimXR]   LUID: High=%ld, Low=%lu", 
                 (long)d.AdapterLuid.HighPart, 
                 (unsigned long)d.AdapterLuid.LowPart);
            Logf("[SimXR]   Dedicated Video Memory: %llu MB", 
                 (unsigned long long)(d.DedicatedVideoMemory / (1024*1024)));
        }
    }
    
    if (foundHardware) {
        req->adapterLuid = bestDesc.AdapterLuid;
        req->minFeatureLevel = D3D_FEATURE_LEVEL_11_0;
        
        // Save this LUID for later validation
        rt::g_adapterLuid = bestDesc.AdapterLuid;
        rt::g_adapterLuidSet = true;
        
        Logf("[SimXR] xrGetD3D11GraphicsRequirementsKHR: Returning:");
        Logf("[SimXR]   type = %d (expected %d)", req->type, XR_TYPE_GRAPHICS_REQUIREMENTS_D3D11_KHR);
        Logf("[SimXR]   next = %p", req->next);
        Logf("[SimXR]   adapterLuid.HighPart = %ld (0x%08X)", 
             (long)req->adapterLuid.HighPart, (unsigned)req->adapterLuid.HighPart);
        Logf("[SimXR]   adapterLuid.LowPart = %lu (0x%08X)", 
             (unsigned long)req->adapterLuid.LowPart, (unsigned)req->adapterLuid.LowPart);
        Logf("[SimXR]   minFeatureLevel = 0x%X (D3D_FEATURE_LEVEL_11_0 = 0x%X)", 
             req->minFeatureLevel, D3D_FEATURE_LEVEL_11_0);
        
        Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: SUCCESS - Returning XR_SUCCESS");
        return XR_SUCCESS;
    }
    
    // No hardware adapter found, this is an error for VR
    Log("[SimXR] xrGetD3D11GraphicsRequirementsKHR: ERROR - No hardware graphics adapter found");
    return XR_ERROR_SYSTEM_INVALID;
}

// xrGetOpenGLGraphicsRequirementsKHR (XR_KHR_opengl_enable)
static XrResult XRAPI_PTR xrGetOpenGLGraphicsRequirementsKHR_runtime(
    XrInstance instance, XrSystemId systemId, XrGraphicsRequirementsOpenGLKHR* req) {
    Logf("[SimXR] xrGetOpenGLGraphicsRequirementsKHR called: instance=%p, systemId=%llu, req=%p",
         instance, (unsigned long long)systemId, req);
    if (!req) {
        Log("[SimXR] xrGetOpenGLGraphicsRequirementsKHR: ERROR - null req");
        return XR_ERROR_VALIDATION_FAILURE;
    }

    // Zero initialize the structure
    memset(req, 0, sizeof(XrGraphicsRequirementsOpenGLKHR));
    req->type = XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_KHR;
    req->next = nullptr;

    // Minimum OpenGL version: 4.0.0 (good compatibility)
    // Maximum: 4.6.0 (latest)
    req->minApiVersionSupported = XR_MAKE_VERSION(4, 0, 0);
    req->maxApiVersionSupported = XR_MAKE_VERSION(4, 6, 0);

    Logf("[SimXR] xrGetOpenGLGraphicsRequirementsKHR: min=%d.%d.%d, max=%d.%d.%d",
         XR_VERSION_MAJOR(req->minApiVersionSupported),
         XR_VERSION_MINOR(req->minApiVersionSupported),
         XR_VERSION_PATCH(req->minApiVersionSupported),
         XR_VERSION_MAJOR(req->maxApiVersionSupported),
         XR_VERSION_MINOR(req->maxApiVersionSupported),
         XR_VERSION_PATCH(req->maxApiVersionSupported));

    Log("[SimXR] xrGetOpenGLGraphicsRequirementsKHR: SUCCESS");
    return XR_SUCCESS;
}

// --- XR_KHR_vulkan_enable / XR_KHR_vulkan_enable2 -------------------------------------------

// XrGraphicsRequirementsVulkan2KHR is a typedef of the v1 struct, so one implementation
// serves both extensions.
static XrResult XRAPI_PTR xrGetVulkanGraphicsRequirementsKHR_runtime(
    XrInstance, XrSystemId, XrGraphicsRequirementsVulkanKHR* req) {
    if (!req) return XR_ERROR_VALIDATION_FAILURE;
    memset(req, 0, sizeof(*req));
    req->type = XR_TYPE_GRAPHICS_REQUIREMENTS_VULKAN_KHR;
    req->minApiVersionSupported = XR_MAKE_VERSION(1, 0, 0);
    req->maxApiVersionSupported = XR_MAKE_VERSION(1, 4, 0);
    Log("[SimXR] xrGetVulkanGraphicsRequirementsKHR: Vulkan 1.0 - 1.4");
    return XR_SUCCESS;
}

static XrResult CopyExtensionString(const std::string& s, uint32_t capacity, uint32_t* countOutput, char* buffer) {
    const uint32_t needed = (uint32_t)s.size() + 1;
    if (countOutput) *countOutput = needed;
    if (capacity == 0) return XR_SUCCESS;
    if (capacity < needed || !buffer) return XR_ERROR_SIZE_INSUFFICIENT;
    memcpy(buffer, s.c_str(), needed);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetVulkanInstanceExtensionsKHR_runtime(
    XrInstance, XrSystemId, uint32_t capacity, uint32_t* countOutput, char* buffer) {
    const std::string s = vkrt::JoinExtensions(vkrt::kInstanceExtensions, std::size(vkrt::kInstanceExtensions));
    if (capacity) Logf("[SimXR] xrGetVulkanInstanceExtensionsKHR: %s", s.c_str());
    return CopyExtensionString(s, capacity, countOutput, buffer);
}

static XrResult XRAPI_PTR xrGetVulkanDeviceExtensionsKHR_runtime(
    XrInstance, XrSystemId, uint32_t capacity, uint32_t* countOutput, char* buffer) {
    const std::string s = vkrt::JoinExtensions(vkrt::kDeviceExtensions, std::size(vkrt::kDeviceExtensions));
    if (capacity) Logf("[SimXR] xrGetVulkanDeviceExtensionsKHR: %s", s.c_str());
    return CopyExtensionString(s, capacity, countOutput, buffer);
}

static XrResult XRAPI_PTR xrGetVulkanGraphicsDeviceKHR_runtime(
    XrInstance, XrSystemId, VkInstance vkInstance, VkPhysicalDevice* out) {
    return vkrt::PickPhysicalDevice(vkInstance, out);
}

static XrResult XRAPI_PTR xrGetVulkanGraphicsDevice2KHR_runtime(
    XrInstance, const XrVulkanGraphicsDeviceGetInfoKHR* getInfo, VkPhysicalDevice* out) {
    if (!getInfo) return XR_ERROR_VALIDATION_FAILURE;
    return vkrt::PickPhysicalDevice(getInfo->vulkanInstance, out);
}

// Thin passthrough: the app's own vkCreateInstance runs, with the interop extensions the
// runtime needs appended to whatever it asked for.
static XrResult XRAPI_PTR xrCreateVulkanInstanceKHR_runtime(
    XrInstance, const XrVulkanInstanceCreateInfoKHR* createInfo, VkInstance* vulkanInstance, VkResult* vulkanResult) {
    if (!createInfo || !createInfo->pfnGetInstanceProcAddr || !createInfo->vulkanCreateInfo ||
        !vulkanInstance || !vulkanResult) return XR_ERROR_VALIDATION_FAILURE;

    vkrt::g_appGipa = createInfo->pfnGetInstanceProcAddr;
    auto enumerate = (PFN_vkEnumerateInstanceExtensionProperties)vkrt::g_appGipa(
        VK_NULL_HANDLE, "vkEnumerateInstanceExtensionProperties");
    auto create = (PFN_vkCreateInstance)vkrt::g_appGipa(VK_NULL_HANDLE, "vkCreateInstance");
    if (!create) { Log("[SimXR][VK] xrCreateVulkanInstanceKHR: no vkCreateInstance"); return XR_ERROR_RUNTIME_FAILURE; }

    std::vector<std::string> available;
    if (enumerate) {
        uint32_t n = 0;
        enumerate(nullptr, &n, nullptr);
        std::vector<VkExtensionProperties> props(n);
        if (n) enumerate(nullptr, &n, props.data());
        for (const auto& p : props) available.emplace_back(p.extensionName);
    }

    std::vector<std::string> storage;
    std::vector<const char*> names = vkrt::MergeExtensions(
        vkrt::kInstanceExtensions, std::size(vkrt::kInstanceExtensions),
        createInfo->vulkanCreateInfo->ppEnabledExtensionNames,
        createInfo->vulkanCreateInfo->enabledExtensionCount, available, storage);

    VkInstanceCreateInfo ici = *createInfo->vulkanCreateInfo;
    ici.enabledExtensionCount = (uint32_t)names.size();
    ici.ppEnabledExtensionNames = names.data();

    *vulkanResult = create(&ici, createInfo->vulkanAllocator, vulkanInstance);
    if (*vulkanResult == VK_SUCCESS) vkrt::g_lastInstance = *vulkanInstance;
    Logf("[SimXR] xrCreateVulkanInstanceKHR: %u extensions, VkResult=%d",
         ici.enabledExtensionCount, (int)*vulkanResult);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateVulkanDeviceKHR_runtime(
    XrInstance, const XrVulkanDeviceCreateInfoKHR* createInfo, VkDevice* vulkanDevice, VkResult* vulkanResult) {
    if (!createInfo || !createInfo->pfnGetInstanceProcAddr || !createInfo->vulkanCreateInfo ||
        !createInfo->vulkanPhysicalDevice || !vulkanDevice || !vulkanResult) return XR_ERROR_VALIDATION_FAILURE;

    vkrt::g_appGipa = createInfo->pfnGetInstanceProcAddr;
    // Both are instance-level, and the only VkInstance in reach is the one the app made
    // through xrCreateVulkanInstanceKHR or named in xrGetVulkanGraphicsDevice2KHR.
    const VkInstance inst = vkrt::g_lastInstance;
    auto enumerate = (PFN_vkEnumerateDeviceExtensionProperties)vkrt::g_appGipa(
        inst, "vkEnumerateDeviceExtensionProperties");
    auto create = (PFN_vkCreateDevice)vkrt::g_appGipa(inst, "vkCreateDevice");
    if (!create) { Log("[SimXR][VK] xrCreateVulkanDeviceKHR: no vkCreateDevice"); return XR_ERROR_RUNTIME_FAILURE; }

    std::vector<std::string> available;
    if (enumerate) {
        uint32_t n = 0;
        enumerate(createInfo->vulkanPhysicalDevice, nullptr, &n, nullptr);
        std::vector<VkExtensionProperties> props(n);
        if (n) enumerate(createInfo->vulkanPhysicalDevice, nullptr, &n, props.data());
        for (const auto& p : props) available.emplace_back(p.extensionName);
    }

    std::vector<std::string> storage;
    std::vector<const char*> names = vkrt::MergeExtensions(
        vkrt::kDeviceExtensions, std::size(vkrt::kDeviceExtensions),
        createInfo->vulkanCreateInfo->ppEnabledExtensionNames,
        createInfo->vulkanCreateInfo->enabledExtensionCount, available, storage);

    VkDeviceCreateInfo dci = *createInfo->vulkanCreateInfo;
    dci.enabledExtensionCount = (uint32_t)names.size();
    dci.ppEnabledExtensionNames = names.data();

    *vulkanResult = create(createInfo->vulkanPhysicalDevice, &dci, createInfo->vulkanAllocator, vulkanDevice);
    Logf("[SimXR] xrCreateVulkanDeviceKHR: %u extensions, VkResult=%d",
         dci.enabledExtensionCount, (int)*vulkanResult);
    return XR_SUCCESS;
}

// --- Minimal implementations ---

static const char* kSupportedExtensions[] = {
    XR_KHR_D3D11_ENABLE_EXTENSION_NAME,
    XR_KHR_D3D12_ENABLE_EXTENSION_NAME,
    XR_KHR_OPENGL_ENABLE_EXTENSION_NAME,  // OpenGL support
    XR_KHR_VULKAN_ENABLE_EXTENSION_NAME,
    XR_KHR_VULKAN_ENABLE2_EXTENSION_NAME,
    XR_KHR_COMPOSITION_LAYER_DEPTH_EXTENSION_NAME,
    XR_KHR_COMPOSITION_LAYER_CYLINDER_EXTENSION_NAME,  // UEVR uses this for UI layers
    "XR_KHR_win32_convert_performance_counter_time"    // Unity often requires this
};

static XrResult XRAPI_PTR xrEnumerateApiLayerProperties_runtime(uint32_t propertyCapacityInput,
                                                                uint32_t* propertyCountOutput,
                                                                XrApiLayerProperties* properties) {
    Log("[SimXR] xrEnumerateApiLayerProperties called");
    // Runtime doesn't provide API layers, only extensions
    if (propertyCountOutput) *propertyCountOutput = 0;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrEnumerateInstanceExtensionProperties_runtime(const char* layerName, uint32_t propertyCapacityInput,
                                                                         uint32_t* propertyCountOutput,
                                                                         XrExtensionProperties* properties) {
    if (layerName && layerName[0] != '\0') return XR_ERROR_LAYER_INVALID;
    const uint32_t count = (uint32_t)(sizeof(kSupportedExtensions)/sizeof(kSupportedExtensions[0]));
    if (propertyCountOutput) *propertyCountOutput = count;
    if (properties && propertyCapacityInput) {
        for (uint32_t i = 0; i < propertyCapacityInput && i < count; ++i) {
            properties[i].type = XR_TYPE_EXTENSION_PROPERTIES;
            properties[i].next = nullptr;
            std::strncpy(properties[i].extensionName, kSupportedExtensions[i], XR_MAX_EXTENSION_NAME_SIZE - 1);
            properties[i].extensionName[XR_MAX_EXTENSION_NAME_SIZE - 1] = '\0';
            properties[i].extensionVersion = 1;
            Logf("[SimXR] ext[%u]=%s", i, properties[i].extensionName);
        }
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateInstance_runtime(const XrInstanceCreateInfo* createInfo, XrInstance* instance) {
    if (!createInfo || !instance) return XR_ERROR_VALIDATION_FAILURE;
    // applicationName may not be null-terminated
    char appName[XR_MAX_APPLICATION_NAME_SIZE + 1] = {0};
    memcpy(appName, createInfo->applicationInfo.applicationName, XR_MAX_APPLICATION_NAME_SIZE);
    Logf("[SimXR] xrCreateInstance: app=%s version=%u", 
         appName,
         createInfo->applicationInfo.applicationVersion);
    rt::g_instance = {};
    rt::g_instance.enabledExtensions.clear();

    // Restore the saved UI settings here rather than at window creation: apps ask
    // for view configurations, whose panel resolution and FOV both come from the
    // saved headset profile, long before a preview window exists.
    static bool s_settingsLoaded = false;
    if (!s_settingsLoaded) {
        s_settingsLoaded = true;
        ui::LoadSettings(mcp::GetSimulatorDataPath());
        char zoomDesc[32] = "fit";
        if (!ui::g_uiState.fitToWindow) {
            snprintf(zoomDesc, sizeof(zoomDesc), "%d%%", (int)(ui::g_uiState.zoomLevel * 100));
        }
        Logf("[SimXR] Settings restored: profile=%ls ipd=%dmm asymmetric=%d zoom=%s",
             ui::GetHeadsetProfileShortName(), ui::GetIpdMillimeters(),
             (int)ui::g_uiState.useAsymmetricFov, zoomDesc);
    }

    // Validate that all requested extensions are supported
    const uint32_t supportedCount = (uint32_t)(sizeof(kSupportedExtensions)/sizeof(kSupportedExtensions[0]));
    for (uint32_t i = 0; i < createInfo->enabledExtensionCount; ++i) {
        bool supported = false;
        for (uint32_t j = 0; j < supportedCount; ++j) {
            if (strcmp(createInfo->enabledExtensionNames[i], kSupportedExtensions[j]) == 0) {
                supported = true;
                break;
            }
        }
        if (!supported) {
            Logf("[SimXR] xrCreateInstance: ERROR - Unsupported extension %s", createInfo->enabledExtensionNames[i]);
            return XR_ERROR_EXTENSION_NOT_PRESENT;
        }
        rt::g_instance.enabledExtensions.emplace_back(createInfo->enabledExtensionNames[i]);
        Logf("[SimXR]   enabledExt[%u]=%s", i, createInfo->enabledExtensionNames[i]);
    }
    rt::g_instance.handle = (XrInstance)1;  // Set a valid handle
    *instance = rt::g_instance.handle;
    Log("[SimXR] xrCreateInstance: SUCCESS");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyInstance_runtime(XrInstance instance) {
    Logf("[SimXR] xrDestroyInstance called: instance=%p", instance);

    // Clear the global instance
    if (instance == rt::g_instance.handle) {
        Log("[SimXR] xrDestroyInstance: Clearing global instance");
        rt::g_instance = {};

        // MUST destroy the window before DLL unloads!
        // The OpenXR loader may unload our DLL after this call.
        // If the window stays alive, its WndProc points to unloaded code = crash.
        {
            std::lock_guard<std::mutex> lock(rt::g_windowMutex);
            if (rt::g_persistentWindow) {
                Log("[SimXR] xrDestroyInstance: Destroying preview window");
                DestroyWindow(rt::g_persistentWindow);
                rt::g_persistentWindow = nullptr;
            }
            rt::g_persistentSwapchain.Reset();
        }
        // Also clear session window reference
        if (rt::g_session.hwnd) {
            rt::g_session.hwnd = nullptr;
        }

        // Unregister window class so it doesn't have dangling WndProc
        if (rt::g_windowClassRegistered) {
            UnregisterClassW(L"OpenXR Simulator", GetModuleHandleW(nullptr));
            rt::g_windowClassRegistered = false;
            Log("[SimXR] xrDestroyInstance: Window class unregistered");
        }
        Log("[SimXR] xrDestroyInstance: Window destroyed for safe DLL unload");
    }

    Log("[SimXR] xrDestroyInstance: SUCCESS - Returning XR_SUCCESS");
    Log("[SimXR] ========== Instance Destroyed - Waiting for new instance ==========");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetInstanceProperties_runtime(XrInstance, XrInstanceProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_INSTANCE_PROPERTIES;
    props->next = nullptr;
    props->runtimeVersion = XR_MAKE_VERSION(1, 0, 27);
    strncpy(props->runtimeName, "OpenXR Simulator Runtime", XR_MAX_RUNTIME_NAME_SIZE - 1);
    props->runtimeName[XR_MAX_RUNTIME_NAME_SIZE - 1] = '\0';
    Log("[SimXR] xrGetInstanceProperties: returning OpenXR Simulator Runtime");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetSystem_runtime(XrInstance, const XrSystemGetInfo* info, XrSystemId* systemId) {
    if (!info || !systemId) return XR_ERROR_VALIDATION_FAILURE;
    Logf("[SimXR] xrGetSystem: formFactor=%d", info->formFactor);
    if (info->formFactor != XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY) {
        Log("[SimXR] xrGetSystem: ERROR - form factor not HMD");
        return XR_ERROR_FORM_FACTOR_UNSUPPORTED;
    }
    *systemId = (XrSystemId)1; 
    Log("[SimXR] xrGetSystem: SUCCESS -> systemId=1"); 
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetSystemProperties_runtime(XrInstance, XrSystemId, XrSystemProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_SYSTEM_PROPERTIES;
    props->next = nullptr;
    strncpy(props->systemName, "OpenXR Simulator", XR_MAX_SYSTEM_NAME_SIZE - 1);
    props->systemName[XR_MAX_SYSTEM_NAME_SIZE - 1] = '\0';
    props->systemId = 1;
    props->vendorId = 0;  // 0 = unknown vendor (more standard than 0xFFFF)
    props->graphicsProperties.maxSwapchainImageWidth = 4096;
    props->graphicsProperties.maxSwapchainImageHeight = 4096;
    props->graphicsProperties.maxLayerCount = 16;
    props->trackingProperties.positionTracking = XR_TRUE;
    props->trackingProperties.orientationTracking = XR_TRUE;
    Log("[SimXR] xrGetSystemProperties: returning OpenXR Simulator");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateViewConfigurations_runtime(XrInstance, XrSystemId, uint32_t capacity, uint32_t* count, XrViewConfigurationType* types) {
    Logf("[SimXR] xrEnumerateViewConfigurations called: capacity=%u", capacity);
    if (count) *count = 1;
    if (capacity >= 1 && types) {
        types[0] = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
        Log("[SimXR] xrEnumerateViewConfigurations: Returning PRIMARY_STEREO");
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateViewConfigurationViews_runtime(XrInstance, XrSystemId, XrViewConfigurationType viewType, uint32_t capacity, uint32_t* count, XrViewConfigurationView* views) {
    Logf("[SimXR] xrEnumerateViewConfigurationViews called: viewType=%d, capacity=%u", (int)viewType, capacity);
    if (count) *count = 2;
    if (capacity >= 2 && views) {
        for (uint32_t i = 0; i < 2; ++i) {
            views[i].type = XR_TYPE_VIEW_CONFIGURATION_VIEW;
            views[i].next = nullptr;
            uint32_t renderW = 0, renderH = 0;
            ui::GetRenderResolution(renderW, renderH);
            views[i].recommendedImageRectWidth = renderW;
            views[i].recommendedImageRectHeight = renderH;
            views[i].recommendedSwapchainSampleCount = 1;
            views[i].maxImageRectWidth = 4096; views[i].maxImageRectHeight = 4096; views[i].maxSwapchainSampleCount = 1;
        }
        Logf("[SimXR] xrEnumerateViewConfigurationViews: Returned 2 views (%ux%u recommended; headset geometry %s)",
             views[0].recommendedImageRectWidth, views[0].recommendedImageRectHeight,
             ui::HeadsetProfileName(ui::g_uiState.headsetProfile));
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateEnvironmentBlendModes_runtime(
    XrInstance, XrSystemId, XrViewConfigurationType, uint32_t capacity, uint32_t* count, XrEnvironmentBlendMode* modes) {
    if (count) *count = 1;
    if (capacity >= 1 && modes) modes[0] = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateSession_runtime(XrInstance instance, const XrSessionCreateInfo* info, XrSession* session) {
    static int sessionCount = 0;
    sessionCount++;
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrCreateSession called (call #%d, instance=%llu)", sessionCount, (unsigned long long)instance);
    Log("[SimXR] ============================================");
    if (!info || !session) return XR_ERROR_VALIDATION_FAILURE;
    
    // Check if we already have an active session
    if (rt::g_session.handle != XR_NULL_HANDLE && rt::g_session.state != XR_SESSION_STATE_IDLE) {
        Logf("[SimXR] xrCreateSession: ERROR - Session already exists (handle=%llu, state=%d)",
            (unsigned long long)rt::g_session.handle, rt::g_session.state);
        // For now, reset the existing session to allow the new one
        // Reset session manually
        rt::g_session.handle = XR_NULL_HANDLE;
        rt::g_session.state = XR_SESSION_STATE_IDLE;
        rt::g_session.d3d11Device.Reset();
        rt::g_session.d3d11Context.Reset();
        rt::g_session.previewSwapchain.Reset();
        rt::g_session.usesD3D12 = false;
        rt::g_session.d3d12Device.Reset();
        rt::g_session.d3d12Queue.Reset();
        rt::ResetD3D12PreviewResources(rt::g_session);
        rt::g_session.previewWidth = 1920;
        rt::g_session.previewHeight = 540;
        rt::g_session.isFocused = false;
    }
    if (rt::g_session.usesVulkan) vkrt::ShutdownSession(rt::g_session);
    // Accept D3D11 and D3D12
    const XrBaseInStructure* entry = reinterpret_cast<const XrBaseInStructure*>(info->next);
    while (entry) {
        if (entry->type == XR_TYPE_GRAPHICS_BINDING_D3D11_KHR) {
            const auto* b = reinterpret_cast<const XrGraphicsBindingD3D11KHR*>(entry);
            
            // Log the device details
            ComPtr<IDXGIDevice> dxgiDevice;
            if (SUCCEEDED(b->device->QueryInterface(IID_PPV_ARGS(&dxgiDevice)))) {
                ComPtr<IDXGIAdapter> adapter;
                if (SUCCEEDED(dxgiDevice->GetAdapter(&adapter))) {
                    DXGI_ADAPTER_DESC desc;
                    adapter->GetDesc(&desc);
                    Logf("[SimXR] xrCreateSession: App D3D11 device LUID=%llu/%llu", 
                         (unsigned long long)desc.AdapterLuid.HighPart,
                         (unsigned long long)desc.AdapterLuid.LowPart);
                }
            }
            
            // Use sessionCount to generate unique handles
            rt::g_session.handle = (XrSession)(uintptr_t)(0x1000 + sessionCount);
            rt::g_session.d3d11Device = b->device;
            rt::g_session.usesD3D12 = false;
            rt::g_session.d3d12Device.Reset();
            rt::g_session.d3d12Queue.Reset();
            rt::ResetD3D12PreviewResources(rt::g_session);
            rt::g_session.state = XR_SESSION_STATE_IDLE;
            b->device->GetImmediateContext(rt::g_session.d3d11Context.GetAddressOf());
            // Window will be created lazily on first frame
            *session = rt::g_session.handle;
            Logf("[SimXR] xrCreateSession: SUCCESS (D3D11, handle=%llu)", (unsigned long long)rt::g_session.handle);
            // Push READY event into queue
            rt::PushState(rt::g_session.handle, XR_SESSION_STATE_READY);
            return XR_SUCCESS;
        } else if (entry->type == XR_TYPE_GRAPHICS_BINDING_D3D12_KHR) {
            const auto* b12 = reinterpret_cast<const XrGraphicsBindingD3D12KHR*>(entry);
            rt::g_session.usesD3D12 = true;
            rt::g_session.d3d12Device = b12->device;
            rt::g_session.d3d12Queue = b12->queue;
            rt::g_session.d3d11Device.Reset();
            rt::g_session.d3d11Context.Reset();
            rt::g_session.previewSwapchain.Reset();
            rt::g_session.handle = (XrSession)(uintptr_t)(0x1000 + sessionCount);
            *session = rt::g_session.handle;
            Logf("[SimXR] xrCreateSession: SUCCESS (D3D12, handle=%llu)", (unsigned long long)rt::g_session.handle);
            rt::PushState(rt::g_session.handle, XR_SESSION_STATE_READY);
            return XR_SUCCESS;
        } else if (entry->type == XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR) {
            // XR_TYPE_GRAPHICS_BINDING_VULKAN2_KHR is an alias of this, so one branch serves
            // XR_KHR_vulkan_enable and XR_KHR_vulkan_enable2 alike.
            const auto* bVk = reinterpret_cast<const XrGraphicsBindingVulkanKHR*>(entry);
            rt::g_session.d3d11Device.Reset();
            rt::g_session.d3d11Context.Reset();
            rt::g_session.previewSwapchain.Reset();
            rt::g_session.usesOpenGL = false;
            rt::g_session.d3d12Device.Reset();
            rt::g_session.d3d12Queue.Reset();
            rt::ResetD3D12PreviewResources(rt::g_session);
            if (!vkrt::InitSession(rt::g_session, *bVk)) {
                vkrt::ShutdownSession(rt::g_session);
                Log("[SimXR] xrCreateSession: ERROR - the Vulkan binding could not be set up");
                return XR_ERROR_GRAPHICS_DEVICE_INVALID;
            }
            // The compositor is the D3D12 one either way; usesVulkan only changes what the
            // app is handed and how the two queues are ordered.
            rt::g_session.usesVulkan = true;
            rt::g_session.usesD3D12 = true;
            rt::g_session.handle = (XrSession)(uintptr_t)(0x1000 + sessionCount);
            *session = rt::g_session.handle;
            Logf("[SimXR] xrCreateSession: SUCCESS (Vulkan, handle=%llu)", (unsigned long long)rt::g_session.handle);
            rt::PushState(rt::g_session.handle, XR_SESSION_STATE_READY);
            return XR_SUCCESS;
        } else if (entry->type == XR_TYPE_GRAPHICS_BINDING_OPENGL_WIN32_KHR) {
            const auto* bGL = reinterpret_cast<const XrGraphicsBindingOpenGLWin32KHR*>(entry);
            rt::g_session.usesOpenGL = true;
            rt::g_session.usesD3D12 = false;
            rt::g_session.glDC = bGL->hDC;
            rt::g_session.glRC = bGL->hGLRC;
            rt::g_session.d3d11Device.Reset();
            rt::g_session.d3d11Context.Reset();
            rt::g_session.d3d12Device.Reset();
            rt::g_session.d3d12Queue.Reset();
            rt::g_session.previewSwapchain.Reset();
            rt::g_session.handle = (XrSession)(uintptr_t)(0x1000 + sessionCount);
            *session = rt::g_session.handle;
            Logf("[SimXR] xrCreateSession: SUCCESS (OpenGL, handle=%llu, hDC=%p, hGLRC=%p)",
                 (unsigned long long)rt::g_session.handle, bGL->hDC, bGL->hGLRC);
            rt::PushState(rt::g_session.handle, XR_SESSION_STATE_READY);
            return XR_SUCCESS;
        }
        entry = entry->next;
    }
    Log("[SimXR] xrCreateSession: ERROR - No supported graphics binding found (D3D11/D3D12/OpenGL)");
    return XR_ERROR_GRAPHICS_DEVICE_INVALID;
}


static XrResult XRAPI_PTR xrDestroySession_runtime(XrSession s) {
    Logf("[SimXR] xrDestroySession called (handle=%llu)", (unsigned long long)s);
    if (s != rt::g_session.handle) {
        Logf("[SimXR] xrDestroySession: ERROR - Invalid handle (expected %llu)", 
             (unsigned long long)rt::g_session.handle);
        return XR_ERROR_HANDLE_INVALID;
    }
    
    // Transfer window and swapchain to global persistent storage
    // Unity likes to create/destroy sessions rapidly for compatibility checks
    {
        std::lock_guard<std::mutex> lock(rt::g_windowMutex);
        if (rt::g_session.hwnd && !rt::g_persistentWindow) {
            rt::g_persistentWindow = rt::g_session.hwnd;
            rt::g_persistentSwapchain = rt::g_session.previewSwapchain;
            rt::g_persistentWidth = rt::g_session.previewWidth;
            rt::g_persistentHeight = rt::g_session.previewHeight;
            Log("[SimXR] xrDestroySession: Preserving window and swapchain for next session");
        } else if (rt::g_session.hwnd == rt::g_persistentWindow) {
            // Already using persistent window, just update the swapchain
            rt::g_persistentSwapchain = rt::g_session.previewSwapchain;
            rt::g_persistentWidth = rt::g_session.previewWidth;
            rt::g_persistentHeight = rt::g_session.previewHeight;
            Log("[SimXR] xrDestroySession: Updating persistent swapchain");
        }
    }
    
    // Reset session but don't destroy the window
    rt::g_session.handle = XR_NULL_HANDLE;
    rt::g_session.state = XR_SESSION_STATE_IDLE;
    rt::g_session.d3d11Device.Reset();
    rt::g_session.d3d11Context.Reset();
    rt::g_session.usesD3D12 = false;
    rt::ResetD3D12PreviewResources(rt::g_session);
    if (rt::g_session.usesVulkan) {
        // The images live on the app's VkDevice, which goes away right after this call, so
        // they cannot wait for an xrDestroySwapchain the app may never make.
        for (auto& sc : rt::g_swapchains) {
            if (sc.second.isVulkan) vkrt::DestroySwapchainImages(rt::g_session, sc.second);
        }
        vkrt::ShutdownSession(rt::g_session);
    }
    rt::g_session.d3d12Device.Reset();
    rt::g_session.d3d12Queue.Reset();
    // Reset OpenGL state
    rt::g_session.usesOpenGL = false;
    rt::g_session.glDC = nullptr;
    rt::g_session.glRC = nullptr;
    rt::g_session.hwnd = nullptr;  // Clear from session but window still exists
    rt::g_session.previewWidth = 1920;
    rt::g_session.previewHeight = 540;
    rt::g_session.isFocused = false;
    rt::g_activeProfile = XR_NULL_PATH;  // re-bound by the next xrAttachSessionActionSets
    Log("[SimXR] xrDestroySession: SUCCESS");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateSwapchainFormats_runtime(XrSession, uint32_t capacity, uint32_t* count, int64_t* formats) {
    // Vulkan path - VkFormat values, in the order an app should prefer them
    if (rt::g_session.usesVulkan) {
        const uint32_t formatCount = (uint32_t)std::size(vkrt::kFormats);
        if (count) *count = formatCount;
        if (capacity > 0 && formats) {
            const uint32_t copyCount = (capacity < formatCount) ? capacity : formatCount;
            for (uint32_t i = 0; i < copyCount; ++i) formats[i] = vkrt::kFormats[i].vk;
            Logf("[SimXR] xrEnumerateSwapchainFormats(Vulkan): %u formats (first: VkFormat %lld)",
                 copyCount, (long long)formats[0]);
        }
        return XR_SUCCESS;
    }

    // OpenGL path - return GL internal formats
    if (rt::g_session.usesOpenGL) {
        const int64_t supportedFormats[] = {
            GL_SRGB8_ALPHA8,          // sRGB
            GL_RGBA8,                 // Standard RGBA
            GL_RGBA16F,               // HDR format
            GL_RGBA32F,               // High precision
            GL_RGB10_A2,              // HDR10 format
            GL_DEPTH_COMPONENT32F,    // Depth buffer
            GL_DEPTH24_STENCIL8,      // Depth + stencil
            GL_DEPTH_COMPONENT16      // 16-bit depth
        };
        const uint32_t formatCount = sizeof(supportedFormats) / sizeof(supportedFormats[0]);

        if (count) *count = formatCount;
        if (capacity > 0 && formats) {
            uint32_t copyCount = (capacity < formatCount) ? capacity : formatCount;
            for (uint32_t i = 0; i < copyCount; ++i) {
                formats[i] = supportedFormats[i];
            }
            Logf("[SimXR] xrEnumerateSwapchainFormats(OpenGL): Returned %u formats (first: 0x%X)", copyCount, (int)formats[0]);
        }
        return XR_SUCCESS;
    }

    // D3D11/D3D12 path - return DXGI formats
    const int64_t supportedFormats[] = {
        DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,  // Unity often prefers sRGB
        DXGI_FORMAT_R8G8B8A8_UNORM,
        DXGI_FORMAT_B8G8R8A8_UNORM_SRGB,  // UEVR uses this
        DXGI_FORMAT_B8G8R8A8_UNORM,
        DXGI_FORMAT_R16G16B16A16_FLOAT,   // HDR format
        DXGI_FORMAT_R32G32B32A32_FLOAT,   // High precision
        DXGI_FORMAT_R10G10B10A2_UNORM,    // HDR10 format
        DXGI_FORMAT_D32_FLOAT_S8X24_UINT, // UEVR's preferred depth format
        DXGI_FORMAT_D32_FLOAT,            // Depth buffer
        DXGI_FORMAT_D24_UNORM_S8_UINT,    // Depth + stencil
        DXGI_FORMAT_D16_UNORM             // 16-bit depth
    };
    const uint32_t formatCount = sizeof(supportedFormats) / sizeof(supportedFormats[0]);

    if (count) *count = formatCount;
    if (capacity > 0 && formats) {
        uint32_t copyCount = (capacity < formatCount) ? capacity : formatCount;
        for (uint32_t i = 0; i < copyCount; ++i) {
            formats[i] = supportedFormats[i];
        }
        Logf("[SimXR] xrEnumerateSwapchainFormats: Returned %u formats (first: %d)", copyCount, (int)formats[0]);
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateSwapchain_runtime(XrSession, const XrSwapchainCreateInfo* ci, XrSwapchain* sc) {
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrCreateSwapchain called: format=%d, size=%ux%u, arraySize=%u, mipCount=%u, sampleCount=%u, usageFlags=0x%X",
         ci ? (int)ci->format : -1, 
         ci ? ci->width : 0, 
         ci ? ci->height : 0, 
         ci ? ci->arraySize : 0, 
         ci ? ci->mipCount : 0, 
         ci ? ci->sampleCount : 0,
         ci ? ci->usageFlags : 0);
    
    // Log specific usage flags
    if (ci && ci->usageFlags) {
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT) 
            Log("[SimXR]   - COLOR_ATTACHMENT");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT) 
            Log("[SimXR]   - DEPTH_STENCIL_ATTACHMENT");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT) 
            Log("[SimXR]   - UNORDERED_ACCESS");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT) 
            Log("[SimXR]   - TRANSFER_SRC");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT) 
            Log("[SimXR]   - TRANSFER_DST");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_SAMPLED_BIT) 
            Log("[SimXR]   - SAMPLED");
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT) 
            Log("[SimXR]   - MUTABLE_FORMAT");
    }
    
    Log("[SimXR] ============================================");
    if (!ci || !sc) return XR_ERROR_VALIDATION_FAILURE;
    rt::Swapchain chain{}; 
    chain.handle = (XrSwapchain)(rt::g_nextSwapchainHandle++);
    chain.format = (DXGI_FORMAT)ci->format;  // Store the original requested format
    chain.width = ci->width; 
    chain.height = ci->height; 
    chain.arraySize = ci->arraySize ? ci->arraySize : 1;
    chain.lastAcquired = UINT32_MAX;  // No image acquired yet
    chain.lastReleased = UINT32_MAX;  // No image released yet
    // Create textures on appropriate backend
    if (rt::g_session.usesVulkan) {
        const XrResult r = vkrt::CreateSwapchainImages(rt::g_session, chain, *ci);
        if (XR_FAILED(r)) {
            vkrt::DestroySwapchainImages(rt::g_session, chain);
            return r;
        }
        rt::g_swapchains.emplace(chain.handle, std::move(chain));
        *sc = chain.handle;
        Logf("[SimXR] xrCreateSwapchain(Vulkan): sc=%p fmt=%lld %ux%u array=%u samples=%u",
             *sc, (long long)ci->format, ci->width, ci->height, ci->arraySize, ci->sampleCount);
        return XR_SUCCESS;
    }
    if (rt::g_session.usesD3D12) {
        chain.backend = rt::Swapchain::Backend::D3D12;
        chain.imageCount = 3;
        for (uint32_t i = 0; i < chain.imageCount; ++i) {
            D3D12_RESOURCE_DESC rd = {};
            rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
            rd.Alignment = 0;
            rd.Width = chain.width;
            rd.Height = chain.height;
            rd.DepthOrArraySize = (UINT)chain.arraySize;
            rd.MipLevels = (UINT)(ci->mipCount ? ci->mipCount : 1);
            chain.mipCount = rd.MipLevels;
            rd.Format = chain.format;
            rd.SampleDesc.Count = (UINT)(ci->sampleCount ? ci->sampleCount : 1);
            rd.SampleDesc.Quality = 0;
            rd.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
            rd.Flags = D3D12_RESOURCE_FLAG_NONE;
            bool isDepth = (chain.format == DXGI_FORMAT_D32_FLOAT ||
                            chain.format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT ||
                            chain.format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
                            chain.format == DXGI_FORMAT_D16_UNORM);
            if (isDepth) {
                // Depth textures need ALLOW_DEPTH_STENCIL to be usable as DSV
                rd.Flags |= D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
                // Use typeless format so UEVR can create typed views
                switch (chain.format) {
                    case DXGI_FORMAT_D32_FLOAT_S8X24_UINT: rd.Format = DXGI_FORMAT_R32G8X24_TYPELESS; break;
                    case DXGI_FORMAT_D32_FLOAT:            rd.Format = DXGI_FORMAT_R32_TYPELESS; break;
                    case DXGI_FORMAT_D24_UNORM_S8_UINT:    rd.Format = DXGI_FORMAT_R24G8_TYPELESS; break;
                    case DXGI_FORMAT_D16_UNORM:            rd.Format = DXGI_FORMAT_R16_TYPELESS; break;
                    default: break;
                }
            } else {
                if (ci->usageFlags & XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT)
                    rd.Flags |= D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
                if (ci->usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT)
                    rd.Flags |= D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
                // Use typeless format for mutable format swapchains so UEVR can create
                // both sRGB and non-sRGB views of the same texture
                if (ci->usageFlags & XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT) {
                    rd.Format = ToTypeless(chain.format);
                }
            }
            D3D12_HEAP_PROPERTIES hp = {}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
            D3D12_RESOURCE_STATES init = D3D12_RESOURCE_STATE_COMMON;
            if (rd.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET)
                init = D3D12_RESOURCE_STATE_RENDER_TARGET;
            else if (rd.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL)
                init = D3D12_RESOURCE_STATE_DEPTH_WRITE;

            // Provide optimized clear value for render targets and depth buffers
            D3D12_CLEAR_VALUE clearValue = {};
            D3D12_CLEAR_VALUE* pClearValue = nullptr;
            if (rd.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET) {
                clearValue.Format = chain.format;
                clearValue.Color[0] = 0.0f; clearValue.Color[1] = 0.0f;
                clearValue.Color[2] = 0.0f; clearValue.Color[3] = 1.0f;
                pClearValue = &clearValue;
            } else if (rd.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL) {
                clearValue.Format = chain.format;
                clearValue.DepthStencil.Depth = 1.0f;
                clearValue.DepthStencil.Stencil = 0;
                pClearValue = &clearValue;
            }

            ComPtr<ID3D12Resource> res;
            HRESULT hr = rt::g_session.d3d12Device->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, init, pClearValue, IID_PPV_ARGS(res.GetAddressOf()));
            if (FAILED(hr)) {
                Logf("[SimXR] CreateCommittedResource(D3D12)[%u] FAILED: 0x%08X", i, (unsigned)hr);
                return XR_ERROR_RUNTIME_FAILURE;
            }
            chain.images12.push_back(res);
            chain.imageStates12.push_back(init);
            chain.releaseState12 = init;
        }
        rt::g_swapchains.emplace(chain.handle, std::move(chain));
        *sc = chain.handle;
        Logf("[SimXR] xrCreateSwapchain(D3D12): sc=%p fmt=%d %ux%u array=%u samples=%u", *sc, (int)ci->format, ci->width, ci->height, ci->arraySize, ci->sampleCount);
        return XR_SUCCESS;
    }
    // OpenGL path
    if (rt::g_session.usesOpenGL) {
        chain.backend = rt::Swapchain::Backend::OpenGL;
        chain.imageCount = 3;

        // Check the current GL context state
        HGLRC currentRC = wglGetCurrentContext();
        HDC currentDC = wglGetCurrentDC();
        Logf("[SimXR] OpenGL swapchain: currentRC=%p, currentDC=%p, sessionRC=%p, sessionDC=%p",
             currentRC, currentDC, rt::g_session.glRC, rt::g_session.glDC);

        // If the app's context is already current, use it directly
        // Otherwise, switch to the app's context
        HGLRC prevRC = currentRC;
        HDC prevDC = currentDC;
        bool contextSwitched = false;

        if (currentRC != rt::g_session.glRC) {
            Log("[SimXR] Context mismatch - switching to app's context");
            if (!wglMakeCurrent(rt::g_session.glDC, rt::g_session.glRC)) {
                Logf("[SimXR] xrCreateSwapchain(OpenGL): wglMakeCurrent failed (error=%lu)", GetLastError());
                return XR_ERROR_RUNTIME_FAILURE;
            }
            contextSwitched = true;
        } else {
            Log("[SimXR] App's GL context is already current - good!");
        }

        // Log the GL version and renderer for debugging
        const char* glVersion = (const char*)glGetString(GL_VERSION);
        const char* glRenderer = (const char*)glGetString(GL_RENDERER);
        Logf("[SimXR] GL context: version=%s, renderer=%s",
             glVersion ? glVersion : "(null)", glRenderer ? glRenderer : "(null)");

        // Map format to OpenGL internal format and pixel format
        // The format from createInfo is a GL internal format (e.g. GL_RGBA8) when using OpenGL path
        auto GLFormatToPixelFormat = [](GLenum internalFmt) -> GLenum {
            switch (internalFmt) {
                case GL_SRGB8_ALPHA8:
                case GL_RGBA8:
                    return GL_RGBA;
                case GL_RGBA16F:
                case GL_RGBA32F:
                    return GL_RGBA;
                case GL_RGB10_A2:
                    return GL_RGBA;
                case GL_DEPTH_COMPONENT32F:
                case GL_DEPTH_COMPONENT16:
                    return GL_DEPTH_COMPONENT;
                case GL_DEPTH24_STENCIL8:
                    return GL_DEPTH_STENCIL;
                default:
                    return GL_RGBA;  // Default fallback
            }
        };

        // ci->format already contains the GL internal format
        GLenum glInternalFormat = (GLenum)ci->format;
        GLenum glFormat = GLFormatToPixelFormat(glInternalFormat);
        chain.glInternalFormat = glInternalFormat;

        bool isDepthFormat = (glInternalFormat == GL_DEPTH_COMPONENT32F ||
                              glInternalFormat == GL_DEPTH24_STENCIL8 ||
                              glInternalFormat == GL_DEPTH_COMPONENT16);

        // Load glTexImage3D if needed for array textures
        if (chain.arraySize > 1 && !EnsureGLTexImage3D()) {
            wglMakeCurrent(prevDC, prevRC);
            return XR_ERROR_RUNTIME_FAILURE;
        }

        // Create OpenGL textures
        for (uint32_t i = 0; i < chain.imageCount; ++i) {
            GLuint tex;
            glGenTextures(1, &tex);

            if (chain.arraySize > 1) {
                // Use texture array
                glBindTexture(GL_TEXTURE_2D_ARRAY, tex);
                g_glTexImage3D(GL_TEXTURE_2D_ARRAY, 0, glInternalFormat,
                               chain.width, chain.height, chain.arraySize,
                               0, glFormat, GL_UNSIGNED_BYTE, nullptr);
                glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glBindTexture(GL_TEXTURE_2D_ARRAY, 0);
            } else {
                // Use regular 2D texture
                glBindTexture(GL_TEXTURE_2D, tex);
                if (isDepthFormat) {
                    GLenum type = (glInternalFormat == GL_DEPTH24_STENCIL8) ? GL_UNSIGNED_INT_24_8 : GL_FLOAT;
                    glTexImage2D(GL_TEXTURE_2D, 0, glInternalFormat,
                                 chain.width, chain.height, 0,
                                 glFormat, type, nullptr);
                } else {
                    // DEBUG: Initialize with bright green to verify texture pipeline
                    std::vector<uint8_t> initData(chain.width * chain.height * 4);
                    for (size_t p = 0; p < initData.size(); p += 4) {
                        initData[p + 0] = 0;    // R
                        initData[p + 1] = 255;  // G - bright green
                        initData[p + 2] = 0;    // B
                        initData[p + 3] = 255;  // A
                    }
                    glTexImage2D(GL_TEXTURE_2D, 0, glInternalFormat,
                                 chain.width, chain.height, 0,
                                 GL_RGBA, GL_UNSIGNED_BYTE, initData.data());
                    Logf("[SimXR] Initialized tex %u with GREEN data", tex);
                }
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glBindTexture(GL_TEXTURE_2D, 0);
            }

            GLenum err = glGetError();
            if (err != GL_NO_ERROR) {
                Logf("[SimXR] OpenGL texture creation error[%u]: 0x%X", i, err);
                if (contextSwitched) wglMakeCurrent(prevDC, prevRC);
                return XR_ERROR_RUNTIME_FAILURE;
            }

            // Verify the texture is valid
            GLboolean isValid = glIsTexture(tex);
            chain.imagesGL.push_back(tex);
            Logf("[SimXR] Created GL texture[%u]: %u (format=0x%X, valid=%d)", i, tex, glInternalFormat, isValid);

            // DEBUG: Immediately read back the texture to verify it has the correct content
            if (!isDepthFormat && EnsureGLFramebufferFuncs()) {
                GLuint verifyFBO = 0;
                g_glGenFramebuffers(1, &verifyFBO);
                g_glBindFramebuffer(GL_FRAMEBUFFER, verifyFBO);
                g_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);

                if (g_glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
                    uint8_t pixel[4] = {0};
                    glReadPixels(chain.width/2, chain.height/2, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
                    Logf("[SimXR]   Immediate readback tex=%u: pixel=[%d,%d,%d,%d]",
                         tex, pixel[0], pixel[1], pixel[2], pixel[3]);
                } else {
                    Logf("[SimXR]   Immediate readback tex=%u: FBO incomplete", tex);
                }

                g_glBindFramebuffer(GL_FRAMEBUFFER, 0);
                g_glDeleteFramebuffers(1, &verifyFBO);
            }
        }

        // Restore previous context only if we switched
        if (contextSwitched) {
            Log("[SimXR] Restoring previous GL context");
            wglMakeCurrent(prevDC, prevRC);
        }

        rt::g_swapchains.emplace(chain.handle, std::move(chain));
        *sc = chain.handle;
        Logf("[SimXR] xrCreateSwapchain(OpenGL): sc=%p fmt=%d %ux%u array=%u imageCount=%u",
             *sc, (int)ci->format, ci->width, ci->height, ci->arraySize, chain.imageCount);
        return XR_SUCCESS;
    }
    // D3D11 path
    D3D11_TEXTURE2D_DESC td{};

    // Determine if this is a depth format
    bool isDepthFormat = (chain.format == DXGI_FORMAT_D32_FLOAT ||
                          chain.format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT ||
                          chain.format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
                          chain.format == DXGI_FORMAT_D16_UNORM);

    // For depth formats that need to be sampled, we must use typeless format
    bool needsTypelessDepth = isDepthFormat && (ci->usageFlags & XR_SWAPCHAIN_USAGE_SAMPLED_BIT);

    // Convert depth format to typeless when sampling is needed
    auto ToTypelessDepth = [](DXGI_FORMAT fmt) -> DXGI_FORMAT {
        switch (fmt) {
            case DXGI_FORMAT_D32_FLOAT:            return DXGI_FORMAT_R32_TYPELESS;
            case DXGI_FORMAT_D32_FLOAT_S8X24_UINT: return DXGI_FORMAT_R32G8X24_TYPELESS;
            case DXGI_FORMAT_D24_UNORM_S8_UINT:    return DXGI_FORMAT_R24G8_TYPELESS;
            case DXGI_FORMAT_D16_UNORM:            return DXGI_FORMAT_R16_TYPELESS;
            default: return fmt;
        }
    };

    // Use typeless format for color textures to allow both UNORM and SRGB views
    // Use typeless format for depth textures when sampling is needed
    if (isDepthFormat) {
        td.Format = needsTypelessDepth ? ToTypelessDepth(chain.format) : chain.format;
    } else {
        td.Format = ToTypeless(chain.format);
    }

    td.Width = chain.width;
    td.Height = chain.height;
    td.ArraySize = chain.arraySize ? chain.arraySize : 1;  // Ensure at least 1
    td.MipLevels = ci->mipCount ? ci->mipCount : 1;  // Ensure at least 1
    chain.mipCount = td.MipLevels;
    td.SampleDesc.Count = ci->sampleCount ? ci->sampleCount : 1;
    td.SampleDesc.Quality = 0;  // Must be 0 for non-MSAA
    // Set bind flags based on format type

    if (isDepthFormat) {
        td.BindFlags = D3D11_BIND_DEPTH_STENCIL;
        // Typeless depth formats can also be shader resources
        if (needsTypelessDepth) {
            td.BindFlags |= D3D11_BIND_SHADER_RESOURCE;
        }
    } else {
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        
        // Add unordered access if requested
        if (ci->usageFlags & XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT) {
            td.BindFlags |= D3D11_BIND_UNORDERED_ACCESS;
        }
    }
    
    td.Usage = D3D11_USAGE_DEFAULT;
    td.CPUAccessFlags = 0;
    
    // Set MiscFlags based on what Unity might need
    td.MiscFlags = 0;
    
    // Log the texture description for debugging
    Logf("[SimXR] Creating swapchain textures: Format=%d, %ux%u, Array=%u, Mips=%u, Samples=%u",
         td.Format, td.Width, td.Height, td.ArraySize, td.MipLevels, td.SampleDesc.Count);
    chain.imageCount = 3;
    for (uint32_t i = 0; i < chain.imageCount; ++i) {
        ComPtr<ID3D11Texture2D> tex; 
        HRESULT hr = rt::g_session.d3d11Device->CreateTexture2D(&td, nullptr, tex.GetAddressOf());
        if (FAILED(hr)) { 
            Logf("[SimXR] CreateTexture2D[%u] FAILED: hr=0x%08X", i, (unsigned)hr);
            Logf("[SimXR]   Format=%d, Size=%ux%u, Array=%u, Mips=%u, Samples=%u, BindFlags=0x%X",
                 td.Format, td.Width, td.Height, td.ArraySize, td.MipLevels, td.SampleDesc.Count, td.BindFlags);
            
            // Try to provide more specific error info
            if (hr == E_INVALIDARG) {
                Log("[SimXR]   ERROR: E_INVALIDARG - Invalid texture parameters");
                // Check common issues
                if (td.ArraySize == 0) Log("[SimXR]   - ArraySize is 0");
                if (td.Width == 0 || td.Height == 0) Log("[SimXR]   - Invalid dimensions");
                if (td.MipLevels == 0) Log("[SimXR]   - MipLevels is 0");
            }
            return XR_ERROR_RUNTIME_FAILURE; 
        }
        Logf("[SimXR] Created swapchain texture[%u]: %p", i, tex.Get());
        chain.images.push_back(std::move(tex));
    }
    rt::g_swapchains.emplace(chain.handle, std::move(chain));
    *sc = chain.handle;
    Logf("[SimXR] xrCreateSwapchain: sc=%p fmt=%d %ux%u array=%u samples=%u", *sc, (int)ci->format, ci->width, ci->height, ci->arraySize, ci->sampleCount);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateSwapchainImages_runtime(XrSwapchain sc, uint32_t capacity, uint32_t* count, XrSwapchainImageBaseHeader* images) {
    auto it = rt::g_swapchains.find(sc); if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    if (it->second.isVulkan) {
        // XrSwapchainImageVulkan2KHR is a typedef of XrSwapchainImageVulkanKHR, so this is
        // the right shape for both extensions.
        const uint32_t n = (uint32_t)it->second.imagesVk.size();
        if (count) *count = n;
        if (capacity >= n && images) {
            auto* arr = reinterpret_cast<XrSwapchainImageVulkanKHR*>(images);
            for (uint32_t i = 0; i < n; ++i) { arr[i].type = XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR; arr[i].image = it->second.imagesVk[i]; }
        }
        Logf("[SimXR] xrEnumerateSwapchainImages(Vulkan): sc=%p count=%u", sc, n);
        return XR_SUCCESS;
    }
    if (it->second.backend == rt::Swapchain::Backend::D3D12) {
        const uint32_t n = (uint32_t)it->second.images12.size();
        if (count) *count = n;
        if (capacity >= n && images) {
            auto* arr = reinterpret_cast<XrSwapchainImageD3D12KHR*>(images);
            for (uint32_t i = 0; i < n; ++i) { arr[i].type = XR_TYPE_SWAPCHAIN_IMAGE_D3D12_KHR; arr[i].texture = it->second.images12[i].Get(); }
        }
        Logf("[SimXR] xrEnumerateSwapchainImages(D3D12): sc=%p count=%u", sc, n);
        return XR_SUCCESS;
    } else if (it->second.backend == rt::Swapchain::Backend::OpenGL) {
        const uint32_t n = (uint32_t)it->second.imagesGL.size();
        if (count) *count = n;
        if (capacity >= n && images) {
            auto* arr = reinterpret_cast<XrSwapchainImageOpenGLKHR*>(images);
            for (uint32_t i = 0; i < n; ++i) {
                arr[i].type = XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_KHR;
                arr[i].image = it->second.imagesGL[i];
            }
            // DEBUG: Log the texture IDs being returned AND verify content still matches
            Logf("[SimXR] xrEnumerateSwapchainImages(OpenGL): sc=%p texIDs=[%u,%u,%u]",
                 sc, n > 0 ? arr[0].image : 0, n > 1 ? arr[1].image : 0, n > 2 ? arr[2].image : 0);

            // DEBUG: Read first texture to verify it still has content
            if (n > 0 && EnsureGLFramebufferFuncs()) {
                GLuint checkTex = arr[0].image;
                GLuint checkFBO = 0;
                g_glGenFramebuffers(1, &checkFBO);
                g_glBindFramebuffer(GL_FRAMEBUFFER, checkFBO);
                g_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, checkTex, 0);
                if (g_glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
                    uint8_t pixel[4] = {0};
                    glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
                    Logf("[SimXR]   After enumerate: tex=%u pixel=[%d,%d,%d,%d]",
                         checkTex, pixel[0], pixel[1], pixel[2], pixel[3]);
                }
                g_glBindFramebuffer(GL_FRAMEBUFFER, 0);
                g_glDeleteFramebuffers(1, &checkFBO);
            }
        } else {
            Logf("[SimXR] xrEnumerateSwapchainImages(OpenGL): sc=%p count=%u (query only)", sc, n);
        }
        return XR_SUCCESS;
    } else {
        const uint32_t n = (uint32_t)it->second.images.size();
        if (count) *count = n;
        if (capacity >= n && images) {
            auto* arr = reinterpret_cast<XrSwapchainImageD3D11KHR*>(images);
            for (uint32_t i = 0; i < n; ++i) { arr[i].type = XR_TYPE_SWAPCHAIN_IMAGE_D3D11_KHR; arr[i].texture = it->second.images[i].Get(); }
        }
        Logf("[SimXR] xrEnumerateSwapchainImages(D3D11): sc=%p count=%u", sc, n);
        return XR_SUCCESS;
    }
}

static XrResult XRAPI_PTR xrAcquireSwapchainImage_runtime(XrSwapchain sc, const XrSwapchainImageAcquireInfo*, uint32_t* index) {
    auto it = rt::g_swapchains.find(sc); if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    auto& ch = it->second;
    uint32_t i = ch.nextIndex;
    ch.nextIndex = (ch.nextIndex + 1) % ch.imageCount;
    ch.lastAcquired = i;  // Track what we just gave to the app
    if (index) *index = i; 
    
    static int acquireCount = 0;
    ++acquireCount;
    // First few calls give startup context; the recurring line is verbose-only,
    // since Log() is deliberately expensive (see its comment).
    if (acquireCount <= 4 || (g_logVerbose && acquireCount % 60 == 1)) {
        Logf("[SimXR] xrAcquireSwapchainImage: sc=%p idx=%u (format=%d, %ux%u)",
             sc, i, (int)ch.format, ch.width, ch.height);
    }
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrWaitSwapchainImage_runtime(XrSwapchain, const XrSwapchainImageWaitInfo*) { return XR_SUCCESS; }
static XrResult XRAPI_PTR xrReleaseSwapchainImage_runtime(XrSwapchain sc, const XrSwapchainImageReleaseInfo*) {
    auto it = rt::g_swapchains.find(sc);
    if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;
    auto& ch = it->second;
    // The app just released the image it acquired earlier
    ch.lastReleased = ch.lastAcquired;

    // For D3D12: the app has finished using this image, reset our tracked state to COMMON.
    // D3D12 implicit state promotion/decay means COMMON is always safe after a GPU sync point.
    if (ch.backend == rt::Swapchain::Backend::D3D12 && ch.lastReleased < ch.imageStates12.size()) {
        ch.imageStates12[ch.lastReleased] = ch.releaseState12;
    }

    static int releaseCount = 0;
    bool shouldLog = (++releaseCount <= 10);
    if (shouldLog || (g_logVerbose && releaseCount % 60 == 1)) {
        Logf("[SimXR] xrReleaseSwapchainImage: sc=%p released=%u", sc, ch.lastReleased);

        // DEBUG: Read texture content at release time to verify it has content.
        // Verbose-only: the probe below runs glFinish plus a full-texture readback,
        // a periodic multi-millisecond stall no ordinary session should pay for.
        if (g_logVerbose &&
            ch.backend == rt::Swapchain::Backend::OpenGL && !ch.imagesGL.empty() && ch.lastReleased < ch.imagesGL.size()) {
            GLuint glTex = ch.imagesGL[ch.lastReleased];

            // Check and log current GL context
            HGLRC currentRC = wglGetCurrentContext();
            HDC currentDC = wglGetCurrentDC();
            Logf("[SimXR]   At release: currentRC=%p, currentDC=%p, sessionRC=%p, sessionDC=%p",
                 currentRC, currentDC, rt::g_session.glRC, rt::g_session.glDC);

            // Ensure the session's GL context is current before reading
            HGLRC savedRC = currentRC;
            HDC savedDC = currentDC;
            if (currentRC != rt::g_session.glRC && rt::g_session.glRC && rt::g_session.glDC) {
                Logf("[SimXR]   At release: Switching to session GL context");
                wglMakeCurrent(rt::g_session.glDC, rt::g_session.glRC);
            }

            // Read a single pixel to see what's there
            glFinish();  // Make sure rendering is done

            // Try memory barrier to ensure texture writes are visible
            // GL_TEXTURE_UPDATE_BARRIER_BIT = 0x00000100
            // GL_FRAMEBUFFER_BARRIER_BIT = 0x00000400
            // GL_ALL_BARRIER_BITS = 0xFFFFFFFF
            typedef void (APIENTRY *PFNGLMEMORYBARRIERPROC)(GLbitfield);
            static PFNGLMEMORYBARRIERPROC glMemoryBarrier = nullptr;
            if (!glMemoryBarrier) {
                glMemoryBarrier = (PFNGLMEMORYBARRIERPROC)wglGetProcAddress("glMemoryBarrier");
            }
            if (glMemoryBarrier) {
                glMemoryBarrier(0xFFFFFFFF);  // GL_ALL_BARRIER_BITS
            }

            // Clear any pending GL errors
            while (glGetError() != GL_NO_ERROR) {}

            // Check if texture is valid
            GLboolean isValidTex = glIsTexture(glTex);

            // First, check what FBO is currently bound (might be game's FBO)
            GLint currentBoundFBO = 0;
            glGetIntegerv(0x8CA6, &currentBoundFBO);  // GL_FRAMEBUFFER_BINDING = 0x8CA6

            // Read from current FBO (game's)
            uint8_t currentFboPixel[4] = {0};
            if (currentBoundFBO != 0) {
                glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, currentFboPixel);
            }

            GLenum glError1 = glGetError();

            // Method 1: Read via new FBO attached to swapchain texture
            if (EnsureGLFramebufferFuncs()) {
                GLuint readFBO = 0;
                g_glGenFramebuffers(1, &readFBO);
                g_glBindFramebuffer(GL_FRAMEBUFFER, readFBO);
                g_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, glTex, 0);

                uint8_t fboPixel[4] = {0};
                if (g_glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
                    glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, fboPixel);
                }

                g_glBindFramebuffer(GL_FRAMEBUFFER, 0);
                g_glDeleteFramebuffers(1, &readFBO);

                // Check if texture exists and has expected properties
                GLint texWidth = 0, texHeight = 0, texFormat = 0;
                glBindTexture(GL_TEXTURE_2D, glTex);
                glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, &texWidth);
                glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, &texHeight);
                glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_INTERNAL_FORMAT, &texFormat);

                // Method 2: Read via glGetTexImage (need full buffer for entire texture)
                uint8_t texPixel[4] = {0};
                if (texWidth > 0 && texHeight > 0) {
                    size_t bufSize = (size_t)texWidth * texHeight * 4;
                    if (bufSize <= 16 * 1024 * 1024) {  // Limit to 16MB
                        std::vector<uint8_t> texData(bufSize);
                        glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, texData.data());
                        // Get first pixel
                        texPixel[0] = texData[0];
                        texPixel[1] = texData[1];
                        texPixel[2] = texData[2];
                        texPixel[3] = texData[3];
                    }
                }
                glBindTexture(GL_TEXTURE_2D, 0);

                GLenum glError2 = glGetError();
                Logf("[SimXR]   At release: tex=%u isValid=%d err1=0x%X err2=0x%X boundFBO=%d currPx=[%d,%d,%d,%d] newPx=[%d,%d,%d,%d] texPx=[%d,%d,%d,%d] size=%dx%d fmt=0x%X",
                     glTex, (int)isValidTex, glError1, glError2, currentBoundFBO,
                     currentFboPixel[0], currentFboPixel[1], currentFboPixel[2], currentFboPixel[3],
                     fboPixel[0], fboPixel[1], fboPixel[2], fboPixel[3],
                     texPixel[0], texPixel[1], texPixel[2], texPixel[3],
                     texWidth, texHeight, texFormat);
            }

            // Restore original GL context if we switched
            if (savedRC != rt::g_session.glRC && savedRC && savedDC) {
                wglMakeCurrent(savedDC, savedRC);
            }
        }
    }
    return XR_SUCCESS;
}

// Helper struct to save and restore D3D11 context state using RAII
struct D3D11StateBackup {
    D3D11StateBackup(ID3D11DeviceContext* ctx) : ctx_(ctx) {
        // IA
        ctx_->IAGetInputLayout(&ia_input_layout);
        ctx_->IAGetPrimitiveTopology(&ia_primitive_topology);
        // RS
        rs_num_viewports = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
        ctx_->RSGetViewports(&rs_num_viewports, rs_viewports);
        rs_num_scissor_rects = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
        ctx_->RSGetScissorRects(&rs_num_scissor_rects, rs_scissor_rects);
        ctx_->RSGetState(&rs_state);
        // OM
        ctx_->OMGetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, om_rtvs, &om_dsv);
        ctx_->OMGetBlendState(&om_blend_state, om_blend_factor, &om_sample_mask);
        ctx_->OMGetDepthStencilState(&om_depth_stencil_state, &om_stencil_ref);
        // Shaders - MUST initialize class instance counts before calling GetShader
        ps_num_class_instances = 256;  // Initialize to array capacity
        ctx_->PSGetShader(&ps_shader, ps_class_instances, &ps_num_class_instances);
        ctx_->PSGetSamplers(0, D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT, ps_samplers);
        ctx_->PSGetShaderResources(0, D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT, ps_srvs);
        vs_num_class_instances = 256;  // Initialize to array capacity
        ctx_->VSGetShader(&vs_shader, vs_class_instances, &vs_num_class_instances);
        ctx_->VSGetConstantBuffers(0, D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT, vs_cbuffers);
    }

    ~D3D11StateBackup() {
        // Restore state
        ctx_->IASetInputLayout(ia_input_layout);
        ctx_->IASetPrimitiveTopology(ia_primitive_topology);
        ctx_->RSSetViewports(rs_num_viewports, rs_viewports);
        ctx_->RSSetScissorRects(rs_num_scissor_rects, rs_scissor_rects);
        ctx_->RSSetState(rs_state);
        ctx_->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, om_rtvs, om_dsv);
        ctx_->OMSetBlendState(om_blend_state, om_blend_factor, om_sample_mask);
        ctx_->OMSetDepthStencilState(om_depth_stencil_state, om_stencil_ref);
        ctx_->PSSetShader(ps_shader, ps_class_instances, ps_num_class_instances);
        ctx_->PSSetSamplers(0, D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT, ps_samplers);
        ctx_->PSSetShaderResources(0, D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT, ps_srvs);
        ctx_->VSSetShader(vs_shader, vs_class_instances, vs_num_class_instances);
        ctx_->VSSetConstantBuffers(0, D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT, vs_cbuffers);

        // Release COM references
        if (ia_input_layout) ia_input_layout->Release();
        if (rs_state) rs_state->Release();
        for (UINT i = 0; i < D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT; ++i) if (om_rtvs[i]) om_rtvs[i]->Release();
        if (om_dsv) om_dsv->Release();
        if (om_blend_state) om_blend_state->Release();
        if (om_depth_stencil_state) om_depth_stencil_state->Release();
        if (ps_shader) ps_shader->Release();
        for (UINT i = 0; i < ps_num_class_instances; ++i) if (ps_class_instances[i]) ps_class_instances[i]->Release();
        for (UINT i = 0; i < D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT; ++i) if (ps_samplers[i]) ps_samplers[i]->Release();
        for (UINT i = 0; i < D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT; ++i) if (ps_srvs[i]) ps_srvs[i]->Release();
        if (vs_shader) vs_shader->Release();
        for (UINT i = 0; i < vs_num_class_instances; ++i) if (vs_class_instances[i]) vs_class_instances[i]->Release();
        for (UINT i = 0; i < D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT; ++i) if (vs_cbuffers[i]) vs_cbuffers[i]->Release();
    }

private:
    ID3D11DeviceContext* ctx_;
    // IA State
    ID3D11InputLayout* ia_input_layout = nullptr;
    D3D11_PRIMITIVE_TOPOLOGY ia_primitive_topology = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
    // RS State  
    UINT rs_num_viewports = 0, rs_num_scissor_rects = 0;
    D3D11_VIEWPORT rs_viewports[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    D3D11_RECT rs_scissor_rects[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    ID3D11RasterizerState* rs_state = nullptr;
    // OM State
    ID3D11RenderTargetView* om_rtvs[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT] = { nullptr };
    ID3D11DepthStencilView* om_dsv = nullptr;
    ID3D11BlendState* om_blend_state = nullptr;
    FLOAT om_blend_factor[4] = { 0.0f };
    UINT om_sample_mask = 0;
    ID3D11DepthStencilState* om_depth_stencil_state = nullptr;
    UINT om_stencil_ref = 0;
    // PS State
    ID3D11PixelShader* ps_shader = nullptr;
    ID3D11ClassInstance* ps_class_instances[256] = { nullptr };
    UINT ps_num_class_instances = 0;
    ID3D11SamplerState* ps_samplers[D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT] = { nullptr };
    ID3D11ShaderResourceView* ps_srvs[D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT] = { nullptr };
    // VS State. Constant buffers are here because the quad layer binds b0 on what is, for a
    // D3D11 session, the app's own immediate context.
    ID3D11VertexShader* vs_shader = nullptr;
    ID3D11ClassInstance* vs_class_instances[256] = { nullptr };
    UINT vs_num_class_instances = 0;
    ID3D11Buffer* vs_cbuffers[D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT] = { nullptr };
};

namespace rt {
    static XrSessionState g_state = XR_SESSION_STATE_IDLE;
    static std::vector<XrEventDataBuffer> g_eventQueue;
    void PushState(XrSession s, XrSessionState ns) {
        g_state = ns;
        g_session.state = ns;
        const char* stateName = "UNKNOWN";
        switch(ns) {
            case XR_SESSION_STATE_IDLE: stateName = "IDLE"; break;
            case XR_SESSION_STATE_READY: stateName = "READY"; break;
            case XR_SESSION_STATE_SYNCHRONIZED: stateName = "SYNCHRONIZED"; break;
            case XR_SESSION_STATE_VISIBLE: stateName = "VISIBLE"; break;
            case XR_SESSION_STATE_FOCUSED: stateName = "FOCUSED"; break;
            case XR_SESSION_STATE_STOPPING: stateName = "STOPPING"; break;
            case XR_SESSION_STATE_LOSS_PENDING: stateName = "LOSS_PENDING"; break;
            case XR_SESSION_STATE_EXITING: stateName = "EXITING"; break;
        }
        Logf("[SimXR] PushState: Session %llu -> %s", (unsigned long long)s, stateName);
        
        XrEventDataSessionStateChanged e{XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED};
        e.session = s; e.state = ns; e.time = 0;
        
        XrEventDataBuffer buf{};
        buf.type = XR_TYPE_EVENT_DATA_BUFFER;  // Set the base type
        std::memcpy(&buf, &e, sizeof(e));
        g_eventQueue.push_back(buf);
        Logf("[SimXR] Event queue now has %zu events", g_eventQueue.size());
    }
}
static XrResult XRAPI_PTR xrPollEvent_runtime(XrInstance, XrEventDataBuffer* b) {
    static int pollCount = 0;
    pollCount++;
    
    if (pollCount <= 5) {  // Log first few polls
        Logf("[SimXR] xrPollEvent called (#%d), queue size=%zu", pollCount, rt::g_eventQueue.size());
    }
    
    if (!b) return XR_ERROR_VALIDATION_FAILURE;
    if (rt::g_eventQueue.empty()) {
        if (pollCount <= 5) {
            Log("[SimXR] xrPollEvent: No events available (XR_EVENT_UNAVAILABLE)");
        }
        return XR_EVENT_UNAVAILABLE;
    }
    *b = rt::g_eventQueue.front();
    rt::g_eventQueue.erase(rt::g_eventQueue.begin());
    
    // Log what event we're delivering
    const XrEventDataBaseHeader* header = reinterpret_cast<const XrEventDataBaseHeader*>(b);
    if (header->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
        const XrEventDataSessionStateChanged* stateEvent = reinterpret_cast<const XrEventDataSessionStateChanged*>(b);
        const char* stateName = "UNKNOWN";
        switch(stateEvent->state) {
            case XR_SESSION_STATE_IDLE: stateName = "IDLE"; break;
            case XR_SESSION_STATE_READY: stateName = "READY"; break;
            case XR_SESSION_STATE_SYNCHRONIZED: stateName = "SYNCHRONIZED"; break;
            case XR_SESSION_STATE_VISIBLE: stateName = "VISIBLE"; break;
            case XR_SESSION_STATE_FOCUSED: stateName = "FOCUSED"; break;
            case XR_SESSION_STATE_STOPPING: stateName = "STOPPING"; break;
            case XR_SESSION_STATE_LOSS_PENDING: stateName = "LOSS_PENDING"; break;
            case XR_SESSION_STATE_EXITING: stateName = "EXITING"; break;
        }
        Logf("[SimXR] xrPollEvent: Delivering SESSION_STATE_CHANGED -> %s (session=%llu, %zu events left)", 
            stateName, (unsigned long long)stateEvent->session, rt::g_eventQueue.size());
    } else {
        Logf("[SimXR] xrPollEvent: Delivering event type %d (%zu events left)", header->type, rt::g_eventQueue.size());
    }
    return XR_SUCCESS;
}
// Defined further down, with the rest of the preview plumbing.
static void ensurePreviewWithoutProjection(rt::Session& s);

static XrResult XRAPI_PTR xrBeginSession_runtime(XrSession s, const XrSessionBeginInfo*) { 
    Log("[SimXR] ============================================");
    Logf("[SimXR] xrBeginSession called (session=%llu)", (unsigned long long)s);
    Log("[SimXR] Session started - moving to SYNCHRONIZED/VISIBLE states");
    Log("[SimXR] ============================================");

    // Bring the preview up now instead of waiting for the first projection
    // layer. An app that boots into a 2D-only screen submits nothing but quad
    // layers, and quads cannot bootstrap the preview themselves, so the window
    // would never appear at all - and the FOCUSED check below could never fire.
    ensurePreviewWithoutProjection(rt::g_session);

    // Pump once so the activation raised by creating the window is handled
    // before we decide whether the session starts out focused.
    MSG msg; while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }

    rt::PushState(s, XR_SESSION_STATE_SYNCHRONIZED); 
    rt::PushState(s, XR_SESSION_STATE_VISIBLE);
    // A simulator has no physical HMD focus hand-off. The preview window is created
    // lazily by xrWaitFrame, while several clients (including BetterVR) wait for
    // FOCUSED before entering their frame loop. Conditioning FOCUSED on that not-yet-
    // created window deadlocks a clean launch. Report input focus immediately; the
    // window activation handler may still move between VISIBLE/FOCUSED later.
    rt::g_session.isFocused = true;
    rt::PushState(s, XR_SESSION_STATE_FOCUSED);
    return XR_SUCCESS; 
}
static XrResult XRAPI_PTR xrEndSession_runtime(XrSession s) { Log("[SimXR] xrEndSession"); rt::PushState(s, XR_SESSION_STATE_STOPPING); rt::PushState(s, XR_SESSION_STATE_IDLE); return XR_SUCCESS; }
static XrResult XRAPI_PTR xrRequestExitSession_runtime(XrSession s) { rt::PushState(s, XR_SESSION_STATE_EXITING); return XR_SUCCESS; }
static XrResult XRAPI_PTR xrWaitFrame_runtime(XrSession, const XrFrameWaitInfo*, XrFrameState* s) {
    if (!s) return XR_ERROR_VALIDATION_FAILURE;
    // Message pump so the preview window stays responsive
    MSG msg; while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    static LARGE_INTEGER freq = [](){ LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    static double periodSec = 1.0 / 90.0;
    static long long periodNs = (long long)(periodSec * 1e9);
    static double nextTick = [](){ LARGE_INTEGER t; QueryPerformanceCounter(&t); return (double)t.QuadPart; }();
    
    // Handle WASD keyboard input for movement (relative to head orientation).
    //
    // Accept input when the foreground window is our preview window OR ANY window owned by the host
    // process (the OpenXR app — e.g. the game). The simulator runtime lives in-process with the host,
    // so checking the foreground window's PID against our own lets head WASD/look work whether the user
    // has focused the small "O" preview or the host's own (often fullscreen) window — yet never steals
    // keystrokes when the user has alt-tabbed to an unrelated app (terminal/browser), since those have a
    // different PID.
    //
    // WHY THIS MATTERS (6DOF translation bug): previously this required `foreground == preview &&
    // isFocused`. As soon as the game's window took the foreground (which it does on launch / when
    // fullscreen), the preview went inactive (isFocused=false) and WASD silently stopped feeding
    // g_headPos — so interactive head translation looked completely dead, even though the underlying
    // pose pipeline was fine (scripted head_pose_command.json poses are focus-independent, which is
    // exactly why those worked while WASD didn't). Gating on "any window of our process is foreground"
    // fixes that. Note: FH5 also binds W/S/A/D to throttle/brake/steer, so while the game window is
    // focused these keys additionally drive the car — park the car when isolating head-only translation.
    const HWND foregroundWindow = GetForegroundWindow();
    bool previewWindowFocused = false;
    if (foregroundWindow != nullptr) {
        if (foregroundWindow == rt::g_session.hwnd) {
            previewWindowFocused = rt::g_session.isFocused.load();
        } else {
            DWORD fgPid = 0;
            GetWindowThreadProcessId(foregroundWindow, &fgPid);
            previewWindowFocused = (fgPid != 0 && fgPid == GetCurrentProcessId());
        }
    }
    if (previewWindowFocused) {
        // Shift is the sprint modifier; both it and the base speed are set from
        // Tools > Movement Speed. GetAsyncKeyState rather than the WM_KEYDOWN
        // path because this reads a held state, and the host window may be the
        // one with focus (see above).
        const bool shiftHeld = (GetAsyncKeyState(VK_SHIFT) & 0x8000) != 0;
        const float moveSpeed = ui::GetMoveSpeed(shiftHeld);  // meters per second
        float deltaTime = (float)periodSec;

        XrQuaternionf headQ = rt::QuatFromYawPitch(rt::g_headYaw, rt::g_headPitch);
        XrVector3f fwd = rt::RotateVectorByQuaternion(headQ, XrVector3f{0.0f, 0.0f, -1.0f});
        XrVector3f right = rt::RotateVectorByQuaternion(headQ, XrVector3f{1.0f, 0.0f, 0.0f});

        if (GetAsyncKeyState('W') & 0x8000) {
            rt::g_headPos.x += fwd.x * moveSpeed * deltaTime;
            rt::g_headPos.y += fwd.y * moveSpeed * deltaTime;
            rt::g_headPos.z += fwd.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('S') & 0x8000) {
            rt::g_headPos.x -= fwd.x * moveSpeed * deltaTime;
            rt::g_headPos.y -= fwd.y * moveSpeed * deltaTime;
            rt::g_headPos.z -= fwd.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('A') & 0x8000) {
            rt::g_headPos.x -= right.x * moveSpeed * deltaTime;
            rt::g_headPos.y -= right.y * moveSpeed * deltaTime;
            rt::g_headPos.z -= right.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('D') & 0x8000) {
            rt::g_headPos.x += right.x * moveSpeed * deltaTime;
            rt::g_headPos.y += right.y * moveSpeed * deltaTime;
            rt::g_headPos.z += right.z * moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('Q') & 0x8000) {
            rt::g_headPos.y -= moveSpeed * deltaTime;
        }
        if (GetAsyncKeyState('E') & 0x8000) {
            rt::g_headPos.y += moveSpeed * deltaTime;
        }

        // ========================================
        // Automatic Controller Animation Mode
        // ========================================
        // Press 'M' to toggle automatic motion - controller moves in a pattern
        // Keep tracked controllers stationary unless the user explicitly opts in.
        // Starting this test animation automatically makes applications with hand
        // IK sweep their arm through the camera and creates false-positive flicker
        // incidents in otherwise passive rendering/performance captures.
        static bool autoMotionEnabled = false;
        static bool mKeyWasPressed = false;
        static float animTime = 0.0f;

        bool mKeyPressed = (GetAsyncKeyState('M') & 0x8000) != 0;
        if (mKeyPressed && !mKeyWasPressed) {
            autoMotionEnabled = !autoMotionEnabled;
            Logf("[SimXR] Auto motion %s", autoMotionEnabled ? "ENABLED" : "DISABLED");
        }
        mKeyWasPressed = mKeyPressed;

        if (autoMotionEnabled) {
            animTime += deltaTime;

            // Move controller in a figure-8 pattern around the camera
            // X: side to side (left/right)
            // Y: up and down
            // Z: forward and back
            float radius = 0.4f;  // 40cm radius of motion
            float speed = 0.5f;   // Complete cycle every ~12 seconds

            // Figure-8 pattern
            rt::g_rightController.posOffset.x = radius * sinf(animTime * speed * 2.0f);
            rt::g_rightController.posOffset.y = -0.2f + 0.3f * sinf(animTime * speed);  // Oscillate between -0.5 and +0.1
            rt::g_rightController.posOffset.z = -0.4f + radius * sinf(animTime * speed) * cosf(animTime * speed);

            // Also rotate the controller
            rt::g_rightController.yawOffset = 0.5f * sinf(animTime * speed * 1.5f);
            rt::g_rightController.pitchOffset = -0.3f + 0.3f * cosf(animTime * speed);
        }

        // Right controller manipulation (numpad keys) - only when auto motion is off
        // Numpad 8/2: Move controller forward/back (in local space)
        // Numpad 4/6: Move controller left/right (in local space)
        // Numpad +/-: Move controller up/down
        // Numpad 7/9: Rotate controller yaw
        // Numpad 1/3: Rotate controller pitch
        const float ctrlMoveSpeed = 0.5f * deltaTime;
        const float ctrlRotSpeed = 1.0f * deltaTime;

        if (!autoMotionEnabled) {
            if (GetAsyncKeyState(VK_NUMPAD8) & 0x8000) {
                rt::g_rightController.posOffset.z -= ctrlMoveSpeed;  // Forward
            }
            if (GetAsyncKeyState(VK_NUMPAD2) & 0x8000) {
                rt::g_rightController.posOffset.z += ctrlMoveSpeed;  // Back
            }
            if (GetAsyncKeyState(VK_NUMPAD4) & 0x8000) {
                rt::g_rightController.posOffset.x -= ctrlMoveSpeed;  // Left
            }
            if (GetAsyncKeyState(VK_NUMPAD6) & 0x8000) {
                rt::g_rightController.posOffset.x += ctrlMoveSpeed;  // Right
            }
            if (GetAsyncKeyState(VK_ADD) & 0x8000) {
                rt::g_rightController.posOffset.y += ctrlMoveSpeed;  // Up
            }
            if (GetAsyncKeyState(VK_SUBTRACT) & 0x8000) {
                rt::g_rightController.posOffset.y -= ctrlMoveSpeed;  // Down
            }
            if (GetAsyncKeyState(VK_NUMPAD7) & 0x8000) {
                rt::g_rightController.yawOffset -= ctrlRotSpeed;  // Rotate left
            }
            if (GetAsyncKeyState(VK_NUMPAD9) & 0x8000) {
                rt::g_rightController.yawOffset += ctrlRotSpeed;  // Rotate right
            }
            if (GetAsyncKeyState(VK_NUMPAD1) & 0x8000) {
                rt::g_rightController.pitchOffset += ctrlRotSpeed;  // Pitch down
            }
            if (GetAsyncKeyState(VK_NUMPAD3) & 0x8000) {
                rt::g_rightController.pitchOffset -= ctrlRotSpeed;  // Pitch up
            }
        }
        // Numpad 5: Reset controller to default position
        if (GetAsyncKeyState(VK_NUMPAD5) & 0x8000) {
            rt::g_rightController.posOffset = {0.2f, -0.3f, -0.4f};
            rt::g_rightController.yawOffset = 0.0f;
            rt::g_rightController.pitchOffset = -0.3f;
            autoMotionEnabled = false;
        }

        // ========================================
        // Controller Button/Trigger Emulation
        // ========================================
        // Right controller buttons (main hand for most games)
        // Space = Trigger (fire weapon)
        // F = Grip (grab)
        // Tab = Menu
        // R = Primary button (A)
        // T = Secondary button (B)
        // Enter = Thumbstick click
        rt::g_rightController.triggerPressed = (GetAsyncKeyState(VK_SPACE) & 0x8000) != 0;
        rt::g_rightController.triggerValue = rt::g_rightController.triggerPressed ? 1.0f : 0.0f;
        rt::g_rightController.gripPressed = (GetAsyncKeyState('F') & 0x8000) != 0;
        rt::g_rightController.gripValue = rt::g_rightController.gripPressed ? 1.0f : 0.0f;
        rt::g_rightController.menuPressed = (GetAsyncKeyState(VK_TAB) & 0x8000) != 0;
        rt::g_rightController.primaryPressed = (GetAsyncKeyState('R') & 0x8000) != 0;
        rt::g_rightController.secondaryPressed = (GetAsyncKeyState('T') & 0x8000) != 0;
        rt::g_rightController.thumbstickPressed = (GetAsyncKeyState(VK_RETURN) & 0x8000) != 0;

        // Left controller buttons (off-hand)
        // Left Ctrl = Trigger
        // Left Alt = Grip
        // G = Menu (left hand)
        // V = Primary (X)
        // B = Secondary (Y)
        rt::g_leftController.triggerPressed = (GetAsyncKeyState(VK_LCONTROL) & 0x8000) != 0;
        rt::g_leftController.triggerValue = rt::g_leftController.triggerPressed ? 1.0f : 0.0f;
        rt::g_leftController.gripPressed = (GetAsyncKeyState(VK_LMENU) & 0x8000) != 0;
        rt::g_leftController.gripValue = rt::g_leftController.gripPressed ? 1.0f : 0.0f;
        rt::g_leftController.menuPressed = (GetAsyncKeyState('G') & 0x8000) != 0;
        rt::g_leftController.primaryPressed = (GetAsyncKeyState('V') & 0x8000) != 0;
        rt::g_leftController.secondaryPressed = (GetAsyncKeyState('B') & 0x8000) != 0;

        // Right controller thumbstick (Arrow keys)
        rt::g_rightController.thumbstick = {0.0f, 0.0f};
        if (GetAsyncKeyState(VK_UP) & 0x8000) rt::g_rightController.thumbstick.y = 1.0f;
        if (GetAsyncKeyState(VK_DOWN) & 0x8000) rt::g_rightController.thumbstick.y = -1.0f;
        if (GetAsyncKeyState(VK_LEFT) & 0x8000) rt::g_rightController.thumbstick.x = -1.0f;
        if (GetAsyncKeyState(VK_RIGHT) & 0x8000) rt::g_rightController.thumbstick.x = 1.0f;

        // Left controller thumbstick (IJKL keys)
        rt::g_leftController.thumbstick = {0.0f, 0.0f};
        if (GetAsyncKeyState('I') & 0x8000) rt::g_leftController.thumbstick.y = 1.0f;
        if (GetAsyncKeyState('K') & 0x8000) rt::g_leftController.thumbstick.y = -1.0f;
        if (GetAsyncKeyState('J') & 0x8000) rt::g_leftController.thumbstick.x = -1.0f;
        if (GetAsyncKeyState('L') & 0x8000) rt::g_leftController.thumbstick.x = 1.0f;

        // ========================================
        // Velocity Tracking for Motion Controls
        // ========================================
        // Calculate controller world positions
        XrPosef rightPose, leftPose;
        rt::GetControllerPose(rt::g_rightController, &rightPose);
        rt::GetControllerPose(rt::g_leftController, &leftPose);

        // Calculate linear velocity from position delta
        if (deltaTime > 0.0f) {
            // Right controller velocity
            rt::g_rightController.linearVelocity.x = (rightPose.position.x - rt::g_rightController.prevPosWorld.x) / deltaTime;
            rt::g_rightController.linearVelocity.y = (rightPose.position.y - rt::g_rightController.prevPosWorld.y) / deltaTime;
            rt::g_rightController.linearVelocity.z = (rightPose.position.z - rt::g_rightController.prevPosWorld.z) / deltaTime;

            // Left controller velocity
            rt::g_leftController.linearVelocity.x = (leftPose.position.x - rt::g_leftController.prevPosWorld.x) / deltaTime;
            rt::g_leftController.linearVelocity.y = (leftPose.position.y - rt::g_leftController.prevPosWorld.y) / deltaTime;
            rt::g_leftController.linearVelocity.z = (leftPose.position.z - rt::g_leftController.prevPosWorld.z) / deltaTime;

            // Angular velocity from yaw/pitch delta
            float totalRightYaw = rt::g_headYaw + rt::g_rightController.yawOffset;
            float totalRightPitch = rt::g_headPitch + rt::g_rightController.pitchOffset;
            rt::g_rightController.angularVelocity.x = (totalRightPitch - rt::g_rightController.prevPitch) / deltaTime;
            rt::g_rightController.angularVelocity.y = (totalRightYaw - rt::g_rightController.prevYaw) / deltaTime;
            rt::g_rightController.angularVelocity.z = 0.0f;

            float totalLeftYaw = rt::g_headYaw + rt::g_leftController.yawOffset;
            float totalLeftPitch = rt::g_headPitch + rt::g_leftController.pitchOffset;
            rt::g_leftController.angularVelocity.x = (totalLeftPitch - rt::g_leftController.prevPitch) / deltaTime;
            rt::g_leftController.angularVelocity.y = (totalLeftYaw - rt::g_leftController.prevYaw) / deltaTime;
            rt::g_leftController.angularVelocity.z = 0.0f;

            // Update previous state for next frame
            rt::g_rightController.prevPosWorld = rightPose.position;
            rt::g_rightController.prevYaw = totalRightYaw;
            rt::g_rightController.prevPitch = totalRightPitch;

            rt::g_leftController.prevPosWorld = leftPose.position;
            rt::g_leftController.prevYaw = totalLeftYaw;
            rt::g_leftController.prevPitch = totalLeftPitch;
        }

    }

    // MCP commands work regardless of window focus (file-based IPC).
    // RefreshCommandsDue keeps this whole block off the frame path until the command
    // directory actually changes; see its comment for why that matters.
    mcp::RefreshCommandsDue();
    if (mcp::g_commandsDue) {
        mcp::HeadPoseCommand cmd = mcp::CheckHeadPoseCommand();
        if (cmd.valid) {
            rt::g_headPos.x = cmd.x;
            rt::g_headPos.y = cmd.y;
            rt::g_headPos.z = cmd.z;
            rt::g_headYaw = cmd.yaw;
            rt::g_headPitch = cmd.pitch;
            if (cmd.hasRoll) rt::g_headRoll = cmd.roll;
            mcp::WriteCommandAck("head_pose", true);
        }

        // Per-eye FOV override (asymmetric headset profile testing).
        mcp::FovCommand fov = mcp::CheckFovCommand();
        if (fov.valid) {
            if (fov.clear) {
                rt::g_useCustomFov = false;
            } else {
                rt::g_useCustomFov = true;
                for (int i = 0; i < 2; ++i) {
                    rt::g_eyeFovL[i] = fov.angleLeft[i];
                    rt::g_eyeFovR[i] = fov.angleRight[i];
                    rt::g_eyeFovU[i] = fov.angleUp[i];
                    rt::g_eyeFovD[i] = fov.angleDown[i];
                }
            }
            mcp::WriteCommandAck("fov", true);
        }

        // Settable IPD (test app's response to IPD ranges; 0mm = no parallax).
        mcp::IpdCommand ipdCmd = mcp::CheckIpdCommand();
        if (ipdCmd.valid) {
            if (ipdCmd.clear) {
                rt::g_useCustomIpd = false;
            } else {
                rt::g_useCustomIpd = true;
                rt::g_customIpd = ipdCmd.ipdMeters;
            }
            mcp::WriteCommandAck("ipd", true);
        }

        // Headset profile preset: applies both FOV and IPD at once.
        mcp::HeadsetProfileCommand prof = mcp::CheckHeadsetProfileCommand();
        if (prof.valid) {
            const int idx = ui::FindHeadsetSpec(prof.name);
            // "generic" carries no headset geometry, so it means the same as clearing.
            const bool clear = idx == 0 ||
                               strcmp(prof.name, "default") == 0 ||
                               strcmp(prof.name, "clear") == 0;
            bool applied = true;
            if (clear) {
                rt::g_useCustomFov = false;
                rt::g_useCustomIpd = false;
            } else if (idx > 0) {
                const float DEG2RAD = 3.14159265f / 180.0f;
                const ui::HeadsetSpec& spec = ui::kHeadsetSpecs[idx];
                rt::g_useCustomFov = true;
                for (int i = 0; i < 2; ++i) {
                    const ui::EyeFov& e = spec.eye[i];
                    rt::g_eyeFovL[i] = e.angleLeft  * DEG2RAD;
                    rt::g_eyeFovR[i] = e.angleRight * DEG2RAD;
                    rt::g_eyeFovU[i] = e.angleUp    * DEG2RAD;
                    rt::g_eyeFovD[i] = e.angleDown  * DEG2RAD;
                }
                rt::g_useCustomIpd = true;
                rt::g_customIpd = spec.ipdMm * 0.001f;
            } else {
                applied = false;
            }
            mcp::WriteCommandAck("headset_profile", applied);
        }

        // Anaglyph preview overlay toggle. The simulator already renders
        // a red/cyan anaglyph composite when ui::g_uiState.displayLayout
        // == DisplayLayout::Anaglyph (using the anaglyphRedBS / anaglyphCyanBS
        // blend states), so we just flip the enum. Remember the previous
        // layout so we can restore it on disable.
        mcp::AnaglyphCommand ana = mcp::CheckAnaglyphCommand();
        if (ana.valid) {
            static ui::DisplayLayout s_savedLayout = ui::DisplayLayout::SideBySide;
            static bool s_haveSaved = false;
            if (ana.enabled) {
                if (!s_haveSaved) {
                    s_savedLayout = ui::g_uiState.displayLayout;
                    s_haveSaved = true;
                }
                ui::g_uiState.displayLayout = ui::DisplayLayout::Anaglyph;
            } else {
                if (s_haveSaved) {
                    ui::g_uiState.displayLayout = s_savedLayout;
                    s_haveSaved = false;
                }
            }
            rt::g_anaglyphPreview = ana.enabled;
            mcp::WriteCommandAck("anaglyph", true);
        }

        // Pose sweep: oscillate yaw/pitch/roll on a sine wave each frame.
        mcp::PoseSweepCommand sweep = mcp::CheckPoseSweepCommand();
        if (sweep.valid) {
            const float DEG2RAD = 3.14159265f / 180.0f;
            rt::g_poseSweepEnabled  = sweep.enabled;
            rt::g_poseSweepYawAmp   = sweep.yawAmpDeg   * DEG2RAD;
            rt::g_poseSweepPitchAmp = sweep.pitchAmpDeg * DEG2RAD;
            rt::g_poseSweepRollAmp  = sweep.rollAmpDeg  * DEG2RAD;
            rt::g_poseSweepFreq     = sweep.freqHz;
            rt::g_poseSweepStartT   = (float)GetTickCount64() * 0.001f;
            mcp::WriteCommandAck("pose_sweep", true);
        }

        mcp::ControllerPoseCommand ctrlCmd = mcp::CheckControllerPoseCommand();
        if (ctrlCmd.valid) {
            rt::ControllerState& ctrl = (ctrlCmd.hand == 0) ? rt::g_leftController : rt::g_rightController;
            ctrl.posOffset.x = ctrlCmd.posX;
            ctrl.posOffset.y = ctrlCmd.posY;
            ctrl.posOffset.z = ctrlCmd.posZ;
            ctrl.yawOffset = ctrlCmd.yaw;
            ctrl.pitchOffset = ctrlCmd.pitch;
            if (ctrlCmd.triggerSet) {
                ctrl.triggerValue = ctrlCmd.trigger;
                ctrl.triggerPressed = (ctrlCmd.trigger >= 0.5f);
            }
            if (ctrlCmd.buttonA >= 0) {
                ctrl.primaryPressed = (ctrlCmd.buttonA != 0);
            }
            mcp::WriteCommandAck("controller_pose", true);
        }
    }

    // Pace on a high-resolution waitable timer. Plain Sleep() rounds up to the
    // system timer interval — as much as 15.6ms when the host never raised it —
    // which quietly turned this 90 Hz cap into a ~60 Hz one. The timer is exact
    // to a fraction of a millisecond regardless of the global timer resolution,
    // and a short yield loop closes the remainder.
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
    static HANDLE pacingTimer = CreateWaitableTimerExW(nullptr, nullptr,
        CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    for (;;) {
        LARGE_INTEGER now; QueryPerformanceCounter(&now);
        const double remainingSec = (nextTick - (double)now.QuadPart) / (double)freq.QuadPart;
        if (remainingSec <= 0.0) break;
        if (pacingTimer && remainingSec > 0.0008) {
            LARGE_INTEGER due;
            due.QuadPart = -(LONGLONG)((remainingSec - 0.0005) * 1.0e7);   // relative, 100ns units
            if (SetWaitableTimer(pacingTimer, &due, 0, nullptr, nullptr, FALSE)) {
                WaitForSingleObject(pacingTimer, 20);
                continue;
            }
        }
        // Final stretch, or Windows before high-resolution timers: yield rather
        // than risk oversleeping a whole timer interval.
        Sleep(remainingSec > 0.002 ? 1 : 0);
    }
    nextTick += periodSec * (double)freq.QuadPart;
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    // After a stall, re-base instead of letting the app run uncapped while
    // nextTick catches up one period per call.
    if ((double)now.QuadPart - nextTick > periodSec * (double)freq.QuadPart) {
        nextTick = (double)now.QuadPart;
    }
    // Convert QPC to nanoseconds using double to avoid overflow on MSVC
    XrTime nowTime = (XrTime)((double)now.QuadPart * 1000000000.0 / (double)freq.QuadPart);
    s->type = XR_TYPE_FRAME_STATE; s->shouldRender = XR_TRUE; s->predictedDisplayPeriod = periodNs; s->predictedDisplayTime = nowTime + periodNs;
    return XR_SUCCESS;
}
static XrResult XRAPI_PTR xrBeginFrame_runtime(XrSession, const XrFrameBeginInfo*) { return XR_SUCCESS; }

// Ensure the preview window exists (create or adopt the persistent one) and
// pick a sensible initial outer size if it's brand new. The window size is
// NOT touched on subsequent calls — once it exists, the user (or the menu
// resize callback) owns its size.
static void ensurePreviewWindow(rt::Session& s, UINT initialClientW, UINT initialClientH) {
    if (s.hwnd) return;

    // Register window class if not done (use global flag)
    if (!rt::g_windowClassRegistered) {
        WNDCLASSW wc{};
        wc.lpfnWndProc = rt::WndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"OpenXR Simulator";
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
        RegisterClassW(&wc);
        rt::g_windowClassRegistered = true;
    }

    // Try to adopt a persistent window from a previous DLL load
    {
        std::lock_guard<std::mutex> lock(rt::g_windowMutex);
        if (rt::g_persistentWindow && IsWindow(rt::g_persistentWindow)) {
            s.hwnd = rt::g_persistentWindow;
            // After DLL reload, the old WndProc pointer is invalid
            SetWindowLongPtrW(s.hwnd, GWLP_WNDPROC, (LONG_PTR)rt::WndProc);
            Log("[SimXR] Updated window WndProc to new DLL address");
            ui::ApplyDarkTheme(s.hwnd);
            ShowWindow(s.hwnd, SW_SHOW);
            UpdateWindow(s.hwnd);
            // Seed clientWidth/Height from the actual current client area
            RECT cr{};
            if (GetClientRect(s.hwnd, &cr)) {
                UINT cw = (UINT)(cr.right - cr.left);
                UINT ch = (UINT)(cr.bottom - cr.top);
                if (cw > 0 && ch > 0) {
                    s.clientWidth.store(cw);
                    s.clientHeight.store(ch);
                }
            }
            return;
        }
    }

    // No persistent window — create a fresh one at the requested initial size
    RECT rc = { 0, 0, (LONG)initialClientW, (LONG)initialClientH };
    AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
    s.hwnd = CreateWindowExW(0, L"OpenXR Simulator", L"OpenXR Simulator (Mouse Look + WASD)", WS_OVERLAPPEDWINDOW,
                             100, 100, rc.right - rc.left, rc.bottom - rc.top, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (!s.hwnd) {
        Log("[SimXR] Failed to create preview window!");
        return;
    }
    ShowWindow(s.hwnd, SW_SHOW);
    UpdateWindow(s.hwnd);
    SetForegroundWindow(s.hwnd);
    SetWindowPos(s.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
    Logf("[SimXR] Created new preview window: hwnd=%p clientSize=%ux%u",
         s.hwnd, initialClientW, initialClientH);

    ui::ApplyDarkTheme(s.hwnd);
    ui::g_uiState.windowWidth = initialClientW;
    ui::g_uiState.windowHeight = initialClientH;
    s.clientWidth.store(initialClientW);
    s.clientHeight.store(initialClientH);

    {
        std::lock_guard<std::mutex> lock(rt::g_windowMutex);
        rt::g_persistentWindow = s.hwnd;
        Log("[SimXR] Saved new window to persistent storage");
    }
}

// The D3D12 preview's scaling pass. Draws one eye into a viewport, taking its
// submitted subimage rect through uvOffset/uvScale.
//
// Four bilinear taps on a half-texel grid average the 4x4 source footprint that a
// 2-4x downscale to the window covers. A single tap only reaches 2x2 of that and
// shimmers on any high-frequency detail; anything wider costs more than the whole
// pass is worth.
static const char* kPreviewBlitHLSL = R"HLSL(
cbuffer Blit : register(b0) {
    float2 uvOffset;
    float2 uvScale;
    float2 uvTap;
    float  srgbEncode;
    float  pad;
};
Texture2D<float4> Src : register(t0);
SamplerState Smp : register(s0);

struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

VSOut VSMain(uint id : SV_VertexID) {
    VSOut o;
    float2 p = float2((id << 1) & 2, id & 2);
    o.uv = uvOffset + p * uvScale;
    o.pos = float4(p.x * 2.0 - 1.0, 1.0 - p.y * 2.0, 0.0, 1.0);
    return o;
}

float4 PSMain(VSOut i) : SV_Target {
    float3 c = Src.Sample(Smp, i.uv + float2(-uvTap.x, -uvTap.y)).rgb
             + Src.Sample(Smp, i.uv + float2( uvTap.x, -uvTap.y)).rgb
             + Src.Sample(Smp, i.uv + float2(-uvTap.x,  uvTap.y)).rgb
             + Src.Sample(Smp, i.uv + float2( uvTap.x,  uvTap.y)).rgb;
    c *= 0.25;
    // Float swapchains hold linear light, so they need the transfer curve the 8-bit
    // back buffer is displayed through. 8-bit sources are already encoded and are
    // filtered in that encoding, which is what the GDI stretch this replaced did.
    if (srgbEncode > 0.5) {
        c = saturate(c);
        c = (c <= 0.0031308) ? c * 12.92 : 1.055 * pow(c, 1.0 / 2.4) - 0.055;
    }
    return float4(c, 1.0);
}
)HLSL";

// Root signature, PSO and descriptor heaps for the scaling pass. Size-independent,
// so this survives the RT being rebuilt on a window resize.
// Quad layers used to be composited on the CPU: the whole layer texture was read back and
// every destination pixel inverse-mapped through the quad's plane by hand. That cost scaled
// with the mirror window's area - 11ms a frame at 2340x1252 with BetterVR's full-resolution
// HUD - and it was the last real overhead the runtime imposed on the app.
//
// The rasteriser does the same job for free. The corners arrive already in the eye's view
// space, so the VS only has to apply the eye's asymmetric projection; emitting a real w
// (rather than screen-space corners) is what makes the interpolation perspective-correct,
// and lets the clipper handle a quad that crosses behind the eye - the case the CPU version
// had to bail out of by scanning the whole eye rect.
//
// Blending happens in linear light because the RT is bound through an sRGB RTV here: the
// hardware decodes the destination, blends, and re-encodes. That is the property the CPU
// compositor existed to provide, and the same one the D3D11 path gets from its own sRGB RTV.
static const char* kPreviewQuadHLSL = R"HLSL(
cbuffer Quad : register(b0) {
    float4 c0;        // view-space corner 0 (top-left),     w unused
    float4 c1;        // view-space corner 1 (top-right)
    float4 c2;        // view-space corner 2 (bottom-right)
    float4 c3;        // view-space corner 3 (bottom-left)
    float4 tans;      // tanLeft, tanRight, tanUp, tanDown
    float4 uvRect;    // u0, v0, u1, v1
    float4 opts;      // x: 1 = opaque, ignore the source alpha
};
Texture2D<float4> Src : register(t0);
SamplerState Smp : register(s0);

struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

VSOut VSMain(uint id : SV_VertexID) {
    // Two triangles over corners 0,1,2,3 wound 0-1-2, 0-2-3.
    const uint idx[6] = { 0, 1, 2, 0, 2, 3 };
    uint c = idx[id];
    float3 v = (c == 0) ? c0.xyz : (c == 1) ? c1.xyz : (c == 2) ? c2.xyz : c3.xyz;
    float2 uv01 = (c == 0) ? float2(0, 0) : (c == 1) ? float2(1, 0)
                : (c == 2) ? float2(1, 1) : float2(0, 1);

    // Asymmetric perspective at a near plane of 1, so the tangents are the plane extents.
    float L = tans.x, R = tans.y, U = tans.z, D = tans.w;
    VSOut o;
    o.pos.x = (2.0 * v.x + (R + L) * v.z) / (R - L);
    o.pos.y = (2.0 * v.y + (U + D) * v.z) / (U - D);
    // Depth is unused (no depth buffer); 0 sits on the near plane and always survives the
    // clip test, while w = -z lets the clipper cut anything at or behind the eye.
    o.pos.z = 0.0;
    o.pos.w = -v.z;
    o.uv = lerp(uvRect.xy, uvRect.zw, uv01);
    return o;
}

float4 PSMain(VSOut i) : SV_Target {
    float4 c = Src.Sample(Smp, i.uv);
    // Premultiplied and straight alpha differ only in the blend state's SrcBlend, so the
    // sample goes through untouched either way; opaque is the one that needs saying here.
    return (opts.x > 0.5) ? float4(c.rgb, 1.0) : c;
}
)HLSL";

// SRV format for sampling a quad layer. Unlike the eye pass - which filters the stored bytes
// in whatever encoding they are in and writes them straight through - the quad is blended, so
// the sample has to come back as linear light. Keeping the sRGB view is what does that.
static DXGI_FORMAT PreviewQuadSrvFormat(DXGI_FORMAT resourceFormat) {
    switch (resourceFormat) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS:       return DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS:       return DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS:   return DXGI_FORMAT_R16G16B16A16_FLOAT;
        case DXGI_FORMAT_R32G32B32A32_TYPELESS:   return DXGI_FORMAT_R32G32B32A32_FLOAT;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS:    return DXGI_FORMAT_R10G10B10A2_UNORM;
        default:                                  return resourceFormat;
    }
}

static bool ensurePreviewQuadPipeline(rt::Session& s) {
    if (s.previewQuadRootSig && s.previewQuadPSO[0]) return true;
    if (!s.d3d12Device) return false;

    D3D12_DESCRIPTOR_RANGE srvRange = {};
    srvRange.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    srvRange.NumDescriptors = 1;
    srvRange.BaseShaderRegister = 0;
    srvRange.OffsetInDescriptorsFromTableStart = D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND;

    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable.NumDescriptorRanges = 1;
    params[0].DescriptorTable.pDescriptorRanges = &srvRange;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants.ShaderRegister = 0;
    params[1].Constants.Num32BitValues = rt::kQuadConstantCount;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_STATIC_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressV = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.MaxLOD = D3D12_FLOAT32_MAX;
    sampler.ShaderRegister = 0;
    sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;

    D3D12_ROOT_SIGNATURE_DESC rsDesc = {};
    rsDesc.NumParameters = 2;
    rsDesc.pParameters = params;
    rsDesc.NumStaticSamplers = 1;
    rsDesc.pStaticSamplers = &sampler;
    rsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;

    ComPtr<ID3DBlob> rsBlob, rsError;
    HRESULT hr = D3D12SerializeRootSignature(&rsDesc, D3D_ROOT_SIGNATURE_VERSION_1,
                                             rsBlob.GetAddressOf(), rsError.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 quad: SerializeRootSignature failed 0x%08X (%s)", (unsigned)hr,
             rsError ? (const char*)rsError->GetBufferPointer() : "");
        return false;
    }
    hr = s.d3d12Device->CreateRootSignature(0, rsBlob->GetBufferPointer(), rsBlob->GetBufferSize(),
                                            IID_PPV_ARGS(s.previewQuadRootSig.GetAddressOf()));
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 quad: CreateRootSignature failed 0x%08X", (unsigned)hr);
        return false;
    }

    ComPtr<ID3DBlob> vs, ps, err;
    const UINT flags = D3DCOMPILE_OPTIMIZATION_LEVEL3;
    hr = D3DCompile(kPreviewQuadHLSL, strlen(kPreviewQuadHLSL), "PreviewQuad", nullptr, nullptr,
                    "VSMain", "vs_5_0", flags, 0, vs.GetAddressOf(), err.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 quad: VS compile failed 0x%08X (%s)", (unsigned)hr,
             err ? (const char*)err->GetBufferPointer() : "");
        return false;
    }
    err.Reset();
    hr = D3DCompile(kPreviewQuadHLSL, strlen(kPreviewQuadHLSL), "PreviewQuad", nullptr, nullptr,
                    "PSMain", "ps_5_0", flags, 0, ps.GetAddressOf(), err.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 quad: PS compile failed 0x%08X (%s)", (unsigned)hr,
             err ? (const char*)err->GetBufferPointer() : "");
        return false;
    }

    // One PSO per blend mode, indexed by rt::LayerBlend.
    for (int mode = 0; mode < 3; ++mode) {
        D3D12_GRAPHICS_PIPELINE_STATE_DESC pso = {};
        pso.pRootSignature = s.previewQuadRootSig.Get();
        pso.VS = { vs->GetBufferPointer(), vs->GetBufferSize() };
        pso.PS = { ps->GetBufferPointer(), ps->GetBufferSize() };
        D3D12_RENDER_TARGET_BLEND_DESC& rtb = pso.BlendState.RenderTarget[0];
        rtb.RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        if (mode != (int)rt::LayerBlend::Opaque) {
            rtb.BlendEnable = TRUE;
            rtb.SrcBlend = (mode == (int)rt::LayerBlend::Premultiplied)
                         ? D3D12_BLEND_ONE : D3D12_BLEND_SRC_ALPHA;
            rtb.DestBlend = D3D12_BLEND_INV_SRC_ALPHA;
            rtb.BlendOp = D3D12_BLEND_OP_ADD;
            rtb.SrcBlendAlpha = D3D12_BLEND_ONE;
            rtb.DestBlendAlpha = D3D12_BLEND_INV_SRC_ALPHA;
            rtb.BlendOpAlpha = D3D12_BLEND_OP_ADD;
        }
        pso.SampleMask = UINT_MAX;
        pso.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
        pso.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        pso.RasterizerState.DepthClipEnable = TRUE;
        pso.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        pso.NumRenderTargets = 1;
        // sRGB view over the same bytes the eye pass wrote, so the blend runs in linear light.
        pso.RTVFormats[0] = rt::kPreviewRTFormatSrgb;
        pso.SampleDesc.Count = 1;
        hr = s.d3d12Device->CreateGraphicsPipelineState(&pso, IID_PPV_ARGS(s.previewQuadPSO[mode].GetAddressOf()));
        if (FAILED(hr)) {
            Logf("[SimXR] DX12 quad: CreateGraphicsPipelineState(%d) failed 0x%08X", mode, (unsigned)hr);
            return false;
        }
    }

    Log("[SimXR] DX12 quad: GPU compositing pipeline created");
    return true;
}

static bool ensurePreviewBlitPipeline(rt::Session& s) {
    if (s.previewPSO && s.previewRootSig && s.previewSrvHeap) return true;
    if (!s.d3d12Device) return false;

    D3D12_DESCRIPTOR_RANGE srvRange = {};
    srvRange.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    srvRange.NumDescriptors = 1;
    srvRange.BaseShaderRegister = 0;
    srvRange.OffsetInDescriptorsFromTableStart = D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND;

    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable.NumDescriptorRanges = 1;
    params[0].DescriptorTable.pDescriptorRanges = &srvRange;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants.ShaderRegister = 0;
    params[1].Constants.Num32BitValues = 8;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_STATIC_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressV = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.MaxLOD = D3D12_FLOAT32_MAX;
    sampler.ShaderRegister = 0;
    sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;

    D3D12_ROOT_SIGNATURE_DESC rsDesc = {};
    rsDesc.NumParameters = 2;
    rsDesc.pParameters = params;
    rsDesc.NumStaticSamplers = 1;
    rsDesc.pStaticSamplers = &sampler;
    rsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;

    ComPtr<ID3DBlob> rsBlob, rsError;
    HRESULT hr = D3D12SerializeRootSignature(&rsDesc, D3D_ROOT_SIGNATURE_VERSION_1,
                                             rsBlob.GetAddressOf(), rsError.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: SerializeRootSignature failed 0x%08X (%s)", (unsigned)hr,
             rsError ? (const char*)rsError->GetBufferPointer() : "");
        return false;
    }
    hr = s.d3d12Device->CreateRootSignature(0, rsBlob->GetBufferPointer(), rsBlob->GetBufferSize(),
                                            IID_PPV_ARGS(s.previewRootSig.GetAddressOf()));
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: CreateRootSignature failed 0x%08X", (unsigned)hr);
        return false;
    }

    ComPtr<ID3DBlob> vs, ps, err;
    const UINT flags = D3DCOMPILE_OPTIMIZATION_LEVEL3;
    hr = D3DCompile(kPreviewBlitHLSL, strlen(kPreviewBlitHLSL), "PreviewBlit", nullptr, nullptr,
                    "VSMain", "vs_5_0", flags, 0, vs.GetAddressOf(), err.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: VS compile failed 0x%08X (%s)", (unsigned)hr,
             err ? (const char*)err->GetBufferPointer() : "");
        return false;
    }
    err.Reset();
    hr = D3DCompile(kPreviewBlitHLSL, strlen(kPreviewBlitHLSL), "PreviewBlit", nullptr, nullptr,
                    "PSMain", "ps_5_0", flags, 0, ps.GetAddressOf(), err.GetAddressOf());
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: PS compile failed 0x%08X (%s)", (unsigned)hr,
             err ? (const char*)err->GetBufferPointer() : "");
        return false;
    }

    D3D12_GRAPHICS_PIPELINE_STATE_DESC pso = {};
    pso.pRootSignature = s.previewRootSig.Get();
    pso.VS = { vs->GetBufferPointer(), vs->GetBufferSize() };
    pso.PS = { ps->GetBufferPointer(), ps->GetBufferSize() };
    pso.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pso.SampleMask = UINT_MAX;
    pso.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pso.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pso.RasterizerState.DepthClipEnable = TRUE;
    pso.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pso.NumRenderTargets = 1;
    pso.RTVFormats[0] = rt::kPreviewRTFormat;
    pso.SampleDesc.Count = 1;
    hr = s.d3d12Device->CreateGraphicsPipelineState(&pso, IID_PPV_ARGS(s.previewPSO.GetAddressOf()));
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: CreateGraphicsPipelineState failed 0x%08X", (unsigned)hr);
        return false;
    }

    // See kPreviewSrvSlots for how the ring is sized against the frames in flight.
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc = {};
    heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    heapDesc.NumDescriptors = rt::kPreviewSrvSlots;
    heapDesc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = s.d3d12Device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(s.previewSrvHeap.GetAddressOf()));
    if (FAILED(hr)) {
        Logf("[SimXR] DX12 preview: CreateDescriptorHeap(SRV) failed 0x%08X", (unsigned)hr);
        return false;
    }
    s.previewSrvStride = s.d3d12Device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    s.previewSrvSlot = 0;

    Log("[SimXR] DX12 preview: scaling pipeline created");
    return true;
}

// Make sure the preview backing store (D3D11 swapchain or D3D12 offscreen RT)
// is sized to `width × height` and uses `format`. For D3D11 this means
// ResizeBuffers (or recreate) so the backbuffer matches the window client area;
// for D3D12 it means resizing the offscreen RT/readback to the natural canvas
// size chosen by the caller. The window itself is NOT touched here.
static void ensurePreviewSized(rt::Session& s, UINT width, UINT height, DXGI_FORMAT format) {
    if (width == 0) width = 1;
    if (height == 0) height = 1;

    // Make sure we have a window first
    ensurePreviewWindow(s, width, height);
    if (!s.hwnd) return;

    // After a DLL reload the Session is fresh but a previous swapchain may
    // still be cached against the persistent HWND — adopt it so we don't try
    // to create a second swapchain for the same window (which DXGI forbids).
    if (!s.usesD3D12 && !s.previewSwapchain) {
        std::lock_guard<std::mutex> lock(rt::g_windowMutex);
        if (rt::g_persistentSwapchain) {
            s.previewSwapchain = rt::g_persistentSwapchain;
            DXGI_SWAP_CHAIN_DESC1 d{};
            if (SUCCEEDED(s.previewSwapchain->GetDesc1(&d))) {
                s.previewWidth = d.Width;
                s.previewHeight = d.Height;
                s.previewFormat = d.Format;
            } else {
                s.previewWidth = rt::g_persistentWidth;
                s.previewHeight = rt::g_persistentHeight;
            }
        }
    }

    if (!s.usesD3D12) {
        if (s.previewSwapchain && s.previewWidth == width && s.previewHeight == height && s.previewFormat == format) return;
    } else {
        // The D3D12 RT is kPreviewRTFormat whatever the app submits - the scaling pass
        // converts - so only a size change can force a rebuild.
        if (s.previewRT12 && s.previewWidth == width && s.previewHeight == height) return;
    }

    if (!s.usesD3D12) {
        // D3D11/GL path: try ResizeBuffers if the format hasn't changed and we
        // already have a swapchain. Otherwise recreate.
        if (s.previewSwapchain && s.previewFormat == format) {
            HRESULT hr = s.previewSwapchain->ResizeBuffers(0, width, height, DXGI_FORMAT_UNKNOWN, 0);
            if (SUCCEEDED(hr)) {
                s.previewWidth = width;
                s.previewHeight = height;
                {
                    std::lock_guard<std::mutex> lock(rt::g_windowMutex);
                    rt::g_persistentSwapchain = s.previewSwapchain;
                    rt::g_persistentWidth = width;
                    rt::g_persistentHeight = height;
                }
                return;
            }
            Logf("[SimXR] ensurePreviewSized: ResizeBuffers failed 0x%08X, recreating swapchain", (unsigned)hr);
        }

        // Recreate from scratch (format changed, first call, or ResizeBuffers failed)
        s.previewSwapchain.Reset();
        {
            std::lock_guard<std::mutex> lock(rt::g_windowMutex);
            rt::g_persistentSwapchain.Reset();
        }
        s.previewWidth = width;
        s.previewHeight = height;
        s.previewFormat = format;

        ComPtr<IDXGIDevice> dxgiDev; s.d3d11Device.As(&dxgiDev);
        ComPtr<IDXGIAdapter> adapter; dxgiDev->GetAdapter(adapter.GetAddressOf());
        ComPtr<IDXGIFactory2> factory; adapter->GetParent(IID_PPV_ARGS(factory.GetAddressOf()));
        DXGI_SWAP_CHAIN_DESC1 desc{};
        desc.Format = format;
        desc.Width = width;
        desc.Height = height;
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        desc.BufferCount = 2;
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        desc.Scaling = DXGI_SCALING_STRETCH;
        desc.SampleDesc.Count = 1;
        HRESULT hr = factory->CreateSwapChainForHwnd(s.d3d11Device.Get(), s.hwnd, &desc, nullptr, nullptr, s.previewSwapchain.GetAddressOf());
        Logf("[SimXR] ensurePreviewSized(DX11): hr=0x%08X swapchain=%p size=%ux%u format=%d",
             (unsigned)hr, s.previewSwapchain.Get(), width, height, format);
        if (FAILED(hr)) {
            Logf("[SimXR] ERROR: Failed to create DX11 preview swapchain with format %d", format);
        } else {
            std::lock_guard<std::mutex> lock(rt::g_windowMutex);
            rt::g_persistentSwapchain = s.previewSwapchain;
            rt::g_persistentWidth = width;
            rt::g_persistentHeight = height;
        }
        return;
    } else {
        // D3D12 path: rebuild the offscreen RT/readback at the requested size
        rt::ResetD3D12PreviewSurfaces(s);
        s.previewWidth = width;
        s.previewHeight = height;
        s.previewFormat = rt::kPreviewRTFormat;
        // DX12 preview: GDI-based rendering to avoid DXGI Present hook conflicts.
        // Steam overlay (gameoverlayrenderer64) and UEVR both hook IDXGISwapChain::Present,
        // creating infinite recursion → EXCEPTION_STACK_OVERFLOW. Instead, we scale the
        // eyes into an offscreen RT the size of the window, read that back, and copy it
        // into the window's DIB section.
        if (!ensurePreviewBlitPipeline(s)) return;
        if (!ensurePreviewQuadPipeline(s)) return;
        if (!s.previewQueue12) {
            D3D12_COMMAND_QUEUE_DESC qd = {};
            qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
            qd.Flags = D3D12_COMMAND_QUEUE_FLAG_NONE;
            if (FAILED(s.d3d12Device->CreateCommandQueue(&qd, IID_PPV_ARGS(s.previewQueue12.GetAddressOf())))) {
                Log("[SimXR] DX12 preview: CreateCommandQueue failed");
                return;
            }
            s.d3d12Device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(s.crossQueueFence.GetAddressOf()));
            s.crossQueueFenceValue = 0;
            Log("[SimXR] DX12 preview: Created command queue + cross-queue fence");
        }

        // Create offscreen render target
        D3D12_RESOURCE_DESC rtDesc = {};
        rtDesc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        rtDesc.Width = width;
        rtDesc.Height = height;
        rtDesc.DepthOrArraySize = 1;
        rtDesc.MipLevels = 1;
        rtDesc.Format = rt::kPreviewRTFormatTypeless;
        rtDesc.SampleDesc.Count = 1;
        rtDesc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        rtDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
        D3D12_CLEAR_VALUE rtClear = {};
        rtClear.Format = rt::kPreviewRTFormat;
        rtClear.Color[3] = 1.0f;
        D3D12_HEAP_PROPERTIES defaultHeap = {};
        defaultHeap.Type = D3D12_HEAP_TYPE_DEFAULT;
        HRESULT hr = s.d3d12Device->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE,
            &rtDesc, D3D12_RESOURCE_STATE_COMMON, &rtClear, IID_PPV_ARGS(s.previewRT12.GetAddressOf()));
        if (FAILED(hr)) {
            Logf("[SimXR] DX12 preview: CreateCommittedResource (RT) failed 0x%08X", (unsigned)hr);
            return;
        }

        D3D12_DESCRIPTOR_HEAP_DESC rtvHeapDesc = {};
        rtvHeapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
        rtvHeapDesc.NumDescriptors = 2;
        hr = s.d3d12Device->CreateDescriptorHeap(&rtvHeapDesc, IID_PPV_ARGS(s.previewRtvHeap.GetAddressOf()));
        if (FAILED(hr)) {
            Logf("[SimXR] DX12 preview: CreateDescriptorHeap(RTV) failed 0x%08X", (unsigned)hr);
            s.previewRT12.Reset();
            return;
        }
        s.previewRtvStride = s.d3d12Device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
        // [0] plain view: the eye pass writes already-encoded bytes through unchanged.
        // [1] sRGB view: quad blending decodes/re-encodes so the blend is in linear light.
        D3D12_CPU_DESCRIPTOR_HANDLE rtvBase = s.previewRtvHeap->GetCPUDescriptorHandleForHeapStart();
        D3D12_RENDER_TARGET_VIEW_DESC rtvDesc = {};
        rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
        rtvDesc.Format = rt::kPreviewRTFormat;
        s.d3d12Device->CreateRenderTargetView(s.previewRT12.Get(), &rtvDesc, rtvBase);
        rtvDesc.Format = rt::kPreviewRTFormatSrgb;
        D3D12_CPU_DESCRIPTOR_HANDLE rtvSrgb = rtvBase;
        rtvSrgb.ptr += s.previewRtvStride;
        s.d3d12Device->CreateRenderTargetView(s.previewRT12.Get(), &rtvDesc, rtvSrgb);

        // One readback buffer per frame in flight (aligned row pitch), so a finished
        // frame can be mapped while the next one is still being written by the GPU.
        UINT rowPitch = ((width * 4 + D3D12_TEXTURE_DATA_PITCH_ALIGNMENT - 1)
                        / D3D12_TEXTURE_DATA_PITCH_ALIGNMENT) * D3D12_TEXTURE_DATA_PITCH_ALIGNMENT;
        s.previewReadbackPitch = rowPitch;
        D3D12_RESOURCE_DESC readbackDesc = {};
        readbackDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        readbackDesc.Width = (UINT64)rowPitch * height;
        readbackDesc.Height = 1;
        readbackDesc.DepthOrArraySize = 1;
        readbackDesc.MipLevels = 1;
        readbackDesc.Format = DXGI_FORMAT_UNKNOWN;
        readbackDesc.SampleDesc.Count = 1;
        readbackDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        D3D12_HEAP_PROPERTIES readbackHeap = {};
        readbackHeap.Type = D3D12_HEAP_TYPE_READBACK;
        for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
            hr = s.d3d12Device->CreateCommittedResource(&readbackHeap, D3D12_HEAP_FLAG_NONE,
                &readbackDesc, D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                IID_PPV_ARGS(s.previewFrames[i].readback.ReleaseAndGetAddressOf()));
            if (FAILED(hr)) {
                Logf("[SimXR] DX12 preview: CreateCommittedResource (readback %u) failed 0x%08X", i, (unsigned)hr);
                s.previewRT12.Reset();
                return;
            }
        }

        // Command list, per-slot allocators and the fence are size-independent, so a
        // resize keeps them and the fence counter carries on where it was.
        for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
            if (!s.previewFrames[i].alloc) {
                s.d3d12Device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                    IID_PPV_ARGS(s.previewFrames[i].alloc.GetAddressOf()));
            }
        }
        if (!s.previewCmdList && s.previewFrames[0].alloc) {
            s.d3d12Device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
                s.previewFrames[0].alloc.Get(), nullptr, IID_PPV_ARGS(s.previewCmdList.GetAddressOf()));
            s.previewCmdList->Close();
        }
        if (!s.previewFence) {
            s.d3d12Device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(s.previewFence.GetAddressOf()));
            s.previewFenceValue = 0;
            s.previewFenceEvent = CreateEvent(nullptr, FALSE, FALSE, nullptr);
        }
        Logf("[SimXR] DX12 preview: GPU-scaled readback initialized (%ux%u, pitch=%u, %u frames in flight)",
             width, height, rowPitch, rt::kPreviewFrames);
        return;
    }
}

// Bring the preview window (and its backing store) up with no projection layer
// to take a size from. Every other route into ensurePreviewSized hangs off
// presentProjection, so an app showing only 2D - BetterVR sits on quad-only
// frames for its whole boot and title sequence - would never get a window at
// all. Sizes from the last projection frame if there was one, otherwise from
// the resolution xrEnumerateViewConfigurationViews recommends.
static void ensurePreviewWithoutProjection(rt::Session& s) {
    std::lock_guard<std::mutex> lock(s.previewMutex);

    int srcW = (int)rt::g_sourceWidth.load();
    int srcH = (int)rt::g_sourceHeight.load();
    if (srcW <= 0 || srcH <= 0) {
        srcW = 1280;
        srcH = 720;
        rt::g_sourceWidth.store((uint32_t)srcW);
        rt::g_sourceHeight.store((uint32_t)srcH);
    }

    // Reopen at the size the window was last left at. With nothing saved -- first run, or
    // a settings file from before this was persisted -- fall back to the panel's shape so
    // the first frame is already the right proportions. Either way clamp to the desktop:
    // two 2064x2208 eyes side by side is wider than any monitor, and the saved size may
    // have come from a larger one. A profile or layout change since makes the restored
    // aspect wrong, which the snap on the first WM_SIZE corrects.
    int windowW = ui::g_uiState.windowWidth;
    int windowH = ui::g_uiState.windowHeight;
    if (windowW <= 0 || windowH <= 0) {
        uint32_t panelW = 0, panelH = 0;
        ui::GetHeadsetPanelResolution(panelW, panelH);
        ui::CalculateWindowSize((int)panelW, (int)panelH, windowW, windowH);
    }
    rt::ClampToWorkArea(windowW, windowH);
    ensurePreviewWindow(s, (UINT)windowW, (UINT)windowH);
    if (!s.hwnd) return;

    // OpenGL sessions create their D3D11 preview device lazily inside
    // presentProjection; until that happens there is nothing to build a
    // swapchain on, so leave the backing store to the first 3D frame.
    if (s.usesD3D12 ? !s.d3d12Device : !s.d3d11Device) return;

    int targetW = srcW, targetH = srcH;
    if (!s.usesD3D12) {
        rt::GetPreviewClientSize(s, srcW, srcH, targetW, targetH);
    } else {
        rt::ComputePreviewRTSize(s, targetW, targetH);
    }
    const DXGI_FORMAT format = (s.previewFormat != DXGI_FORMAT_UNKNOWN) ? s.previewFormat : DXGI_FORMAT_R8G8B8A8_UNORM;
    ensurePreviewSized(s, (UINT)targetW, (UINT)targetH, format);
}

// A cached SRV-only staging texture for the D3D11 mirror. Two entries rotate so the
// left and right eye each keep theirs; a size or format change just recreates one.
static ID3D11Texture2D* acquireTempTexture(rt::Session& s, rt::Session::TempTexEntry* cache,
                                           UINT& next, UINT width, UINT height,
                                           DXGI_FORMAT format, ID3D11ShaderResourceView** outSrv) {
    rt::Session::TempTexEntry& entry = cache[next];
    next = (next + 1) % 2;
    if (entry.texture && entry.width == width && entry.height == height && entry.format == format) {
        *outSrv = entry.srv.Get();
        return entry.texture.Get();
    }
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = format;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    entry = rt::Session::TempTexEntry{};
    if (FAILED(s.d3d11Device->CreateTexture2D(&desc, nullptr, entry.texture.GetAddressOf()))) {
        entry = rt::Session::TempTexEntry{};
        return nullptr;
    }
    D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
    srvDesc.Format = format;
    srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvDesc.Texture2D.MipLevels = 1;
    if (FAILED(s.d3d11Device->CreateShaderResourceView(entry.texture.Get(), &srvDesc,
                                                       entry.srv.GetAddressOf()))) {
        entry = rt::Session::TempTexEntry{};
        return nullptr;
    }
    entry.width = width;
    entry.height = height;
    entry.format = format;
    *outSrv = entry.srv.Get();
    return entry.texture.Get();
}

static void blitViewToHalf(rt::Session& s, rt::Swapchain& chain, uint32_t srcIndex, uint32_t arraySlice,
                           const XrRect2Di& rect, ID3D11RenderTargetView* rtv,
                           const D3D11_VIEWPORT& vp, ID3D11BlendState* blendState) {
    if (!rtv) {
        Log("[SimXR] blitViewToHalf: rtv is null!");
        return;
    }

    if (!rt::InitBlitResources(s)) {
        Log("[SimXR] Cannot blit, blit resources failed to initialize.");
        return;
    }

    // Check if we have a valid image
    if (srcIndex >= chain.images.size()) {
        Logf("[SimXR] blitViewToHalf: srcIndex %u >= images.size() %zu", srcIndex, chain.images.size());
        return;
    }
    if (!chain.images[srcIndex]) {
        Logf("[SimXR] blitViewToHalf: images[%u] is null", srcIndex);
        return;
    }

    // Prepare source texture
    ComPtr<ID3D11Texture2D> sourceTexture = chain.images[srcIndex];
    D3D11_TEXTURE2D_DESC srcDesc;
    sourceTexture->GetDesc(&srcDesc);
    
    // Skip depth formats - they can't be rendered to the preview window
    bool isDepthFormat = (srcDesc.Format == DXGI_FORMAT_D32_FLOAT ||
                          srcDesc.Format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT ||
                          srcDesc.Format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
                          srcDesc.Format == DXGI_FORMAT_D16_UNORM ||
                          srcDesc.Format == DXGI_FORMAT_R32_TYPELESS ||
                          srcDesc.Format == DXGI_FORMAT_R32G8X24_TYPELESS ||
                          srcDesc.Format == DXGI_FORMAT_R24G8_TYPELESS ||
                          srcDesc.Format == DXGI_FORMAT_R16_TYPELESS);
    if (isDepthFormat) {
        // Depth swapchains are for depth testing, not preview rendering
        static int depthSkipCount = 0;
        if (++depthSkipCount % 60 == 1) {
            Logf("[SimXR] blitViewToHalf: Skipping depth format %d", srcDesc.Format);
        }
        return;
    }
    
    // Choose a typed format for SRV and temp texture  
    // Preserve sRGB if original was sRGB to enable auto-conversion
    DXGI_FORMAT typedFormat = srcDesc.Format;
    switch (srcDesc.Format) {
        case DXGI_FORMAT_R8G8B8A8_TYPELESS:
            typedFormat = (chain.format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) ? DXGI_FORMAT_R8G8B8A8_UNORM_SRGB : DXGI_FORMAT_R8G8B8A8_UNORM;
            break;
        case DXGI_FORMAT_B8G8R8A8_TYPELESS:
            typedFormat = (chain.format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) ? DXGI_FORMAT_B8G8R8A8_UNORM_SRGB : DXGI_FORMAT_B8G8R8A8_UNORM;
            break;
        case DXGI_FORMAT_R16G16B16A16_TYPELESS: 
            typedFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; 
            break;
        case DXGI_FORMAT_R32G32B32A32_TYPELESS: 
            typedFormat = DXGI_FORMAT_R32G32B32A32_FLOAT; 
            break;
        case DXGI_FORMAT_R10G10B10A2_TYPELESS:
            typedFormat = DXGI_FORMAT_R10G10B10A2_UNORM;
            break;
        // Typed UNORM source submitted into an sRGB swapchain: the bytes are
        // sRGB-ENCODED, so the SRV must be the matching _SRGB view to decode
        // sRGB->linear on sample. The preview RTV re-encodes linear->sRGB, so
        // without this decode we double-encode (blues boosted -> magenta reads
        // as purple in the preview window).
        case DXGI_FORMAT_R8G8B8A8_UNORM:
            if (chain.format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB)
                typedFormat = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
            break;
        case DXGI_FORMAT_B8G8R8A8_UNORM:
            if (chain.format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB)
                typedFormat = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
            break;
        default:
            break; // Already typed or unknown
    }
    // Copy the correct subresource (handles array slice)
    UINT srcSubresource = D3D11CalcSubresource(0, arraySlice, chain.mipCount);

    // Handle imageRect cropping for better visual accuracy
    // Apps can submit sub-rects, and sampling the full texture can show mostly black areas
    // Skip cropping when showFullRender is enabled to show the entire swapchain
    int32_t rectX = rect.offset.x;
    int32_t rectY = rect.offset.y;
    int32_t rectW = rect.extent.width;
    int32_t rectH = rect.extent.height;
    const int32_t srcW = (int32_t)srcDesc.Width;
    const int32_t srcH = (int32_t)srcDesc.Height;
    bool rectValid = rectW > 0 && rectH > 0;
    bool rectClamped = false;
    if (rectValid) {
        if (rectX < 0) { rectW += rectX; rectX = 0; rectClamped = true; }
        if (rectY < 0) { rectH += rectY; rectY = 0; rectClamped = true; }
        if (rectX >= srcW || rectY >= srcH) { rectValid = false; }
        if (rectValid && rectX + rectW > srcW) { rectW = srcW - rectX; rectClamped = true; }
        if (rectValid && rectY + rectH > srcH) { rectH = srcH - rectY; rectClamped = true; }
        if (rectW <= 0 || rectH <= 0) { rectValid = false; }
    }
    if (!rectValid && (rect.extent.width != 0 || rect.extent.height != 0)) {
        static int invalidRectCount = 0;
        if (++invalidRectCount % 60 == 1) {
            Logf("[SimXR] Invalid imageRect; using full texture (offset=%d,%d extent=%d,%d size=%dx%d)",
                 rect.offset.x, rect.offset.y, rect.extent.width, rect.extent.height, srcW, srcH);
        }
    }
    bool shouldCrop = !ui::g_uiState.showFullRender &&
                      srcDesc.SampleDesc.Count == 1 &&
                      rectValid &&
                      (rectW < srcW || rectH < srcH || rectX != 0 || rectY != 0);
    // The mirror always samples its own SRV-only copy: the app may still have the
    // source bound as an RTV, and D3D11 nulls the SRV if it were sampled directly.
    // The copy lands in a cached texture rather than a freshly created one.
    const UINT tempW = shouldCrop ? (UINT)rectW : srcDesc.Width;
    const UINT tempH = shouldCrop ? (UINT)rectH : srcDesc.Height;
    ID3D11ShaderResourceView* srv = nullptr;
    ID3D11Texture2D* viewTexture = acquireTempTexture(s, s.blitTempCache, s.blitTempNext,
                                                      tempW, tempH, typedFormat, &srv);
    if (!viewTexture) {
        Log("[SimXR] Failed to create temp texture for blit");
        return;
    }

    if (shouldCrop) {
        // Copy only the specified rect
        D3D11_BOX box{};
        box.left = rectX;
        box.top = rectY;
        box.right = rectX + rectW;
        box.bottom = rectY + rectH;
        box.front = 0;
        box.back = 1;
        s.d3d11Context->CopySubresourceRegion(viewTexture, 0, 0, 0, 0, sourceTexture.Get(), srcSubresource, &box);
        LogVf("[SimXR] Applied %s imageRect: %dx%d from (%d,%d)",
              rectClamped ? "clamped" : "cropped", rectW, rectH, rectX, rectY);
    } else if (srcDesc.SampleDesc.Count > 1) {
        // If app used MSAA, resolve it first using the typed format
        s.d3d11Context->ResolveSubresource(viewTexture, 0, sourceTexture.Get(), srcSubresource, typedFormat);
    } else {
        // Otherwise just copy the full texture
        s.d3d11Context->CopySubresourceRegion(viewTexture, 0, 0, 0, 0, sourceTexture.Get(), srcSubresource, nullptr);
    }

    s.d3d11Context->RSSetViewports(1, &vp);

    // Set shaders and resources
    s.d3d11Context->VSSetShader(s.blitVS.Get(), nullptr, 0);
    s.d3d11Context->PSSetShader(s.blitPS.Get(), nullptr, 0);

    ID3D11ShaderResourceView* srvs[] = { srv };
    s.d3d11Context->PSSetShaderResources(0, 1, srvs);
    ID3D11SamplerState* samplers[] = { s.samplerState.Get() };
    s.d3d11Context->PSSetSamplers(0, 1, samplers);

    // Set pipeline state
    s.d3d11Context->IASetInputLayout(nullptr);
    s.d3d11Context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    s.d3d11Context->OMSetBlendState(blendState, nullptr, 0xFFFFFFFF);
    s.d3d11Context->OMSetDepthStencilState(nullptr, 0);
    s.d3d11Context->RSSetState(s.noCullRS.Get());  // Use no-cull rasterizer state to prevent triangle culling

    // Bind render target
    ID3D11RenderTargetView* rtvs[1] = { rtv };
    s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);

    // Draw fullscreen quad
    s.d3d11Context->Draw(4, 0);
    
    // Unbind SRV to avoid conflicts with future RTV usage
    ID3D11ShaderResourceView* nullSRV[1] = { nullptr };
    s.d3d11Context->PSSetShaderResources(0, 1, nullSRV);
    
    static int debugCount = 0;
    if (g_logVerbose && ++debugCount % 120 == 1) {
        Logf("[SimXR] blitViewToHalf: srcIdx=%u slice=%u typedFmt=%d srcFmt=%d",
             srcIndex, arraySlice, typedFormat, srcDesc.Format);
        Logf("[SimXR]   viewport: x=%.0f y=%.0f w=%.0f h=%.0f", vp.TopLeftX, vp.TopLeftY, vp.Width, vp.Height);
        Logf("[SimXR]   srcSize: %ux%u, tempSize: %ux%u", srcDesc.Width, srcDesc.Height, tempW, tempH);
    }
}

// The D3D12 preview never goes through a swapchain, so the composited frame only exists in
// the readback buffer paintPreviewComposite has mapped. Called from there, with the pixels
// of the frame that was just shown, so a shot can never be of a frame the window never had.
static void saveD3D12Screenshot(const uint8_t* pixels, UINT width, UINT height, UINT pitch,
                                uint64_t capturedFrame) {
    if (!mcp::g_screenshotRequested) return;

    const std::string path = mcp::GetSimulatorDataPath() + "\\screenshot.bmp";
    if (mcp::SavePixelsToBMP(pixels, width, height, path.c_str(), (int)pitch, true)) {
        mcp::WriteScreenshotStatus(mcp::g_screenshotLayer.c_str(), width, height, capturedFrame);
        std::wstring wp(path.begin(), path.end());
        ui::NotifyScreenshotSaved(wp);
    }
    mcp::g_screenshotRequested = false;
}

// Whether the mirror updates this frame, latched once per xrEndFrame from the Mirror Rate
// setting: a frame's eyes and its quad layers have to be composited together or not at all,
// so the cap cannot be re-evaluated per layer. Every backend honours it - what it saves is
// the readback on D3D12 and OpenGL, and the composite and Present on all three.
static bool g_previewDueThisFrame = true;

// --- Preview frame slots (D3D12) --------------------------------------------------------------

// Open this frame's slot, or nullptr if all of them are still on the GPU. The eye composite
// and every quad layer of a frame share one slot and one allocator: whichever pass runs first
// resets the allocator, later passes only reset the command list, and xrEndFrame closes the
// slot. Returning nullptr drops the frame from the preview, which is the whole point — the
// caller is the app's render thread and must never wait for the GPU here.
static rt::PreviewFrame12* beginPreviewSlot(rt::Session& s) {
    if (!s.previewFence || !s.previewCmdList || !s.previewQueue12) return nullptr;
    if (!g_previewDueThisFrame) return nullptr;
    rt::PreviewFrame12& f = s.previewFrames[s.previewSlot];
    if (s.previewSlotOpen) return &f;
    // Still executing, or still holding a composite the painter has not picked up.
    if (f.pending || f.fenceValue > s.previewFence->GetCompletedValue()) return nullptr;
    if (!f.alloc || FAILED(f.alloc->Reset())) return nullptr;
    f.recording = false;
    f.hasProjection = false;
    f.quadLayers = 0;
    f.quadComposed = false;
    memset(f.quadRects, 0, sizeof(f.quadRects));
    f.quadSourceAlphaCoverage = 0.0f;
    s.previewSlotOpen = true;
    return &f;
}

// Open the render target for this frame's layers. Idempotent: whichever layer pass runs
// first pays for the command-list reset, the transition and the clear, and the rest just
// draw. Every layer of a frame therefore lands in one RT and comes back in one readback,
// which is also why the mirror can no longer show the eyes for a moment without the HUD
// over them - the two are never in the back buffer separately.
static bool beginPreviewRT(rt::Session& s, rt::PreviewFrame12& f) {
    if (f.recording) return true;
    if (!s.previewRT12 || !s.previewRtvHeap) return false;
    if (FAILED(s.previewCmdList->Reset(f.alloc.Get(), nullptr))) return false;

    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = s.previewRT12.Get();
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    s.previewCmdList->ResourceBarrier(1, &barrier);

    ID3D12DescriptorHeap* heaps[] = { s.previewSrvHeap.Get() };
    s.previewCmdList->SetDescriptorHeaps(1, heaps);
    s.previewCmdList->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);

    const D3D12_CPU_DESCRIPTOR_HANDLE rtv = s.previewRtvHeap->GetCPUDescriptorHandleForHeapStart();
    const float clearBlack[4] = { 0.0f, 0.0f, 0.0f, 1.0f };
    s.previewCmdList->ClearRenderTargetView(rtv, clearBlack, 0, nullptr);

    f.recording = true;
    return true;
}

// Read the composited RT back and hand the slot to the painter.
static void closePreviewSlot(rt::Session& s, uint32_t frameCount, bool hasProjection) {
    if (!s.previewSlotOpen) return;
    s.previewSlotOpen = false;
    rt::PreviewFrame12& f = s.previewFrames[s.previewSlot];
    if (!f.recording) return;                       // nothing was recorded this frame
    f.recording = false;
    if (!f.readback || !s.previewRT12) return;

    // Label the slot now, while the pose that produced it is still the live one.
    f.frame = frameCount;
    f.hasProjection = hasProjection;
    f.headYaw = rt::g_headYaw; f.headPitch = rt::g_headPitch; f.headRoll = rt::g_headRoll;
    f.headX = rt::g_headPos.x; f.headY = rt::g_headPos.y; f.headZ = rt::g_headPos.z;
    f.proj = mcp::g_lastProjEntry;

    const D3D12_RESOURCE_DESC rtDesc = s.previewRT12->GetDesc();
    const UINT rtWidth = (UINT)rtDesc.Width, rtHeight = rtDesc.Height;

    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = s.previewRT12.Get();
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    s.previewCmdList->ResourceBarrier(1, &barrier);

    D3D12_TEXTURE_COPY_LOCATION dst = {};
    dst.pResource = f.readback.Get();
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Offset = 0;
    dst.PlacedFootprint.Footprint.Format = rt::kPreviewRTFormat;
    dst.PlacedFootprint.Footprint.Width = rtWidth;
    dst.PlacedFootprint.Footprint.Height = rtHeight;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = s.previewReadbackPitch;
    D3D12_TEXTURE_COPY_LOCATION src = {};
    src.pResource = s.previewRT12.Get();
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    src.SubresourceIndex = 0;
    s.previewCmdList->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);

    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    s.previewCmdList->ResourceBarrier(1, &barrier);
    s.previewCmdList->Close();

    // Order the read of the app's swapchain images after the app's own frame work.
    if (s.crossQueueFence && s.d3d12Queue) {
        s.crossQueueFenceValue++;
        s.d3d12Queue->Signal(s.crossQueueFence.Get(), s.crossQueueFenceValue);
        s.previewQueue12->Wait(s.crossQueueFence.Get(), s.crossQueueFenceValue);
    }
    // On a Vulkan session the frame's work is on the app's VkQueue instead, and the shared
    // fence FrameSyncBegin signalled from there is what this queue has to wait on.
    if (s.usesVulkan && s.vk.timeline && s.vk.fence && s.vk.appSignalled) {
        s.previewQueue12->Wait(s.vk.fence.Get(), s.vk.appSignalled);
    }

    ID3D12CommandList* lists[] = { s.previewCmdList.Get() };
    s.previewQueue12->ExecuteCommandLists(1, lists);
    const UINT64 value = ++s.previewFenceValue;
    s.previewQueue12->Signal(s.previewFence.Get(), value);
    if (s.usesVulkan && s.vk.timeline && s.vk.fence) {
        s.vk.previewSignalled = ++s.vk.counter;
        s.previewQueue12->Signal(s.vk.fence.Get(), s.vk.previewSignalled);
        LogVf("[SimXR][VK] sync: preview waits for %llu, signals %llu",
              (unsigned long long)s.vk.appSignalled, (unsigned long long)s.vk.previewSignalled);
    }

    // The preview reads the app's swapchain images on its own queue. The CPU wait this path
    // used to do was also what kept the app from rendering the next frame into those images
    // while the read was still in flight, so with the wait gone the app's queue has to be
    // told to wait for the read instead. It is a GPU-side wait on work already ahead of it
    // in the pipeline, so unlike the CPU wait it costs nothing.
    if (s.d3d12Queue) s.d3d12Queue->Wait(s.previewFence.Get(), value);

    f.fenceValue = value;
    f.rtWidth = rtWidth;
    f.rtHeight = rtHeight;
    f.pending = true;
    s.previewSlot = (s.previewSlot + 1) % rt::kPreviewFrames;
}

// Show a finished composite. The readback already holds the window's own pixel count in
// GDI's byte order, so this is one StretchDIBits from the mapped rows straight to the
// window - no intermediate back buffer, and none of the per-frame memcpy that copying into
// one costs (~15MB a frame at a 2560x1440 window, on the app's render thread).
//
// The screenshot and burst paths read the same mapping, which is also what makes them
// structurally unable to capture a frame the window never showed.
static void paintPreviewComposite(rt::Session& s, rt::PreviewFrame12& f) {
    if (!f.readback || !s.hwnd || f.rtWidth == 0 || f.rtHeight == 0) return;
    if (s.previewReadbackPitch == 0) return;

    void* mapped = nullptr;
    D3D12_RANGE readRange = { 0, (SIZE_T)s.previewReadbackPitch * f.rtHeight };
    if (FAILED(f.readback->Map(0, &readRange, &mapped)) || !mapped) return;

    bool paintedPreview = false;
    HDC windowDC = GetDC(s.hwnd);
    if (windowDC) {
        RECT cr{};
        GetClientRect(s.hwnd, &cr);
        const int clientW = cr.right - cr.left;
        const int clientH = cr.bottom - cr.top;

        // The rows are 256-byte aligned, which GDI has no field for - but a DIB whose
        // width is the whole pitch has exactly that stride, and the source rect then
        // takes the real pixels out of the left of each row.
        BITMAPINFO bmi = {};
        bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bmi.bmiHeader.biWidth = (LONG)(s.previewReadbackPitch / 4);
        bmi.bmiHeader.biHeight = -(LONG)f.rtHeight;   // top-down
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;

        // The internal surface may be capped below the client size. Its aspect
        // matches the client, so this is a pure final scale and the projection/
        // quad composition remains pixel-coherent before GDI sees it.
        paintedPreview = StretchDIBits(windowDC, 0, 0, clientW, clientH,
                                       0, 0, (int)f.rtWidth, (int)f.rtHeight,
                                       mapped, &bmi, DIB_RGB_COLORS, SRCCOPY) != GDI_ERROR;
        ReleaseDC(s.hwnd, windowDC);
    }

    saveD3D12Screenshot((const uint8_t*)mapped, f.rtWidth, f.rtHeight,
                        s.previewReadbackPitch, f.frame);

    // The burst records the frame that was just shown, described by the metadata that
    // frame was recorded with rather than by whatever the head is doing now.
    if (mcp::g_burstActive && f.hasProjection) {
        mcp::g_lastProjEntry = f.proj;
        mcp::BurstOnFrame((const uint8_t*)mapped, (int)f.rtWidth, (int)f.rtHeight,
                          (int)s.previewReadbackPitch, f.frame,
                          f.headYaw, f.headPitch, f.headRoll, f.headX, f.headY, f.headZ);
    }

    // The detector samples the aligned readback rows in place; no repack. Before it
    // took a row pitch, matching the 256-byte alignment on a window whose width was
    // not a 64-pixel multiple cost a full-frame memcpy here on every painted frame.
    const uint8_t* detectorPixels = (const uint8_t*)mapped;
    if (f.hasProjection) {
        flicker::ObservePreview(detectorPixels, f.rtWidth, f.rtHeight,
                                s.previewReadbackPitch, f.fenceValue, f.frame);
    }
    flicker::UiFrameInfo uiInfo;
    uiInfo.quadLayers = f.quadLayers;
    uiInfo.projectionRefreshed = f.hasProjection;
    uiInfo.freshReadback = true;
    uiInfo.cacheValid = f.quadComposed;
    uiInfo.composed = f.quadComposed;
    memcpy(uiInfo.rects, f.quadRects, sizeof(uiInfo.rects));
    uiInfo.sourceAlphaCoverage = f.quadSourceAlphaCoverage;
    flicker::ObserveUi(detectorPixels, f.rtWidth, f.rtHeight, s.previewReadbackPitch,
                       f.fenceValue, f.frame, uiInfo);
    flicker::ObservePaint(f.fenceValue, paintedPreview);

    D3D12_RANGE writeRange = { 0, 0 };
    f.readback->Unmap(0, &writeRange);
}

// Draws the eye swapchains, scaled, into the offscreen RT, reads that back and copies it
// into the preview back buffer. Never touches DXGI Present, which the Steam overlay and
// UEVR both hook - calling it from inside the app's own Present recurses until the stack
// gives out. Scaling on the GPU keeps the readback at the window's size: the alternative,
// reading back the full stereo render and resampling it with StretchDIBits, cost 25ms a
// frame on a 5120x1440 submission.
static void blitD3D12ToPreview(rt::Session& s,
                                rt::Swapchain& chainL, uint32_t leftIdx, uint32_t leftSlice, const rt::SubImageRect& rectL,
                                rt::Swapchain* chainR, uint32_t rightIdx, uint32_t rightSlice, const rt::SubImageRect& rectR,
                                ui::DisplayLayout layout, ui::ViewMode viewMode) {
    if (!s.previewRT12 || !s.previewCmdList ||
        !s.previewRtvHeap || !s.previewSrvHeap || !s.previewRootSig || !s.previewPSO) {
        Log("[SimXR] blitD3D12ToPreview: Missing D3D12 preview resources");
        return;
    }

    // Skip depth-only swapchains
    bool isDepthFormat = (chainL.format == DXGI_FORMAT_D32_FLOAT ||
                          chainL.format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT ||
                          chainL.format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
                          chainL.format == DXGI_FORMAT_D16_UNORM);
    if (isDepthFormat) {
        return;
    }

    // All three frames still on the GPU: skip this one instead of waiting for it.
    rt::PreviewFrame12* slot = beginPreviewSlot(s);
    if (!slot || !slot->readback) return;
    // The RT is the size the window shows, so this pass is also the downscale: each eye
    // is drawn into its half of it, filtered, instead of copied at full resolution and
    // resampled on the CPU afterwards.
    if (!beginPreviewRT(s, *slot)) return;

    ID3D12Resource* renderTarget = s.previewRT12.Get();

    auto transition = [&](ID3D12Resource* res, D3D12_RESOURCE_STATES& state, D3D12_RESOURCE_STATES newState) {
        if (!res || state == newState) return;
        D3D12_RESOURCE_BARRIER b = {};
        b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b.Transition.pResource = res;
        b.Transition.StateBefore = state;
        b.Transition.StateAfter = newState;
        b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        s.previewCmdList->ResourceBarrier(1, &b);
        state = newState;
    };

    D3D12_RESOURCE_DESC rtDesc = renderTarget->GetDesc();
    UINT rtWidth = (UINT)rtDesc.Width;
    UINT rtHeight = rtDesc.Height;

    // Plain (non-sRGB) view: the eye pass writes the source bytes through in the encoding
    // they are stored in, so nothing must convert them on the way out.
    const D3D12_CPU_DESCRIPTOR_HANDLE rtvHandle = s.previewRtvHeap->GetCPUDescriptorHandleForHeapStart();
    s.previewCmdList->SetGraphicsRootSignature(s.previewRootSig.Get());
    s.previewCmdList->SetPipelineState(s.previewPSO.Get());
    s.previewCmdList->OMSetRenderTargets(1, &rtvHandle, FALSE, nullptr);

    auto drawEye = [&](rt::Swapchain& chain, uint32_t idx, uint32_t slice, const rt::SubImageRect& rect,
                       int dstX, int dstY, int dstW, int dstH, const char* label) -> bool {
        if (idx >= chain.images12.size() || !chain.images12[idx]) return false;
        if (chain.imageStates12.size() <= idx) {
            Logf("[SimXR] blitD3D12ToPreview: %s missing state tracking", label);
            return false;
        }
        const uint32_t arraySize = chain.arraySize ? chain.arraySize : 1;
        if (slice >= arraySize) {
            Logf("[SimXR] blitD3D12ToPreview: %s slice %u out of range (arraySize=%u)", label, slice, arraySize);
            return false;
        }
        if (dstW <= 0 || dstH <= 0 || rect.w == 0 || rect.h == 0) return false;
        // Panned entirely out of the RT: nothing to draw, and bailing here keeps the
        // swapchain image out of a transition it would never be moved back from.
        if (dstX + dstW <= 0 || dstY + dstH <= 0 ||
            dstX >= (int)rtWidth || dstY >= (int)rtHeight) return false;

        ID3D12Resource* srcTex = chain.images12[idx].Get();
        const D3D12_RESOURCE_DESC sd = srcTex->GetDesc();
        if (sd.SampleDesc.Count > 1) {
            static int msaaLog = 0;
            if (++msaaLog % 120 == 1) {
                Logf("[SimXR] blitD3D12ToPreview: %s is %ux MSAA; preview needs a resolve first, skipping",
                     label, sd.SampleDesc.Count);
            }
            return false;
        }

        D3D12_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = rt::PreviewSrvFormat(sd.Format);
        srvDesc.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        if (sd.DepthOrArraySize > 1) {
            srvDesc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2DARRAY;
            srvDesc.Texture2DArray.FirstArraySlice = slice;
            srvDesc.Texture2DArray.ArraySize = 1;
            srvDesc.Texture2DArray.MipLevels = 1;
        } else {
            srvDesc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
            srvDesc.Texture2D.MipLevels = 1;
        }

        const UINT slot = s.previewSrvSlot;
        s.previewSrvSlot = (s.previewSrvSlot + 1) % rt::kPreviewSrvSlots;
        D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.previewSrvHeap->GetCPUDescriptorHandleForHeapStart();
        D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.previewSrvHeap->GetGPUDescriptorHandleForHeapStart();
        cpu.ptr += (SIZE_T)slot * s.previewSrvStride;
        gpu.ptr += (UINT64)slot * s.previewSrvStride;
        s.d3d12Device->CreateShaderResourceView(srcTex, &srvDesc, cpu);

        transition(srcTex, chain.imageStates12[idx], D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);

        const float texW = (float)sd.Width, texH = (float)sd.Height;
        const float uvScaleX = (float)rect.w / texW, uvScaleY = (float)rect.h / texH;
        const float constants[8] = {
            (float)rect.x / texW, (float)rect.y / texH,
            uvScaleX, uvScaleY,
            0.25f * uvScaleX / (float)dstW, 0.25f * uvScaleY / (float)dstH,
            (srvDesc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT ||
             srvDesc.Format == DXGI_FORMAT_R32G32B32A32_FLOAT) ? 1.0f : 0.0f,
            0.0f,
        };
        s.previewCmdList->SetGraphicsRoot32BitConstants(1, 8, constants, 0);
        s.previewCmdList->SetGraphicsRootDescriptorTable(0, gpu);

        // A zoomed-in eye is larger than the RT and starts off the left/top of it, so the
        // viewport runs outside while the scissor stays inside: the rasterizer drops the
        // overflow and the cleared black shows through anywhere the image doesn't reach.
        D3D12_VIEWPORT vp = { (float)dstX, (float)dstY, (float)dstW, (float)dstH, 0.0f, 1.0f };
        D3D12_RECT scissor = { (std::max)(0, dstX), (std::max)(0, dstY),
                               (std::min)((int)rtWidth, dstX + dstW),
                               (std::min)((int)rtHeight, dstY + dstH) };
        s.previewCmdList->RSSetViewports(1, &vp);
        s.previewCmdList->RSSetScissorRects(1, &scissor);
        s.previewCmdList->DrawInstanced(3, 1, 0, 0);

        // Back to the state the app released it in, so its own barriers still line up.
        transition(srcTex, chain.imageStates12[idx], chain.releaseState12);
        return true;
    };

    const bool singleEye = (viewMode != ui::ViewMode::BothEyes);
    bool forceSingleEye = false;
    static bool loggedAnaglyph = false;
    ui::DisplayLayout effectiveLayout = layout;
    if (!singleEye && layout == ui::DisplayLayout::Anaglyph) {
        if (!loggedAnaglyph) {
            Log("[SimXR] blitD3D12ToPreview: Anaglyph not supported on D3D12 path; showing left eye only");
            loggedAnaglyph = true;
        }
        forceSingleEye = true;
    }
    if (effectiveLayout == ui::DisplayLayout::Anaglyph) {
        effectiveLayout = ui::DisplayLayout::SideBySide;
    }

    const bool hasLeft = leftIdx < chainL.images12.size() && chainL.images12[leftIdx];
    const bool hasRight = chainR && rightIdx < chainR->images12.size() && chainR->images12[rightIdx];

    // The RT is the client area, so zoom and pan decide where inside it the eyes go.
    const rt::FitRect present = rt::ComputePresentRect((int)rtWidth, (int)rtHeight);
    const int presentX = (int)lroundf(present.x);
    const int presentY = (int)lroundf(present.y);
    const int presentW = (std::max)(1, (int)lroundf(present.w));
    const int presentH = (std::max)(1, (int)lroundf(present.h));

    if (singleEye || forceSingleEye) {
        if (viewMode == ui::ViewMode::RightEyeOnly && hasRight) {
            drawEye(*chainR, rightIdx, rightSlice, rectR, presentX, presentY, presentW, presentH, "R");
        } else if (hasLeft) {
            drawEye(chainL, leftIdx, leftSlice, rectL, presentX, presentY, presentW, presentH, "L");
        } else if (hasRight) {
            drawEye(*chainR, rightIdx, rightSlice, rectR, presentX, presentY, presentW, presentH, "R");
        }
    } else {
        const bool overUnder = (effectiveLayout == ui::DisplayLayout::OverUnder);
        const int halfW = overUnder ? presentW : presentW / 2;
        const int halfH = overUnder ? presentH / 2 : presentH;
        const int rightX = overUnder ? presentX : presentX + halfW;
        const int rightY = overUnder ? presentY + halfH : presentY;

        if (hasLeft) {
            drawEye(chainL, leftIdx, leftSlice, rectL, presentX, presentY, halfW, halfH, "L");
        }
        if (hasRight) {
            drawEye(*chainR, rightIdx, rightSlice, rectR, rightX, rightY, halfW, halfH, "R");
        } else if (hasLeft) {
            drawEye(chainL, leftIdx, leftSlice, rectL, rightX, rightY, halfW, halfH, "L");
        }
    }

    // The RT stays open: quad layers of this frame draw into it next, and closePreviewSlot
    // does the single readback once every layer is in.

    // Process window messages
    MSG msg;
    while (PeekMessageW(&msg, s.hwnd, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }

    static int blitCount = 0;
    if (g_logVerbose && ++blitCount % 60 == 1) {
        Logf("[SimXR] blitD3D12ToPreview: L[%u] R[%u] (%ux%u) recorded into slot %u",
             leftIdx, rightIdx, rtWidth, rtHeight, s.previewSlot);
    }
}

// Flag to track if Present should be called (deferred until all layers rendered)
static bool g_presentPending = false;

// Wipe the preview to black. Used on frames that carry no projection layer so
// overlays are not composited over the stale - and by then wrong - stereo image
// the last 3D frame left behind. D3D11 and OpenGL only: a D3D12 session gets the
// same wipe from the clear that opens its render target.
static void clearPreviewToBlack(rt::Session& s) {
    if (!s.hwnd) return;

    if (!s.previewSwapchain || !s.d3d11Device || !s.d3d11Context) return;
    ComPtr<ID3D11Texture2D> backbuffer;
    if (FAILED(s.previewSwapchain->GetBuffer(0, IID_PPV_ARGS(backbuffer.GetAddressOf())))) return;
    ComPtr<ID3D11RenderTargetView> rtv;
    if (FAILED(s.d3d11Device->CreateRenderTargetView(backbuffer.Get(), nullptr, rtv.GetAddressOf()))) return;
    const float black[4] = { 0.0f, 0.0f, 0.0f, 1.0f };
    s.d3d11Context->ClearRenderTargetView(rtv.Get(), black);
    // presentProjection normally owns the Present; with no projection layer
    // nothing else would flip this frame.
    g_presentPending = true;
}

// Frames per second in the title bar. Counted once per submitted frame, ahead of anything
// that can skip the composite, so the rate shown is the application's and not the mirror's.
static void updatePreviewTitle(rt::Session& s) {
    if (!s.hwnd) return;
    static int frames = 0;
    static auto lastUpdate = std::chrono::high_resolution_clock::now();
    ++frames;
    const auto now = std::chrono::high_resolution_clock::now();
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - lastUpdate).count();
    if (elapsed >= 500) {
        ui::g_lastFps = (int)(frames * 1000 / elapsed);
        frames = 0;
        lastUpdate = now;
    } else if (ui::g_lastScreenshotTickMs == 0) {
        // Otherwise refresh every frame while a screenshot notice is up, so it appears
        // immediately rather than at the next 500ms tick.
        return;
    }
    ui::StatsInfo si = rt::BuildStatsInfo(s);
    ui::UpdateWindowTitle(s.hwnd, &si);
}

static void presentProjection(rt::Session& s, const XrCompositionLayerProjection& proj, bool skipPresent = false) {
    LogV("[SimXR] ============================================");
    LogVf("[SimXR] presentProjection called: viewCount=%u, skipPresent=%d", proj.viewCount, (int)skipPresent);
    LogV("[SimXR] RENDERING FRAME TO PREVIEW WINDOW");
    LogV("[SimXR] ============================================");
    if (proj.viewCount < 1) {
        Log("[SimXR] presentProjection: No views, returning");
        return;
    }
    const auto& vL = proj.views[0];
    auto itL = rt::g_swapchains.find(vL.subImage.swapchain); 
    if (itL == rt::g_swapchains.end()) {
        Log("[SimXR] presentProjection: Left swapchain not found");
        return;
    }
    auto& chL = itL->second;
    // Size from the declared rect, not the texture: an app rendering 1920x1080 into a
    // square swapchain wants a 16:9 eye, not the square.
    const rt::SubImageRect rectL = rt::ResolveSubImageRect(vL.subImage.imageRect, chL.width, chL.height, "left eye");
    rt::SubImageRect rectR = rectL;
    uint32_t width = rectL.w, height = rectL.h;
    const rt::Swapchain* chRPtr = &chL;
    if (proj.viewCount > 1) {
        const auto& vR = proj.views[1];
        auto itR = rt::g_swapchains.find(vR.subImage.swapchain);
        if (itR != rt::g_swapchains.end()) {
            chRPtr = &itR->second;
            rectR = rt::ResolveSubImageRect(vR.subImage.imageRect, itR->second.width, itR->second.height, "right eye");
            if (rectR.w > width) width = rectR.w;
            if (rectR.h > height) height = rectR.h;
        }
    }
    // Publish the source per-eye size so menu/keyboard zoom callbacks can
    // compute a window target without having to walk the swapchain map.
    const UINT prevSourceW = rt::g_sourceWidth.exchange(width);
    const UINT prevSourceH = rt::g_sourceHeight.exchange(height);

    // The window is opened at xrBeginSession, before any projection layer exists,
    // so it starts out sized from the recommended resolution rather than this one.
    // Re-fit it the first time a real size arrives, and on any later change.
    if (s.hwnd && (prevSourceW != width || prevSourceH != height)) {
        rt::FitWindowToContentAspect(s.hwnd);
    }
    updatePreviewTitle(s);

    {
        std::lock_guard<std::mutex> lock(s.previewMutex);

        // OpenGL preview path - read pixels from GL textures and display via D3D11
        if (s.usesOpenGL) {
            // Nothing below this point is cheap: the path reads both eyes back off the GPU
            // before it can show them, which is exactly what Mirror Rate exists to skip.
            if (!g_previewDueThisFrame) return;

            static int glFrameCount = 0;
            glFrameCount++;

            if (glFrameCount % 60 == 1) {
                Logf("[SimXR] GL PREVIEW: frame=%d, width=%u, height=%u", glFrameCount, width, height);
            }

            // Make the app's GL context current
            HGLRC savedRC = wglGetCurrentContext();
            HDC savedDC = wglGetCurrentDC();

            if (glFrameCount % 60 == 1) {
                Logf("[SimXR] GL PREVIEW: savedRC=%p, savedDC=%p, s.glRC=%p, s.glDC=%p",
                     savedRC, savedDC, s.glRC, s.glDC);
            }

            if (s.glRC && s.glDC) {
                BOOL result = wglMakeCurrent(s.glDC, s.glRC);
                if (glFrameCount % 60 == 1) {
                    Logf("[SimXR] GL PREVIEW: wglMakeCurrent result=%d", result);
                }
            } else {
                if (glFrameCount % 60 == 1) {
                    Log("[SimXR] GL PREVIEW: WARNING - s.glRC or s.glDC is null!");
                }
            }

            // Read pixel data from GL textures into CPU buffers
            std::vector<uint8_t>& leftPixels = s.glEyePixels[0];    // reused across frames
            std::vector<uint8_t>& rightPixels = s.glEyePixels[1];
            leftPixels.resize((size_t)width * height * 4);
            rightPixels.resize((size_t)width * height * 4);

            // Get left eye texture
            GLuint leftTex = 0;
            if (chL.imagesGL.size() > 0) {
                uint32_t idx = chL.lastReleased;
                if (idx == UINT32_MAX || idx >= chL.imageCount) idx = chL.lastAcquired;
                if (idx != UINT32_MAX && idx < chL.imagesGL.size()) {
                    leftTex = chL.imagesGL[idx];
                }
            }

            // Get right eye texture
            GLuint rightTex = 0;
            if (chRPtr && chRPtr->imagesGL.size() > 0) {
                uint32_t idx = chRPtr->lastReleased;
                if (idx == UINT32_MAX || idx >= chRPtr->imageCount) idx = chRPtr->lastAcquired;
                if (idx != UINT32_MAX && idx < chRPtr->imagesGL.size()) {
                    rightTex = chRPtr->imagesGL[idx];
                }
            }

            // Read left eye pixels using glGetTexImage
            if (leftTex != 0) {
                glBindTexture(GL_TEXTURE_2D, leftTex);
                glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, leftPixels.data());
            }

            // Read right eye pixels
            if (rightTex != 0) {
                glBindTexture(GL_TEXTURE_2D, rightTex);
                glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, rightPixels.data());
            }

            // Flip images vertically - OpenGL has Y=0 at bottom, D3D expects Y=0 at top
            auto flipImageVertically = [](std::vector<uint8_t>& pixels, uint32_t width, uint32_t height) {
                const uint32_t rowSize = width * 4;
                std::vector<uint8_t> tempRow(rowSize);
                for (uint32_t y = 0; y < height / 2; y++) {
                    uint8_t* topRow = pixels.data() + y * rowSize;
                    uint8_t* bottomRow = pixels.data() + (height - 1 - y) * rowSize;
                    memcpy(tempRow.data(), topRow, rowSize);
                    memcpy(topRow, bottomRow, rowSize);
                    memcpy(bottomRow, tempRow.data(), rowSize);
                }
            };
            if (leftTex != 0) flipImageVertically(leftPixels, width, height);
            if (rightTex != 0) flipImageVertically(rightPixels, width, height);

            // MCP Integration - capture a screenshot if one was asked for (OpenGL path).
            // xrEndFrame drains the request before it decides whether this frame is due,
            // so a pending shot is what forced the frame we are in.
            if (mcp::g_screenshotRequested) {
                bool captured = false;
                std::string outPath = mcp::GetSimulatorDataPath() + "\\screenshot.bmp";
                if (mcp::g_screenshotLayer == "quad") {
                    // Capture only quad layer
                    mcp::CaptureQuadScreenshot();
                    outPath = mcp::GetSimulatorDataPath() + "\\screenshot_quad.bmp";
                    captured = true;
                } else if (mcp::g_screenshotLayer == "all") {
                    // Capture both projection and quad layers
                    mcp::CaptureScreenshotGL(
                        leftTex != 0 ? leftPixels.data() : nullptr,
                        rightTex != 0 ? rightPixels.data() : nullptr,
                        width, height);
                    // Also capture quad layer separately
                    if (mcp::g_quadLayerCaptured) {
                        std::string quadPath = mcp::GetSimulatorDataPath() + "\\screenshot_quad.bmp";
                        mcp::SavePixelsToBMP(mcp::g_quadLayerPixels.data(),
                            mcp::g_quadLayerWidth, mcp::g_quadLayerHeight, quadPath.c_str());
                    }
                    mcp::g_screenshotRequested = false;
                    captured = true;
                } else {
                    // Default: capture projection layer
                    mcp::CaptureScreenshotGL(
                        leftTex != 0 ? leftPixels.data() : nullptr,
                        rightTex != 0 ? rightPixels.data() : nullptr,
                        width, height);
                    captured = true;
                }
                if (captured) {
                    std::wstring wp(outPath.begin(), outPath.end());
                    ui::NotifyScreenshotSaved(wp);
                }
            }

            // Restore original GL context
            if (savedRC) {
                wglMakeCurrent(savedDC, savedRC);
            }

            // Log progress and check if pixel data is valid
            if (glFrameCount % 60 == 1) {
                Logf("[SimXR] OpenGL frame #%d - leftTex=%u, rightTex=%u, size=%ux%u",
                     glFrameCount, leftTex, rightTex, width, height);

                // Check if pixels are all zero (black) or have content
                uint32_t leftSum = 0, rightSum = 0;
                for (size_t i = 0; i < std::min((size_t)1000, leftPixels.size()); i++) {
                    leftSum += leftPixels[i];
                }
                for (size_t i = 0; i < std::min((size_t)1000, rightPixels.size()); i++) {
                    rightSum += rightPixels[i];
                }
                Logf("[SimXR] GL PREVIEW: leftPixelSum=%u, rightPixelSum=%u (first 1000 bytes)", leftSum, rightSum);

                // Print first few pixel values (RGBA)
                if (leftPixels.size() >= 16) {
                    Logf("[SimXR] GL PREVIEW: First 4 pixels (RGBA): [%d,%d,%d,%d] [%d,%d,%d,%d] [%d,%d,%d,%d] [%d,%d,%d,%d]",
                         leftPixels[0], leftPixels[1], leftPixels[2], leftPixels[3],
                         leftPixels[4], leftPixels[5], leftPixels[6], leftPixels[7],
                         leftPixels[8], leftPixels[9], leftPixels[10], leftPixels[11],
                         leftPixels[12], leftPixels[13], leftPixels[14], leftPixels[15]);
                }

                // Check a pixel in the middle of the image
                size_t midOffset = (height / 2) * width * 4 + (width / 2) * 4;
                if (midOffset + 4 <= leftPixels.size()) {
                    Logf("[SimXR] GL PREVIEW: Middle pixel (RGBA): [%d,%d,%d,%d]",
                         leftPixels[midOffset], leftPixels[midOffset+1], leftPixels[midOffset+2], leftPixels[midOffset+3]);
                }
            }

            // Now create/use D3D11 preview if we don't have one yet
            if (!s.d3d11Device) {
                // Create D3D11 device for preview window
                D3D_FEATURE_LEVEL featureLevel;
                UINT flags = 0;
                #ifdef _DEBUG
                flags |= D3D11_CREATE_DEVICE_DEBUG;
                #endif
                HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
                                               nullptr, 0, D3D11_SDK_VERSION,
                                               &s.d3d11Device, &featureLevel, &s.d3d11Context);
                if (FAILED(hr)) {
                    Logf("[SimXR] Failed to create D3D11 device for GL preview: 0x%08X", hr);
                    return;
                }
                Log("[SimXR] Created D3D11 device for OpenGL preview");

                // Force shader recompilation by resetting blit resources
                s.blitVS.Reset();
                s.blitPS.Reset();
                s.quadVS.Reset();
                s.quadCB.Reset();
                s.samplerState.Reset();
                s.noCullRS.Reset();
                s.anaglyphRedBS.Reset();
                s.anaglyphCyanBS.Reset();
                s.layerBlendStates.clear();
                // Cached staging textures belong to the old device too.
                for (auto& entry : s.blitTempCache) entry = rt::Session::TempTexEntry{};
                for (auto& entry : s.quadTempCache) entry = rt::Session::TempTexEntry{};
                for (int eye = 0; eye < 2; ++eye) {
                    s.glEyeTex[eye].Reset();
                    s.glEyeSrv[eye].Reset();
                }
                s.glEyeTexW = s.glEyeTexH = 0;
                Log("[SimXR] Reset blit resources for fresh shader compilation");
            }

            // Use the standard preview path now that we have a D3D11 device
            DXGI_FORMAT displayFormat = DXGI_FORMAT_R8G8B8A8_UNORM;
            const auto viewMode = ui::g_uiState.viewMode;
            const auto layout = ui::g_uiState.displayLayout;

            // Swapchain backbuffer = current window client area so DXGI doesn't
            // have to scale; we letterbox the stereo content into it ourselves.
            int targetWidth = (int)width;
            int targetHeight = (int)height;
            rt::GetPreviewClientSize(s, (int)width, (int)height, targetWidth, targetHeight);

            if (glFrameCount % 60 == 1) {
                Logf("[SimXR] GL PREVIEW: clientSize=%dx%d, calling ensurePreviewSized", targetWidth, targetHeight);
            }

            ensurePreviewSized(s, (UINT)targetWidth, (UINT)targetHeight, displayFormat);

            if (!s.previewSwapchain) {
                Log("[SimXR] GL PREVIEW: ERROR - previewSwapchain is NULL after ensurePreviewSized!");
                return;
            }

            // Get the backbuffer
            ComPtr<ID3D11Texture2D> bb;
            if (FAILED(s.previewSwapchain->GetBuffer(0, IID_PPV_ARGS(bb.GetAddressOf())))) {
                Log("[SimXR] Failed to get preview swapchain buffer for GL preview");
                return;
            }

            // Upload GL pixel data into cached textures; the pair is recreated only
            // when the eye size changes rather than on every mirrored frame.
            if (s.glEyeTexW != width || s.glEyeTexH != height || !s.glEyeTex[0]) {
                D3D11_TEXTURE2D_DESC texDesc = {};
                texDesc.Width = width;
                texDesc.Height = height;
                texDesc.MipLevels = 1;
                texDesc.ArraySize = 1;
                texDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
                texDesc.SampleDesc.Count = 1;
                texDesc.Usage = D3D11_USAGE_DEFAULT;
                texDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
                for (int eye = 0; eye < 2; ++eye) {
                    s.glEyeTex[eye].Reset();
                    s.glEyeSrv[eye].Reset();
                    if (SUCCEEDED(s.d3d11Device->CreateTexture2D(&texDesc, nullptr,
                                                                 s.glEyeTex[eye].GetAddressOf()))) {
                        s.d3d11Device->CreateShaderResourceView(s.glEyeTex[eye].Get(), nullptr,
                                                                s.glEyeSrv[eye].GetAddressOf());
                    }
                }
                s.glEyeTexW = width;
                s.glEyeTexH = height;
            }
            if (leftTex != 0 && s.glEyeTex[0]) {
                s.d3d11Context->UpdateSubresource(s.glEyeTex[0].Get(), 0, nullptr,
                                                  leftPixels.data(), width * 4, 0);
            }
            if (rightTex != 0 && s.glEyeTex[1]) {
                s.d3d11Context->UpdateSubresource(s.glEyeTex[1].Get(), 0, nullptr,
                                                  rightPixels.data(), width * 4, 0);
            }

            // Use shader-based rendering for proper side-by-side display
            const bool singleEye = (viewMode != ui::ViewMode::BothEyes);
            const bool showLeft = (viewMode != ui::ViewMode::RightEyeOnly);
            const bool showRight = (viewMode != ui::ViewMode::LeftEyeOnly);

            // Initialize blit resources if not already done
            if (!rt::InitBlitResources(s)) {
                Log("[SimXR] OpenGL preview: Failed to init blit resources");
                return;
            }

            // Create render target view for the backbuffer
            ComPtr<ID3D11RenderTargetView> rtv;
            if (FAILED(s.d3d11Device->CreateRenderTargetView(bb.Get(), nullptr, rtv.GetAddressOf()))) {
                Log("[SimXR] OpenGL preview: Failed to create RTV");
                return;
            }

            // SRVs are cached alongside the upload textures.
            ID3D11ShaderResourceView* leftSRV = s.glEyeSrv[0].Get();
            ID3D11ShaderResourceView* rightSRV = s.glEyeSrv[1].Get();

            if (glFrameCount % 60 == 1) {
                Logf("[SimXR] GL PREVIEW: leftTex2D=%p rightTex2D=%p leftSRV=%p rightSRV=%p",
                     s.glEyeTex[0].Get(), s.glEyeTex[1].Get(), leftSRV, rightSRV);
            }

            // Clear the render target (full backbuffer → black borders for letterbox)
            ID3D11RenderTargetView* rtvs[1] = { rtv.Get() };
            s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);
            const float clearColor[4] = {0.0f, 0.0f, 0.0f, 1.0f};
            s.d3d11Context->ClearRenderTargetView(rtv.Get(), clearColor);

            // Where the eyes land in the backbuffer (= client area). "Fit to Window"
            // scales the whole stereo image into it rather than cropping; any other
            // zoom scales the panel and lets the rasterizer clip what overhangs.
            rt::FitRect fit = rt::ComputePresentRect((int)s.previewWidth, (int)s.previewHeight);

            D3D11_VIEWPORT fullVp = { fit.x, fit.y, fit.w, fit.h, 0.0f, 1.0f };
            D3D11_VIEWPORT leftVp = fullVp;
            D3D11_VIEWPORT rightVp = fullVp;
            if (!singleEye) {
                if (layout == ui::DisplayLayout::SideBySide) {
                    leftVp.Width  = fit.w * 0.5f;
                    rightVp.Width = fit.w * 0.5f;
                    rightVp.TopLeftX = fit.x + fit.w * 0.5f;
                } else if (layout == ui::DisplayLayout::OverUnder) {
                    leftVp.Height  = fit.h * 0.5f;
                    rightVp.Height = fit.h * 0.5f;
                    rightVp.TopLeftY = fit.y + fit.h * 0.5f;
                }
            }

            // Helper lambda to blit a texture to a viewport
            auto blitTexture = [&](ID3D11ShaderResourceView* srv, const D3D11_VIEWPORT& vp) {
                if (!srv) {
                    if (glFrameCount % 60 == 1) Log("[SimXR] GL PREVIEW: blitTexture - SRV is null!");
                    return;
                }

                if (glFrameCount % 60 == 1) {
                    Logf("[SimXR] GL PREVIEW: blitTexture - vp=(%.0f,%.0f,%.0f,%.0f) srv=%p",
                         vp.TopLeftX, vp.TopLeftY, vp.Width, vp.Height, srv);
                }

                // Ensure render target is bound
                ID3D11RenderTargetView* currentRTVs[1] = { rtv.Get() };
                s.d3d11Context->OMSetRenderTargets(1, currentRTVs, nullptr);

                s.d3d11Context->RSSetViewports(1, &vp);
                s.d3d11Context->VSSetShader(s.blitVS.Get(), nullptr, 0);
                s.d3d11Context->PSSetShader(s.blitPS.Get(), nullptr, 0);

                ID3D11ShaderResourceView* srvs[] = { srv };
                s.d3d11Context->PSSetShaderResources(0, 1, srvs);
                ID3D11SamplerState* samplers[] = { s.samplerState.Get() };
                s.d3d11Context->PSSetSamplers(0, 1, samplers);

                s.d3d11Context->IASetInputLayout(nullptr);
                s.d3d11Context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
                s.d3d11Context->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
                s.d3d11Context->OMSetDepthStencilState(nullptr, 0);
                s.d3d11Context->RSSetState(s.noCullRS.Get());

                s.d3d11Context->Draw(4, 0);

                // Unbind SRV
                ID3D11ShaderResourceView* nullSRV[] = { nullptr };
                s.d3d11Context->PSSetShaderResources(0, 1, nullSRV);
            };

            // Render the eyes
            if (singleEye) {
                if (showLeft && leftSRV) {
                    blitTexture(leftSRV, fullVp);
                } else if (showRight && rightSRV) {
                    blitTexture(rightSRV, fullVp);
                }
            } else {
                // Side by side (or over/under)
                if (showLeft && leftSRV) {
                    blitTexture(leftSRV, leftVp);
                }
                if (showRight && rightSRV) {
                    blitTexture(rightSRV, rightVp);
                } else if (showRight && leftSRV) {
                    // Mirror left eye if no right eye available
                    blitTexture(leftSRV, rightVp);
                }
            }

            // Present (may be deferred if overlays are pending)
            if (!skipPresent) {
                MSG msg;
                while (PeekMessageW(&msg, s.hwnd, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }

                if (glFrameCount % 60 == 1) {
                    Logf("[SimXR] GL PREVIEW: About to Present - hwnd=%p, swapchain=%p", s.hwnd, s.previewSwapchain.Get());
                }

                HRESULT presentHr = s.previewSwapchain->Present(0, 0);
                if (FAILED(presentHr) && glFrameCount % 60 == 1) {
                    Logf("[SimXR] GL PREVIEW: Present FAILED with hr=0x%08X", presentHr);
                }
            } else {
                g_presentPending = true;
            }

            return;
        }

        // Use UNORM format for swapchain (SRGB not valid for FLIP_DISCARD)
        // We create SRGB RTVs for proper gamma when rendering. Ignored on D3D12,
        // where the preview RT is always kPreviewRTFormat and the scaling pass
        // converts whatever the app submitted into it.
        const DXGI_FORMAT displayFormat = DXGI_FORMAT_R8G8B8A8_UNORM;

        const auto viewMode = ui::g_uiState.viewMode;
        const auto layout = ui::g_uiState.displayLayout;
        int targetWidth = (int)width;
        int targetHeight = (int)height;
        if (!s.usesD3D12) {
            // D3D11: backbuffer size = current window client area. The eyes
            // get letterboxed into it (no DXGI scaling = no aspect squish).
            rt::GetPreviewClientSize(s, (int)width, (int)height, targetWidth, targetHeight);
        } else {
            // D3D12 (GDI path): the offscreen RT is the client area and the eyes are
            // drawn into their zoomed rect inside it, so the scaling pass does the
            // resample on the GPU and the readback after it is the window's pixel
            // count. Taking the shape from the panel and not from the render target
            // is what turns the app's non-square pixels back into square ones.
            rt::ComputePreviewRTSize(s, targetWidth, targetHeight);
        }
        ensurePreviewSized(s, (UINT)targetWidth, (UINT)targetHeight, displayFormat);
        const bool singleEye = (viewMode != ui::ViewMode::BothEyes);
        const bool showLeft = (viewMode != ui::ViewMode::RightEyeOnly);
        const bool showRight = (viewMode != ui::ViewMode::LeftEyeOnly);

        // Get left image index
        uint32_t leftIdx = 0;
        if (chL.lastReleased != UINT32_MAX && chL.lastReleased < chL.imageCount) {
            leftIdx = chL.lastReleased;
        } else if (chL.lastAcquired != UINT32_MAX && chL.lastAcquired < chL.imageCount) {
            leftIdx = chL.lastAcquired;
        }

        static int blitCount = 0;
        if (g_logVerbose && ++blitCount % 60 == 1) {
            Logf("[SimXR] Blitting left eye: idx=%u (lastReleased=%u, lastAcquired=%u, imageCount=%u)",
                 leftIdx, chL.lastReleased, chL.lastAcquired, chL.imageCount);
        }

        if (!s.usesD3D12) {
            // ===== D3D11 PATH =====
            if (!s.previewSwapchain) return;

            // Mirror Rate: the composite and the Present it feeds are what this skips.
            // The frame counter below stays outside it, so the rate shown in the title is
            // still the application's and not the mirror's.
            if (g_previewDueThisFrame) {
                // Save D3D11 context state - will auto-restore when stateBackup goes out of scope
                D3D11StateBackup stateBackup(s.d3d11Context.Get());

                // Get the backbuffer and create RTV
                ComPtr<ID3D11Texture2D> bb;
                if (FAILED(s.previewSwapchain->GetBuffer(0, IID_PPV_ARGS(bb.GetAddressOf())))) {
                    Log("[SimXR] Failed to get preview swapchain buffer.");
                    return;
                }

                // Explicit sRGB RTV for proper gamma encoding
                ComPtr<ID3D11RenderTargetView> rtv;
                if (!rt::CreatePreviewRtv(s, bb.Get(), rtv)) {
                    Log("[SimXR] Failed to create RTV for preview.");
                    return;
                }

                // Bind RTV and clear the full backbuffer (= client area) to black
                // so any letterbox border ends up black instead of stale content.
                ID3D11RenderTargetView* rtvs[1] = { rtv.Get() };
                s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);
                const float clearColor[4] = {0.0f, 0.0f, 0.0f, 1.0f};
                s.d3d11Context->ClearRenderTargetView(rtv.Get(), clearColor);

                // Where the eyes land in the backbuffer (= client area), after zoom and pan.
                // Single eye uses the rect whole; SBS / OverUnder split it; Anaglyph
                // overlays both eyes into it.
                rt::FitRect fit = rt::ComputePresentRect((int)s.previewWidth, (int)s.previewHeight);

                D3D11_VIEWPORT fullVp = { fit.x, fit.y, fit.w, fit.h, 0.0f, 1.0f };
                D3D11_VIEWPORT leftVp = fullVp;
                D3D11_VIEWPORT rightVp = fullVp;
                if (!singleEye) {
                    if (layout == ui::DisplayLayout::SideBySide) {
                        leftVp.Width  = fit.w * 0.5f;
                        rightVp.Width = fit.w * 0.5f;
                        rightVp.TopLeftX = fit.x + fit.w * 0.5f;
                    } else if (layout == ui::DisplayLayout::OverUnder) {
                        leftVp.Height  = fit.h * 0.5f;
                        rightVp.Height = fit.h * 0.5f;
                        rightVp.TopLeftY = fit.y + fit.h * 0.5f;
                    }
                }

                ID3D11BlendState* leftBlend = nullptr;
                ID3D11BlendState* rightBlend = nullptr;
                if (!singleEye && layout == ui::DisplayLayout::Anaglyph) {
                    leftBlend = s.anaglyphRedBS.Get();
                    rightBlend = s.anaglyphCyanBS.Get();
                }

                if (showLeft) {
                    blitViewToHalf(s, chL, leftIdx, vL.subImage.imageArrayIndex, vL.subImage.imageRect,
                                   rtv.Get(), leftVp, leftBlend);
                }

                // Blit right eye
                if (showRight && proj.viewCount > 1) {
                    const auto& vR = proj.views[1];
                    auto& chR = const_cast<rt::Swapchain&>(*chRPtr);
                    uint32_t rightIdx = 0;
                    if (chR.lastReleased != UINT32_MAX && chR.lastReleased < chR.imageCount) {
                        rightIdx = chR.lastReleased;
                    } else if (chR.lastAcquired != UINT32_MAX && chR.lastAcquired < chR.imageCount) {
                        rightIdx = chR.lastAcquired;
                    }
                    blitViewToHalf(s, chR, rightIdx, vR.subImage.imageArrayIndex, vR.subImage.imageRect,
                                   rtv.Get(), rightVp, rightBlend);
                } else if (showRight && !showLeft) {
                    // Mirror left eye if right-only mode but only one view
                    blitViewToHalf(s, chL, leftIdx, vL.subImage.imageArrayIndex, vL.subImage.imageRect,
                                   rtv.Get(), rightVp, rightBlend);
                }

                // Present D3D11 (may be deferred if overlays are pending). Sync interval 0:
                // this is a mirror, and it runs on the app's render thread, so waiting for
                // vblank here would pace the application to the monitor rather than to the
                // headset it thinks it is driving.
                if (!skipPresent) {
                    MSG msg;
                    while (PeekMessageW(&msg, s.hwnd, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
                    s.previewSwapchain->Present(0, 0);
                } else {
                    g_presentPending = true;
                }
            }

            // MCP Integration - capture a screenshot if one was asked for. xrEndFrame
            // drains the request before it decides whether this frame is due, so a pending
            // shot is what forced the frame we are in.
            if (mcp::g_screenshotRequested) {
                mcp::CaptureScreenshot(s.d3d11Device.Get(), s.d3d11Context.Get(), s.previewSwapchain.Get());
                std::string p = mcp::GetSimulatorDataPath() + "\\screenshot.bmp";
                std::wstring wp(p.begin(), p.end());
                ui::NotifyScreenshotSaved(wp);
            }

        } else {
            // ===== D3D12 PATH (GDI-based presentation) =====
            if (!s.previewRT12) return;

            // The cross-queue sync that orders this read after the app's own frame work
            // is raised once for the whole frame, in closePreviewSlot.

            // Blit using D3D12 copy commands → readback → GDI (no DXGI Present, no hook conflicts)
            if (proj.viewCount > 1) {
                const auto& vR = proj.views[1];
                auto& chR = const_cast<rt::Swapchain&>(*chRPtr);
                uint32_t rightIdx = 0;
                if (chR.lastReleased != UINT32_MAX && chR.lastReleased < chR.imageCount) {
                    rightIdx = chR.lastReleased;
                } else if (chR.lastAcquired != UINT32_MAX && chR.lastAcquired < chR.imageCount) {
                    rightIdx = chR.lastAcquired;
                }
                blitD3D12ToPreview(s, chL, leftIdx, vL.subImage.imageArrayIndex, rectL,
                                   &chR, rightIdx, vR.subImage.imageArrayIndex, rectR,
                                   layout, viewMode);
            } else {
                blitD3D12ToPreview(s, chL, leftIdx, vL.subImage.imageArrayIndex, rectL,
                                   nullptr, 0, 0, rectL, layout, viewMode);
            }

            // No Present call needed: the composite is shown by paintPreviewComposite once
            // the GPU has finished it, which is also where a screenshot is served from - not
            // here, where the quad layers of this frame have yet to go into the target.
        }
    }
}

static void renderQuadLayer(rt::Session& s, const XrCompositionLayerQuad* quad) {
    if (!quad) return;
    // D3D12 sessions use previewRT12, not previewSwapchain
    if (!s.previewSwapchain && !s.previewRT12) return;
    // A layer of a frame the mirror is skipping. presentProjection skipped the eyes under
    // it and will not present, so drawing this would be work nothing ever shows.
    if (!g_previewDueThisFrame) return;

    auto it = rt::g_swapchains.find(quad->subImage.swapchain);
    if (it == rt::g_swapchains.end()) return;

    auto& chain = it->second;

    // Get texture dimensions from the quad subImage
    uint32_t texWidth = quad->subImage.imageRect.extent.width;
    uint32_t texHeight = quad->subImage.imageRect.extent.height;
    if (texWidth == 0) texWidth = chain.width;
    if (texHeight == 0) texHeight = chain.height;

    // Get texture index
    uint32_t texIdx = (chain.lastReleased != UINT32_MAX) ? chain.lastReleased :
                      (chain.lastAcquired != UINT32_MAX) ? chain.lastAcquired : 0;

    // --- D3D12 quad compositing -----------------------------------------------------------------------
    // Rasterised into the same render target the eyes were just drawn into, once per eye
    // half, and read back with them as one image. The corners are projected into each eye's
    // view space here; the vertex shader applies that eye's asymmetric projection so the
    // interpolation is perspective-correct and a quad crossing behind the eye is clipped
    // rather than mis-drawn.
    if (s.usesD3D12) {
        if (texIdx >= chain.images12.size() || !chain.images12[texIdx]) return;
        if (chain.imageStates12.size() <= texIdx) return;
        if (!s.previewCmdList || !s.previewQueue12 || !s.previewFence || !s.hwnd) return;
        if (!s.previewQuadRootSig || !s.previewQuadPSO[0]) return;

        const uint32_t qArraySize = chain.arraySize ? chain.arraySize : 1;
        if (quad->subImage.imageArrayIndex >= qArraySize) {
            static int badSlice = 0;
            if (++badSlice % 120 == 1) {
                Logf("[SimXR] quad: imageArrayIndex %u out of range (arraySize=%u)",
                     quad->subImage.imageArrayIndex, qArraySize);
            }
            return;
        }

        ID3D12Resource* quadTex = chain.images12[texIdx].Get();
        const D3D12_RESOURCE_DESC qd = quadTex->GetDesc();
        if (qd.SampleDesc.Count > 1) return;      // needs a resolve first; not worth it for a mirror

        // Share this frame's slot and its open render target with the eye pass. On a
        // quad-only frame nothing has opened the RT yet, so this is what clears it - which
        // is also what replaces the old "wipe the back buffer to black" step.
        rt::PreviewFrame12* slot = beginPreviewSlot(s);
        if (!slot) return;
        if (!beginPreviewRT(s, *slot)) return;
        ++slot->quadLayers;

        const D3D12_RESOURCE_DESC rtDesc = s.previewRT12->GetDesc();
        const int rtWidth = (int)rtDesc.Width, rtHeight = (int)rtDesc.Height;

        // Only the region the app declared valid is sampled.
        const rt::SubImageRect qrect = rt::ResolveSubImageRect(quad->subImage.imageRect, chain.width, chain.height, "quad");
        const float texW = (float)qd.Width, texH = (float)qd.Height;
        if (texW <= 0.0f || texH <= 0.0f) return;
        const float uvRect[4] = { (float)qrect.x / texW, (float)qrect.y / texH,
                                  (float)(qrect.x + qrect.w) / texW, (float)(qrect.y + qrect.h) / texH };

        D3D12_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = PreviewQuadSrvFormat(qd.Format);
        srvDesc.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        if (qd.DepthOrArraySize > 1) {
            srvDesc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2DARRAY;
            srvDesc.Texture2DArray.FirstArraySlice = quad->subImage.imageArrayIndex;
            srvDesc.Texture2DArray.ArraySize = 1;
            srvDesc.Texture2DArray.MipLevels = 1;
        } else {
            srvDesc.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
            srvDesc.Texture2D.MipLevels = 1;
        }
        const UINT slot2 = s.previewSrvSlot;
        s.previewSrvSlot = (s.previewSrvSlot + 1) % rt::kPreviewSrvSlots;
        D3D12_CPU_DESCRIPTOR_HANDLE cpu = s.previewSrvHeap->GetCPUDescriptorHandleForHeapStart();
        D3D12_GPU_DESCRIPTOR_HANDLE gpu = s.previewSrvHeap->GetGPUDescriptorHandleForHeapStart();
        cpu.ptr += (SIZE_T)slot2 * s.previewSrvStride;
        gpu.ptr += (UINT64)slot2 * s.previewSrvStride;
        s.d3d12Device->CreateShaderResourceView(quadTex, &srvDesc, cpu);

        auto& quadState = chain.imageStates12[texIdx];
        auto barrier12 = [&](D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
            if (before == after) return;
            D3D12_RESOURCE_BARRIER b = {}; b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            b.Transition.pResource = quadTex; b.Transition.StateBefore = before; b.Transition.StateAfter = after;
            b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            s.previewCmdList->ResourceBarrier(1, &b);
        };
        const D3D12_RESOURCE_STATES qstate = quadState;
        barrier12(qstate, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);

        float yaw, pitch, roll;
        rt::GetEffectiveHeadAngles(yaw, pitch, roll);
        bool headLocked = false;
        XrVector3f worldCorners[4];
        rt::QuadWorldCorners(*quad, yaw, pitch, roll, worldCorners, &headLocked);

        // The sRGB view: blending decodes the destination to linear and re-encodes it.
        D3D12_CPU_DESCRIPTOR_HANDLE rtvSrgb = s.previewRtvHeap->GetCPUDescriptorHandleForHeapStart();
        rtvSrgb.ptr += s.previewRtvStride;
        const rt::LayerBlend blendMode = rt::BlendForLayerFlags(quad->layerFlags);
        slot->quadSourceAlphaCoverage =
            blendMode == rt::LayerBlend::Opaque ? 1.0f : slot->quadSourceAlphaCoverage;
        s.previewCmdList->SetGraphicsRootSignature(s.previewQuadRootSig.Get());
        s.previewCmdList->SetPipelineState(s.previewQuadPSO[(int)blendMode].Get());
        s.previewCmdList->OMSetRenderTargets(1, &rtvSrgb, FALSE, nullptr);
        s.previewCmdList->SetGraphicsRootDescriptorTable(0, gpu);

        // Same split of the RT the eye pass used, so the quad lands over the right eye.
        const rt::FitRect present = rt::ComputePresentRect(rtWidth, rtHeight);
        const int presentX = (int)lroundf(present.x), presentY = (int)lroundf(present.y);
        const int presentW = (std::max)(1, (int)lroundf(present.w));
        const int presentH = (std::max)(1, (int)lroundf(present.h));
        const ui::ViewMode viewMode = ui::g_uiState.viewMode;
        const ui::DisplayLayout layout = ui::g_uiState.displayLayout;

        struct EyeRect { uint32_t eye; int x, y, w, h; };
        EyeRect eyes[2];
        int eyeCount = 0;
        if (viewMode != ui::ViewMode::BothEyes || layout == ui::DisplayLayout::Anaglyph) {
            const uint32_t eye = (viewMode == ui::ViewMode::RightEyeOnly) ? 1u : 0u;
            eyes[eyeCount++] = { eye, presentX, presentY, presentW, presentH };
        } else if (layout == ui::DisplayLayout::OverUnder) {
            const int h = presentH / 2;
            eyes[eyeCount++] = { 0u, presentX, presentY,     presentW, h };
            eyes[eyeCount++] = { 1u, presentX, presentY + h, presentW, h };
        } else {
            const int w = presentW / 2;
            eyes[eyeCount++] = { 0u, presentX,     presentY, w, presentH };
            eyes[eyeCount++] = { 1u, presentX + w, presentY, w, presentH };
        }

        for (int e = 0; e < eyeCount; ++e) {
            const EyeRect& er = eyes[e];
            if (er.w < 1 || er.h < 1) continue;
            if (!rt::QuadVisibleInEye(quad->eyeVisibility, er.eye)) continue;

            const XrPosef view = rt::ViewPoseFromAngles(er.eye, yaw, pitch, roll);
            const XrFovf fov = rt::GetViewFov(er.eye);

            float consts[rt::kQuadConstantCount] = {};
            float minPx = FLT_MAX, minPy = FLT_MAX;
            float maxPx = -FLT_MAX, maxPy = -FLT_MAX;
            for (int i = 0; i < 4; ++i) {
                const XrVector3f v = rt::WorldToView(worldCorners[i], view);
                consts[i * 4 + 0] = v.x;
                consts[i * 4 + 1] = v.y;
                consts[i * 4 + 2] = v.z;
                consts[i * 4 + 3] = 0.0f;
            }
            consts[16] = tanf(fov.angleLeft);
            consts[17] = tanf(fov.angleRight);
            consts[18] = tanf(fov.angleUp);
            consts[19] = tanf(fov.angleDown);
            if (consts[17] - consts[16] < 1e-6f || consts[18] - consts[19] < 1e-6f) continue;
            consts[20] = uvRect[0]; consts[21] = uvRect[1];
            consts[22] = uvRect[2]; consts[23] = uvRect[3];
            consts[24] = (blendMode == rt::LayerBlend::Opaque) ? 1.0f : 0.0f;
            s.previewCmdList->SetGraphicsRoot32BitConstants(1, rt::kQuadConstantCount, consts, 0);

            // Preserve the projected UI region for the branch's temporal UI-only
            // detector. These are the same clip equations the vertex shader uses.
            for (int i = 0; i < 4; ++i) {
                const float vx = consts[i * 4 + 0];
                const float vy = consts[i * 4 + 1];
                const float vz = consts[i * 4 + 2];
                if (vz >= -1e-4f) continue;
                const float clipX = (2.0f * vx + (consts[17] + consts[16]) * vz) /
                                    (consts[17] - consts[16]);
                const float clipY = (2.0f * vy + (consts[18] + consts[19]) * vz) /
                                    (consts[18] - consts[19]);
                const float ndcX = clipX / -vz;
                const float ndcY = clipY / -vz;
                const float px = er.x + (ndcX + 1.0f) * 0.5f * er.w;
                const float py = er.y + (1.0f - ndcY) * 0.5f * er.h;
                minPx = (std::min)(minPx, px); minPy = (std::min)(minPy, py);
                maxPx = (std::max)(maxPx, px); maxPy = (std::max)(maxPy, py);
            }
            if (minPx <= maxPx && minPy <= maxPy) {
                const int x0 = (std::max)(0, (std::min)(rtWidth, (int)floorf(minPx)));
                const int y0 = (std::max)(0, (std::min)(rtHeight, (int)floorf(minPy)));
                const int x1 = (std::max)(0, (std::min)(rtWidth, (int)ceilf(maxPx)));
                const int y1 = (std::max)(0, (std::min)(rtHeight, (int)ceilf(maxPy)));
                slot->quadRects[er.eye][0] = x0;
                slot->quadRects[er.eye][1] = y0;
                slot->quadRects[er.eye][2] = (std::max)(0, x1 - x0);
                slot->quadRects[er.eye][3] = (std::max)(0, y1 - y0);
            }

            const D3D12_VIEWPORT vp = { (float)er.x, (float)er.y, (float)er.w, (float)er.h, 0.0f, 1.0f };
            const D3D12_RECT scissor = { (std::max)(0, er.x), (std::max)(0, er.y),
                                         (std::min)(rtWidth, er.x + er.w),
                                         (std::min)(rtHeight, er.y + er.h) };
            s.previewCmdList->RSSetViewports(1, &vp);
            s.previewCmdList->RSSetScissorRects(1, &scissor);
            s.previewCmdList->DrawInstanced(6, 1, 0, 0);
            slot->quadComposed = true;
        }

        // Back to the state the app released it in, so its own barriers still line up.
        barrier12(D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, qstate);

        static int s_qlog = 0;
        if (g_logVerbose && ++s_qlog % 120 == 1) {
            Logf("[SimXR] D3D12 quad drawn: tex=%ux%u blend=%d space=%s eyeVis=%d size=(%.2f,%.2f)",
                 qrect.w, qrect.h, (int)blendMode, headLocked ? "VIEW" : "WORLD",
                 (int)quad->eyeVisibility, quad->size.width, quad->size.height);
        }
        return;
    }

    static int quadLogCount = 0;
    bool shouldLog = g_logVerbose && (++quadLogCount % 60 == 1);

    if (shouldLog) {
        Logf("[SimXR] Quad swapchain: handle=%llu, lastReleased=%u, lastAcquired=%u, texIdx=%u, imageCount=%u",
             (unsigned long long)quad->subImage.swapchain, chain.lastReleased, chain.lastAcquired,
             texIdx, chain.imageCount);
    }

    // Filled by whichever backend branch runs; lifetime is owned by the session's
    // temp-texture cache, so nothing here is created per frame in the steady state.
    ID3D11ShaderResourceView* quadSrv = nullptr;

    // Check if using OpenGL
    if (chain.backend == rt::Swapchain::Backend::OpenGL && !chain.imagesGL.empty()) {
        if (texIdx >= chain.imagesGL.size()) return;
        GLuint glTex = chain.imagesGL[texIdx];
        if (glTex == 0) return;

        // Save and switch to app's GL context
        HGLRC savedRC = wglGetCurrentContext();
        HDC savedDC = wglGetCurrentDC();

        // DEBUG: Log current context BEFORE switch
        if (shouldLog) {
            Logf("[SimXR] Quad GL context: current={RC=%p,DC=%p}, stored={RC=%p,DC=%p}",
                 savedRC, savedDC, s.glRC, s.glDC);
        }

        if (s.glRC && s.glDC) {
            BOOL switchResult = wglMakeCurrent(s.glDC, s.glRC);
            if (shouldLog) {
                HGLRC afterRC = wglGetCurrentContext();
                Logf("[SimXR] Quad GL context switch: result=%d, afterRC=%p (expected %p)",
                     switchResult, afterRC, s.glRC);
            }
        }

        // Ensure all GL commands are finished before reading
        glFinish();

        // DEBUG: Verify texture exists and is valid
        if (shouldLog) {
            GLboolean isValid = glIsTexture(glTex);
            Logf("[SimXR] Quad texture check: glTex=%u, glIsTexture=%d", glTex, isValid);
        }

        // Read pixels from GL texture using FBO (more reliable than glGetTexImage)
        std::vector<uint8_t>& pixels = s.glQuadPixels;   // reused across frames
        pixels.resize((size_t)texWidth * texHeight * 4);

        // Try FBO method first if available
        bool usedFBO = false;
        if (EnsureGLFramebufferFuncs()) {
            // Create a temporary FBO to read the texture
            GLuint readFBO = 0;
            g_glGenFramebuffers(1, &readFBO);
            g_glBindFramebuffer(GL_FRAMEBUFFER, readFBO);
            g_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, glTex, 0);

            GLenum fboStatus = g_glCheckFramebufferStatus(GL_FRAMEBUFFER);
            if (fboStatus == GL_FRAMEBUFFER_COMPLETE) {
                glReadPixels(0, 0, texWidth, texHeight, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
                usedFBO = true;
            } else {
                if (shouldLog) {
                    Logf("[SimXR] Quad FBO not complete: status=0x%X, falling back to glGetTexImage", fboStatus);
                }
            }

            // Cleanup FBO
            g_glBindFramebuffer(GL_FRAMEBUFFER, 0);
            g_glDeleteFramebuffers(1, &readFBO);
        }

        // Fallback to glGetTexImage
        if (!usedFBO) {
            glBindTexture(GL_TEXTURE_2D, glTex);
            glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
        }

        // Debug: check pixel values before flip
        if (shouldLog) {
            uint32_t pixelSum = 0;
            for (size_t i = 0; i < std::min((size_t)4000, pixels.size()); i++) pixelSum += pixels[i];
            Logf("[SimXR] Quad GL pixels: sum=%u, first 4=[%d,%d,%d,%d][%d,%d,%d,%d][%d,%d,%d,%d][%d,%d,%d,%d]",
                 pixelSum, pixels[0], pixels[1], pixels[2], pixels[3],
                 pixels[4], pixels[5], pixels[6], pixels[7],
                 pixels[8], pixels[9], pixels[10], pixels[11],
                 pixels[12], pixels[13], pixels[14], pixels[15]);
            // Check middle of image
            size_t midIdx = (texHeight/2 * texWidth + texWidth/2) * 4;
            if (midIdx + 3 < pixels.size()) {
                Logf("[SimXR] Quad GL middle pixel: [%d,%d,%d,%d]",
                     pixels[midIdx], pixels[midIdx+1], pixels[midIdx+2], pixels[midIdx+3]);
            }
        }

        // Flip vertically (OpenGL has Y=0 at bottom)
        const uint32_t rowSize = texWidth * 4;
        std::vector<uint8_t> tempRow(rowSize);
        for (uint32_t y = 0; y < texHeight / 2; y++) {
            uint8_t* topRow = pixels.data() + y * rowSize;
            uint8_t* bottomRow = pixels.data() + (texHeight - 1 - y) * rowSize;
            memcpy(tempRow.data(), topRow, rowSize);
            memcpy(topRow, bottomRow, rowSize);
            memcpy(bottomRow, tempRow.data(), rowSize);
        }

        // Store quad layer pixels only when a screenshot actually asked for them;
        // continuously copying the whole quad every frame served nothing else.
        if (mcp::g_screenshotRequested) {
            mcp::StoreQuadLayerPixels(pixels.data(), texWidth, texHeight);
        }

        // Restore GL context
        if (savedRC) wglMakeCurrent(savedDC, savedRC);

        // Upload into a cached D3D11 texture. A GL_SRGB8_ALPHA8 quad holds sRGB-encoded
        // bytes, so it needs the _SRGB view to decode to linear before the blend - otherwise
        // it is blended as if it were linear and comes out wrong against the eyes.
        const DXGI_FORMAT glQuadFormat =
            (chain.glInternalFormat == GL_SRGB8_ALPHA8) ? DXGI_FORMAT_R8G8B8A8_UNORM_SRGB
                                                        : DXGI_FORMAT_R8G8B8A8_UNORM;
        ID3D11Texture2D* uploadTex = acquireTempTexture(s, s.quadTempCache, s.quadTempNext,
                                                        texWidth, texHeight, glQuadFormat, &quadSrv);
        if (!uploadTex) {
            if (shouldLog) Log("[SimXR] renderQuadLayer: Failed to create D3D11 texture from GL pixels");
            return;
        }
        s.d3d11Context->UpdateSubresource(uploadTex, 0, nullptr, pixels.data(), texWidth * 4, 0);

        if (shouldLog) {
            Logf("[SimXR] Rendering quad layer (OpenGL): size=%.2fx%.2f, texSize=%ux%u, glTex=%u",
                 quad->size.width, quad->size.height, texWidth, texHeight, glTex);
        }
    } else if (!chain.images.empty()) {
        // D3D11 path
        if (texIdx >= chain.images.size() || !chain.images[texIdx]) return;

        // Skip depth formats
        D3D11_TEXTURE2D_DESC srcDesc;
        chain.images[texIdx]->GetDesc(&srcDesc);
        if (srcDesc.Format == DXGI_FORMAT_D32_FLOAT || srcDesc.Format == DXGI_FORMAT_D24_UNORM_S8_UINT ||
            srcDesc.Format == DXGI_FORMAT_D16_UNORM || srcDesc.Format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT) {
            return;
        }

        // Convert typeless formats to typed formats for SRV creation
        DXGI_FORMAT typedFormat = srcDesc.Format;
        switch (srcDesc.Format) {
            case DXGI_FORMAT_R8G8B8A8_TYPELESS:
                typedFormat = (chain.format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) ? DXGI_FORMAT_R8G8B8A8_UNORM_SRGB : DXGI_FORMAT_R8G8B8A8_UNORM;
                break;
            case DXGI_FORMAT_B8G8R8A8_TYPELESS:
                typedFormat = (chain.format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) ? DXGI_FORMAT_B8G8R8A8_UNORM_SRGB : DXGI_FORMAT_B8G8R8A8_UNORM;
                break;
            case DXGI_FORMAT_R16G16B16A16_TYPELESS:
                typedFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
                break;
            case DXGI_FORMAT_R32G32B32A32_TYPELESS:
                typedFormat = DXGI_FORMAT_R32G32B32A32_FLOAT;
                break;
            case DXGI_FORMAT_R10G10B10A2_TYPELESS:
                typedFormat = DXGI_FORMAT_R10G10B10A2_UNORM;
                break;
            // Typed UNORM source in an sRGB swapchain: sample through the
            // matching _SRGB view so the GPU decodes sRGB->linear (the preview
            // RTV re-encodes); otherwise we double-encode and colors shift
            // (magenta -> purple).
            case DXGI_FORMAT_R8G8B8A8_UNORM:
                if (chain.format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB)
                    typedFormat = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
                break;
            case DXGI_FORMAT_B8G8R8A8_UNORM:
                if (chain.format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB)
                    typedFormat = DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
                break;
            default:
                break; // Already typed or unknown
        }

        const uint32_t arraySize = chain.arraySize ? chain.arraySize : 1;
        const uint32_t arraySlice = quad->subImage.imageArrayIndex;
        if (arraySlice >= arraySize) {
            if (shouldLog) {
                Logf("[SimXR] quad: imageArrayIndex %u out of range (arraySize=%u)", arraySlice, arraySize);
            }
            return;
        }

        // Clamp the app's rect the way the eye path does, and size the temp texture to the
        // rect rather than the whole image - the copy lands at (0,0), so a full-size temp
        // would leave the sub-rect shrunk into the corner of the sampled UV range.
        const rt::SubImageRect rect = rt::ResolveSubImageRect(quad->subImage.imageRect,
                                                              srcDesc.Width, srcDesc.Height, "quad");

        // One slice at the rect's size, from the cache: a full-size temp would leave
        // the sub-rect shrunk into the corner of the sampled UV range.
        ID3D11Texture2D* quadTex = acquireTempTexture(s, s.quadTempCache, s.quadTempNext,
                                                      rect.w, rect.h, typedFormat, &quadSrv);
        if (!quadTex) return;

        const uint32_t srcSubresource = D3D11CalcSubresource(0, arraySlice, chain.mipCount ? chain.mipCount : 1);
        ID3D11Texture2D* copySrc = chain.images[texIdx].Get();
        uint32_t copySubresource = srcSubresource;

        // CopySubresourceRegion cannot resolve MSAA, and ResolveSubresource cannot crop, so a
        // multisampled quad needs a full-size resolve target in between.
        ComPtr<ID3D11Texture2D> resolved;
        if (srcDesc.SampleDesc.Count > 1) {
            D3D11_TEXTURE2D_DESC resolveDesc = {};
            resolveDesc.Width = srcDesc.Width;
            resolveDesc.Height = srcDesc.Height;
            resolveDesc.MipLevels = 1;
            resolveDesc.ArraySize = 1;
            resolveDesc.Format = typedFormat;
            resolveDesc.SampleDesc.Count = 1;
            resolveDesc.Usage = D3D11_USAGE_DEFAULT;
            resolveDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            if (FAILED(s.d3d11Device->CreateTexture2D(&resolveDesc, nullptr, resolved.GetAddressOf()))) return;
            s.d3d11Context->ResolveSubresource(resolved.Get(), 0, copySrc, srcSubresource, typedFormat);
            copySrc = resolved.Get();
            copySubresource = 0;
        }

        D3D11_BOX box = { rect.x, rect.y, 0, rect.x + rect.w, rect.y + rect.h, 1 };
        s.d3d11Context->CopySubresourceRegion(quadTex, 0, 0, 0, 0, copySrc, copySubresource, &box);

        if (shouldLog) {
            Logf("[SimXR] Rendering quad layer (D3D11): size=%.2fx%.2f, rect=%u,%u %ux%u, typedFmt=%d, srcFmt=%d, arraySlice=%u",
                 quad->size.width, quad->size.height, rect.x, rect.y, rect.w, rect.h,
                 typedFormat, srcDesc.Format, arraySlice);
        }
    } else {
        if (shouldLog) Log("[SimXR] renderQuadLayer: No valid images in swapchain");
        return;
    }

    // A quad-only frame never runs the projection path, so this is the only chance to compile.
    if (!rt::InitBlitResources(s)) return;

    // For a D3D11 session this is the app's own immediate context; leave it as we found it.
    D3D11StateBackup stateBackup(s.d3d11Context.Get());

    // The guard at the top of the function only proves one of the two preview targets exists,
    // and the D3D12 one has already returned by here.
    if (!s.previewSwapchain) return;
    ComPtr<ID3D11Texture2D> bb;
    if (FAILED(s.previewSwapchain->GetBuffer(0, IID_PPV_ARGS(bb.GetAddressOf())))) return;

    ComPtr<ID3D11RenderTargetView> rtv;
    if (!rt::CreatePreviewRtv(s, bb.Get(), rtv)) return;

    // The SRV came out of the temp-texture cache with the staging copy above.
    if (!quadSrv) return;

    // Calculate viewports matching the projection layer layout (letterboxed)
    const auto layout = ui::g_uiState.displayLayout;
    const auto viewMode = ui::g_uiState.viewMode;
    const bool singleEye = (viewMode != ui::ViewMode::BothEyes);

    // Mirror the rect the projection layer lands in, zoom and pan included.
    rt::FitRect qFit = rt::ComputePresentRect((int)s.previewWidth, (int)s.previewHeight);

    D3D11_VIEWPORT leftVp  = { qFit.x, qFit.y, qFit.w, qFit.h, 0.0f, 1.0f };
    D3D11_VIEWPORT rightVp = leftVp;
    if (!singleEye) {
        if (layout == ui::DisplayLayout::SideBySide) {
            leftVp.Width  = qFit.w * 0.5f;
            rightVp.Width = qFit.w * 0.5f;
            rightVp.TopLeftX = qFit.x + qFit.w * 0.5f;
        } else if (layout == ui::DisplayLayout::OverUnder) {
            leftVp.Height  = qFit.h * 0.5f;
            rightVp.Height = qFit.h * 0.5f;
            rightVp.TopLeftY = qFit.y + qFit.h * 0.5f;
        }
    }

    // The quad is placed by its projected corners, not by the viewport, so each eye gets the
    // whole eye rect and clip-space positioning does the rest. D3D11 clips to the frustum,
    // which the viewport transform maps onto exactly this rect, so a quad that overhangs the
    // eye cannot bleed into the other half.
    struct EyeDraw { uint32_t eye; const D3D11_VIEWPORT* vp; };
    EyeDraw eyes[2];
    int eyeCount = 0;
    if (viewMode == ui::ViewMode::RightEyeOnly) {
        eyes[eyeCount++] = { 1u, &rightVp };
    } else if (viewMode == ui::ViewMode::LeftEyeOnly) {
        eyes[eyeCount++] = { 0u, &leftVp };
    } else {
        eyes[eyeCount++] = { 0u, &leftVp };
        eyes[eyeCount++] = { 1u, &rightVp };
    }

    float yaw, pitch, roll;
    rt::GetEffectiveHeadAngles(yaw, pitch, roll);

    bool headLocked = false;
    XrVector3f worldCorners[4];
    rt::QuadWorldCorners(*quad, yaw, pitch, roll, worldCorners, &headLocked);

    const rt::LayerBlend blendMode = rt::BlendForLayerFlags(quad->layerFlags);

    // Set up shared render state
    s.d3d11Context->VSSetShader(s.quadVS.Get(), nullptr, 0);
    s.d3d11Context->PSSetShader(s.blitPS.Get(), nullptr, 0);

    ID3D11Buffer* cbs[] = { s.quadCB.Get() };
    s.d3d11Context->VSSetConstantBuffers(0, 1, cbs);

    ID3D11ShaderResourceView* srvs[] = { quadSrv };
    s.d3d11Context->PSSetShaderResources(0, 1, srvs);
    ID3D11SamplerState* samplers[] = { s.samplerState.Get() };
    s.d3d11Context->PSSetSamplers(0, 1, samplers);

    s.d3d11Context->IASetInputLayout(nullptr);
    s.d3d11Context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);

    s.d3d11Context->OMSetDepthStencilState(nullptr, 0);
    s.d3d11Context->RSSetState(s.noCullRS.Get());

    ID3D11RenderTargetView* rtvs[1] = { rtv.Get() };
    s.d3d11Context->OMSetRenderTargets(1, rtvs, nullptr);

    const bool anaglyph = (!singleEye && layout == ui::DisplayLayout::Anaglyph);

    for (int e = 0; e < eyeCount; ++e) {
        const uint32_t eye = eyes[e].eye;
        if (!rt::QuadVisibleInEye(quad->eyeVisibility, eye)) continue;

        const XrPosef view = rt::ViewPoseFromAngles(eye, yaw, pitch, roll);
        const XrFovf fov = rt::GetViewFov(eye);
        const float tanL = tanf(fov.angleLeft), tanR = tanf(fov.angleRight);
        const float tanU = tanf(fov.angleUp),   tanD = tanf(fov.angleDown);
        if (tanR - tanL < 1e-6f || tanU - tanD < 1e-6f) continue;

        // Projection, written without dividing by view depth so that it stays finite for a
        // corner at or behind the eye. Multiplying an NDC value back up by w would be NaN
        // exactly there, and the near plane is the case the GPU clipper is here to handle.
        const float ax = 2.0f / (tanR - tanL), bx = (tanR + tanL) / (tanR - tanL);
        const float ay = 2.0f / (tanU - tanD), by = (tanU + tanD) / (tanU - tanD);

        float clip[4][4];
        static const int kStripOrder[4] = { 0, 1, 3, 2 };   // fan order -> TL, TR, BL, BR
        for (int i = 0; i < 4; ++i) {
            const XrVector3f v = rt::WorldToView(worldCorners[kStripOrder[i]], view);
            const float w = -v.z;
            clip[i][0] = ax * v.x + bx * v.z;
            clip[i][1] = ay * v.y + by * v.z;
            clip[i][2] = 0.5f * w;   // mid-range: 0 <= z <= w reduces to w >= 0, the near clip
            clip[i][3] = w;
        }

        D3D11_MAPPED_SUBRESOURCE mapped{};
        if (FAILED(s.d3d11Context->Map(s.quadCB.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) continue;
        memcpy(mapped.pData, clip, sizeof(clip));
        s.d3d11Context->Unmap(s.quadCB.Get(), 0);

        // Anaglyph fuses the eyes by channel; the two eyes' quads land at genuinely different
        // places, so without the mask they would ghost instead.
        UINT8 mask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
        if (anaglyph) {
            mask = (eye == 0) ? (UINT8)D3D11_COLOR_WRITE_ENABLE_RED
                              : (UINT8)(D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE);
        }
        ID3D11BlendState* blend = rt::GetLayerBlendState(s, blendMode, mask);
        if (!blend) continue;
        s.d3d11Context->OMSetBlendState(blend, nullptr, 0xFFFFFFFF);

        s.d3d11Context->RSSetViewports(1, eyes[e].vp);
        s.d3d11Context->Draw(4, 0);
    }

    if (shouldLog) {
        Logf("[SimXR] Quad layer: blend=%s space=%s eyeVis=%d pose=(%.2f,%.2f,%.2f) size=(%.2f,%.2f)",
             blendMode == rt::LayerBlend::Opaque ? "opaque" :
             blendMode == rt::LayerBlend::Premultiplied ? "premultiplied" : "unpremultiplied",
             headLocked ? "VIEW" : "WORLD", (int)quad->eyeVisibility,
             quad->pose.position.x, quad->pose.position.y, quad->pose.position.z,
             quad->size.width, quad->size.height);
    }

    // Cleanup
    ID3D11ShaderResourceView* nullSRV[1] = { nullptr };
    s.d3d11Context->PSSetShaderResources(0, 1, nullSRV);
}

// Draw the newest preview frame the GPU has actually finished into the GDI back buffer.
// Nothing here waits: a frame that is not ready yet stays pending and is picked up on a
// later call, and if two are ready only the newest is painted - painting the older one
// first would just be overdrawn in the same call.
static void consumeCompletedPreviewFrame(rt::Session& s) {
    if (!s.previewFence || !s.hwnd) return;
    const UINT64 done = s.previewFence->GetCompletedValue();

    int newest = -1;
    for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
        const rt::PreviewFrame12& f = s.previewFrames[i];
        if (!f.pending || f.fenceValue > done) continue;
        if (newest < 0 || f.fenceValue > s.previewFrames[newest].fenceValue) newest = (int)i;
    }
    if (newest < 0) return;

    for (UINT i = 0; i < rt::kPreviewFrames; ++i) {
        rt::PreviewFrame12& f = s.previewFrames[i];
        if (f.pending && f.fenceValue <= done && (int)i != newest) f.pending = false;
    }

    rt::PreviewFrame12& f = s.previewFrames[newest];
    f.pending = false;

    // Every layer of the frame - eyes and quads alike - was composited into the render
    // target on the GPU, so this shows one finished image, and captures it if asked.
    paintPreviewComposite(s, f);
}

static XrResult XRAPI_PTR xrEndFrame_runtime(XrSession, const XrFrameEndInfo* info) {
    // Reentrance guard: when our preview swapchain calls Present(), Steam's overlay
    // (gameoverlayrenderer64) hooks it, which can trigger UEVR to re-submit frames
    // through the OpenXR API layer chain, re-entering this function and causing
    // infinite recursion → EXCEPTION_STACK_OVERFLOW.
    static std::atomic_flag inEndFrame = ATOMIC_FLAG_INIT;
    if (inEndFrame.test_and_set(std::memory_order_acquire)) {
        static std::atomic<int> reentrantCount{0};
        const int count = reentrantCount.fetch_add(1, std::memory_order_relaxed) + 1;
        if (count <= 5) {
            Logf("[SimXR] xrEndFrame: BLOCKED concurrent/reentrant call #%d", count);
        }
        return XR_SUCCESS;
    }

    LARGE_INTEGER endFrameStart{};
    QueryPerformanceCounter(&endFrameStart);

    static int frameCount = 0;
    frameCount++;
    mcp::g_runtimeFrameCount.store((uint64_t)frameCount, std::memory_order_release);

    // Log every frame for first 10 frames, then every 60 frames
    bool shouldLog = g_logVerbose && ((frameCount <= 10) || (frameCount % 60 == 1));

    if (shouldLog) {
        Logf("[SimXR] xrEndFrame called (frame #%d)", frameCount);
    }

    // Check D3D12 device status
    if (rt::g_session.usesD3D12 && rt::g_session.d3d12Device && shouldLog) {
        HRESULT reason = rt::g_session.d3d12Device->GetDeviceRemovedReason();
        if (reason != S_OK) {
            Logf("[SimXR] xrEndFrame: D3D12 DEVICE REMOVED! reason=0x%08X", (unsigned)reason);
        }
    }

    if (!info) {
        Log("[SimXR] xrEndFrame: ERROR - info is null");
        inEndFrame.clear(std::memory_order_release);
        return XR_ERROR_VALIDATION_FAILURE;
    }

    // Drain the requests that decide whether this frame has to reach the mirror, before
    // deciding it. Every backend's screenshot path then just tests g_screenshotRequested.
    if (mcp::g_commandsDue) mcp::CheckScreenshotRequest();

    // Frame-burst capture: the optional pose step lands at this frame boundary,
    // so the frame submitted right now (rendered with the OLD pose) is the
    // burst's baseline and every later frame shows the app catching up.
    if (mcp::g_commandsDue) {
        mcp::BurstCommand bc = mcp::CheckBurstCommand();
        if (bc.valid && !rt::g_session.usesD3D12) {
            // A burst is recorded out of the D3D12 preview's DIB back buffer, the only place
            // a composited frame sits in CPU memory. The D3D11 and OpenGL previews go
            // straight to a swapchain, so say so rather than leaving a poller waiting on a
            // burst_done.json that is never coming.
            Log("[SimXR] burst: only D3D12 and Vulkan sessions can be recorded");
            mcp::WriteCommandAck("burst", false);
        } else if (bc.valid) {
            if (bc.pose.valid) {
                rt::g_headPos.x = bc.pose.x;
                rt::g_headPos.y = bc.pose.y;
                rt::g_headPos.z = bc.pose.z;
                rt::g_headYaw = bc.pose.yaw;
                rt::g_headPitch = bc.pose.pitch;
                if (bc.pose.hasRoll) rt::g_headRoll = bc.pose.roll;
            }
            mcp::BurstStart(bc.frames);
            mcp::WriteCommandAck("burst", true);
        }
    }

    // Decide once, here, whether the mirror updates this frame; every preview path below
    // hangs off it (see g_previewDueThisFrame). A pending screenshot or a running burst
    // overrides the cap - both asked for a particular frame, not for the next one the rate
    // happens to allow, and with the mirror off there would be no next one.
    g_previewDueThisFrame = ui::PreviewFrameDue() || mcp::g_screenshotRequested || mcp::g_burstActive;

    // Order the app's Vulkan queue against the compositor's D3D12 queue. Skipped when the
    // mirror is not due this frame: nothing reads the app's images then.
    if (g_previewDueThisFrame) vkrt::FrameSyncBegin(rt::g_session);

    if (shouldLog) {
        Logf("[SimXR] xrEndFrame: layers=%u", info->layerCount);
    }

    // First pass: count layer types to know if we need to defer Present
    int projectionCount = 0, quadCount = 0, cylinderCount = 0, otherCount = 0;
    bool hasStereoProjection = false;
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader* base = info->layers[i];
        if (!base) continue;
        switch (base->type) {
            case XR_TYPE_COMPOSITION_LAYER_PROJECTION: {
                projectionCount++;
                const auto* projection =
                    reinterpret_cast<const XrCompositionLayerProjection*>(base);
                if (projection->views && projection->viewCount >= 2 &&
                    projection->views[0].subImage.imageRect.extent.width > 0 &&
                    projection->views[0].subImage.imageRect.extent.height > 0 &&
                    projection->views[1].subImage.imageRect.extent.width > 0 &&
                    projection->views[1].subImage.imageRect.extent.height > 0) {
                    hasStereoProjection = true;
                }
                break;
            }
            case XR_TYPE_COMPOSITION_LAYER_QUAD: quadCount++; break;
            case (XrStructureType)37: cylinderCount++; break; // XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR
            default: otherCount++; break;
        }
    }
    const auto projectionTimingNow = perf::ProjectionTimingTracker::Clock::now();
    rt::g_projectionTiming.Observe(hasStereoProjection, projectionTimingNow);
    flicker::ObserveSubmission((uint64_t)frameCount, (uint32_t)projectionCount,
                               info->layerCount);

    // Determine if we need to defer Present for overlay layers
    bool hasOverlays = (quadCount > 0 || cylinderCount > 0);
    g_presentPending = false;

    // Second pass: render projection layers (background)
    // If there are overlays, skip Present until after they're rendered
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader* base = info->layers[i];
        if (!base) continue;

        if (base->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
            const auto* proj = reinterpret_cast<const XrCompositionLayerProjection*>(base);

            // Capture the FOV+pose+rect the app is submitting into the
            // projection log. MCP get_projection_log returns the recent
            // window so the caller can diff "what app told the
            // compositor" vs "what the simulator is configured to use."
            if (proj->viewCount >= 1 && proj->views) {
                mcp::ProjLogEntry e{};
                e.frame   = frameCount;
                e.poseQx  = proj->views[0].pose.orientation.x;
                e.poseQy  = proj->views[0].pose.orientation.y;
                e.poseQz  = proj->views[0].pose.orientation.z;
                e.poseQw  = proj->views[0].pose.orientation.w;
                e.posX    = proj->views[0].pose.position.x;
                e.posY    = proj->views[0].pose.position.y;
                e.posZ    = proj->views[0].pose.position.z;
                uint32_t cap = (proj->viewCount >= 2) ? 2u : 1u;
                for (uint32_t v = 0; v < cap; ++v) {
                    e.aL[v] = proj->views[v].fov.angleLeft;
                    e.aR[v] = proj->views[v].fov.angleRight;
                    e.aU[v] = proj->views[v].fov.angleUp;
                    e.aD[v] = proj->views[v].fov.angleDown;
                    e.rectX[v] = proj->views[v].subImage.imageRect.offset.x;
                    e.rectY[v] = proj->views[v].subImage.imageRect.offset.y;
                    e.rectW[v] = proj->views[v].subImage.imageRect.extent.width;
                    e.rectH[v] = proj->views[v].subImage.imageRect.extent.height;
                }
                mcp::g_projLog[mcp::g_projLogHead] = e;
                mcp::g_projLogHead = (mcp::g_projLogHead + 1) % mcp::PROJ_LOG_CAPACITY;
                if (mcp::g_projLogCount < mcp::PROJ_LOG_CAPACITY) ++mcp::g_projLogCount;
                mcp::g_lastProjEntry = e;
            }

            presentProjection(rt::g_session, *proj, hasOverlays);  // skipPresent if overlays pending
        }
    }
    LARGE_INTEGER afterProjection{};
    QueryPerformanceCounter(&afterProjection);

    // No projection layer this frame - the app is on a 2D-only screen. Bring the
    // preview up ourselves (nothing else will) and clear it, so the overlay pass
    // below has somewhere to composite instead of bailing out at its
    // !previewSwapchain && !previewRT12 guard.
    if (projectionCount == 0) {
        ensurePreviewWithoutProjection(rt::g_session);
        if (!g_previewDueThisFrame) {
            // Nothing paints this frame, so there is nothing to wipe either.
        } else if (!rt::g_session.usesD3D12) {
            clearPreviewToBlack(rt::g_session);
        } else if (!hasOverlays) {
            // D3D12 normally clears as part of opening the render target, which whichever
            // layer pass runs first does - so the black and the layers over it reach the
            // window in one repaint. A frame carrying no layers at all has no such pass,
            // so open and close the target here to wipe the last 3D frame off the mirror.
            if (rt::PreviewFrame12* slot = beginPreviewSlot(rt::g_session)) {
                beginPreviewRT(rt::g_session, *slot);
            }
        }
    }

    // Third pass: render overlay layers (quad, cylinder) on top of the projection
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader* base = info->layers[i];
        if (!base) continue;

        switch (base->type) {
            case XR_TYPE_COMPOSITION_LAYER_QUAD: {
                const auto* quad = reinterpret_cast<const XrCompositionLayerQuad*>(base);
                renderQuadLayer(rt::g_session, quad);
                break;
            }
            case (XrStructureType)37: {
                // TODO: Implement cylinder layer rendering
                break;
            }
            default:
                break;
        }
    }
    LARGE_INTEGER afterOverlays{};
    QueryPerformanceCounter(&afterOverlays);

    // Every layer of this frame has now been recorded, so hand the slot to the painter and
    // paint whichever earlier frame the GPU has finished in the meantime.
    if (rt::g_session.usesD3D12) {
        closePreviewSlot(rt::g_session, (uint32_t)frameCount, projectionCount > 0);
        vkrt::FrameSyncEnd(rt::g_session);
        // Shows a finished composite if the GPU has produced one, and serves the
        // screenshot and burst requests off the very pixels it showed.
        consumeCompletedPreviewFrame(rt::g_session);
    }
    LARGE_INTEGER afterDetectionAndPaint{};
    QueryPerformanceCounter(&afterDetectionAndPaint);

    // MCP Integration - write frame status BEFORE Present (Present may block on D3D12)
    mcp::WriteFrameStatus(frameCount, rt::g_session.previewWidth, rt::g_session.previewHeight,
                          "RGBA8", mcp::GetSessionStateName((int)rt::g_session.state),
                          rt::g_headYaw, rt::g_headPitch, rt::g_headRoll,
                          rt::g_headPos.x, rt::g_headPos.y, rt::g_headPos.z);

    // Drain MCP projection-log dump request if pending.
    if (mcp::g_commandsDue && mcp::CheckProjLogDumpRequest()) {
        mcp::DumpProjectionLog();
    }
    // Every command file has been looked at for this change notification.
    mcp::g_commandsDue = false;

    // D3D11 and OpenGL capture from the preview swapchain inside presentProjection, which a
    // frame carrying no projection layer never reaches - so on a 2D-only frame the request
    // was drained but never served, and g_previewDueThisFrame stayed latched on it, quietly
    // disabling Mirror Rate. Take the shot off the back buffer here instead, before the
    // deferred Present below makes its contents undefined.
    if (!rt::g_session.usesD3D12 && mcp::g_screenshotRequested && g_previewDueThisFrame &&
        rt::g_session.previewSwapchain && rt::g_session.d3d11Device) {
        mcp::CaptureScreenshot(rt::g_session.d3d11Device.Get(), rt::g_session.d3d11Context.Get(),
                               rt::g_session.previewSwapchain.Get());
        const std::string shotPath = mcp::GetSimulatorDataPath() + "\\screenshot.bmp";
        ui::NotifyScreenshotSaved(std::wstring(shotPath.begin(), shotPath.end()));
    }

    // Now Present after all layers are rendered (D3D11/GL only; D3D12 uses GDI in blit function)
    if (g_presentPending) {
        auto& s = rt::g_session;
        MSG msg;
        while (PeekMessageW(&msg, s.hwnd, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }

        if (s.usesD3D12) {
            // D3D12 GDI-based path: blitD3D12ToPreview already painted via GDI, nothing to do
            static uint64_t deferredD3D12Count = 0;
            if (++deferredD3D12Count <= 5 || deferredD3D12Count % 120 == 1) {
                Log("[SimXR] Deferred D3D12: composed GDI preview already staged");
            }
        } else if (s.previewSwapchain) {
            s.previewSwapchain->Present(0, 0);
        }
        g_presentPending = false;
    }

    // Keep the measurement visible without drawing into the eye images (and
    // therefore without contaminating screenshots or flicker/color analysis).
    // F3 / Tools > Show Statistics adds p50/p95 and the rolling sample count.
    if (rt::g_session.hwnd &&
        rt::g_projectionTiming.ShouldRefreshTitle(projectionTimingNow)) {
        ui::StatsInfo stats = rt::BuildStatsInfo(rt::g_session);
        ui::UpdateWindowTitle(rt::g_session.hwnd, &stats);
    }

    if (shouldLog && (quadCount > 0 || cylinderCount > 0)) {
        Logf("[SimXR] xrEndFrame: proj=%d quad=%d cyl=%d other=%d",
             projectionCount, quadCount, cylinderCount, otherCount);
    }

    if (projectionCount == 0 && shouldLog) {
        Logf("[SimXR] xrEndFrame: no projection layer - 2D-only frame (%d overlay layers)",
             quadCount + cylinderCount);
    }

    LARGE_INTEGER endFrameFinish{};
    QueryPerformanceCounter(&endFrameFinish);
    {
        static LARGE_INTEGER frequency = []() {
            LARGE_INTEGER value{};
            QueryPerformanceFrequency(&value);
            return value;
        }();
        static uint64_t timingFrames = 0;
        static double projectionMs = 0.0;
        static double overlayMs = 0.0;
        static double detectPaintMs = 0.0;
        static double remainderMs = 0.0;
        static double totalMs = 0.0;
        const auto elapsedMs = [&](const LARGE_INTEGER& begin, const LARGE_INTEGER& end) {
            return 1000.0 * (double)(end.QuadPart - begin.QuadPart) / (double)frequency.QuadPart;
        };
        projectionMs += elapsedMs(endFrameStart, afterProjection);
        overlayMs += elapsedMs(afterProjection, afterOverlays);
        detectPaintMs += elapsedMs(afterOverlays, afterDetectionAndPaint);
        remainderMs += elapsedMs(afterDetectionAndPaint, endFrameFinish);
        totalMs += elapsedMs(endFrameStart, endFrameFinish);
        if (++timingFrames == 300) {
            Logf("[SimXR] xrEndFrame timing avg300: total=%.3fms projection=%.3f overlay=%.3f detectPaint=%.3f status=%.3f",
                 totalMs / timingFrames, projectionMs / timingFrames,
                 overlayMs / timingFrames, detectPaintMs / timingFrames,
                 remainderMs / timingFrames);
            timingFrames = 0;
            projectionMs = overlayMs = detectPaintMs = remainderMs = totalMs = 0.0;
        }
    }

    inEndFrame.clear(std::memory_order_release);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrLocateViews_runtime(XrSession, const XrViewLocateInfo* li, XrViewState* vs, uint32_t cap, uint32_t* outCount, XrView* views) {
    if (outCount) *outCount = 2;
    if (vs) { 
        vs->type = XR_TYPE_VIEW_STATE; 
        // Set both VALID and TRACKED bits so Unity knows this is a real tracked HMD
        vs->viewStateFlags = XR_VIEW_STATE_ORIENTATION_VALID_BIT | 
                            XR_VIEW_STATE_POSITION_VALID_BIT | 
                            XR_VIEW_STATE_ORIENTATION_TRACKED_BIT | 
                            XR_VIEW_STATE_POSITION_TRACKED_BIT; 
    }
    if (cap < 2 || !views) return XR_SUCCESS;

    // Composition-layer placement reads these same two helpers, which is what keeps
    // an overlay pinned to the geometry the app renders around it.
    float effYaw, effPitch, effRoll;
    rt::GetEffectiveHeadAngles(effYaw, effPitch, effRoll);

    for (uint32_t i = 0; i < 2; ++i) {
        views[i].type = XR_TYPE_VIEW;
        views[i].pose = rt::ViewPoseFromAngles(i, effYaw, effPitch, effRoll);
        views[i].fov = rt::GetViewFov(i);
    }
    static int locateCount = 0;
    if (++locateCount % 90 == 1) {  // Log every 90 frames (~1 second)
        Logf("[SimXR] xrLocateViews: pos=(%.2f,%.2f,%.2f) yaw=%.2f pitch=%.2f", 
             rt::g_headPos.x, rt::g_headPos.y, rt::g_headPos.z, 
             rt::g_headYaw, rt::g_headPitch);
    }
    return XR_SUCCESS;
}

// Add missing space/action functions for compatibility
static XrResult XRAPI_PTR xrCreateReferenceSpace_runtime(XrSession, const XrReferenceSpaceCreateInfo* info, XrSpace* space) {
    if (!info || !space) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSpace = 100;
    *space = (XrSpace)(nextSpace++);
    rt::g_referenceSpaces[*space] = rt::RefSpace{ info->referenceSpaceType, info->poseInReferenceSpace };
    Logf("[SimXR] xrCreateReferenceSpace: type=%d space=%p", info->referenceSpaceType, *space);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroySpace_runtime(XrSpace space) {
    rt::g_referenceSpaces.erase(space);
    Logf("[SimXR] xrDestroySpace: space=%p", space);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrLocateSpace_runtime(XrSpace space, XrSpace baseSpace, XrTime time, XrSpaceLocation* location) {
    if (!location) return XR_ERROR_VALIDATION_FAILURE;
    location->type = XR_TYPE_SPACE_LOCATION;

    // Check if this is a controller space
    auto it = rt::g_controllerSpaces.find(space);
    if (it != rt::g_controllerSpaces.end()) {
        int ctrlType = it->second;
        const rt::ControllerState& ctrl = (ctrlType == 1) ? rt::g_leftController : rt::g_rightController;

        if (ctrl.isTracking) {
            location->locationFlags = XR_SPACE_LOCATION_POSITION_VALID_BIT |
                                      XR_SPACE_LOCATION_ORIENTATION_VALID_BIT |
                                      XR_SPACE_LOCATION_POSITION_TRACKED_BIT |
                                      XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT;
            rt::GetControllerPose(ctrl, &location->pose);

            // Handle velocity if chained (XrSpaceVelocity)
            XrSpaceVelocity* velocity = (XrSpaceVelocity*)location->next;
            if (velocity && velocity->type == XR_TYPE_SPACE_VELOCITY) {
                velocity->velocityFlags = XR_SPACE_VELOCITY_LINEAR_VALID_BIT | XR_SPACE_VELOCITY_ANGULAR_VALID_BIT;
                // Return the calculated velocities from the controller state
                velocity->linearVelocity = ctrl.linearVelocity;
                velocity->angularVelocity = ctrl.angularVelocity;
            }

            static int logCount = 0;
            if (++logCount % 500 == 1) {
                float speed = sqrtf(ctrl.linearVelocity.x * ctrl.linearVelocity.x +
                                   ctrl.linearVelocity.y * ctrl.linearVelocity.y +
                                   ctrl.linearVelocity.z * ctrl.linearVelocity.z);
                Logf("[SimXR] xrLocateSpace: controller %d at (%.2f, %.2f, %.2f) vel=(%.2f, %.2f, %.2f) speed=%.2f m/s",
                     ctrlType, location->pose.position.x, location->pose.position.y, location->pose.position.z,
                     ctrl.linearVelocity.x, ctrl.linearVelocity.y, ctrl.linearVelocity.z, speed);
            }
        } else {
            location->locationFlags = 0;
            location->pose.orientation = {0, 0, 0, 1};
            location->pose.position = {0, 0, 0};
        }
    } else {
        // Reference spaces (VIEW / LOCAL / STAGE). Report fully-tracked: a real
        // runtime sets the *_TRACKED_BIT whenever tracking is live, and UEVR's
        // "first valid poses" gate requires those bits before it will submit.
        location->locationFlags = XR_SPACE_LOCATION_POSITION_VALID_BIT |
                                  XR_SPACE_LOCATION_ORIENTATION_VALID_BIT |
                                  XR_SPACE_LOCATION_POSITION_TRACKED_BIT |
                                  XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT;

        auto typeIt = rt::g_referenceSpaces.find(space);
        XrReferenceSpaceType spaceType = typeIt != rt::g_referenceSpaces.end()
            ? typeIt->second.type : XR_REFERENCE_SPACE_TYPE_LOCAL;
        if (spaceType == XR_REFERENCE_SPACE_TYPE_VIEW) {
            // VIEW space located against LOCAL/STAGE == the current head pose, so
            // HMD orientation/position actually track (yaw/pitch/roll from MCP).
            location->pose.orientation = rt::QuatFromYawPitchRoll(rt::g_headYaw, rt::g_headPitch, rt::g_headRoll);
            location->pose.position = rt::g_headPos;
        } else {
            // LOCAL / STAGE are fixed reference frames -> identity.
            location->pose.orientation = {0, 0, 0, 1};
            location->pose.position = {0, 0, 0};
        }
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateReferenceSpaces_runtime(XrSession, uint32_t capacity, uint32_t* count, XrReferenceSpaceType* spaces) {
    if (count) *count = 3;
    if (capacity >= 3 && spaces) {
        spaces[0] = XR_REFERENCE_SPACE_TYPE_VIEW;
        spaces[1] = XR_REFERENCE_SPACE_TYPE_LOCAL;
        spaces[2] = XR_REFERENCE_SPACE_TYPE_STAGE;
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateActionSpace_runtime(XrSession, const XrActionSpaceCreateInfo* info, XrSpace* space) {
    if (!info || !space) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSpace = 200;
    *space = (XrSpace)(nextSpace++);

    // Detect controller subaction paths and register the space
    int controllerType = 0;  // 0=none, 1=left, 2=right
    Logf("[SimXR] xrCreateActionSpace: subactionPath=%llu, g_pathStrings.size()=%zu",
         (unsigned long long)info->subactionPath, rt::g_pathStrings.size());

    if (info->subactionPath != XR_NULL_PATH) {
        auto it = rt::g_pathStrings.find(info->subactionPath);
        if (it != rt::g_pathStrings.end()) {
            const std::string& pathStr = it->second;
            Logf("[SimXR] xrCreateActionSpace: found path='%s'", pathStr.c_str());
            if (pathStr.find("/user/hand/left") != std::string::npos) {
                controllerType = 1;  // Left controller
                Logf("[SimXR] xrCreateActionSpace: LEFT controller space %llu", (unsigned long long)*space);
            } else if (pathStr.find("/user/hand/right") != std::string::npos) {
                controllerType = 2;  // Right controller
                Logf("[SimXR] xrCreateActionSpace: RIGHT controller space %llu", (unsigned long long)*space);
            }
        } else {
            Logf("[SimXR] xrCreateActionSpace: path %llu NOT FOUND in g_pathStrings", (unsigned long long)info->subactionPath);
        }
    } else {
        Log("[SimXR] xrCreateActionSpace: subactionPath is XR_NULL_PATH");
    }

    if (controllerType > 0) {
        rt::g_controllerSpaces[*space] = controllerType;
    }

    Log("[SimXR] xrCreateActionSpace");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateActionSet_runtime(XrInstance, const XrActionSetCreateInfo* info, XrActionSet* set) {
    if (!info || !set) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextSet = 300;
    *set = (XrActionSet)(nextSet++);
    // actionSetName may not be null-terminated
    char setName[XR_MAX_ACTION_SET_NAME_SIZE + 1] = {0};
    memcpy(setName, info->actionSetName, XR_MAX_ACTION_SET_NAME_SIZE);
    Logf("[SimXR] xrCreateActionSet: name=%s", setName);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyActionSet_runtime(XrActionSet set) {
    Log("[SimXR] xrDestroyActionSet");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrCreateAction_runtime(XrActionSet, const XrActionCreateInfo* info, XrAction* action) {
    if (!info || !action) return XR_ERROR_VALIDATION_FAILURE;
    static uintptr_t nextAction = 400;
    *action = (XrAction)(nextAction++);
    // actionName may not be null-terminated
    char actName[XR_MAX_ACTION_NAME_SIZE + 1] = {0};
    memcpy(actName, info->actionName, XR_MAX_ACTION_NAME_SIZE);
    Logf("[SimXR] xrCreateAction: name=%s, type=%d", actName, info->actionType);

    // Store action name for input mapping
    rt::g_actionNames[*action] = actName;

    // Detect which hand this action is bound to based on subactionPaths
    int handBinding = 0;  // 0=both/any
    if (info->countSubactionPaths > 0 && info->subactionPaths) {
        for (uint32_t i = 0; i < info->countSubactionPaths; i++) {
            auto it = rt::g_pathStrings.find(info->subactionPaths[i]);
            if (it != rt::g_pathStrings.end()) {
                if (it->second.find("left") != std::string::npos) handBinding |= 1;
                if (it->second.find("right") != std::string::npos) handBinding |= 2;
            }
        }
    }
    rt::g_actionHand[*action] = handBinding;

    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroyAction_runtime(XrAction action) {
    Log("[SimXR] xrDestroyAction");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrSuggestInteractionProfileBindings_runtime(XrInstance, const XrInteractionProfileSuggestedBinding* bindings) {
    if (!bindings) return XR_ERROR_VALIDATION_FAILURE;
    // interactionProfile is an XrPath (integer), not a C-string
    Logf("[SimXR] xrSuggestInteractionProfileBindings: profile=0x%llx",
         (unsigned long long)bindings->interactionProfile);
    if (bindings->interactionProfile != XR_NULL_PATH) {
        auto& list = rt::g_suggestedProfiles;
        if (std::find(list.begin(), list.end(), bindings->interactionProfile) == list.end()) {
            list.push_back(bindings->interactionProfile);
        }
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrAttachSessionActionSets_runtime(XrSession, const XrSessionActionSetsAttachInfo* info) {
    if (!info) return XR_ERROR_VALIDATION_FAILURE;
    Logf("[SimXR] xrAttachSessionActionSets: count=%u", info->countActionSets);

    // The synthetic controllers behave like Touch, so prefer that profile.
    for (XrPath p : rt::g_suggestedProfiles) {
        auto it = rt::g_pathStrings.find(p);
        if (it != rt::g_pathStrings.end() && it->second.find("oculus/touch") != std::string::npos) {
            rt::g_activeProfile = p;
            break;
        }
    }
    if (rt::g_activeProfile == XR_NULL_PATH && !rt::g_suggestedProfiles.empty()) {
        rt::g_activeProfile = rt::g_suggestedProfiles.front();
    }
    if (rt::g_activeProfile != XR_NULL_PATH) {
        auto it = rt::g_pathStrings.find(rt::g_activeProfile);
        Logf("[SimXR] xrAttachSessionActionSets: active interaction profile = %s", it != rt::g_pathStrings.end() ? it->second.c_str() : "<unknown>");

        XrEventDataInteractionProfileChanged e{ XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED };
        e.session = rt::g_session.handle;
        XrEventDataBuffer buf{};
        buf.type = XR_TYPE_EVENT_DATA_BUFFER;
        std::memcpy(&buf, &e, sizeof(e));
        rt::g_eventQueue.push_back(buf);
    }
    return XR_SUCCESS;
}

// Helper to get controller state based on action and subactionPath
static rt::ControllerState* GetControllerForAction(XrAction action, XrPath subactionPath) {
    // First check subactionPath
    if (subactionPath != XR_NULL_PATH) {
        auto pathIt = rt::g_pathStrings.find(subactionPath);
        if (pathIt != rt::g_pathStrings.end()) {
            if (pathIt->second.find("left") != std::string::npos) return &rt::g_leftController;
            if (pathIt->second.find("right") != std::string::npos) return &rt::g_rightController;
        }
    }
    // Fall back to action's hand binding
    auto handIt = rt::g_actionHand.find(action);
    if (handIt != rt::g_actionHand.end()) {
        if (handIt->second == 1) return &rt::g_leftController;
        if (handIt->second == 2) return &rt::g_rightController;
    }
    // Default to right hand
    return &rt::g_rightController;
}

// Helper to check if action name matches input type (case-insensitive substring match)
static bool ActionNameMatches(const std::string& name, const char* pattern) {
    std::string lower = name;
    for (auto& c : lower) c = (char)tolower(c);
    std::string patLower = pattern;
    for (auto& c : patLower) c = (char)tolower(c);
    return lower.find(patLower) != std::string::npos;
}

static XrResult XRAPI_PTR xrGetActionStateBoolean_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateBoolean* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_BOOLEAN;
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;

    // Get controller for this action
    rt::ControllerState* ctrl = GetControllerForAction(info->action, info->subactionPath);

    // Get action name to determine input type
    bool buttonState = false;
    auto nameIt = rt::g_actionNames.find(info->action);
    if (nameIt != rt::g_actionNames.end()) {
        const std::string& name = nameIt->second;
        if (ActionNameMatches(name, "trigger") || ActionNameMatches(name, "select") || ActionNameMatches(name, "fire")) {
            buttonState = ctrl->triggerPressed;
        } else if (ActionNameMatches(name, "grip") || ActionNameMatches(name, "squeeze") || ActionNameMatches(name, "grab")) {
            buttonState = ctrl->gripPressed;
        } else if (ActionNameMatches(name, "menu")) {
            buttonState = ctrl->menuPressed;
        } else if (ActionNameMatches(name, "primary") || ActionNameMatches(name, "a_button") || ActionNameMatches(name, "x_button")) {
            buttonState = ctrl->primaryPressed;
        } else if (ActionNameMatches(name, "secondary") || ActionNameMatches(name, "b_button") || ActionNameMatches(name, "y_button")) {
            buttonState = ctrl->secondaryPressed;
        } else if (ActionNameMatches(name, "thumbstick") || ActionNameMatches(name, "joystick")) {
            buttonState = ctrl->thumbstickPressed;
        }
    }

    state->currentState = buttonState ? XR_TRUE : XR_FALSE;
    state->isActive = XR_TRUE;  // Controllers are always active in simulator
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStateFloat_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateFloat* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_FLOAT;
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;

    // Get controller for this action
    rt::ControllerState* ctrl = GetControllerForAction(info->action, info->subactionPath);

    // Get action name to determine input type
    float floatState = 0.0f;
    auto nameIt = rt::g_actionNames.find(info->action);
    if (nameIt != rt::g_actionNames.end()) {
        const std::string& name = nameIt->second;
        if (ActionNameMatches(name, "trigger") || ActionNameMatches(name, "select") || ActionNameMatches(name, "fire")) {
            floatState = ctrl->triggerValue;
        } else if (ActionNameMatches(name, "grip") || ActionNameMatches(name, "squeeze") || ActionNameMatches(name, "grab")) {
            floatState = ctrl->gripValue;
        }
    }

    state->currentState = floatState;
    state->isActive = XR_TRUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStatePose_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStatePose* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_POSE;
    state->isActive = XR_TRUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetActionStateVector2f_runtime(XrSession, const XrActionStateGetInfo* info, XrActionStateVector2f* state) {
    if (!info || !state) return XR_ERROR_VALIDATION_FAILURE;
    state->type = XR_TYPE_ACTION_STATE_VECTOR2F;
    state->changedSinceLastSync = XR_FALSE;
    state->lastChangeTime = 0;

    // Get controller for this action
    rt::ControllerState* ctrl = GetControllerForAction(info->action, info->subactionPath);

    // Get action name to determine input type
    auto nameIt = rt::g_actionNames.find(info->action);
    if (nameIt != rt::g_actionNames.end()) {
        const std::string& name = nameIt->second;
        if (ActionNameMatches(name, "thumbstick") || ActionNameMatches(name, "joystick") ||
            ActionNameMatches(name, "move") || ActionNameMatches(name, "turn")) {
            state->currentState = ctrl->thumbstick;
        } else {
            state->currentState = {0.0f, 0.0f};
        }
    } else {
        state->currentState = {0.0f, 0.0f};
    }

    state->isActive = XR_TRUE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrSyncActions_runtime(XrSession, const XrActionsSyncInfo* info) {
    if (!info) return XR_ERROR_VALIDATION_FAILURE;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStringToPath_runtime(XrInstance, const char* pathString, XrPath* path) {
    if (!pathString || !path) return XR_ERROR_VALIDATION_FAILURE;
    // Simple hash as path ID
    size_t hash = 5381;
    for (const char* c = pathString; *c; ++c) {
        hash = ((hash << 5) + hash) + *c;
    }
    *path = (XrPath)hash;
    // Store path string for controller detection
    rt::g_pathStrings[*path] = pathString;
    Logf("[SimXR] xrStringToPath: %s -> %llu", pathString, (unsigned long long)*path);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrPathToString_runtime(XrInstance, XrPath path, uint32_t bufferCapacityInput, uint32_t* bufferCountOutput, char* buffer) {
    auto it = rt::g_pathStrings.find(path);
    if (it == rt::g_pathStrings.end()) return XR_ERROR_PATH_INVALID;
    const std::string& str = it->second;
    const uint32_t len = (uint32_t)str.size() + 1;
    if (bufferCountOutput) *bufferCountOutput = len;
    if (bufferCapacityInput == 0) return XR_SUCCESS;  // sizing call
    if (!buffer) return XR_ERROR_VALIDATION_FAILURE;
    if (bufferCapacityInput < len) return XR_ERROR_SIZE_INSUFFICIENT;
    std::memcpy(buffer, str.c_str(), len);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetCurrentInteractionProfile_runtime(XrSession, XrPath topLevelUserPath, XrInteractionProfileState* interactionProfile) {
    if (!interactionProfile) return XR_ERROR_VALIDATION_FAILURE;
    interactionProfile->type = XR_TYPE_INTERACTION_PROFILE_STATE;
    interactionProfile->interactionProfile = rt::g_activeProfile;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrEnumerateBoundSourcesForAction_runtime(XrSession, const XrBoundSourcesForActionEnumerateInfo* info, uint32_t sourceCapacityInput, uint32_t* sourceCountOutput, XrPath* sources) {
    if (sourceCountOutput) *sourceCountOutput = 0;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetInputSourceLocalizedName_runtime(XrSession, const XrInputSourceLocalizedNameGetInfo* info, uint32_t bufferCapacityInput, uint32_t* bufferCountOutput, char* buffer) {
    const char* name = "Unknown";
    size_t len = strlen(name) + 1;
    if (bufferCountOutput) *bufferCountOutput = (uint32_t)len;
    if (buffer && bufferCapacityInput > 0) {
        strncpy(buffer, name, bufferCapacityInput - 1);
        buffer[bufferCapacityInput - 1] = '\0';
    }
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrDestroySwapchain_runtime(XrSwapchain sc) {
    auto it = rt::g_swapchains.find(sc);
    if (it == rt::g_swapchains.end()) return XR_ERROR_HANDLE_INVALID;

    // A D3D12 command list does not keep the resources it references alive, and the
    // preview no longer waits for its own submissions inside xrEndFrame - so a composite
    // reading these images can still be queued. Dropping the last reference here would
    // pull them out from under it. Not on any frame path, so the wait costs nothing.
    if (rt::g_session.usesD3D12) rt::WaitForPreviewIdle(rt::g_session);

    // For OpenGL swapchains, delete the textures
    if (it->second.backend == rt::Swapchain::Backend::OpenGL && !it->second.imagesGL.empty()) {
        // Make app's GL context current if available
        HGLRC prevRC = wglGetCurrentContext();
        HDC prevDC = wglGetCurrentDC();
        if (rt::g_session.glDC && rt::g_session.glRC) {
            wglMakeCurrent(rt::g_session.glDC, rt::g_session.glRC);
        }
        for (GLuint tex : it->second.imagesGL) {
            glDeleteTextures(1, &tex);
        }
        if (prevRC) wglMakeCurrent(prevDC, prevRC);
    }

    if (it->second.isVulkan) vkrt::DestroySwapchainImages(rt::g_session, it->second);

    rt::g_swapchains.erase(it);
    Logf("[SimXR] xrDestroySwapchain: sc=%p", sc);
    return XR_SUCCESS;
}

// Missing functions Unity needs
static XrResult XRAPI_PTR xrResultToString_runtime(XrInstance, XrResult value, char buffer[XR_MAX_RESULT_STRING_SIZE]) {
    const char* str = "XR_SUCCESS";
    if (value != XR_SUCCESS) str = "XR_ERROR";
    strncpy(buffer, str, XR_MAX_RESULT_STRING_SIZE - 1);
    buffer[XR_MAX_RESULT_STRING_SIZE - 1] = '\0';
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStructureTypeToString_runtime(XrInstance, XrStructureType value, char buffer[XR_MAX_STRUCTURE_NAME_SIZE]) {
    snprintf(buffer, XR_MAX_STRUCTURE_NAME_SIZE, "XrStructureType_%d", (int)value);
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetReferenceSpaceBoundsRect_runtime(XrSession, XrReferenceSpaceType, XrExtent2Df* bounds) {
    if (!bounds) return XR_ERROR_VALIDATION_FAILURE;
    bounds->width = 3.0f;
    bounds->height = 3.0f;
    Log("[SimXR] xrGetReferenceSpaceBoundsRect: 3x3 meters");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrGetViewConfigurationProperties_runtime(XrInstance, XrSystemId, XrViewConfigurationType type, 
                                                                   XrViewConfigurationProperties* props) {
    if (!props) return XR_ERROR_VALIDATION_FAILURE;
    props->type = XR_TYPE_VIEW_CONFIGURATION_PROPERTIES;
    props->viewConfigurationType = type;
    props->fovMutable = XR_FALSE;
    Log("[SimXR] xrGetViewConfigurationProperties");
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrApplyHapticFeedback_runtime(XrSession, const XrHapticActionInfo* info, const XrHapticBaseHeader* haptic) {
    // Just stub for now
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrStopHapticFeedback_runtime(XrSession, const XrHapticActionInfo* info) {
    // Just stub for now
    return XR_SUCCESS;
}

// Time conversion functions for XR_KHR_win32_convert_performance_counter_time
static XrResult XRAPI_PTR xrConvertWin32PerformanceCounterToTimeKHR_runtime(XrInstance instance,
                                                                            const LARGE_INTEGER* performanceCounter,
                                                                            XrTime* time) {
    if (!performanceCounter || !time) return XR_ERROR_VALIDATION_FAILURE;
    
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    
    // Convert to nanoseconds
    const long long secs = performanceCounter->QuadPart / freq.QuadPart;
    const long long rem = performanceCounter->QuadPart % freq.QuadPart;
    *time = secs * 1000000000LL + (rem * 1000000000LL) / freq.QuadPart;
    return XR_SUCCESS;
}

static XrResult XRAPI_PTR xrConvertTimeToWin32PerformanceCounterKHR_runtime(XrInstance instance,
                                                                             XrTime time,
                                                                             LARGE_INTEGER* performanceCounter) {
    if (!performanceCounter) return XR_ERROR_VALIDATION_FAILURE;
    
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    
    // Convert from nanoseconds  
    const long long secs = time / 1000000000LL;
    const long long rem = time % 1000000000LL;
    performanceCounter->QuadPart = secs * freq.QuadPart + (rem * freq.QuadPart) / 1000000000LL;
    return XR_SUCCESS;
}

// ----------------------------------------------

struct NameFn { const char* name; PFN_xrVoidFunction fn; };

static const NameFn kFnTable[] = {
    {"xrGetInstanceProcAddr", (PFN_xrVoidFunction)xrGetInstanceProcAddr_runtime},
    {"xrEnumerateApiLayerProperties", (PFN_xrVoidFunction)xrEnumerateApiLayerProperties_runtime},
    {"xrEnumerateInstanceExtensionProperties", (PFN_xrVoidFunction)xrEnumerateInstanceExtensionProperties_runtime},
    {"xrCreateInstance", (PFN_xrVoidFunction)xrCreateInstance_runtime},
    {"xrDestroyInstance", (PFN_xrVoidFunction)xrDestroyInstance_runtime},
    {"xrGetInstanceProperties", (PFN_xrVoidFunction)xrGetInstanceProperties_runtime},
    {"xrGetSystem", (PFN_xrVoidFunction)xrGetSystem_runtime},
    {"xrGetSystemProperties", (PFN_xrVoidFunction)xrGetSystemProperties_runtime},
    {"xrEnumerateViewConfigurations", (PFN_xrVoidFunction)xrEnumerateViewConfigurations_runtime},
    {"xrEnumerateViewConfigurationViews", (PFN_xrVoidFunction)xrEnumerateViewConfigurationViews_runtime},
    {"xrEnumerateEnvironmentBlendModes", (PFN_xrVoidFunction)xrEnumerateEnvironmentBlendModes_runtime},
    {"xrCreateSession", (PFN_xrVoidFunction)xrCreateSession_runtime},
    {"xrDestroySession", (PFN_xrVoidFunction)xrDestroySession_runtime},
    {"xrEnumerateSwapchainFormats", (PFN_xrVoidFunction)xrEnumerateSwapchainFormats_runtime},
    {"xrCreateSwapchain", (PFN_xrVoidFunction)xrCreateSwapchain_runtime},
    {"xrDestroySwapchain", (PFN_xrVoidFunction)xrDestroySwapchain_runtime},
    {"xrEnumerateSwapchainImages", (PFN_xrVoidFunction)xrEnumerateSwapchainImages_runtime},
    {"xrAcquireSwapchainImage", (PFN_xrVoidFunction)xrAcquireSwapchainImage_runtime},
    {"xrWaitSwapchainImage", (PFN_xrVoidFunction)xrWaitSwapchainImage_runtime},
    {"xrReleaseSwapchainImage", (PFN_xrVoidFunction)xrReleaseSwapchainImage_runtime},
    {"xrBeginSession", (PFN_xrVoidFunction)xrBeginSession_runtime},
    {"xrEndSession", (PFN_xrVoidFunction)xrEndSession_runtime},
    {"xrWaitFrame", (PFN_xrVoidFunction)xrWaitFrame_runtime},
    {"xrBeginFrame", (PFN_xrVoidFunction)xrBeginFrame_runtime},
    {"xrEndFrame", (PFN_xrVoidFunction)xrEndFrame_runtime},
    {"xrPollEvent", (PFN_xrVoidFunction)xrPollEvent_runtime},
    {"xrLocateViews", (PFN_xrVoidFunction)xrLocateViews_runtime},
    {"xrGetD3D11GraphicsRequirementsKHR", (PFN_xrVoidFunction)xrGetD3D11GraphicsRequirementsKHR_runtime},
    {"xrGetD3D12GraphicsRequirementsKHR", (PFN_xrVoidFunction)xrGetD3D12GraphicsRequirementsKHR_runtime},
    {"xrGetOpenGLGraphicsRequirementsKHR", (PFN_xrVoidFunction)xrGetOpenGLGraphicsRequirementsKHR_runtime},
    // XR_KHR_vulkan_enable
    {"xrGetVulkanGraphicsRequirementsKHR", (PFN_xrVoidFunction)xrGetVulkanGraphicsRequirementsKHR_runtime},
    {"xrGetVulkanInstanceExtensionsKHR", (PFN_xrVoidFunction)xrGetVulkanInstanceExtensionsKHR_runtime},
    {"xrGetVulkanDeviceExtensionsKHR", (PFN_xrVoidFunction)xrGetVulkanDeviceExtensionsKHR_runtime},
    {"xrGetVulkanGraphicsDeviceKHR", (PFN_xrVoidFunction)xrGetVulkanGraphicsDeviceKHR_runtime},
    // XR_KHR_vulkan_enable2
    {"xrGetVulkanGraphicsRequirements2KHR", (PFN_xrVoidFunction)xrGetVulkanGraphicsRequirementsKHR_runtime},
    {"xrCreateVulkanInstanceKHR", (PFN_xrVoidFunction)xrCreateVulkanInstanceKHR_runtime},
    {"xrCreateVulkanDeviceKHR", (PFN_xrVoidFunction)xrCreateVulkanDeviceKHR_runtime},
    {"xrGetVulkanGraphicsDevice2KHR", (PFN_xrVoidFunction)xrGetVulkanGraphicsDevice2KHR_runtime},
    {"xrRequestExitSession", (PFN_xrVoidFunction)xrRequestExitSession_runtime},
    // Space functions
    {"xrCreateReferenceSpace", (PFN_xrVoidFunction)xrCreateReferenceSpace_runtime},
    {"xrDestroySpace", (PFN_xrVoidFunction)xrDestroySpace_runtime},
    {"xrLocateSpace", (PFN_xrVoidFunction)xrLocateSpace_runtime},
    {"xrEnumerateReferenceSpaces", (PFN_xrVoidFunction)xrEnumerateReferenceSpaces_runtime},
    {"xrCreateActionSpace", (PFN_xrVoidFunction)xrCreateActionSpace_runtime},
    // Action functions
    {"xrCreateActionSet", (PFN_xrVoidFunction)xrCreateActionSet_runtime},
    {"xrDestroyActionSet", (PFN_xrVoidFunction)xrDestroyActionSet_runtime},
    {"xrCreateAction", (PFN_xrVoidFunction)xrCreateAction_runtime},
    {"xrDestroyAction", (PFN_xrVoidFunction)xrDestroyAction_runtime},
    {"xrSuggestInteractionProfileBindings", (PFN_xrVoidFunction)xrSuggestInteractionProfileBindings_runtime},
    {"xrAttachSessionActionSets", (PFN_xrVoidFunction)xrAttachSessionActionSets_runtime},
    {"xrGetActionStateBoolean", (PFN_xrVoidFunction)xrGetActionStateBoolean_runtime},
    {"xrGetActionStateFloat", (PFN_xrVoidFunction)xrGetActionStateFloat_runtime},
    {"xrGetActionStatePose", (PFN_xrVoidFunction)xrGetActionStatePose_runtime},
    {"xrGetActionStateVector2f", (PFN_xrVoidFunction)xrGetActionStateVector2f_runtime},
    {"xrSyncActions", (PFN_xrVoidFunction)xrSyncActions_runtime},
    // Path functions
    {"xrStringToPath", (PFN_xrVoidFunction)xrStringToPath_runtime},
    {"xrPathToString", (PFN_xrVoidFunction)xrPathToString_runtime},
    // Interaction functions
    {"xrGetCurrentInteractionProfile", (PFN_xrVoidFunction)xrGetCurrentInteractionProfile_runtime},
    {"xrEnumerateBoundSourcesForAction", (PFN_xrVoidFunction)xrEnumerateBoundSourcesForAction_runtime},
    {"xrGetInputSourceLocalizedName", (PFN_xrVoidFunction)xrGetInputSourceLocalizedName_runtime},
    // Utility functions
    {"xrResultToString", (PFN_xrVoidFunction)xrResultToString_runtime},
    {"xrStructureTypeToString", (PFN_xrVoidFunction)xrStructureTypeToString_runtime},
    {"xrGetReferenceSpaceBoundsRect", (PFN_xrVoidFunction)xrGetReferenceSpaceBoundsRect_runtime},
    {"xrGetViewConfigurationProperties", (PFN_xrVoidFunction)xrGetViewConfigurationProperties_runtime},
    // Haptic functions
    {"xrApplyHapticFeedback", (PFN_xrVoidFunction)xrApplyHapticFeedback_runtime},
    {"xrStopHapticFeedback", (PFN_xrVoidFunction)xrStopHapticFeedback_runtime},
    // Time conversion functions
    {"xrConvertWin32PerformanceCounterToTimeKHR", (PFN_xrVoidFunction)xrConvertWin32PerformanceCounterToTimeKHR_runtime},
    {"xrConvertTimeToWin32PerformanceCounterKHR", (PFN_xrVoidFunction)xrConvertTimeToWin32PerformanceCounterKHR_runtime},
};

static XrResult XRAPI_PTR xrGetInstanceProcAddr_runtime(XrInstance instance, const char* name, PFN_xrVoidFunction* fn) {
    if (!name || !fn) {
        Logf("[SimXR] xrGetInstanceProcAddr: ERROR - name=%p, fn=%p", name, fn);
        return XR_ERROR_VALIDATION_FAILURE;
    }
    
    // Reduce logging verbosity for xrGetInstanceProcAddr
    static bool reduceLogging = false;
    static int callCount = 0;
    callCount++;
    
    for (auto& e : kFnTable) {
        if (strcmp(name, e.name) == 0) { 
            *fn = e.fn;
            if (callCount < 100 || strstr(name, "D3D11") || strstr(name, "Create") || strstr(name, "Destroy")) {
                Logf("[SimXR] xrGetInstanceProcAddr: %s -> FOUND", name);
            }
            return XR_SUCCESS; 
        }
    }
    
    if (callCount < 100 || strstr(name, "D3D11")) {
        Logf("[SimXR] xrGetInstanceProcAddr: %s -> NOT FOUND", name);
    }
    return XR_ERROR_FUNCTION_UNSUPPORTED;
}
