// Standalone replay of BetterVR's OpenXR call sequence against whatever runtime
// XR_RUNTIME_JSON points at. Mirrors src/rendering/openxr.cpp + renderer.cpp +
// swapchain.cpp so a runtime that passes this is a plausible drop-in.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstring>
#include <vector>
#include <string>
#include <array>

#define XR_USE_GRAPHICS_API_D3D12
#define XR_USE_PLATFORM_WIN32
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

using Microsoft::WRL::ComPtr;

static int g_fail = 0;
static int g_warn = 0;

static void ok(const char* what) { printf("  [ ok ] %s\n", what); }
static void fail(const char* what, XrResult r) { printf("  [FAIL] %s -> XrResult %d\n", what, (int)r); g_fail++; }
static void warn(const char* what) { printf("  [warn] %s\n", what); g_warn++; }
static void step(const char* what) { printf("\n== %s\n", what); }

#define XRC(call, what) do { XrResult _r = (call); if (XR_FAILED(_r)) { fail(what, _r); return 1; } ok(what); } while (0)
#define XRC_SOFT(call, what) do { XrResult _r = (call); if (XR_FAILED(_r)) { fail(what, _r); } else ok(what); } while (0)

static XrInstance g_instance = XR_NULL_HANDLE;
static XrPath P(const char* s) { XrPath p = XR_NULL_PATH; xrStringToPath(g_instance, s, &p); return p; }

