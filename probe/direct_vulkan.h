#pragma once
// Core loader exports; avoids a Vulkan SDK import-library dependency in the probe.
#define UI_VK_FUNCTIONS(X) \
 X(vkAllocateCommandBuffers) X(vkBeginCommandBuffer) X(vkCmdBeginRenderPass) X(vkCmdClearAttachments) \
 X(vkCmdEndRenderPass) X(vkCreateCommandPool) X(vkCreateDevice) X(vkCreateFramebuffer) X(vkCreateImageView) \
 X(vkCreateInstance) X(vkCreateRenderPass) X(vkCreateSampler) X(vkDestroyCommandPool) X(vkDestroyDevice) \
 X(vkDestroyFramebuffer) X(vkDestroyImageView) X(vkDestroyInstance) X(vkDestroyRenderPass) X(vkDestroySampler) \
 X(vkDeviceWaitIdle) X(vkEndCommandBuffer) X(vkEnumerateInstanceLayerProperties) X(vkGetDeviceQueue) \
 X(vkGetInstanceProcAddr) X(vkGetPhysicalDeviceQueueFamilyProperties) X(vkQueueSubmit) X(vkQueueWaitIdle) X(vkResetCommandBuffer)
#define DECLARE_VK(name) static PFN_##name name=nullptr;
UI_VK_FUNCTIONS(DECLARE_VK)
#undef DECLARE_VK
static bool InitDirectVulkan() {
    const auto module=LoadLibraryA("vulkan-1.dll");
    if (!module) { printf("Vulkan loader unavailable\n"); return false; }
#define LOAD_VK(name) name=(PFN_##name)GetProcAddress(module,#name); if (!name) return false;
    UI_VK_FUNCTIONS(LOAD_VK)
#undef LOAD_VK
    return true;
}
