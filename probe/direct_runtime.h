// Standalone probe dispatch: exercise the actual runtime DLL without borrowing
// an OpenXR loader/library from another checkout. Normal probe builds use the loader.
#pragma once
#include <loader_interfaces.h>

#define UI_PROBE_FUNCTIONS(X) \
 X(xrAcquireSwapchainImage) X(xrAttachSessionActionSets) X(xrBeginFrame) X(xrBeginSession) \
 X(xrCreateAction) X(xrCreateActionSet) X(xrCreateActionSpace) X(xrCreateInstance) \
 X(xrCreateReferenceSpace) X(xrCreateSession) X(xrCreateSwapchain) X(xrDestroyInstance) \
 X(xrDestroySession) X(xrDestroySpace) X(xrDestroySwapchain) X(xrEndFrame) X(xrEndSession) \
 X(xrEnumerateInstanceExtensionProperties) X(xrEnumerateSwapchainFormats) X(xrEnumerateSwapchainImages) \
 X(xrEnumerateViewConfigurationViews) X(xrGetActionStateBoolean) X(xrGetActionStateFloat) \
 X(xrGetActionStatePose) X(xrGetActionStateVector2f) X(xrGetCurrentInteractionProfile) \
 X(xrGetInstanceProperties) X(xrGetSystem) X(xrGetSystemProperties) X(xrGetViewConfigurationProperties) \
 X(xrLocateSpace) X(xrLocateViews) X(xrPathToString) X(xrPollEvent) X(xrReleaseSwapchainImage) \
 X(xrRequestExitSession) X(xrStringToPath) X(xrSuggestInteractionProfileBindings) X(xrSyncActions) \
 X(xrWaitFrame) X(xrWaitSwapchainImage)

#define DECLARE_XR(name) static PFN_##name name = nullptr;
UI_PROBE_FUNCTIONS(DECLARE_XR)
#undef DECLARE_XR
static PFN_xrGetInstanceProcAddr xrGetInstanceProcAddr=nullptr;
static PFN_xrCreateInstance directCreateInstance=nullptr;

static XrResult XRAPI_PTR CreateDirectInstance(const XrInstanceCreateInfo* info, XrInstance* instance) {
    const XrResult result=directCreateInstance(info,instance);
    if (XR_FAILED(result)) return result;
    // Instance commands become discoverable only after an instance exists.
#define LOAD_INSTANCE_XR(name) do { \
    const XrResult loaded=xrGetInstanceProcAddr(*instance,#name,(PFN_xrVoidFunction*)&name); \
    if (XR_FAILED(loaded)) { printf("Cannot resolve %s: %d\n",#name,(int)loaded); return loaded; } \
} while (0);
    UI_PROBE_FUNCTIONS(LOAD_INSTANCE_XR)
#undef LOAD_INSTANCE_XR
    xrCreateInstance=CreateDirectInstance;
    return result;
}

static bool InitDirectRuntime() {
    char path[MAX_PATH]{};
    if (!GetEnvironmentVariableA("SIMXR_RUNTIME_DLL",path,MAX_PATH)) {
        GetModuleFileNameA(nullptr,path,MAX_PATH);
        char* slash=strrchr(path,'\\');
        if (!slash) return false;
        strcpy_s(slash+1,MAX_PATH-(slash+1-path),"openxr_simulator.dll");
    }
    HMODULE module=LoadLibraryA(path);
    if (!module) { printf("Cannot load runtime %s (Win32 %lu)\n",path,GetLastError()); return false; }
    const auto negotiate=(PFN_xrNegotiateLoaderRuntimeInterface)GetProcAddress(module,"xrNegotiateLoaderRuntimeInterface");
    if (!negotiate) return false;
    XrNegotiateLoaderInfo loader{XR_LOADER_INTERFACE_STRUCT_LOADER_INFO,1,sizeof(XrNegotiateLoaderInfo),1,1,XR_MAKE_VERSION(1,0,0),XR_MAKE_VERSION(1,0,0)};
    XrNegotiateRuntimeRequest request{XR_LOADER_INTERFACE_STRUCT_RUNTIME_REQUEST,1,sizeof(XrNegotiateRuntimeRequest)};
    if (XR_FAILED(negotiate(&loader,&request))) return false;
    xrGetInstanceProcAddr=request.getInstanceProcAddr;
#define LOAD_XR(name) if (XR_FAILED(xrGetInstanceProcAddr(XR_NULL_HANDLE,#name,(PFN_xrVoidFunction*)&name))) return false;
    LOAD_XR(xrCreateInstance)
    LOAD_XR(xrEnumerateInstanceExtensionProperties)
#undef LOAD_XR
    directCreateInstance=xrCreateInstance;
    xrCreateInstance=CreateDirectInstance;
    return true;
}
