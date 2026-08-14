// Vulkan counterpart of xr_probe.cpp: replays the OpenXR sequence a native-Vulkan
// BetterVR would run against whatever runtime XR_RUNTIME_JSON points at.
//
// XR_KHR_vulkan_enable2 by default, XR_KHR_vulkan_enable with XR_PROBE_VK_ENABLE1=1.
// The Khronos validation layer is on and any error fails the run - which only means
// anything because every acquired image is really rendered into, through a render pass
// that declares the image is already in COLOR_ATTACHMENT_OPTIMAL (depth:
// DEPTH_STENCIL_ATTACHMENT_OPTIMAL) and leaves it there. That is what the spec says a
// runtime hands over, it is what BetterVR assumes, and a runtime that hands over anything
// else is caught here rather than in a corrupted frame.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <unknwn.h>   // XR_USE_PLATFORM_WIN32 exposes extensions declared in terms of IUnknown
#include <cstdio>
#include <cstring>
#include <cmath>
#include <vector>
#include <string>
#include <array>

#define VK_USE_PLATFORM_WIN32_KHR
#include <vulkan/vulkan.h>

#define XR_USE_GRAPHICS_API_VULKAN
#define XR_USE_PLATFORM_WIN32
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

static int g_fail = 0;
static int g_warn = 0;
static int g_vkErrors = 0;
static bool g_selfTest = false;   // inside the deliberate-error check; count, don't print

static void ok(const char* what) { printf("  [ ok ] %s\n", what); }
static void fail(const char* what, XrResult r) { printf("  [FAIL] %s -> XrResult %d\n", what, (int)r); g_fail++; }
static void warn(const char* what) { printf("  [warn] %s\n", what); g_warn++; }
static void step(const char* what) { printf("\n== %s\n", what); }

#define XRC(call, what) do { XrResult _r = (call); if (XR_FAILED(_r)) { fail(what, _r); return 1; } ok(what); } while (0)
#define XRC_SOFT(call, what) do { XrResult _r = (call); if (XR_FAILED(_r)) { fail(what, _r); } else ok(what); } while (0)
#define VKC(call, what) do { VkResult _r = (call); if (_r != VK_SUCCESS) { printf("  [FAIL] %s -> VkResult %d\n", what, (int)_r); g_fail++; return 1; } } while (0)

static XrInstance g_instance = XR_NULL_HANDLE;
static XrPath P(const char* s) { XrPath p = XR_NULL_PATH; xrStringToPath(g_instance, s, &p); return p; }

template <typename T>
static bool XrFn(const char* name, T& out) {
    if (XR_FAILED(xrGetInstanceProcAddr(g_instance, name, (PFN_xrVoidFunction*)&out)) || !out) {
        printf("  [FAIL] xrGetInstanceProcAddr(%s)\n", name);
        g_fail++;
        return false;
    }
    return true;
}

static VKAPI_ATTR VkBool32 VKAPI_CALL DebugCallback(VkDebugUtilsMessageSeverityFlagBitsEXT severity,
                                                    VkDebugUtilsMessageTypeFlagsEXT type,
                                                    const VkDebugUtilsMessengerCallbackDataEXT* data, void*) {
    const char* msg = (data && data->pMessage) ? data->pMessage : "(no message)";
    // GENERAL covers the loader's own complaints - a stale implicit-layer manifest, say -
    // which say nothing about the runtime under test. Only VALIDATION fails the run.
    const bool isValidation = (type & VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT) != 0;
    if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT) {
        if (isValidation) {
            if (!g_selfTest && g_vkErrors < 8) printf("  [FAIL] Vulkan validation: %s\n", msg);
            g_vkErrors++;
        } else if (g_selfTest) {
            // still counted below; the self-test only cares that something arrived
            g_vkErrors++;
        } else {
            printf("  [warn] Vulkan loader: %s\n", msg);
            g_warn++;
        }
    } else if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT) {
        printf("  [warn] Vulkan %s: %s\n", isValidation ? "validation" : "loader", msg);
    }
    return VK_FALSE;
}

// An empty-but-set variable means off. PowerShell's `$env:X = ''` sets rather than removes,
// so `getenv() != nullptr` silently turns every flag on for the rest of a session.
static bool EnvOn(const char* name) {
    const char* v = getenv(name);
    return v && v[0] && strcmp(v, "0") != 0;
}

static std::vector<std::string> SplitSpaces(const std::string& s) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i < s.size()) {
        while (i < s.size() && s[i] == ' ') ++i;
        size_t j = i;
        while (j < s.size() && s[j] != ' ') ++j;
        if (j > i) out.emplace_back(s.substr(i, j - i));
        i = j;
    }
    return out;
}

// A swapchain plus everything needed to render into it: one view and framebuffer per image
// and the render pass that declares the layout the runtime is required to hand them over in.
struct Chain {
    XrSwapchain handle = XR_NULL_HANDLE;
    std::vector<VkImage> images;
    std::vector<VkImageView> views;
    std::vector<VkFramebuffer> framebuffers;
    VkRenderPass renderPass = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    bool depth = false;
    uint32_t idx = 0;
};