int main() {
    step("xrEnumerateInstanceExtensionProperties");
    uint32_t extCount = 0;
    XRC(xrEnumerateInstanceExtensionProperties(nullptr, 0, &extCount, nullptr), "enumerate extension count");
    std::vector<XrExtensionProperties> exts(extCount, { XR_TYPE_EXTENSION_PROPERTIES });
    XRC(xrEnumerateInstanceExtensionProperties(nullptr, extCount, &extCount, exts.data()), "enumerate extensions");
    bool hasD3D12 = false, hasDepth = false, hasTimeConv = false, hasDebugUtils = false;
    for (auto& e : exts) {
        printf("       - %s\n", e.extensionName);
        if (!strcmp(e.extensionName, XR_KHR_D3D12_ENABLE_EXTENSION_NAME)) hasD3D12 = true;
        if (!strcmp(e.extensionName, XR_KHR_COMPOSITION_LAYER_DEPTH_EXTENSION_NAME)) hasDepth = true;
        if (!strcmp(e.extensionName, XR_KHR_WIN32_CONVERT_PERFORMANCE_COUNTER_TIME_EXTENSION_NAME)) hasTimeConv = true;
        if (!strcmp(e.extensionName, XR_EXT_DEBUG_UTILS_EXTENSION_NAME)) hasDebugUtils = true;
    }
    if (!hasD3D12) { printf("  [FAIL] XR_KHR_D3D12_enable missing - BetterVR cannot run\n"); return 1; }
    if (!hasDepth) { printf("  [FAIL] XR_KHR_composition_layer_depth missing - BetterVR requires it unconditionally\n"); return 1; }
    if (!hasTimeConv) { printf("  [FAIL] XR_KHR_win32_convert_performance_counter_time missing - BetterVR requires it unconditionally\n"); return 1; }
    ok("all three mandatory BetterVR extensions present");
    if (!hasDebugUtils) warn("XR_EXT_debug_utils absent (optional; BetterVR just logs a notice)");

    step("xrCreateInstance");
    std::vector<const char*> enabled = { XR_KHR_D3D12_ENABLE_EXTENSION_NAME, XR_KHR_COMPOSITION_LAYER_DEPTH_EXTENSION_NAME, XR_KHR_WIN32_CONVERT_PERFORMANCE_COUNTER_TIME_EXTENSION_NAME };
    XrInstanceCreateInfo ici = { XR_TYPE_INSTANCE_CREATE_INFO };
    ici.enabledExtensionCount = (uint32_t)enabled.size();
    ici.enabledExtensionNames = enabled.data();
    ici.applicationInfo = { "BetterVR", 1, "Cemu", 1, XR_API_VERSION_1_0 };
    XRC(xrCreateInstance(&ici, &g_instance), "xrCreateInstance");

    PFN_xrGetD3D12GraphicsRequirementsKHR pfnReq = nullptr;
    XRC(xrGetInstanceProcAddr(g_instance, "xrGetD3D12GraphicsRequirementsKHR", (PFN_xrVoidFunction*)&pfnReq), "get xrGetD3D12GraphicsRequirementsKHR");
    PFN_xrConvertWin32PerformanceCounterToTimeKHR pfnPcToTime = nullptr;
    PFN_xrConvertTimeToWin32PerformanceCounterKHR pfnTimeToPc = nullptr;
    XRC(xrGetInstanceProcAddr(g_instance, "xrConvertWin32PerformanceCounterToTimeKHR", (PFN_xrVoidFunction*)&pfnPcToTime), "get xrConvertWin32PerformanceCounterToTimeKHR");
    XRC(xrGetInstanceProcAddr(g_instance, "xrConvertTimeToWin32PerformanceCounterKHR", (PFN_xrVoidFunction*)&pfnTimeToPc), "get xrConvertTimeToWin32PerformanceCounterKHR");

    step("system + properties");
    XrSystemGetInfo sgi = { XR_TYPE_SYSTEM_GET_INFO };
    sgi.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId systemId = XR_NULL_SYSTEM_ID;
    XRC(xrGetSystem(g_instance, &sgi, &systemId), "xrGetSystem");

    XrSystemProperties sysProps = { XR_TYPE_SYSTEM_PROPERTIES };
    XRC(xrGetSystemProperties(g_instance, systemId, &sysProps), "xrGetSystemProperties");
    XrInstanceProperties instProps = { XR_TYPE_INSTANCE_PROPERTIES };
    XRC(xrGetInstanceProperties(g_instance, &instProps), "xrGetInstanceProperties");
    printf("       systemName = \"%s\"\n", sysProps.systemName);
    printf("       runtimeName = \"%s\"  version %d.%d.%d\n", instProps.runtimeName,
           XR_VERSION_MAJOR(instProps.runtimeVersion), XR_VERSION_MINOR(instProps.runtimeVersion), XR_VERSION_PATCH(instProps.runtimeVersion));
    printf("       orientationTracking=%d positionTracking=%d\n", sysProps.trackingProperties.orientationTracking, sysProps.trackingProperties.positionTracking);
    if (strstr(instProps.runtimeName, "Meta XR Simulator")) ok("runtimeName matches BetterVR's isMetaSimulator sniff");
    else warn("runtimeName does not contain \"Meta XR Simulator\" -> BetterVR's isMetaSimulator branch stays off");

    XrViewConfigurationProperties vcProps = { XR_TYPE_VIEW_CONFIGURATION_PROPERTIES };
    XRC(xrGetViewConfigurationProperties(g_instance, systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, &vcProps), "xrGetViewConfigurationProperties(PRIMARY_STEREO)");
    printf("       fovMutable = %d\n", vcProps.fovMutable);

    XrGraphicsRequirementsD3D12KHR gfxReq = { XR_TYPE_GRAPHICS_REQUIREMENTS_D3D12_KHR };
    XRC(pfnReq(g_instance, systemId, &gfxReq), "xrGetD3D12GraphicsRequirementsKHR");
    printf("       adapterLuid = %08lX:%08lX  minFeatureLevel = 0x%X\n", (unsigned long)gfxReq.adapterLuid.HighPart, (unsigned long)gfxReq.adapterLuid.LowPart, (unsigned)gfxReq.minFeatureLevel);

    step("xrEnumerateViewConfigurationViews");
    uint32_t viewCount = 0;
    XRC(xrEnumerateViewConfigurationViews(g_instance, systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, 0, &viewCount, nullptr), "view config view count");
    if (viewCount != 2) { printf("  [FAIL] expected 2 views, got %u - BetterVR asserts on this\n", viewCount); return 1; }
    ok("view count == 2");
    std::array<XrViewConfigurationView, 2> vcv = { XrViewConfigurationView{ XR_TYPE_VIEW_CONFIGURATION_VIEW }, XrViewConfigurationView{ XR_TYPE_VIEW_CONFIGURATION_VIEW } };
    XRC(xrEnumerateViewConfigurationViews(g_instance, systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, viewCount, &viewCount, vcv.data()), "view config views");
    printf("       recommended %ux%u  max %ux%u  samples %u\n", vcv[0].recommendedImageRectWidth, vcv[0].recommendedImageRectHeight, vcv[0].maxImageRectWidth, vcv[0].maxImageRectHeight, vcv[0].recommendedSwapchainSampleCount);
    if (vcv[0].recommendedImageRectWidth == 0 || vcv[0].recommendedImageRectHeight == 0) { printf("  [FAIL] zero recommended resolution\n"); g_fail++; }

    step("D3D12 device on the runtime's adapter");
    // The debug layer is the whole point of the render pass below: a barrier with
    // the wrong StateBefore is undefined behaviour that silently works on plenty of
    // drivers, and only becomes a hard error once validation is on.
    ComPtr<ID3D12Debug> d3dDebug;
    if (SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&d3dDebug)))) { d3dDebug->EnableDebugLayer(); ok("D3D12 debug layer enabled"); }
    else warn("D3D12 debug layer unavailable - install the Graphics Tools feature to catch resource-state bugs");
    ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) { printf("  [FAIL] CreateDXGIFactory1\n"); return 1; }
    ComPtr<IDXGIAdapter1> adapter;
    if (gfxReq.adapterLuid.LowPart || gfxReq.adapterLuid.HighPart) {
        ComPtr<IDXGIAdapter1> a;
        for (UINT i = 0; factory->EnumAdapters1(i, &a) != DXGI_ERROR_NOT_FOUND; ++i) {
            DXGI_ADAPTER_DESC1 d; a->GetDesc1(&d);
            if (d.AdapterLuid.LowPart == gfxReq.adapterLuid.LowPart && d.AdapterLuid.HighPart == gfxReq.adapterLuid.HighPart) { adapter = a; break; }
            a.Reset();
        }
        if (!adapter) warn("runtime's adapterLuid matched no adapter; falling back to default");
    }
    ComPtr<ID3D12Device> device;
    D3D_FEATURE_LEVEL fl = gfxReq.minFeatureLevel ? gfxReq.minFeatureLevel : D3D_FEATURE_LEVEL_11_0;
    if (FAILED(D3D12CreateDevice(adapter.Get(), fl, IID_PPV_ARGS(&device)))) { printf("  [FAIL] D3D12CreateDevice\n"); return 1; }
    ok("D3D12CreateDevice at the runtime's minFeatureLevel");
    ComPtr<ID3D12CommandQueue> queue;
    D3D12_COMMAND_QUEUE_DESC qd = {}; qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(device->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue)))) { printf("  [FAIL] CreateCommandQueue\n"); return 1; }
    ok("CreateCommandQueue");

    ComPtr<ID3D12InfoQueue> infoQueue;
    device->QueryInterface(IID_PPV_ARGS(&infoQueue));

    ComPtr<ID3D12CommandAllocator> cmdAlloc;
    ComPtr<ID3D12GraphicsCommandList> cmdList;
    ComPtr<ID3D12Fence> fence;
    UINT64 fenceValue = 0;
    HANDLE fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ComPtr<ID3D12DescriptorHeap> rtvHeap, dsvHeap;
    if (FAILED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&cmdAlloc))) ||
        FAILED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, cmdAlloc.Get(), nullptr, IID_PPV_ARGS(&cmdList))) ||
        FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)))) {
        printf("  [FAIL] could not create command list / fence\n"); return 1;
    }
    cmdList->Close();
    {
        D3D12_DESCRIPTOR_HEAP_DESC hd = {}; hd.NumDescriptors = 1;
        hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
        if (FAILED(device->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&rtvHeap)))) { printf("  [FAIL] RTV heap\n"); return 1; }
        hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_DSV;
        if (FAILED(device->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&dsvHeap)))) { printf("  [FAIL] DSV heap\n"); return 1; }
    }
    ok("command list, fence and RTV/DSV heaps");

    step("xrCreateSession (D3D12 binding)");
    XrGraphicsBindingD3D12KHR binding = { XR_TYPE_GRAPHICS_BINDING_D3D12_KHR };
    binding.device = device.Get();
    binding.queue = queue.Get();
    XrSessionCreateInfo sci = { XR_TYPE_SESSION_CREATE_INFO };
    sci.systemId = systemId;
    sci.next = &binding;
    XrSession session = XR_NULL_HANDLE;
    XRC(xrCreateSession(g_instance, &sci, &session), "xrCreateSession");

    const XrPosef identity = { {0,0,0,1}, {0,0,0} };
    XrSpace stageSpace = XR_NULL_HANDLE, headSpace = XR_NULL_HANDLE;
    XrReferenceSpaceCreateInfo rsci = { XR_TYPE_REFERENCE_SPACE_CREATE_INFO };
    rsci.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_STAGE; rsci.poseInReferenceSpace = identity;
    XRC(xrCreateReferenceSpace(session, &rsci, &stageSpace), "xrCreateReferenceSpace(STAGE)");
    rsci.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_VIEW;
    XRC(xrCreateReferenceSpace(session, &rsci, &headSpace), "xrCreateReferenceSpace(VIEW)");

    step("actions (BetterVR's shape: subaction paths, pose/bool/float/vec2/vibration)");
    std::array<XrPath, 2> handPaths = { P("/user/hand/left"), P("/user/hand/right") };
    if (handPaths[0] == XR_NULL_PATH || handPaths[1] == XR_NULL_PATH) { printf("  [FAIL] xrStringToPath returned XR_NULL_PATH\n"); g_fail++; }
    else ok("xrStringToPath for hand paths");

    XrActionSet gameplaySet = XR_NULL_HANDLE, menuSet = XR_NULL_HANDLE;
    XrActionSetCreateInfo asci = { XR_TYPE_ACTION_SET_CREATE_INFO };
    strcpy_s(asci.actionSetName, "gameplay_fps"); strcpy_s(asci.localizedActionSetName, "Gameplay");
    XRC(xrCreateActionSet(g_instance, &asci, &gameplaySet), "xrCreateActionSet(gameplay_fps)");
    strcpy_s(asci.actionSetName, "menu"); strcpy_s(asci.localizedActionSetName, "Menu Navigation");
    XRC(xrCreateActionSet(g_instance, &asci, &menuSet), "xrCreateActionSet(menu)");

    auto mkAction = [&](XrActionSet set, const char* id, const char* name, XrActionType type, XrAction& out) {
        XrActionCreateInfo aci = { XR_TYPE_ACTION_CREATE_INFO };
        aci.actionType = type;
        strncpy_s(aci.actionName, id, XR_MAX_ACTION_NAME_SIZE - 1);
        strncpy_s(aci.localizedActionName, name, XR_MAX_LOCALIZED_ACTION_NAME_SIZE - 1);
        aci.countSubactionPaths = (uint32_t)handPaths.size();
        aci.subactionPaths = handPaths.data();
        XrResult r = xrCreateAction(set, &aci, &out);
        if (XR_FAILED(r)) { char buf[128]; sprintf_s(buf, "xrCreateAction(%s)", id); fail(buf, r); }
    };
    XrAction gripPose{}, aimPose{}, move{}, camera{}, jump{}, grab{}, rumble{}, menuSelect{};
    mkAction(gameplaySet, "pose", "Grip Pose", XR_ACTION_TYPE_POSE_INPUT, gripPose);
    mkAction(gameplaySet, "aim_pose", "Aim Pose", XR_ACTION_TYPE_POSE_INPUT, aimPose);
    mkAction(gameplaySet, "move", "Move", XR_ACTION_TYPE_VECTOR2F_INPUT, move);
    mkAction(gameplaySet, "camera", "Camera Rotation", XR_ACTION_TYPE_VECTOR2F_INPUT, camera);
    mkAction(gameplaySet, "jump", "Jump", XR_ACTION_TYPE_BOOLEAN_INPUT, jump);
    mkAction(gameplaySet, "grab_interact", "Interact", XR_ACTION_TYPE_FLOAT_INPUT, grab);
    mkAction(gameplaySet, "rumble", "Rumble", XR_ACTION_TYPE_VIBRATION_OUTPUT, rumble);
    mkAction(menuSet, "select", "Select", XR_ACTION_TYPE_BOOLEAN_INPUT, menuSelect);
    if (!g_fail) ok("all action types accepted (incl. VIBRATION_OUTPUT)");

    step("xrSuggestInteractionProfileBindings");
    {
        std::vector<XrActionSuggestedBinding> b = {
            { gripPose, P("/user/hand/left/input/grip/pose") },
            { gripPose, P("/user/hand/right/input/grip/pose") },
            { aimPose,  P("/user/hand/left/input/aim/pose") },
            { aimPose,  P("/user/hand/right/input/aim/pose") },
            { move,     P("/user/hand/left/input/thumbstick") },
            { camera,   P("/user/hand/right/input/thumbstick") },
            { jump,     P("/user/hand/right/input/a/click") },
            { grab,     P("/user/hand/left/input/squeeze/value") },
            { rumble,   P("/user/hand/left/output/haptic") },
            { menuSelect, P("/user/hand/right/input/a/click") },
        };
        XrInteractionProfileSuggestedBinding sb = { XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING };
        sb.interactionProfile = P("/interaction_profiles/oculus/touch_controller");
        sb.countSuggestedBindings = (uint32_t)b.size();
        sb.suggestedBindings = b.data();
        XRC_SOFT(xrSuggestInteractionProfileBindings(g_instance, &sb), "suggest bindings (oculus/touch_controller)");
        sb.interactionProfile = P("/interaction_profiles/khr/simple_controller");
        std::vector<XrActionSuggestedBinding> simple = {
            { gripPose, P("/user/hand/left/input/grip/pose") },
            { gripPose, P("/user/hand/right/input/grip/pose") },
            { menuSelect, P("/user/hand/right/input/select/click") },
        };
        sb.countSuggestedBindings = (uint32_t)simple.size();
        sb.suggestedBindings = simple.data();
        XRC_SOFT(xrSuggestInteractionProfileBindings(g_instance, &sb), "suggest bindings (khr/simple_controller)");
    }

    step("xrAttachSessionActionSets + action spaces");
    {
        std::array<XrActionSet, 2> sets = { gameplaySet, menuSet };
        XrSessionActionSetsAttachInfo ai = { XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO };
        ai.countActionSets = (uint32_t)sets.size();
        ai.actionSets = sets.data();
        XRC(xrAttachSessionActionSets(session, &ai), "xrAttachSessionActionSets");
    }
    XrSpace handSpaces[2] = {};
    for (int side = 0; side < 2; ++side) {
        XrActionSpaceCreateInfo aspci = { XR_TYPE_ACTION_SPACE_CREATE_INFO };
        aspci.action = gripPose; aspci.subactionPath = handPaths[side]; aspci.poseInActionSpace = identity;
        XRC_SOFT(xrCreateActionSpace(session, &aspci, &handSpaces[side]), side == 0 ? "xrCreateActionSpace(left grip)" : "xrCreateActionSpace(right grip)");
    }
    {
        XrInteractionProfileState ips = { XR_TYPE_INTERACTION_PROFILE_STATE };
        XRC_SOFT(xrGetCurrentInteractionProfile(session, handPaths[0], &ips), "xrGetCurrentInteractionProfile(left)");
        if (ips.interactionProfile != XR_NULL_PATH) {
            char buf[XR_MAX_PATH_LENGTH]; uint32_t n = 0;
            if (XR_SUCCEEDED(xrPathToString(g_instance, ips.interactionProfile, sizeof(buf), &n, buf))) printf("       active profile: %s\n", buf);
        } else {
            warn("no active interaction profile (BetterVR's DetectActiveControllerType falls back)");
        }
    }

    step("swapchains (exactly BetterVR's two formats)");
    uint32_t fmtCount = 0;
    XRC(xrEnumerateSwapchainFormats(session, 0, &fmtCount, nullptr), "swapchain format count");
    std::vector<int64_t> formats(fmtCount);
    XRC(xrEnumerateSwapchainFormats(session, fmtCount, &fmtCount, formats.data()), "swapchain formats");
    bool haveColor = false, haveDepth = false;
    for (int64_t f : formats) {
        if (f == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) haveColor = true;
        if (f == DXGI_FORMAT_D32_FLOAT) haveDepth = true;
    }
    if (!haveColor) { printf("  [FAIL] DXGI_FORMAT_R8G8B8A8_UNORM_SRGB not offered - Swapchain<> ctor throws\n"); g_fail++; }
    else ok("R8G8B8A8_UNORM_SRGB offered");
    if (!haveDepth) { printf("  [FAIL] DXGI_FORMAT_D32_FLOAT not offered - depth Swapchain<> ctor throws\n"); g_fail++; }
    else ok("D32_FLOAT offered");

    // XR_PROBE_SUBRECT replays the shape VR mods use: render 1920x1080 into a bigger
    // square swapchain and declare the used region with subImage.imageRect. Off by
    // default so the plain BetterVR replay is unchanged.
    const bool subRect = getenv("XR_PROBE_SUBRECT") != nullptr;
    const uint32_t W = subRect ? 2048u : 1440u;   // a game-render-sized request, not the recommended size
    const uint32_t H = subRect ? 2048u : 1584u;
    const XrRect2Di viewRect = subRect ? XrRect2Di{ {64, 128}, {1920, 1080} }
                                       : XrRect2Di{ {0, 0}, {(int32_t)W, (int32_t)H} };
    if (subRect) printf("       sub-rect mode: %ux%u swapchain, imageRect (%d,%d) %dx%d\n",
                        W, H, viewRect.offset.x, viewRect.offset.y, viewRect.extent.width, viewRect.extent.height);
    struct Chain { XrSwapchain handle = XR_NULL_HANDLE; std::vector<ComPtr<ID3D12Resource>> tex; uint32_t idx = 0; };
    auto makeChain = [&](DXGI_FORMAT fmt, bool depth, Chain& c, const char* label) -> bool {
        XrSwapchainCreateInfo ci = { XR_TYPE_SWAPCHAIN_CREATE_INFO };
        ci.width = W; ci.height = H; ci.arraySize = 1; ci.sampleCount = 1; ci.format = fmt; ci.mipCount = 1; ci.faceCount = 1;
        ci.usageFlags = (depth ? XR_SWAPCHAIN_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT : XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT) | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        XrResult r = xrCreateSwapchain(session, &ci, &c.handle);
        if (XR_FAILED(r)) { fail(label, r); return false; }
        uint32_t n = 0;
        if (XR_FAILED(xrEnumerateSwapchainImages(c.handle, 0, &n, nullptr)) || n == 0) { printf("  [FAIL] %s: no swapchain images\n", label); g_fail++; return false; }
        std::vector<XrSwapchainImageD3D12KHR> imgs(n, { XR_TYPE_SWAPCHAIN_IMAGE_D3D12_KHR });
        r = xrEnumerateSwapchainImages(c.handle, n, &n, reinterpret_cast<XrSwapchainImageBaseHeader*>(imgs.data()));
        if (XR_FAILED(r)) { fail(label, r); return false; }
        for (auto& im : imgs) {
            if (!im.texture) { printf("  [FAIL] %s: null ID3D12Resource in swapchain image\n", label); g_fail++; return false; }
            D3D12_RESOURCE_DESC d = im.texture->GetDesc();
            if (d.Width != W || d.Height != H) { printf("  [FAIL] %s: image is %llux%u, asked for %ux%u\n", label, (unsigned long long)d.Width, d.Height, W, H); g_fail++; return false; }
            c.tex.push_back(im.texture);
        }
        // The texture must belong to OUR device or every later copy is UB.
        ComPtr<ID3D12Device> owner;
        if (SUCCEEDED(c.tex[0]->GetDevice(IID_PPV_ARGS(&owner))) && owner.Get() != device.Get()) { printf("  [FAIL] %s: swapchain texture belongs to a different ID3D12Device\n", label); g_fail++; return false; }
        printf("       %s: %u images at %ux%u on the app's device\n", label, n, W, H);
        return true;
    };
    Chain colorL, colorR, depthL, depthR, quadChain;
    bool chainsOk = true;
    chainsOk &= makeChain(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, false, colorL, "color swapchain (left)");
    chainsOk &= makeChain(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, false, colorR, "color swapchain (right)");
    chainsOk &= makeChain(DXGI_FORMAT_D32_FLOAT, true, depthL, "depth swapchain (left)");
    chainsOk &= makeChain(DXGI_FORMAT_D32_FLOAT, true, depthR, "depth swapchain (right)");
    chainsOk &= makeChain(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, false, quadChain, "quad-layer swapchain (2D HUD)");
    if (!chainsOk) return 1;
    ok("all five swapchains created");

    step("session state machine (xrPollEvent -> READY -> xrBeginSession)");
    XrSessionState state = XR_SESSION_STATE_UNKNOWN;
    bool began = false;
    for (int i = 0; i < 200 && !began; ++i) {
        XrEventDataBuffer ev = { XR_TYPE_EVENT_DATA_BUFFER };
        XrResult r = xrPollEvent(g_instance, &ev);
        while (r == XR_SUCCESS) {
            if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
                state = reinterpret_cast<XrEventDataSessionStateChanged*>(&ev)->state;
                printf("       session state -> %d\n", (int)state);
                if (state == XR_SESSION_STATE_READY) {
                    XrSessionBeginInfo bi = { XR_TYPE_SESSION_BEGIN_INFO };
                    bi.primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                    XRC_SOFT(xrBeginSession(session, &bi), "xrBeginSession");
                    began = true;
                }
            }
            ev = { XR_TYPE_EVENT_DATA_BUFFER };
            r = xrPollEvent(g_instance, &ev);
        }
        if (!began) Sleep(10);
    }
    if (!began) { printf("  [FAIL] runtime never delivered SESSION_STATE_READY - BetterVR would hang before its first frame\n"); return 1; }

    // 30 frames is enough to prove the sequence works, but far too short for anything
    // that has to observe a live session from outside (the MCP screenshot tools, for
    // one). XR_PROBE_FRAMES holds the session open for as long as the observer needs.
    int totalFrames = 30;
    if (const char* env = getenv("XR_PROBE_FRAMES")) {
        int parsed = atoi(env);
        if (parsed > 0) totalFrames = parsed;
    }

    char stepLabel[128];
    snprintf(stepLabel, sizeof(stepLabel), "frame loop x %d (waitFrame/beginFrame/locateViews/acquire/release/endFrame + syncActions)", totalFrames);
    step(stepLabel);
    int framesRendered = 0, focusedFrames = 0, quadOnlyFrames = 0;
    bool sawPosVar = false, sawSaneFov = true;
    float firstYaw = 0.0f;
    for (int f = 0; f < totalFrames; ++f) {
        XrEventDataBuffer ev = { XR_TYPE_EVENT_DATA_BUFFER };
        while (xrPollEvent(g_instance, &ev) == XR_SUCCESS) {
            if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) state = reinterpret_cast<XrEventDataSessionStateChanged*>(&ev)->state;
            ev = { XR_TYPE_EVENT_DATA_BUFFER };
        }
        if (state == XR_SESSION_STATE_FOCUSED) focusedFrames++;

        XrFrameWaitInfo fwi = { XR_TYPE_FRAME_WAIT_INFO };
        XrFrameState fs = { XR_TYPE_FRAME_STATE };
        XrResult r = xrWaitFrame(session, &fwi, &fs);
        if (XR_FAILED(r)) { fail("xrWaitFrame", r); break; }
        if (f == 0) printf("       predictedDisplayTime=%lld period=%lld shouldRender=%d\n", (long long)fs.predictedDisplayTime, (long long)fs.predictedDisplayPeriod, fs.shouldRender);
        if (fs.predictedDisplayTime <= 0) { printf("  [FAIL] predictedDisplayTime is not positive\n"); g_fail++; break; }

        XrFrameBeginInfo fbi = { XR_TYPE_FRAME_BEGIN_INFO };
        r = xrBeginFrame(session, &fbi);
        if (XR_FAILED(r)) { fail("xrBeginFrame", r); break; }

        // BetterVR syncs actions every frame while the session is up.
        XrActiveActionSet aas = { gameplaySet, XR_NULL_PATH };
        XrActionsSyncInfo asi = { XR_TYPE_ACTIONS_SYNC_INFO };
        asi.countActiveActionSets = 1; asi.activeActionSets = &aas;
        r = xrSyncActions(session, &asi);
        if (XR_FAILED(r) && r != XR_SESSION_NOT_FOCUSED) { fail("xrSyncActions", r); break; }

        XrActionStateGetInfo gi = { XR_TYPE_ACTION_STATE_GET_INFO };
        gi.action = gripPose; gi.subactionPath = handPaths[0];
        XrActionStatePose ps = { XR_TYPE_ACTION_STATE_POSE };
        if (XR_FAILED(xrGetActionStatePose(session, &gi, &ps))) { printf("  [FAIL] xrGetActionStatePose\n"); g_fail++; break; }
        gi.action = jump;
        XrActionStateBoolean bs = { XR_TYPE_ACTION_STATE_BOOLEAN };
        if (XR_FAILED(xrGetActionStateBoolean(session, &gi, &bs))) { printf("  [FAIL] xrGetActionStateBoolean\n"); g_fail++; break; }
        gi.action = move;
        XrActionStateVector2f v2 = { XR_TYPE_ACTION_STATE_VECTOR2F };
        if (XR_FAILED(xrGetActionStateVector2f(session, &gi, &v2))) { printf("  [FAIL] xrGetActionStateVector2f\n"); g_fail++; break; }
        gi.action = grab;
        XrActionStateFloat fv = { XR_TYPE_ACTION_STATE_FLOAT };
        if (XR_FAILED(xrGetActionStateFloat(session, &gi, &fv))) { printf("  [FAIL] xrGetActionStateFloat\n"); g_fail++; break; }

        // hand pose location (BetterVR chains XrSpaceVelocity onto this)
        if (handSpaces[0]) {
            XrSpaceVelocity vel = { XR_TYPE_SPACE_VELOCITY };
            XrSpaceLocation loc = { XR_TYPE_SPACE_LOCATION, &vel };
            if (XR_FAILED(xrLocateSpace(handSpaces[0], stageSpace, fs.predictedDisplayTime, &loc))) { printf("  [FAIL] xrLocateSpace(hand->stage)\n"); g_fail++; break; }
        }
        XrSpaceLocation headLoc = { XR_TYPE_SPACE_LOCATION };
        if (XR_FAILED(xrLocateSpace(headSpace, stageSpace, fs.predictedDisplayTime, &headLoc))) { printf("  [FAIL] xrLocateSpace(view->stage)\n"); g_fail++; break; }

        XrViewLocateInfo vli = { XR_TYPE_VIEW_LOCATE_INFO };
        vli.viewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
        vli.displayTime = fs.predictedDisplayTime;
        vli.space = stageSpace;
        XrViewState vs = { XR_TYPE_VIEW_STATE };
        uint32_t n = 2;
        std::array<XrView, 2> views = { XrView{ XR_TYPE_VIEW }, XrView{ XR_TYPE_VIEW } };
        r = xrLocateViews(session, &vli, &vs, n, &n, views.data());
        if (XR_FAILED(r)) { fail("xrLocateViews", r); break; }
        if (f == 0) {
            printf("       viewStateFlags=0x%llX  n=%u\n", (unsigned long long)vs.viewStateFlags, n);
            printf("       L pos=(%.3f,%.3f,%.3f) fov=(%.3f,%.3f,%.3f,%.3f)\n", views[0].pose.position.x, views[0].pose.position.y, views[0].pose.position.z,
                   views[0].fov.angleLeft, views[0].fov.angleRight, views[0].fov.angleUp, views[0].fov.angleDown);
            printf("       R pos=(%.3f,%.3f,%.3f) fov=(%.3f,%.3f,%.3f,%.3f)\n", views[1].pose.position.x, views[1].pose.position.y, views[1].pose.position.z,
                   views[1].fov.angleLeft, views[1].fov.angleRight, views[1].fov.angleUp, views[1].fov.angleDown);
            firstYaw = views[0].pose.orientation.y;
            if (views[0].pose.position.x == views[1].pose.position.x) warn("both eyes share an X position - no stereo IPD separation");
        }
        if (views[0].pose.orientation.y != firstYaw) sawPosVar = true;
        if (!(vs.viewStateFlags & XR_VIEW_STATE_ORIENTATION_VALID_BIT) || !(vs.viewStateFlags & XR_VIEW_STATE_POSITION_VALID_BIT)) {
            if (f == 0) { printf("  [FAIL] viewState missing ORIENTATION_VALID/POSITION_VALID - BetterVR treats views as unusable\n"); g_fail++; }
        }
        if (views[0].fov.angleLeft >= views[0].fov.angleRight || views[0].fov.angleDown >= views[0].fov.angleUp) sawSaneFov = false;

        // Acquire, actually draw into the image, then release WITHOUT any barrier -
        // exactly what Layer3D::RecordRender does. It never transitions the
        // swapchain because XR_KHR_D3D12_enable hands colour images over in
        // RENDER_TARGET and depth in DEPTH_WRITE and wants them back that way
        // (renderer.cpp:492 and :514). A runtime that tracks them as anything else
        // issues a mismatched barrier, which is what validation catches here.
        auto cycle = [&](Chain& c, bool depth, int eye = -1) -> bool {
            uint32_t idx = 0;
            if (XR_FAILED(xrAcquireSwapchainImage(c.handle, nullptr, &idx))) return false;
            XrSwapchainImageWaitInfo wi = { XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO };
            wi.timeout = XR_INFINITE_DURATION;
            if (XR_FAILED(xrWaitSwapchainImage(c.handle, &wi))) return false;
            c.idx = idx;

            if (FAILED(cmdAlloc->Reset()) || FAILED(cmdList->Reset(cmdAlloc.Get(), nullptr))) return false;
            if (depth) {
                D3D12_DEPTH_STENCIL_VIEW_DESC dv = {};
                dv.Format = DXGI_FORMAT_D32_FLOAT;
                dv.ViewDimension = D3D12_DSV_DIMENSION_TEXTURE2D;
                D3D12_CPU_DESCRIPTOR_HANDLE h = dsvHeap->GetCPUDescriptorHandleForHeapStart();
                device->CreateDepthStencilView(c.tex[idx].Get(), &dv, h);
                cmdList->ClearDepthStencilView(h, D3D12_CLEAR_FLAG_DEPTH, 1.0f, 0, 0, nullptr);
            } else {
                D3D12_RENDER_TARGET_VIEW_DESC rv = {};
                rv.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
                rv.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
                D3D12_CPU_DESCRIPTOR_HANDLE h = rtvHeap->GetCPUDescriptorHandleForHeapStart();
                device->CreateRenderTargetView(c.tex[idx].Get(), &rv, h);

                // Everything outside the declared rect gets a colour the preview must
                // never show. Green on screen means imageRect was ignored somewhere.
                if (subRect) {
                    const float outside[4] = { 0.0f, 0.9f, 0.1f, 1.0f };
                    cmdList->ClearRenderTargetView(h, outside, 0, nullptr);
                }
                const D3D12_RECT rect = { viewRect.offset.x, viewRect.offset.y,
                                          viewRect.offset.x + viewRect.extent.width,
                                          viewRect.offset.y + viewRect.extent.height };
                if (eye >= 0) {
                    const float clear[4] = { 0.1f, 0.2f, 0.4f, 1.0f };
                    cmdList->ClearRenderTargetView(h, clear, 1, &rect);

                    // A flat clear is indistinguishable between the eyes, so a stereo
                    // checker can only ever report "no parallax" on it. Mark each eye
                    // with the same square at a 20px horizontal offset: real, known
                    // disparity, inside the center window a disparity search looks at.
                    const float mark[4] = { 0.95f, 0.9f, 0.25f, 1.0f };
                    const LONG cx = rect.left + viewRect.extent.width / 2 + (eye == 0 ? 10 : -10);
                    const LONG cy = rect.top + viewRect.extent.height / 2;
                    D3D12_RECT square = { cx - 100, cy - 100, cx + 100, cy + 100 };
                    cmdList->ClearRenderTargetView(h, mark, 1, &square);
                } else {
                    // The quad (2D) layer, in a colour nothing else uses: it is the only
                    // way to see, in the preview window, whether the overlay survives the
                    // eye blit that lands in the same frame.
                    const float hud[4] = { 0.8f, 0.1f, 0.5f, 1.0f };
                    cmdList->ClearRenderTargetView(h, hud, 1, &rect);
                }
            }
            if (FAILED(cmdList->Close())) return false;
            ID3D12CommandList* lists[] = { cmdList.Get() };
            queue->ExecuteCommandLists(1, lists);
            if (FAILED(queue->Signal(fence.Get(), ++fenceValue))) return false;
            if (fence->GetCompletedValue() < fenceValue) {
                fence->SetEventOnCompletion(fenceValue, fenceEvent);
                WaitForSingleObject(fenceEvent, 5000);
            }

            XrSwapchainImageReleaseInfo ri = { XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO };
            return XR_SUCCEEDED(xrReleaseSwapchainImage(c.handle, &ri));
        };
        // BetterVR gates its projection layer on IsInGame() (RND_Renderer::EndFrame),
        // so its whole boot and title sequence is quad-only frames. Replay both shapes.
        const bool quadOnly = (f < totalFrames / 2);
        if (quadOnly) quadOnlyFrames++;

        bool cycled = cycle(quadChain, false);
        if (!quadOnly) {
            cycled = cycled && cycle(colorL, false, 0) && cycle(colorR, false, 1)
                            && cycle(depthL, true)     && cycle(depthR, true);
        }
        if (!cycled) { printf("  [FAIL] swapchain acquire/render/release cycle failed on frame %d\n", f); g_fail++; break; }

        if (FAILED(device->GetDeviceRemovedReason())) { printf("  [FAIL] D3D12 device removed on frame %d (reason 0x%08X)\n", f, (unsigned)device->GetDeviceRemovedReason()); g_fail++; break; }

        XrCompositionLayerDepthInfoKHR depthInfo[2] = {};
        XrCompositionLayerProjectionView projViews[2] = {};
        Chain* colorChains[2] = { &colorL, &colorR };
        Chain* depthChains[2] = { &depthL, &depthR };
        for (int e = 0; e < 2; ++e) {
            depthInfo[e] = { XR_TYPE_COMPOSITION_LAYER_DEPTH_INFO_KHR };
            depthInfo[e].subImage.swapchain = depthChains[e]->handle;
            depthInfo[e].subImage.imageRect = viewRect;
            depthInfo[e].subImage.imageArrayIndex = 0;
            depthInfo[e].minDepth = 0.0f; depthInfo[e].maxDepth = 1.0f;
            depthInfo[e].nearZ = 0.05f; depthInfo[e].farZ = INFINITY;

            projViews[e] = { XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW };
            projViews[e].next = &depthInfo[e];
            projViews[e].pose = views[e].pose;
            projViews[e].fov = views[e].fov;
            projViews[e].subImage.swapchain = colorChains[e]->handle;
            projViews[e].subImage.imageRect = viewRect;
            projViews[e].subImage.imageArrayIndex = 0;
        }
        XrCompositionLayerProjection proj = { XR_TYPE_COMPOSITION_LAYER_PROJECTION };
        proj.space = stageSpace; proj.viewCount = 2; proj.views = projViews;

        XrCompositionLayerQuad quad = { XR_TYPE_COMPOSITION_LAYER_QUAD };
        quad.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
        quad.space = stageSpace;
        quad.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
        quad.subImage.swapchain = quadChain.handle;
        quad.subImage.imageRect = viewRect;
        // World-locked at eye height, 2 m ahead, like a BetterVR 2D screen. A runtime
        // that reads a STAGE pose as head-relative throws this off the top of the view.
        quad.pose = identity;
        quad.pose.position.y = (views[0].pose.position.y + views[1].pose.position.y) * 0.5f;
        quad.pose.position.z = -2.0f;
        quad.size = { 1.6f, 0.9f };

        const XrCompositionLayerBaseHeader* layers[2];
        uint32_t layerCount = 0;
        if (!quadOnly) layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&proj);
        layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&quad);

        XrFrameEndInfo fei = { XR_TYPE_FRAME_END_INFO };
        fei.displayTime = fs.predictedDisplayTime;
        fei.environmentBlendMode = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
        fei.layerCount = layerCount;
        fei.layers = const_cast<const XrCompositionLayerBaseHeader**>(layers);
        r = xrEndFrame(session, &fei);
        if (XR_FAILED(r)) { fail("xrEndFrame", r); break; }
        framesRendered++;
    }
    printf("       rendered %d/%d frames (%d quad-only), %d of them with state==FOCUSED\n",
           framesRendered, totalFrames, quadOnlyFrames, focusedFrames);
    if (framesRendered == totalFrames) {
        char label[160];
        snprintf(label, sizeof(label), "%d clean frames: %d quad-only, then %d with a projection(+depth) layer and a quad layer",
                 totalFrames, quadOnlyFrames, totalFrames - quadOnlyFrames);
        ok(label);
    }
    if (focusedFrames == 0) warn("session never reached FOCUSED - BetterVR gates its input handling on that");
    if (!sawSaneFov) { printf("  [FAIL] nonsensical FOV angles (left>=right or down>=up)\n"); g_fail++; }

    step("D3D12 validation messages");
    if (infoQueue) {
        const UINT64 n = infoQueue->GetNumStoredMessages();
        UINT64 errors = 0;
        for (UINT64 i = 0; i < n; ++i) {
            SIZE_T len = 0;
            if (FAILED(infoQueue->GetMessage(i, nullptr, &len)) || len == 0) continue;
            std::vector<char> raw(len);
            auto* m = reinterpret_cast<D3D12_MESSAGE*>(raw.data());
            if (FAILED(infoQueue->GetMessage(i, m, &len))) continue;
            if (m->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION || m->Severity == D3D12_MESSAGE_SEVERITY_ERROR) {
                if (errors < 5) printf("  [FAIL] D3D12 %s: %.*s\n", m->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION ? "CORRUPTION" : "ERROR", (int)m->DescriptionByteLength, m->pDescription);
                errors++;
            }
        }
        if (errors) { printf("  [FAIL] %llu D3D12 validation error(s) during the frame loop\n", (unsigned long long)errors); g_fail++; }
        else ok("no D3D12 validation errors across the frame loop");
    } else {
        warn("no ID3D12InfoQueue - resource-state bugs cannot be detected in this run");
    }

    step("time conversion");
    LARGE_INTEGER pc; QueryPerformanceCounter(&pc);
    XrTime t = 0;
    XRC_SOFT(pfnPcToTime(g_instance, &pc, &t), "xrConvertWin32PerformanceCounterToTimeKHR");
    LARGE_INTEGER back = {};
    XRC_SOFT(pfnTimeToPc(g_instance, t, &back), "xrConvertTimeToWin32PerformanceCounterKHR");
    if (t <= 0) { printf("  [FAIL] converted XrTime is not positive\n"); g_fail++; }
    else printf("       XrTime=%lld  roundtrip QPC delta=%lld ticks\n", (long long)t, (long long)(back.QuadPart - pc.QuadPart));

    // RND_Renderer::~RND_Renderer calls these back to back without waiting for
    // STOPPING, and wraps xrEndSession in checkXRResult - which throws and pops a
    // MessageBox. A runtime that insists on the conformant STOPPING handshake
    // makes BetterVR's shutdown path fail, so probe it exactly as BetterVR does.
    step("teardown (BetterVR's exact shutdown order)");
    XRC_SOFT(xrRequestExitSession(session), "xrRequestExitSession");
    XRC_SOFT(xrEndSession(session), "xrEndSession right after xrRequestExitSession");
    for (Chain* c : { &colorL, &colorR, &depthL, &depthR, &quadChain }) if (c->handle) xrDestroySwapchain(c->handle);
    for (XrSpace s : { handSpaces[0], handSpaces[1], headSpace, stageSpace }) if (s) xrDestroySpace(s);
    XRC_SOFT(xrDestroySession(session), "xrDestroySession");
    XRC_SOFT(xrDestroyInstance(g_instance), "xrDestroyInstance");

    printf("\n================ %d failure(s), %d warning(s) ================\n", g_fail, g_warn);
    return g_fail ? 1 : 0;
}