int main() {
    // Unbuffered: a probe that stalls has to have already told you how far it got.
    setvbuf(stdout, nullptr, _IONBF, 0);
    const bool enable1 = EnvOn("XR_PROBE_VK_ENABLE1");
    printf("== extension path: %s\n", enable1 ? "XR_KHR_vulkan_enable (v1)" : "XR_KHR_vulkan_enable2");

    step("xrEnumerateInstanceExtensionProperties");
    uint32_t extCount = 0;
    XRC(xrEnumerateInstanceExtensionProperties(nullptr, 0, &extCount, nullptr), "enumerate extension count");
    std::vector<XrExtensionProperties> exts(extCount, { XR_TYPE_EXTENSION_PROPERTIES });
    XRC(xrEnumerateInstanceExtensionProperties(nullptr, extCount, &extCount, exts.data()), "enumerate extensions");
    bool hasVk1 = false, hasVk2 = false, hasDepth = false, hasTimeConv = false;
    for (auto& e : exts) {
        printf("       - %s\n", e.extensionName);
        if (!strcmp(e.extensionName, XR_KHR_VULKAN_ENABLE_EXTENSION_NAME)) hasVk1 = true;
        if (!strcmp(e.extensionName, XR_KHR_VULKAN_ENABLE2_EXTENSION_NAME)) hasVk2 = true;
        if (!strcmp(e.extensionName, XR_KHR_COMPOSITION_LAYER_DEPTH_EXTENSION_NAME)) hasDepth = true;
        if (!strcmp(e.extensionName, XR_KHR_WIN32_CONVERT_PERFORMANCE_COUNTER_TIME_EXTENSION_NAME)) hasTimeConv = true;
    }
    const char* gfxExt = enable1 ? XR_KHR_VULKAN_ENABLE_EXTENSION_NAME : XR_KHR_VULKAN_ENABLE2_EXTENSION_NAME;
    if (enable1 ? !hasVk1 : !hasVk2) { printf("  [FAIL] %s missing\n", gfxExt); return 1; }
    if (!hasDepth) { printf("  [FAIL] XR_KHR_composition_layer_depth missing - BetterVR requires it unconditionally\n"); return 1; }
    if (!hasTimeConv) { printf("  [FAIL] XR_KHR_win32_convert_performance_counter_time missing - BetterVR requires it unconditionally\n"); return 1; }
    ok("graphics + depth + time-conversion extensions present");
    if (hasVk1 && hasVk2) ok("both XR_KHR_vulkan_enable and _enable2 are offered");

    step("xrCreateInstance");
    std::vector<const char*> enabled = { gfxExt, XR_KHR_COMPOSITION_LAYER_DEPTH_EXTENSION_NAME,
                                         XR_KHR_WIN32_CONVERT_PERFORMANCE_COUNTER_TIME_EXTENSION_NAME };
    XrInstanceCreateInfo ici = { XR_TYPE_INSTANCE_CREATE_INFO };
    ici.enabledExtensionCount = (uint32_t)enabled.size();
    ici.enabledExtensionNames = enabled.data();
    ici.applicationInfo = { "BetterVR", 1, "Cemu", 1, XR_API_VERSION_1_0 };
    XRC(xrCreateInstance(&ici, &g_instance), "xrCreateInstance");

    PFN_xrConvertWin32PerformanceCounterToTimeKHR pfnPcToTime = nullptr;
    PFN_xrConvertTimeToWin32PerformanceCounterKHR pfnTimeToPc = nullptr;
    if (!XrFn("xrConvertWin32PerformanceCounterToTimeKHR", pfnPcToTime)) return 1;
    if (!XrFn("xrConvertTimeToWin32PerformanceCounterKHR", pfnTimeToPc)) return 1;

    step("system + properties");
    XrSystemGetInfo sgi = { XR_TYPE_SYSTEM_GET_INFO };
    sgi.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId systemId = XR_NULL_SYSTEM_ID;
    XRC(xrGetSystem(g_instance, &sgi, &systemId), "xrGetSystem");
    XrInstanceProperties instProps = { XR_TYPE_INSTANCE_PROPERTIES };
    XRC(xrGetInstanceProperties(g_instance, &instProps), "xrGetInstanceProperties");
    printf("       runtimeName = \"%s\"\n", instProps.runtimeName);

    step("xrGetVulkanGraphicsRequirements (must precede any Vulkan object creation)");
    XrGraphicsRequirementsVulkanKHR gfxReq = { XR_TYPE_GRAPHICS_REQUIREMENTS_VULKAN_KHR };
    if (enable1) {
        PFN_xrGetVulkanGraphicsRequirementsKHR pfn = nullptr;
        if (!XrFn("xrGetVulkanGraphicsRequirementsKHR", pfn)) return 1;
        XRC(pfn(g_instance, systemId, &gfxReq), "xrGetVulkanGraphicsRequirementsKHR");
    } else {
        PFN_xrGetVulkanGraphicsRequirements2KHR pfn = nullptr;
        if (!XrFn("xrGetVulkanGraphicsRequirements2KHR", pfn)) return 1;
        XRC(pfn(g_instance, systemId, &gfxReq), "xrGetVulkanGraphicsRequirements2KHR");
    }
    printf("       Vulkan %d.%d.%d - %d.%d.%d\n",
           XR_VERSION_MAJOR(gfxReq.minApiVersionSupported), XR_VERSION_MINOR(gfxReq.minApiVersionSupported), XR_VERSION_PATCH(gfxReq.minApiVersionSupported),
           XR_VERSION_MAJOR(gfxReq.maxApiVersionSupported), XR_VERSION_MINOR(gfxReq.maxApiVersionSupported), XR_VERSION_PATCH(gfxReq.maxApiVersionSupported));

    step("xrEnumerateViewConfigurationViews");
    uint32_t viewCount = 0;
    XRC(xrEnumerateViewConfigurationViews(g_instance, systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, 0, &viewCount, nullptr), "view config view count");
    if (viewCount != 2) { printf("  [FAIL] expected 2 views, got %u\n", viewCount); return 1; }
    std::array<XrViewConfigurationView, 2> vcv = { XrViewConfigurationView{ XR_TYPE_VIEW_CONFIGURATION_VIEW }, XrViewConfigurationView{ XR_TYPE_VIEW_CONFIGURATION_VIEW } };
    XRC(xrEnumerateViewConfigurationViews(g_instance, systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, viewCount, &viewCount, vcv.data()), "view config views");
    printf("       recommended %ux%u samples %u\n", vcv[0].recommendedImageRectWidth, vcv[0].recommendedImageRectHeight, vcv[0].recommendedSwapchainSampleCount);

    // ---- VkInstance -------------------------------------------------------------------
    step("VkInstance (validation layer on)");
    uint32_t layerCount = 0;
    vkEnumerateInstanceLayerProperties(&layerCount, nullptr);
    std::vector<VkLayerProperties> layers(layerCount);
    if (layerCount) vkEnumerateInstanceLayerProperties(&layerCount, layers.data());
    bool haveValidation = false;
    for (auto& l : layers) if (!strcmp(l.layerName, "VK_LAYER_KHRONOS_validation")) haveValidation = true;
    if (!haveValidation) warn("VK_LAYER_KHRONOS_validation not installed - layout bugs cannot be detected in this run");
    else ok("VK_LAYER_KHRONOS_validation available");

    uint32_t loaderVersion = VK_API_VERSION_1_0;
    if (auto pfnVer = (PFN_vkEnumerateInstanceVersion)vkGetInstanceProcAddr(VK_NULL_HANDLE, "vkEnumerateInstanceVersion")) pfnVer(&loaderVersion);
    uint32_t apiVersion = VK_API_VERSION_1_2;
    if (VK_API_VERSION_MAJOR(loaderVersion) == 1 && VK_API_VERSION_MINOR(loaderVersion) < 2) apiVersion = loaderVersion;
    printf("       loader reports Vulkan %u.%u, asking for %u.%u\n",
           VK_API_VERSION_MAJOR(loaderVersion), VK_API_VERSION_MINOR(loaderVersion),
           VK_API_VERSION_MAJOR(apiVersion), VK_API_VERSION_MINOR(apiVersion));

    VkApplicationInfo appInfo{ VK_STRUCTURE_TYPE_APPLICATION_INFO };
    appInfo.pApplicationName = "BetterVR";
    appInfo.pEngineName = "Cemu";
    appInfo.apiVersion = apiVersion;

    VkDebugUtilsMessengerCreateInfoEXT dbgInfo{ VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT };
    dbgInfo.messageSeverity = VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT;
    dbgInfo.messageType = VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT |
                          VK_DEBUG_UTILS_MESSAGE_TYPE_PERFORMANCE_BIT_EXT;
    dbgInfo.pfnUserCallback = DebugCallback;

    std::vector<const char*> instExts = { VK_EXT_DEBUG_UTILS_EXTENSION_NAME };
    std::vector<std::string> v1InstExtStorage;
    if (enable1) {
        // v1 makes the app collect the runtime's instance extensions and create the
        // VkInstance itself; v2 injects them inside xrCreateVulkanInstanceKHR.
        PFN_xrGetVulkanInstanceExtensionsKHR pfn = nullptr;
        if (!XrFn("xrGetVulkanInstanceExtensionsKHR", pfn)) return 1;
        uint32_t n = 0;
        XRC(pfn(g_instance, systemId, 0, &n, nullptr), "xrGetVulkanInstanceExtensionsKHR (size)");
        std::string buf(n, '\0');
        XRC(pfn(g_instance, systemId, n, &n, buf.data()), "xrGetVulkanInstanceExtensionsKHR");
        v1InstExtStorage = SplitSpaces(buf.c_str());
        printf("       runtime instance extensions: %s\n", buf.c_str());
        for (auto& e : v1InstExtStorage) instExts.push_back(e.c_str());
    }
    const char* validationLayer = "VK_LAYER_KHRONOS_validation";

    VkInstanceCreateInfo vkici{ VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO };
    vkici.pNext = &dbgInfo;
    vkici.pApplicationInfo = &appInfo;
    vkici.enabledExtensionCount = (uint32_t)instExts.size();
    vkici.ppEnabledExtensionNames = instExts.data();
    if (haveValidation) { vkici.enabledLayerCount = 1; vkici.ppEnabledLayerNames = &validationLayer; }

    VkInstance vkInstance = VK_NULL_HANDLE;
    if (enable1) {
        VKC(vkCreateInstance(&vkici, nullptr, &vkInstance), "vkCreateInstance");
        ok("vkCreateInstance (app-owned, v1 path)");
    } else {
        PFN_xrCreateVulkanInstanceKHR pfn = nullptr;
        if (!XrFn("xrCreateVulkanInstanceKHR", pfn)) return 1;
        XrVulkanInstanceCreateInfoKHR ci{ XR_TYPE_VULKAN_INSTANCE_CREATE_INFO_KHR };
        ci.systemId = systemId;
        ci.pfnGetInstanceProcAddr = &vkGetInstanceProcAddr;
        ci.vulkanCreateInfo = &vkici;
        VkResult vr = VK_SUCCESS;
        XRC(pfn(g_instance, &ci, &vkInstance, &vr), "xrCreateVulkanInstanceKHR");
        if (vr != VK_SUCCESS) { printf("  [FAIL] the runtime's vkCreateInstance returned %d\n", (int)vr); return 1; }
        ok("xrCreateVulkanInstanceKHR created the VkInstance");
    }

    // The messenger chained into VkInstanceCreateInfo only lives for the duration of
    // vkCreateInstance, so without this one nothing after instance creation is ever
    // reported and the run passes no matter what the runtime does.
    VkDebugUtilsMessengerEXT messenger = VK_NULL_HANDLE;
    if (auto pfnCreateDbg = (PFN_vkCreateDebugUtilsMessengerEXT)vkGetInstanceProcAddr(vkInstance, "vkCreateDebugUtilsMessengerEXT")) {
        const VkResult mr = pfnCreateDbg(vkInstance, &dbgInfo, nullptr, &messenger);
        if (mr != VK_SUCCESS || messenger == VK_NULL_HANDLE) { printf("  [FAIL] vkCreateDebugUtilsMessengerEXT -> %d\n", (int)mr); g_fail++; }
        else ok("debug messenger attached for the whole run");
    } else {
        printf("  [FAIL] vkCreateDebugUtilsMessengerEXT unavailable - nothing would be validated\n");
        g_fail++;
    }

    // ---- VkPhysicalDevice --------------------------------------------------------------
    step("xrGetVulkanGraphicsDevice (must be the adapter the compositor uses)");
    VkPhysicalDevice physicalDevice = VK_NULL_HANDLE;
    if (enable1) {
        PFN_xrGetVulkanGraphicsDeviceKHR pfn = nullptr;
        if (!XrFn("xrGetVulkanGraphicsDeviceKHR", pfn)) return 1;
        XRC(pfn(g_instance, systemId, vkInstance, &physicalDevice), "xrGetVulkanGraphicsDeviceKHR");
    } else {
        PFN_xrGetVulkanGraphicsDevice2KHR pfn = nullptr;
        if (!XrFn("xrGetVulkanGraphicsDevice2KHR", pfn)) return 1;
        XrVulkanGraphicsDeviceGetInfoKHR gi{ XR_TYPE_VULKAN_GRAPHICS_DEVICE_GET_INFO_KHR };
        gi.systemId = systemId;
        gi.vulkanInstance = vkInstance;
        XRC(pfn(g_instance, &gi, &physicalDevice), "xrGetVulkanGraphicsDevice2KHR");
    }
    if (!physicalDevice) { printf("  [FAIL] the runtime returned a null VkPhysicalDevice\n"); return 1; }
    {
        VkPhysicalDeviceIDProperties idProps{ VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES };
        VkPhysicalDeviceProperties2 props{ VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 };
        props.pNext = &idProps;
        auto pfnProps2 = (PFN_vkGetPhysicalDeviceProperties2)vkGetInstanceProcAddr(vkInstance, "vkGetPhysicalDeviceProperties2");
        if (!pfnProps2) pfnProps2 = (PFN_vkGetPhysicalDeviceProperties2)vkGetInstanceProcAddr(vkInstance, "vkGetPhysicalDeviceProperties2KHR");
        if (pfnProps2) {
            pfnProps2(physicalDevice, &props);
            printf("       %s, LUID valid=%d\n", props.properties.deviceName, (int)idProps.deviceLUIDValid);
            if (!idProps.deviceLUIDValid) warn("VkPhysicalDeviceIDProperties has no LUID - the runtime cannot have matched an adapter");
            else ok("physical device exposes a LUID the runtime could match");
        }
    }

    // ---- VkDevice ----------------------------------------------------------------------
    step("VkDevice (graphics queue + timeline semaphores)");
    uint32_t famCount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(physicalDevice, &famCount, nullptr);
    std::vector<VkQueueFamilyProperties> fams(famCount);
    vkGetPhysicalDeviceQueueFamilyProperties(physicalDevice, &famCount, fams.data());
    uint32_t queueFamily = UINT32_MAX;
    for (uint32_t i = 0; i < famCount; ++i) if (fams[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { queueFamily = i; break; }
    if (queueFamily == UINT32_MAX) { printf("  [FAIL] no graphics queue family\n"); return 1; }
    printf("       graphics queue family %u\n", queueFamily);

    const float priority = 1.0f;
    VkDeviceQueueCreateInfo qci{ VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO };
    qci.queueFamilyIndex = queueFamily;
    qci.queueCount = 1;
    qci.pQueuePriorities = &priority;

    VkPhysicalDeviceTimelineSemaphoreFeatures timeline{ VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TIMELINE_SEMAPHORE_FEATURES };
    timeline.timelineSemaphore = VK_TRUE;

    std::vector<const char*> devExts;
    std::vector<std::string> v1DevExtStorage;
    if (enable1) {
        PFN_xrGetVulkanDeviceExtensionsKHR pfn = nullptr;
        if (!XrFn("xrGetVulkanDeviceExtensionsKHR", pfn)) return 1;
        uint32_t n = 0;
        XRC(pfn(g_instance, systemId, 0, &n, nullptr), "xrGetVulkanDeviceExtensionsKHR (size)");
        std::string buf(n, '\0');
        XRC(pfn(g_instance, systemId, n, &n, buf.data()), "xrGetVulkanDeviceExtensionsKHR");
        v1DevExtStorage = SplitSpaces(buf.c_str());
        printf("       runtime device extensions: %s\n", buf.c_str());
        for (auto& e : v1DevExtStorage) devExts.push_back(e.c_str());
    }

    VkDeviceCreateInfo dci{ VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO };
    dci.pNext = &timeline;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = (uint32_t)devExts.size();
    dci.ppEnabledExtensionNames = devExts.empty() ? nullptr : devExts.data();

    VkDevice device = VK_NULL_HANDLE;
    if (enable1) {
        VKC(vkCreateDevice(physicalDevice, &dci, nullptr, &device), "vkCreateDevice");
        ok("vkCreateDevice (app-owned, v1 path)");
    } else {
        PFN_xrCreateVulkanDeviceKHR pfn = nullptr;
        if (!XrFn("xrCreateVulkanDeviceKHR", pfn)) return 1;
        XrVulkanDeviceCreateInfoKHR ci{ XR_TYPE_VULKAN_DEVICE_CREATE_INFO_KHR };
        ci.systemId = systemId;
        ci.pfnGetInstanceProcAddr = &vkGetInstanceProcAddr;
        ci.vulkanPhysicalDevice = physicalDevice;
        ci.vulkanCreateInfo = &dci;
        VkResult vr = VK_SUCCESS;
        XRC(pfn(g_instance, &ci, &device, &vr), "xrCreateVulkanDeviceKHR");
        if (vr != VK_SUCCESS) { printf("  [FAIL] the runtime's vkCreateDevice returned %d\n", (int)vr); return 1; }
        ok("xrCreateVulkanDeviceKHR created the VkDevice");
    }

    VkQueue queue = VK_NULL_HANDLE;
    vkGetDeviceQueue(device, queueFamily, 0, &queue);
    VkCommandPool cmdPool = VK_NULL_HANDLE;
    VkCommandPoolCreateInfo pci{ VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO };
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = queueFamily;
    VKC(vkCreateCommandPool(device, &pci, nullptr, &cmdPool), "vkCreateCommandPool");
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo cbai{ VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO };
    cbai.commandPool = cmdPool;
    cbai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbai.commandBufferCount = 1;
    VKC(vkAllocateCommandBuffers(device, &cbai, &cmd), "vkAllocateCommandBuffers");
    ok("queue, command pool and command buffer");

    // Prove the detector works before trusting it. A probe whose validation callback is
    // silent passes every runtime, which is the failure mode this whole file exists to
    // avoid. maxLod < minLod is a purely numeric VUID: the layer reports it and the call
    // still succeeds, so nothing is left in a bad state.
    if (haveValidation) {
        g_selfTest = true;
        const int before = g_vkErrors;
        VkSamplerCreateInfo bad{ VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO };
        bad.minLod = 1.0f;
        bad.maxLod = 0.0f;
        VkSampler badSampler = VK_NULL_HANDLE;
        if (vkCreateSampler(device, &bad, nullptr, &badSampler) == VK_SUCCESS && badSampler)
            vkDestroySampler(device, badSampler, nullptr);
        g_selfTest = false;
        if (g_vkErrors == before) { printf("  [FAIL] the validation layer reported nothing for a known-bad call - it is not watching\n"); g_fail++; }
        else ok("validation layer verified live (deliberate error caught)");
        g_vkErrors = before;
    }

    // ---- session -----------------------------------------------------------------------
    step("xrCreateSession (Vulkan binding)");
    XrGraphicsBindingVulkanKHR binding = { XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR };
    binding.instance = vkInstance;
    binding.physicalDevice = physicalDevice;
    binding.device = device;
    binding.queueFamilyIndex = queueFamily;
    binding.queueIndex = 0;
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

    step("actions (BetterVR's shape)");
    std::array<XrPath, 2> handPaths = { P("/user/hand/left"), P("/user/hand/right") };
    XrActionSet gameplaySet = XR_NULL_HANDLE;
    XrActionSetCreateInfo asci = { XR_TYPE_ACTION_SET_CREATE_INFO };
    strcpy_s(asci.actionSetName, "gameplay_fps"); strcpy_s(asci.localizedActionSetName, "Gameplay");
    XRC(xrCreateActionSet(g_instance, &asci, &gameplaySet), "xrCreateActionSet(gameplay_fps)");
    XrAction gripPose{};
    {
        XrActionCreateInfo aci = { XR_TYPE_ACTION_CREATE_INFO };
        aci.actionType = XR_ACTION_TYPE_POSE_INPUT;
        strcpy_s(aci.actionName, "pose"); strcpy_s(aci.localizedActionName, "Grip Pose");
        aci.countSubactionPaths = (uint32_t)handPaths.size();
        aci.subactionPaths = handPaths.data();
        XRC(xrCreateAction(gameplaySet, &aci, &gripPose), "xrCreateAction(pose)");
    }
    {
        std::vector<XrActionSuggestedBinding> b = {
            { gripPose, P("/user/hand/left/input/grip/pose") },
            { gripPose, P("/user/hand/right/input/grip/pose") },
        };
        XrInteractionProfileSuggestedBinding sb = { XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING };
        sb.interactionProfile = P("/interaction_profiles/oculus/touch_controller");
        sb.countSuggestedBindings = (uint32_t)b.size();
        sb.suggestedBindings = b.data();
        XRC_SOFT(xrSuggestInteractionProfileBindings(g_instance, &sb), "suggest bindings");
        XrSessionActionSetsAttachInfo ai = { XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO };
        ai.countActionSets = 1; ai.actionSets = &gameplaySet;
        XRC(xrAttachSessionActionSets(session, &ai), "xrAttachSessionActionSets");
    }

    // ---- swapchains --------------------------------------------------------------------
    step("swapchain formats (must be VkFormat under a Vulkan session)");
    uint32_t fmtCount = 0;
    XRC(xrEnumerateSwapchainFormats(session, 0, &fmtCount, nullptr), "swapchain format count");
    std::vector<int64_t> formats(fmtCount);
    XRC(xrEnumerateSwapchainFormats(session, fmtCount, &fmtCount, formats.data()), "swapchain formats");
    bool haveColorSrgb = false, haveColorBgraSrgb = false, haveColorUnorm = false, haveD32 = false, haveD24 = false;
    for (int64_t f : formats) {
        printf("       - VkFormat %lld\n", (long long)f);
        if (f == VK_FORMAT_R8G8B8A8_SRGB) haveColorSrgb = true;
        if (f == VK_FORMAT_B8G8R8A8_SRGB) haveColorBgraSrgb = true;
        if (f == VK_FORMAT_R8G8B8A8_UNORM) haveColorUnorm = true;
        if (f == VK_FORMAT_D32_SFLOAT) haveD32 = true;
        if (f == VK_FORMAT_D24_UNORM_S8_UINT) haveD24 = true;
    }
    if (!haveColorSrgb && !haveColorBgraSrgb && !haveColorUnorm) { printf("  [FAIL] none of BetterVR's preferred colour formats offered\n"); g_fail++; }
    else ok("a colour format from BetterVR's preference list is offered");
    if (!haveD32 && !haveD24) { printf("  [FAIL] neither D32_SFLOAT nor D24_UNORM_S8_UINT offered\n"); g_fail++; }
    else ok("a depth format from BetterVR's preference list is offered");
    const VkFormat colorFormat = haveColorSrgb ? VK_FORMAT_R8G8B8A8_SRGB
                               : haveColorBgraSrgb ? VK_FORMAT_B8G8R8A8_SRGB : VK_FORMAT_R8G8B8A8_UNORM;
    const VkFormat depthFormat = haveD32 ? VK_FORMAT_D32_SFLOAT : VK_FORMAT_D24_UNORM_S8_UINT;

    const bool subRect = EnvOn("XR_PROBE_SUBRECT");
    const uint32_t W = subRect ? 2048u : 1440u;
    const uint32_t H = subRect ? 2048u : 1584u;
    const XrRect2Di viewRect = subRect ? XrRect2Di{ {64, 128}, {1920, 1080} }
                                       : XrRect2Di{ {0, 0}, {(int32_t)W, (int32_t)H} };
    if (subRect) printf("       sub-rect mode: %ux%u swapchain, imageRect (%d,%d) %dx%d\n",
                        W, H, viewRect.offset.x, viewRect.offset.y, viewRect.extent.width, viewRect.extent.height);

    step("swapchains (VkImages from the runtime, with views and framebuffers over them)");
    auto makeChain = [&](VkFormat fmt, bool depth, Chain& c, const char* label) -> bool {
        c.format = fmt;
        c.depth = depth;
        XrSwapchainCreateInfo ci = { XR_TYPE_SWAPCHAIN_CREATE_INFO };
        ci.width = W; ci.height = H; ci.arraySize = 1; ci.sampleCount = 1; ci.format = (int64_t)fmt;
        ci.mipCount = 1; ci.faceCount = 1;
        ci.usageFlags = (depth ? XR_SWAPCHAIN_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT : XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT) | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        XrResult r = xrCreateSwapchain(session, &ci, &c.handle);
        if (XR_FAILED(r)) { fail(label, r); return false; }

        uint32_t n = 0;
        if (XR_FAILED(xrEnumerateSwapchainImages(c.handle, 0, &n, nullptr)) || n == 0) { printf("  [FAIL] %s: no swapchain images\n", label); g_fail++; return false; }
        std::vector<XrSwapchainImageVulkanKHR> imgs(n, { XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR });
        r = xrEnumerateSwapchainImages(c.handle, n, &n, reinterpret_cast<XrSwapchainImageBaseHeader*>(imgs.data()));
        if (XR_FAILED(r)) { fail(label, r); return false; }
        for (auto& im : imgs) {
            if (im.image == VK_NULL_HANDLE) { printf("  [FAIL] %s: null VkImage in swapchain image\n", label); g_fail++; return false; }
            c.images.push_back(im.image);
        }

        // The render pass is the whole test: initialLayout says "the runtime already put
        // this image in the attachment-optimal layout", and validation checks that claim
        // against what it has tracked for the image.
        const VkImageLayout layout = depth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL
                                           : VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        VkAttachmentDescription att{};
        att.format = fmt;
        att.samples = VK_SAMPLE_COUNT_1_BIT;
        att.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        att.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        att.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        att.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        att.initialLayout = layout;
        att.finalLayout = layout;
        VkAttachmentReference ref{ 0, layout };
        VkSubpassDescription sub{};
        sub.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        if (depth) sub.pDepthStencilAttachment = &ref;
        else { sub.colorAttachmentCount = 1; sub.pColorAttachments = &ref; }
        VkSubpassDependency deps[2]{};
        deps[0].srcSubpass = VK_SUBPASS_EXTERNAL;
        deps[0].dstSubpass = 0;
        deps[0].srcStageMask = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
        deps[0].dstStageMask = depth ? VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT : VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        deps[0].srcAccessMask = 0;
        deps[0].dstAccessMask = depth ? VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT : VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
        deps[1] = deps[0];
        deps[1].srcSubpass = 0;
        deps[1].dstSubpass = VK_SUBPASS_EXTERNAL;
        deps[1].srcStageMask = deps[0].dstStageMask;
        deps[1].dstStageMask = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
        deps[1].srcAccessMask = deps[0].dstAccessMask;
        deps[1].dstAccessMask = 0;
        VkRenderPassCreateInfo rpci{ VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO };
        rpci.attachmentCount = 1; rpci.pAttachments = &att;
        rpci.subpassCount = 1; rpci.pSubpasses = &sub;
        rpci.dependencyCount = 2; rpci.pDependencies = deps;
        if (vkCreateRenderPass(device, &rpci, nullptr, &c.renderPass) != VK_SUCCESS) { printf("  [FAIL] %s: vkCreateRenderPass\n", label); g_fail++; return false; }

        for (VkImage img : c.images) {
            VkImageViewCreateInfo vci{ VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO };
            vci.image = img;
            vci.viewType = VK_IMAGE_VIEW_TYPE_2D;
            vci.format = fmt;
            vci.subresourceRange.aspectMask = depth ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT;
            vci.subresourceRange.levelCount = 1;
            vci.subresourceRange.layerCount = 1;
            VkImageView view = VK_NULL_HANDLE;
            if (vkCreateImageView(device, &vci, nullptr, &view) != VK_SUCCESS) { printf("  [FAIL] %s: vkCreateImageView over the runtime's VkImage\n", label); g_fail++; return false; }
            c.views.push_back(view);

            VkFramebufferCreateInfo fci{ VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO };
            fci.renderPass = c.renderPass;
            fci.attachmentCount = 1; fci.pAttachments = &view;
            fci.width = W; fci.height = H; fci.layers = 1;
            VkFramebuffer fb = VK_NULL_HANDLE;
            if (vkCreateFramebuffer(device, &fci, nullptr, &fb) != VK_SUCCESS) { printf("  [FAIL] %s: vkCreateFramebuffer\n", label); g_fail++; return false; }
            c.framebuffers.push_back(fb);
        }
        printf("       %s: %u VkImages at %ux%u, views and framebuffers created\n", label, n, W, H);
        return true;
    };

    Chain colorL, colorR, depthL, depthR, quadChain;
    bool chainsOk = true;
    chainsOk &= makeChain(colorFormat, false, colorL, "color swapchain (left)");
    chainsOk &= makeChain(colorFormat, false, colorR, "color swapchain (right)");
    chainsOk &= makeChain(depthFormat, true, depthL, "depth swapchain (left)");
    chainsOk &= makeChain(depthFormat, true, depthR, "depth swapchain (right)");
    chainsOk &= makeChain(colorFormat, false, quadChain, "quad-layer swapchain (2D HUD)");
    if (!chainsOk) return 1;
    ok("all five swapchains created and made renderable");

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
    if (!began) { printf("  [FAIL] runtime never delivered SESSION_STATE_READY\n"); return 1; }

    int totalFrames = 30;
    if (const char* env = getenv("XR_PROBE_FRAMES")) {
        int parsed = atoi(env);
        if (parsed > 0) totalFrames = parsed;
    }
    // For an observer watching a long run from outside: XR_PROBE_SLEEP_MS paces the loop to
    // something a screenshot can keep up with, and XR_PROBE_PROJECTION_ONLY drops the
    // quad-only phase so every frame carries stereo content for validate_stereo to look at.
    int sleepMs = 0;
    if (const char* env = getenv("XR_PROBE_SLEEP_MS")) sleepMs = atoi(env);
    const bool projectionOnly = EnvOn("XR_PROBE_PROJECTION_ONLY");
    char stepLabel[160];
    snprintf(stepLabel, sizeof(stepLabel), "frame loop x %d (render into every acquired image, no barriers of our own)", totalFrames);
    step(stepLabel);

    int framesRendered = 0, focusedFrames = 0, quadOnlyFrames = 0;
    bool sawSaneFov = true;
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
        XrFrameBeginInfo fbi = { XR_TYPE_FRAME_BEGIN_INFO };
        r = xrBeginFrame(session, &fbi);
        if (XR_FAILED(r)) { fail("xrBeginFrame", r); break; }

        XrActiveActionSet aas = { gameplaySet, XR_NULL_PATH };
        XrActionsSyncInfo asi = { XR_TYPE_ACTIONS_SYNC_INFO };
        asi.countActiveActionSets = 1; asi.activeActionSets = &aas;
        r = xrSyncActions(session, &asi);
        if (XR_FAILED(r) && r != XR_SESSION_NOT_FOCUSED) { fail("xrSyncActions", r); break; }

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
            printf("       L pos=(%.3f,%.3f,%.3f) fov=(%.3f,%.3f,%.3f,%.3f)\n", views[0].pose.position.x, views[0].pose.position.y, views[0].pose.position.z,
                   views[0].fov.angleLeft, views[0].fov.angleRight, views[0].fov.angleUp, views[0].fov.angleDown);
            printf("       R pos=(%.3f,%.3f,%.3f)\n", views[1].pose.position.x, views[1].pose.position.y, views[1].pose.position.z);
            if (views[0].pose.position.x == views[1].pose.position.x) warn("both eyes share an X position - no stereo IPD separation");
        }
        if (views[0].fov.angleLeft >= views[0].fov.angleRight || views[0].fov.angleDown >= views[0].fov.angleUp) sawSaneFov = false;

        // Acquire, render, release. Exactly the shape BetterVR's Layer3D::RecordRender will
        // have on Vulkan: a render pass straight onto the acquired image and no transition
        // in either direction, because the runtime owes the app the attachment layout.
        auto cycle = [&](Chain& c, int eye = -1) -> bool {
            uint32_t idx = 0;
            if (XR_FAILED(xrAcquireSwapchainImage(c.handle, nullptr, &idx))) return false;
            XrSwapchainImageWaitInfo wi = { XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO };
            wi.timeout = XR_INFINITE_DURATION;
            if (XR_FAILED(xrWaitSwapchainImage(c.handle, &wi))) return false;
            c.idx = idx;

            if (vkResetCommandBuffer(cmd, 0) != VK_SUCCESS) return false;
            VkCommandBufferBeginInfo bi{ VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO };
            bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
            if (vkBeginCommandBuffer(cmd, &bi) != VK_SUCCESS) return false;

            VkClearValue clear{};
            if (c.depth) clear.depthStencil = { 1.0f, 0 };
            else if (subRect) clear.color = { { 0.0f, 0.9f, 0.1f, 1.0f } };   // must never reach the preview
            else if (eye >= 0) clear.color = { { 0.1f, 0.2f, 0.4f, 1.0f } };
            else clear.color = { { 0.8f, 0.1f, 0.5f, 1.0f } };

            VkRenderPassBeginInfo rp{ VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO };
            rp.renderPass = c.renderPass;
            rp.framebuffer = c.framebuffers[idx];
            rp.renderArea = { {0, 0}, {W, H} };
            rp.clearValueCount = 1;
            rp.pClearValues = &clear;
            vkCmdBeginRenderPass(cmd, &rp, VK_SUBPASS_CONTENTS_INLINE);

            if (!c.depth) {
                const VkRect2D declared = { { viewRect.offset.x, viewRect.offset.y },
                                            { (uint32_t)viewRect.extent.width, (uint32_t)viewRect.extent.height } };
                auto clearRect = [&](const float rgba[4], VkRect2D rect) {
                    VkClearAttachment ca{};
                    ca.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                    ca.colorAttachment = 0;
                    memcpy(ca.clearValue.color.float32, rgba, sizeof(float) * 4);
                    VkClearRect cr{};
                    cr.rect = rect;
                    cr.baseArrayLayer = 0;
                    cr.layerCount = 1;
                    vkCmdClearAttachments(cmd, 1, &ca, 1, &cr);
                };
                if (eye >= 0) {
                    const float base[4] = { 0.1f, 0.2f, 0.4f, 1.0f };
                    if (subRect) clearRect(base, declared);
                    // Same square in both eyes, offset 20px horizontally: real, known
                    // disparity for a stereo checker to find.
                    const float mark[4] = { 0.95f, 0.9f, 0.25f, 1.0f };
                    const int32_t cx = declared.offset.x + viewRect.extent.width / 2 + (eye == 0 ? 10 : -10);
                    const int32_t cy = declared.offset.y + viewRect.extent.height / 2;
                    clearRect(mark, VkRect2D{ { cx - 100, cy - 100 }, { 200, 200 } });
                } else {
                    const float hud[4] = { 0.8f, 0.1f, 0.5f, 1.0f };
                    if (subRect) clearRect(hud, declared);
                    // Four bands that look different under each composition-layer blend
                    // mode, so a runtime that ignores layerFlags is visible in the mirror.
                    const int32_t bandW = viewRect.extent.width / 5;
                    const int32_t bandTop = declared.offset.y + viewRect.extent.height / 4;
                    const int32_t bandH = viewRect.extent.height / 4;
                    const float bands[4][4] = {
                        { 0.0f, 0.0f, 0.0f, 0.0f },
                        { 1.0f, 1.0f, 1.0f, 0.0f },
                        { 1.0f, 1.0f, 1.0f, 0.5f },
                        { 1.0f, 1.0f, 1.0f, 1.0f },
                    };
                    for (int b = 0; b < 4; ++b) {
                        clearRect(bands[b], VkRect2D{ { declared.offset.x + bandW * b, bandTop },
                                                      { (uint32_t)bandW, (uint32_t)bandH } });
                    }
                }
            }
            vkCmdEndRenderPass(cmd);
            if (vkEndCommandBuffer(cmd) != VK_SUCCESS) return false;

            VkSubmitInfo si{ VK_STRUCTURE_TYPE_SUBMIT_INFO };
            si.commandBufferCount = 1;
            si.pCommandBuffers = &cmd;
            if (vkQueueSubmit(queue, 1, &si, VK_NULL_HANDLE) != VK_SUCCESS) return false;
            if (vkQueueWaitIdle(queue) != VK_SUCCESS) return false;

            XrSwapchainImageReleaseInfo ri = { XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO };
            return XR_SUCCEEDED(xrReleaseSwapchainImage(c.handle, &ri));
        };

        const int phaseLen = (totalFrames > 200) ? 100 : (totalFrames / 2);
        const bool quadOnly = !projectionOnly && ((phaseLen <= 0) || ((f / phaseLen) % 2) == 0);
        if (quadOnly) quadOnlyFrames++;

        bool cycled = cycle(quadChain);
        if (!quadOnly) {
            cycled = cycled && cycle(colorL, 0) && cycle(colorR, 1) && cycle(depthL) && cycle(depthR);
        }
        if (!cycled) { printf("  [FAIL] swapchain acquire/render/release cycle failed on frame %d\n", f); g_fail++; break; }

        XrCompositionLayerDepthInfoKHR depthInfo[2] = {};
        XrCompositionLayerProjectionView projViews[2] = {};
        Chain* colorChains[2] = { &colorL, &colorR };
        Chain* depthChains[2] = { &depthL, &depthR };
        for (int e = 0; e < 2; ++e) {
            depthInfo[e] = { XR_TYPE_COMPOSITION_LAYER_DEPTH_INFO_KHR };
            depthInfo[e].subImage.swapchain = depthChains[e]->handle;
            depthInfo[e].subImage.imageRect = viewRect;
            depthInfo[e].minDepth = 0.0f; depthInfo[e].maxDepth = 1.0f;
            depthInfo[e].nearZ = 0.05f; depthInfo[e].farZ = INFINITY;

            projViews[e] = { XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW };
            projViews[e].next = &depthInfo[e];
            projViews[e].pose = views[e].pose;
            projViews[e].fov = views[e].fov;
            projViews[e].subImage.swapchain = colorChains[e]->handle;
            projViews[e].subImage.imageRect = viewRect;
        }
        XrCompositionLayerProjection proj = { XR_TYPE_COMPOSITION_LAYER_PROJECTION };
        proj.space = stageSpace; proj.viewCount = 2; proj.views = projViews;

        const float eyeY = (views[0].pose.position.y + views[1].pose.position.y) * 0.5f;
        XrCompositionLayerQuad quad = { XR_TYPE_COMPOSITION_LAYER_QUAD };
        quad.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
        quad.space = stageSpace;
        quad.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
        quad.subImage.swapchain = quadChain.handle;
        quad.subImage.imageRect = viewRect;
        quad.pose = identity;
        quad.pose.position.y = eyeY;
        quad.pose.position.z = -2.0f;
        quad.size = { 1.6f, 0.9f };

        // The same three extra quads xr_probe.cpp submits, so the two probes composite the
        // same scene and their preview output can be diffed pixel for pixel.
        XrCompositionLayerQuad angled = quad;
        angled.pose.position = { -1.1f, eyeY, -1.6f };
        angled.pose.orientation = { 0.0f, sinf(0.5f * 40.0f * 3.14159265f / 180.0f),
                                    0.0f, cosf(0.5f * 40.0f * 3.14159265f / 180.0f) };
        angled.size = { 0.7f, 0.7f };
        XrCompositionLayerQuad rightOnly = quad;
        rightOnly.eyeVisibility = XR_EYE_VISIBILITY_RIGHT;
        rightOnly.pose.position = { 1.1f, eyeY, -1.6f };
        rightOnly.size = { 0.5f, 0.5f };
        XrCompositionLayerQuad overlap = quad;
        overlap.pose.position = { 0.35f, eyeY - 0.25f, -1.9f };
        overlap.size = { 0.5f, 0.5f };

        const XrCompositionLayerBaseHeader* layers[5];
        uint32_t layerCount = 0;
        if (!quadOnly) layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&proj);
        layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&quad);
        layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&angled);
        layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&rightOnly);
        layers[layerCount++] = reinterpret_cast<XrCompositionLayerBaseHeader*>(&overlap);

        XrFrameEndInfo fei = { XR_TYPE_FRAME_END_INFO };
        fei.displayTime = fs.predictedDisplayTime;
        fei.environmentBlendMode = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
        fei.layerCount = layerCount;
        fei.layers = const_cast<const XrCompositionLayerBaseHeader**>(layers);
        r = xrEndFrame(session, &fei);
        if (XR_FAILED(r)) { fail("xrEndFrame", r); break; }
        framesRendered++;
        if (sleepMs > 0) Sleep((DWORD)sleepMs);
    }
    printf("       rendered %d/%d frames (%d quad-only), %d of them with state==FOCUSED\n",
           framesRendered, totalFrames, quadOnlyFrames, focusedFrames);
    if (framesRendered == totalFrames) {
        char label[192];
        snprintf(label, sizeof(label), "%d clean frames: %d quad-only, then %d with a projection(+depth) layer and a quad layer",
                 totalFrames, quadOnlyFrames, totalFrames - quadOnlyFrames);
        ok(label);
    }
    if (focusedFrames == 0) warn("session never reached FOCUSED");
    if (!sawSaneFov) { printf("  [FAIL] nonsensical FOV angles\n"); g_fail++; }

    step("Vulkan validation messages");
    if (!haveValidation) warn("validation layer was not loaded - layout bugs cannot be detected in this run");
    else if (g_vkErrors) { printf("  [FAIL] %d Vulkan validation error(s) during the run\n", g_vkErrors); g_fail++; }
    else ok("no Vulkan validation errors across the frame loop");

    step("time conversion");
    LARGE_INTEGER pc; QueryPerformanceCounter(&pc);
    XrTime t = 0;
    XRC_SOFT(pfnPcToTime(g_instance, &pc, &t), "xrConvertWin32PerformanceCounterToTimeKHR");
    LARGE_INTEGER back = {};
    XRC_SOFT(pfnTimeToPc(g_instance, t, &back), "xrConvertTimeToWin32PerformanceCounterKHR");

    step("teardown (BetterVR's exact shutdown order)");
    XRC_SOFT(xrRequestExitSession(session), "xrRequestExitSession");
    XRC_SOFT(xrEndSession(session), "xrEndSession right after xrRequestExitSession");
    vkDeviceWaitIdle(device);
    for (Chain* c : { &colorL, &colorR, &depthL, &depthR, &quadChain }) {
        for (VkFramebuffer fb : c->framebuffers) vkDestroyFramebuffer(device, fb, nullptr);
        for (VkImageView v : c->views) vkDestroyImageView(device, v, nullptr);
        if (c->renderPass) vkDestroyRenderPass(device, c->renderPass, nullptr);
        if (c->handle) xrDestroySwapchain(c->handle);
    }
    for (XrSpace s : { headSpace, stageSpace }) if (s) xrDestroySpace(s);
    XRC_SOFT(xrDestroySession(session), "xrDestroySession");
    XRC_SOFT(xrDestroyInstance(g_instance), "xrDestroyInstance");

    printf("       destroying Vulkan objects\n");
    vkDestroyCommandPool(device, cmdPool, nullptr);
    vkDestroyDevice(device, nullptr);
    if (messenger) {
        if (auto pfnDestroyDbg = (PFN_vkDestroyDebugUtilsMessengerEXT)vkGetInstanceProcAddr(vkInstance, "vkDestroyDebugUtilsMessengerEXT"))
            pfnDestroyDbg(vkInstance, messenger, nullptr);
    }
    vkDestroyInstance(vkInstance, nullptr);
    ok("Vulkan teardown");

    printf("\n================ %d failure(s), %d warning(s) ================\n", g_fail, g_warn);
    return g_fail ? 1 : 0;
}
