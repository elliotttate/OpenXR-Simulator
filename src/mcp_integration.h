// MCP Integration - Screenshot capture and status reporting for OpenXR Simulator
#pragma once

#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <string>
#include <vector>
#include <cstdio>
#include <cstring>
#include <cstdarg>

namespace mcp {

using Microsoft::WRL::ComPtr;

// MCP-specific logging
inline void McpLog(const char* msg) {
    OutputDebugStringA("[SimXR-MCP] ");
    OutputDebugStringA(msg);
    OutputDebugStringA("\n");
}
inline void McpLogf(const char* fmt, ...) {
    char buf[2048];
    va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    McpLog(buf);
}

inline std::string GetSimulatorDataPath() {
    char base[MAX_PATH]{};
    DWORD len = GetEnvironmentVariableA("LOCALAPPDATA", base, (DWORD)sizeof(base));
    if (len > 0 && len < sizeof(base)) {
        return std::string(base) + "\\OpenXR-Simulator";
    }
    return ".";
}

inline bool g_screenshotRequested = false;
inline std::string g_screenshotEye = "both";
inline std::string g_screenshotLayer = "projection";  // "projection", "quad", or "all"

// Storage for quad layer pixels (set by renderQuadLayer)
inline std::vector<uint8_t> g_quadLayerPixels;
inline uint32_t g_quadLayerWidth = 0;
inline uint32_t g_quadLayerHeight = 0;
inline bool g_quadLayerCaptured = false;

// Check if MCP has requested a screenshot
inline void CheckScreenshotRequest() {
    std::string reqPath = GetSimulatorDataPath() + "\\screenshot_request.json";
    FILE* f = nullptr;
    if (fopen_s(&f, reqPath.c_str(), "r") == 0 && f) {
        char buf[512];
        size_t n = fread(buf, 1, sizeof(buf)-1, f);
        buf[n] = 0;
        fclose(f);

        g_screenshotRequested = true;
        g_screenshotEye = "both";
        g_screenshotLayer = "projection";  // default

        const char* eyePos = strstr(buf, "\"eye\"");
        if (eyePos) {
            if (strstr(eyePos, "\"left\"")) g_screenshotEye = "left";
            else if (strstr(eyePos, "\"right\"")) g_screenshotEye = "right";
        }

        // Check for layer type specification
        const char* layerPos = strstr(buf, "\"layer\"");
        if (layerPos) {
            if (strstr(layerPos, "\"quad\"")) g_screenshotLayer = "quad";
            else if (strstr(layerPos, "\"all\"")) g_screenshotLayer = "all";
        }

        DeleteFileA(reqPath.c_str());
        McpLogf("Screenshot request detected: layer=%s, eye=%s", g_screenshotLayer.c_str(), g_screenshotEye.c_str());
    }
}

// Store quad layer pixels for screenshot capture
inline void StoreQuadLayerPixels(const uint8_t* pixels, uint32_t width, uint32_t height) {
    if (!pixels || width == 0 || height == 0) return;
    g_quadLayerWidth = width;
    g_quadLayerHeight = height;
    g_quadLayerPixels.resize(width * height * 4);
    memcpy(g_quadLayerPixels.data(), pixels, width * height * 4);
    g_quadLayerCaptured = true;
}

// Forward declaration - CaptureQuadScreenshot is defined after SavePixelsToBMP
inline void CaptureQuadScreenshot();

// Save a D3D11 texture to BMP file
inline bool SaveTextureToBMP(ID3D11Device* device, ID3D11DeviceContext* ctx,
                              ID3D11Texture2D* texture, const char* filename) {
    if (!device || !ctx || !texture) return false;

    D3D11_TEXTURE2D_DESC desc;
    texture->GetDesc(&desc);

    // Create staging texture for CPU read
    D3D11_TEXTURE2D_DESC stagingDesc = desc;
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.BindFlags = 0;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    stagingDesc.MiscFlags = 0;

    ComPtr<ID3D11Texture2D> staging;
    HRESULT hr = device->CreateTexture2D(&stagingDesc, nullptr, staging.GetAddressOf());
    if (FAILED(hr)) {
        McpLogf("Failed to create staging texture: 0x%08X", hr);
        return false;
    }

    // Copy to staging
    ctx->CopyResource(staging.Get(), texture);

    // Map and read pixels
    D3D11_MAPPED_SUBRESOURCE mapped;
    hr = ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) {
        McpLogf("Failed to map staging texture: 0x%08X", hr);
        return false;
    }

    // Write BMP file
    FILE* file = nullptr;
    if (fopen_s(&file, filename, "wb") != 0 || !file) {
        ctx->Unmap(staging.Get(), 0);
        return false;
    }

    uint32_t w = desc.Width;
    uint32_t h = desc.Height;
    uint32_t rowSize = w * 3;
    uint32_t rowPadding = (4 - (rowSize % 4)) % 4;
    uint32_t rowStride = rowSize + rowPadding;
    uint32_t imageSize = rowStride * h;

    // BMP Header (54 bytes)
    uint8_t bmpHeader[54] = {
        'B', 'M',           // Signature
        0, 0, 0, 0,         // File size
        0, 0, 0, 0,         // Reserved
        54, 0, 0, 0,        // Data offset
        40, 0, 0, 0,        // DIB header size
        0, 0, 0, 0,         // Width
        0, 0, 0, 0,         // Height
        1, 0,               // Planes
        24, 0,              // Bits per pixel
        0, 0, 0, 0,         // Compression
        0, 0, 0, 0,         // Image size
        0, 0, 0, 0,         // X pixels/meter
        0, 0, 0, 0,         // Y pixels/meter
        0, 0, 0, 0,         // Colors in table
        0, 0, 0, 0          // Important colors
    };

    uint32_t fileSize = 54 + imageSize;
    memcpy(bmpHeader + 2, &fileSize, 4);
    memcpy(bmpHeader + 18, &w, 4);
    memcpy(bmpHeader + 22, &h, 4);
    memcpy(bmpHeader + 34, &imageSize, 4);

    fwrite(bmpHeader, 1, 54, file);

    // Write pixel data (BMP is bottom-up, BGR)
    std::vector<uint8_t> row(rowStride, 0);
    uint8_t* src = (uint8_t*)mapped.pData;

    for (int y = h - 1; y >= 0; y--) {
        uint8_t* srcRow = src + y * mapped.RowPitch;
        for (uint32_t x = 0; x < w; x++) {
            // Handle different DXGI formats
            if (desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
                desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB) {
                row[x*3 + 0] = srcRow[x*4 + 2]; // B
                row[x*3 + 1] = srcRow[x*4 + 1]; // G
                row[x*3 + 2] = srcRow[x*4 + 0]; // R
            } else if (desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
                       desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) {
                row[x*3 + 0] = srcRow[x*4 + 0]; // B
                row[x*3 + 1] = srcRow[x*4 + 1]; // G
                row[x*3 + 2] = srcRow[x*4 + 2]; // R
            } else {
                // Fallback - assume RGBA
                row[x*3 + 0] = srcRow[x*4 + 2];
                row[x*3 + 1] = srcRow[x*4 + 1];
                row[x*3 + 2] = srcRow[x*4 + 0];
            }
        }
        fwrite(row.data(), 1, rowStride, file);
    }

    fclose(file);
    ctx->Unmap(staging.Get(), 0);

    McpLogf("Screenshot saved: %s (%ux%u)", filename, w, h);
    return true;
}

// Capture screenshot from preview swapchain (D3D11 path)
inline void CaptureScreenshot(ID3D11Device* device, ID3D11DeviceContext* ctx,
                               IDXGISwapChain1* swapchain) {
    if (!swapchain) return;

    ComPtr<ID3D11Texture2D> backbuffer;
    if (FAILED(swapchain->GetBuffer(0, IID_PPV_ARGS(backbuffer.GetAddressOf())))) {
        McpLog("Failed to get backbuffer for screenshot");
        return;
    }

    std::string outPath = GetSimulatorDataPath() + "\\screenshot.bmp";
    SaveTextureToBMP(device, ctx, backbuffer.Get(), outPath.c_str());

    g_screenshotRequested = false;
}

// Forward declaration - CaptureScreenshotD3D12 is defined after SavePixelsToBMP
inline void CaptureScreenshotD3D12(ID3D12Device* device, ID3D12CommandQueue* queue,
                                     ID3D12Resource* renderTarget,
                                     ID3D12CommandAllocator* cmdAlloc,
                                     ID3D12GraphicsCommandList* cmdList,
                                     ID3D12Fence* fence, HANDLE fenceEvent,
                                     UINT64& fenceValue);

// Save raw RGBA pixel data to BMP (for OpenGL path)
inline bool SavePixelsToBMP(const uint8_t* pixels, uint32_t width, uint32_t height,
                             const char* filename) {
    if (!pixels || width == 0 || height == 0) return false;

    FILE* file = nullptr;
    if (fopen_s(&file, filename, "wb") != 0 || !file) {
        McpLogf("Failed to open file for writing: %s", filename);
        return false;
    }

    uint32_t rowSize = width * 3;
    uint32_t rowPadding = (4 - (rowSize % 4)) % 4;
    uint32_t rowStride = rowSize + rowPadding;
    uint32_t imageSize = rowStride * height;

    // BMP Header (54 bytes)
    uint8_t bmpHeader[54] = {
        'B', 'M',           // Signature
        0, 0, 0, 0,         // File size
        0, 0, 0, 0,         // Reserved
        54, 0, 0, 0,        // Data offset
        40, 0, 0, 0,        // DIB header size
        0, 0, 0, 0,         // Width
        0, 0, 0, 0,         // Height
        1, 0,               // Planes
        24, 0,              // Bits per pixel
        0, 0, 0, 0,         // Compression
        0, 0, 0, 0,         // Image size
        0, 0, 0, 0,         // X pixels/meter
        0, 0, 0, 0,         // Y pixels/meter
        0, 0, 0, 0,         // Colors in table
        0, 0, 0, 0          // Important colors
    };

    uint32_t fileSize = 54 + imageSize;
    memcpy(bmpHeader + 2, &fileSize, 4);
    memcpy(bmpHeader + 18, &width, 4);
    memcpy(bmpHeader + 22, &height, 4);
    memcpy(bmpHeader + 34, &imageSize, 4);

    fwrite(bmpHeader, 1, 54, file);

    // Write pixel data (BMP is bottom-up, BGR)
    std::vector<uint8_t> row(rowStride, 0);

    for (int y = height - 1; y >= 0; y--) {
        const uint8_t* srcRow = pixels + y * width * 4;
        for (uint32_t x = 0; x < width; x++) {
            // RGBA to BGR
            row[x*3 + 0] = srcRow[x*4 + 2]; // B
            row[x*3 + 1] = srcRow[x*4 + 1]; // G
            row[x*3 + 2] = srcRow[x*4 + 0]; // R
        }
        fwrite(row.data(), 1, rowStride, file);
    }

    fclose(file);
    McpLogf("Screenshot saved (pixels): %s (%ux%u)", filename, width, height);
    return true;
}

// Capture quad layer screenshot (implementation - forward declared above)
inline void CaptureQuadScreenshot() {
    if (!g_quadLayerCaptured || g_quadLayerPixels.empty()) {
        McpLog("No quad layer pixels available for screenshot");
        g_screenshotRequested = false;
        return;
    }

    std::string outPath = GetSimulatorDataPath() + "\\screenshot_quad.bmp";
    SavePixelsToBMP(g_quadLayerPixels.data(), g_quadLayerWidth, g_quadLayerHeight, outPath.c_str());
    McpLogf("Quad layer screenshot saved: %s (%ux%u)", outPath.c_str(), g_quadLayerWidth, g_quadLayerHeight);
    g_screenshotRequested = false;
}

// Capture screenshot from D3D12 offscreen render target (implementation)
inline void CaptureScreenshotD3D12(ID3D12Device* device, ID3D12CommandQueue* queue,
                                     ID3D12Resource* renderTarget,
                                     ID3D12CommandAllocator* cmdAlloc,
                                     ID3D12GraphicsCommandList* cmdList,
                                     ID3D12Fence* fence, HANDLE fenceEvent,
                                     UINT64& fenceValue) {
    if (!device || !queue || !renderTarget || !cmdAlloc || !cmdList) {
        McpLog("D3D12 screenshot: missing resources");
        g_screenshotRequested = false;
        return;
    }

    D3D12_RESOURCE_DESC desc = renderTarget->GetDesc();
    uint32_t w = (uint32_t)desc.Width;
    uint32_t h = (uint32_t)desc.Height;

    // Calculate row pitch aligned to D3D12_TEXTURE_DATA_PITCH_ALIGNMENT (256 bytes)
    UINT64 rowPitch = (w * 4 + D3D12_TEXTURE_DATA_PITCH_ALIGNMENT - 1) & ~(D3D12_TEXTURE_DATA_PITCH_ALIGNMENT - 1);
    UINT64 totalSize = rowPitch * h;

    // Create readback buffer
    D3D12_HEAP_PROPERTIES heapProps = {};
    heapProps.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bufDesc = {};
    bufDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bufDesc.Width = totalSize;
    bufDesc.Height = 1;
    bufDesc.DepthOrArraySize = 1;
    bufDesc.MipLevels = 1;
    bufDesc.SampleDesc.Count = 1;
    bufDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;

    ComPtr<ID3D12Resource> readback;
    if (FAILED(device->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &bufDesc,
                                                D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                                IID_PPV_ARGS(readback.GetAddressOf())))) {
        McpLog("D3D12 screenshot: CreateCommittedResource (readback) failed");
        g_screenshotRequested = false;
        return;
    }

    // Wait for previous commands to finish before reusing the command allocator
    if (fence && fenceValue > 1) {
        if (fence->GetCompletedValue() < fenceValue - 1) {
            fence->SetEventOnCompletion(fenceValue - 1, fenceEvent);
            WaitForSingleObject(fenceEvent, 1000);
        }
    }

    // Record copy commands
    cmdAlloc->Reset();
    cmdList->Reset(cmdAlloc, nullptr);

    // Transition render target from COMMON to COPY_SOURCE
    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = renderTarget;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cmdList->ResourceBarrier(1, &barrier);

    D3D12_TEXTURE_COPY_LOCATION srcLoc = {};
    srcLoc.pResource = renderTarget;
    srcLoc.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    srcLoc.SubresourceIndex = 0;

    D3D12_TEXTURE_COPY_LOCATION dstLoc = {};
    dstLoc.pResource = readback.Get();
    dstLoc.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dstLoc.PlacedFootprint.Offset = 0;
    dstLoc.PlacedFootprint.Footprint.Format = desc.Format;
    dstLoc.PlacedFootprint.Footprint.Width = w;
    dstLoc.PlacedFootprint.Footprint.Height = h;
    dstLoc.PlacedFootprint.Footprint.Depth = 1;
    dstLoc.PlacedFootprint.Footprint.RowPitch = (UINT)rowPitch;

    cmdList->CopyTextureRegion(&dstLoc, 0, 0, 0, &srcLoc, nullptr);

    // Transition render target back to COMMON
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    cmdList->ResourceBarrier(1, &barrier);

    cmdList->Close();
    ID3D12CommandList* cmdLists[] = { cmdList };
    queue->ExecuteCommandLists(1, cmdLists);

    // Wait for copy to complete
    queue->Signal(fence, fenceValue);
    if (fence->GetCompletedValue() < fenceValue) {
        fence->SetEventOnCompletion(fenceValue, fenceEvent);
        WaitForSingleObject(fenceEvent, 2000);
    }
    fenceValue++;

    // Map and read pixels
    void* mappedData = nullptr;
    D3D12_RANGE readRange = { 0, (SIZE_T)totalSize };
    if (FAILED(readback->Map(0, &readRange, &mappedData))) {
        McpLog("D3D12 screenshot: Map failed");
        g_screenshotRequested = false;
        return;
    }

    // Convert to tightly-packed RGBA for SavePixelsToBMP
    std::vector<uint8_t> pixels(w * h * 4);
    bool isBGRA = (desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
                   desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB);

    for (uint32_t y = 0; y < h; y++) {
        const uint8_t* srcRow = (const uint8_t*)mappedData + y * rowPitch;
        uint8_t* dstRow = pixels.data() + y * w * 4;
        if (isBGRA) {
            for (uint32_t x = 0; x < w; x++) {
                dstRow[x * 4 + 0] = srcRow[x * 4 + 2]; // R
                dstRow[x * 4 + 1] = srcRow[x * 4 + 1]; // G
                dstRow[x * 4 + 2] = srcRow[x * 4 + 0]; // B
                dstRow[x * 4 + 3] = srcRow[x * 4 + 3]; // A
            }
        } else {
            memcpy(dstRow, srcRow, w * 4);
        }
    }

    D3D12_RANGE writeRange = { 0, 0 };
    readback->Unmap(0, &writeRange);

    std::string outPath = GetSimulatorDataPath() + "\\screenshot.bmp";
    SavePixelsToBMP(pixels.data(), w, h, outPath.c_str());
    McpLogf("D3D12 screenshot captured: %ux%u format=%d", w, h, (int)desc.Format);

    g_screenshotRequested = false;
}

// Capture screenshot from OpenGL pixel data (side-by-side left+right eyes)
inline void CaptureScreenshotGL(const uint8_t* leftPixels, const uint8_t* rightPixels,
                                 uint32_t width, uint32_t height) {
    if (!leftPixels && !rightPixels) {
        McpLog("No pixel data for GL screenshot");
        g_screenshotRequested = false;
        return;
    }

    // Create side-by-side image
    uint32_t totalWidth = (leftPixels && rightPixels) ? width * 2 : width;
    std::vector<uint8_t> combined(totalWidth * height * 4, 0);

    if (leftPixels && rightPixels) {
        // Side by side: left eye on left, right eye on right
        for (uint32_t y = 0; y < height; y++) {
            // Copy left eye
            memcpy(combined.data() + y * totalWidth * 4,
                   leftPixels + y * width * 4,
                   width * 4);
            // Copy right eye
            memcpy(combined.data() + y * totalWidth * 4 + width * 4,
                   rightPixels + y * width * 4,
                   width * 4);
        }
    } else if (leftPixels) {
        memcpy(combined.data(), leftPixels, width * height * 4);
    } else {
        memcpy(combined.data(), rightPixels, width * height * 4);
    }

    std::string outPath = GetSimulatorDataPath() + "\\screenshot.bmp";
    SavePixelsToBMP(combined.data(), totalWidth, height, outPath.c_str());

    g_screenshotRequested = false;
}

// Write frame status JSON for MCP
inline void WriteFrameStatus(uint32_t frameCount, uint32_t width, uint32_t height,
                              const char* format, const char* sessionState,
                              float headYaw = 0, float headPitch = 0,
                              float headX = 0, float headY = 1.7f, float headZ = 0) {
    static uint32_t lastWrite = UINT32_MAX;
    // Only write every 30 frames to reduce I/O (but always write the first frame)
    if (lastWrite != UINT32_MAX && frameCount - lastWrite < 30) return;
    lastWrite = frameCount;

    std::string path = GetSimulatorDataPath() + "\\runtime_status.json";
    FILE* file = nullptr;
    if (fopen_s(&file, path.c_str(), "w") != 0 || !file) return;

    // Get current timestamp
    SYSTEMTIME st;
    GetLocalTime(&st);

    fprintf(file, "{\n");
    fprintf(file, "  \"timestamp\": \"%04d-%02d-%02dT%02d:%02d:%02d\",\n",
            st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    fprintf(file, "  \"frame_count\": %u,\n", frameCount);
    fprintf(file, "  \"preview_width\": %u,\n", width);
    fprintf(file, "  \"preview_height\": %u,\n", height);
    fprintf(file, "  \"format\": \"%s\",\n", format ? format : "unknown");
    fprintf(file, "  \"session_state\": \"%s\",\n", sessionState ? sessionState : "unknown");
    fprintf(file, "  \"target_fps\": 90,\n");
    fprintf(file, "  \"frame_time_ms\": 11.1,\n");
    fprintf(file, "  \"head_tracking\": {\n");
    fprintf(file, "    \"position\": {\"x\": %.3f, \"y\": %.3f, \"z\": %.3f},\n", headX, headY, headZ);
    fprintf(file, "    \"yaw\": %.3f,\n", headYaw);
    fprintf(file, "    \"pitch\": %.3f\n", headPitch);
    fprintf(file, "  }\n");
    fprintf(file, "}\n");
    fclose(file);
}

// Get session state name
inline const char* GetSessionStateName(int state) {
    switch (state) {
        case 0: return "UNKNOWN";
        case 1: return "IDLE";
        case 2: return "READY";
        case 3: return "SYNCHRONIZED";
        case 4: return "VISIBLE";
        case 5: return "FOCUSED";
        case 6: return "STOPPING";
        case 7: return "LOSS_PENDING";
        case 8: return "EXITING";
        default: return "UNKNOWN";
    }
}

// Head pose control structure for MCP
struct HeadPoseCommand {
    bool valid = false;
    float x = 0.0f;
    float y = 1.7f;
    float z = 0.0f;
    float yaw = 0.0f;
    float pitch = 0.0f;
    float roll = 0.0f;
    bool  hasRoll = false;     // false = leave g_headRoll alone (back-compat)
};

// Simple JSON float parser
inline float ParseJsonFloat(const char* json, const char* key, float defaultVal) {
    char searchKey[64];
    snprintf(searchKey, sizeof(searchKey), "\"%s\"", key);
    const char* pos = strstr(json, searchKey);
    if (!pos) return defaultVal;
    pos = strchr(pos, ':');
    if (!pos) return defaultVal;
    pos++;
    while (*pos == ' ' || *pos == '\t') pos++;
    return (float)atof(pos);
}

// Returns true if `key` appears in the JSON (regardless of value).
// Used to detect "field omitted" vs "field set to 0".
inline bool JsonHasKey(const char* json, const char* key) {
    char searchKey[64];
    snprintf(searchKey, sizeof(searchKey), "\"%s\"", key);
    return strstr(json, searchKey) != nullptr;
}

// Check for head pose command from MCP
// File format: {"x": 0, "y": 1.7, "z": 0, "yaw": 0, "pitch": 0, "roll": 0}
// "roll" is optional — omit to keep the simulator's current roll value.
inline HeadPoseCommand CheckHeadPoseCommand() {
    HeadPoseCommand cmd;
    std::string cmdPath = GetSimulatorDataPath() + "\\head_pose_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, cmdPath.c_str(), "r") == 0 && f) {
        char buf[512];
        size_t n = fread(buf, 1, sizeof(buf)-1, f);
        buf[n] = 0;
        fclose(f);

        cmd.valid = true;
        cmd.x = ParseJsonFloat(buf, "x", 0.0f);
        cmd.y = ParseJsonFloat(buf, "y", 1.7f);
        cmd.z = ParseJsonFloat(buf, "z", 0.0f);
        cmd.yaw = ParseJsonFloat(buf, "yaw", 0.0f);
        cmd.pitch = ParseJsonFloat(buf, "pitch", 0.0f);
        cmd.hasRoll = JsonHasKey(buf, "roll");
        if (cmd.hasRoll) cmd.roll = ParseJsonFloat(buf, "roll", 0.0f);

        // Delete the file after reading (one-shot command)
        DeleteFileA(cmdPath.c_str());
        McpLogf("Head pose command: pos(%.2f, %.2f, %.2f) yaw=%.2f pitch=%.2f roll=%.2f",
                cmd.x, cmd.y, cmd.z, cmd.yaw, cmd.pitch,
                cmd.hasRoll ? cmd.roll : NAN);
    }
    return cmd;
}

// ---------- FOV / IPD / Headset-profile commands ----------
//
// These let MCP override the simulator's symmetric-FOV / hardcoded-IPD
// defaults so projection-matrix bugs and per-eye-IPD bugs that only
// show up against a real headset's profile are reproducible in the
// simulator. Set values are sticky until cleared or a new profile is
// applied.

struct FovCommand {
    bool valid = false;
    bool clear = false;  // {"clear": true} reverts to the symmetric UI default
    float angleLeft[2]  = { 0, 0 };  // radians, < 0
    float angleRight[2] = { 0, 0 };  // radians, > 0
    float angleUp[2]    = { 0, 0 };  // radians, > 0
    float angleDown[2]  = { 0, 0 };  // radians, < 0
};

// File format (radians):
//   {"left":  {"aL": -0.95, "aR": 0.78, "aU": 0.85, "aD": -0.95},
//    "right": {"aL": -0.78, "aR": 0.95, "aU": 0.85, "aD": -0.95}}
// Or for clear: {"clear": true}
inline FovCommand CheckFovCommand() {
    FovCommand cmd;
    std::string p = GetSimulatorDataPath() + "\\fov_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, p.c_str(), "r") != 0 || !f) return cmd;
    char buf[1024];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    DeleteFileA(p.c_str());
    cmd.valid = true;
    if (JsonHasKey(buf, "clear")) {
        cmd.clear = true;
        McpLog("FOV command: clear (revert to symmetric default)");
        return cmd;
    }
    // Find "left": { ... } and "right": { ... } sub-objects and parse from there.
    auto parseEye = [&](const char* eyeKey, int idx) {
        char k[16];
        snprintf(k, sizeof(k), "\"%s\"", eyeKey);
        const char* eye = strstr(buf, k);
        if (!eye) return;
        const char* brace = strchr(eye, '{');
        if (!brace) return;
        cmd.angleLeft[idx]  = ParseJsonFloat(brace, "aL", -1.0f);
        cmd.angleRight[idx] = ParseJsonFloat(brace, "aR",  1.0f);
        cmd.angleUp[idx]    = ParseJsonFloat(brace, "aU",  1.0f);
        cmd.angleDown[idx]  = ParseJsonFloat(brace, "aD", -1.0f);
    };
    parseEye("left",  0);
    parseEye("right", 1);
    McpLogf("FOV command: L=[%.2f,%.2f,%.2f,%.2f]rad  R=[%.2f,%.2f,%.2f,%.2f]rad",
            cmd.angleLeft[0], cmd.angleRight[0], cmd.angleUp[0], cmd.angleDown[0],
            cmd.angleLeft[1], cmd.angleRight[1], cmd.angleUp[1], cmd.angleDown[1]);
    return cmd;
}

struct IpdCommand {
    bool valid = false;
    bool clear = false;
    float ipdMeters = 0.064f;
};

// File format: {"ipd_mm": 64} or {"clear": true}
inline IpdCommand CheckIpdCommand() {
    IpdCommand cmd;
    std::string p = GetSimulatorDataPath() + "\\ipd_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, p.c_str(), "r") != 0 || !f) return cmd;
    char buf[256];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    DeleteFileA(p.c_str());
    cmd.valid = true;
    if (JsonHasKey(buf, "clear")) {
        cmd.clear = true;
        McpLog("IPD command: clear (revert to 64mm default)");
        return cmd;
    }
    float mm = ParseJsonFloat(buf, "ipd_mm", 64.0f);
    cmd.ipdMeters = mm * 0.001f;
    McpLogf("IPD command: %.1f mm", mm);
    return cmd;
}

// Headset profile: a named preset that applies both FOV and IPD at once.
// Profiles are decoded inside the runtime — this struct just carries the name.
struct HeadsetProfileCommand {
    bool valid = false;
    char name[32] = {};
};

inline HeadsetProfileCommand CheckHeadsetProfileCommand() {
    HeadsetProfileCommand cmd;
    std::string p = GetSimulatorDataPath() + "\\headset_profile_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, p.c_str(), "r") != 0 || !f) return cmd;
    char buf[256];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    DeleteFileA(p.c_str());
    // Find "name":"value"
    const char* k = strstr(buf, "\"name\"");
    if (!k) return cmd;
    const char* col = strchr(k, ':');
    if (!col) return cmd;
    const char* q = strchr(col, '"');
    if (!q) return cmd;
    ++q;
    const char* eq = strchr(q, '"');
    if (!eq) return cmd;
    size_t L = (size_t)(eq - q);
    if (L >= sizeof(cmd.name)) L = sizeof(cmd.name) - 1;
    memcpy(cmd.name, q, L);
    cmd.name[L] = 0;
    cmd.valid = true;
    McpLogf("Headset profile command: %s", cmd.name);
    return cmd;
}

struct AnaglyphCommand {
    bool valid = false;
    bool enabled = false;
};

// File format: {"enabled": true} or {"enabled": false}
inline AnaglyphCommand CheckAnaglyphCommand() {
    AnaglyphCommand cmd;
    std::string p = GetSimulatorDataPath() + "\\anaglyph_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, p.c_str(), "r") != 0 || !f) return cmd;
    char buf[256];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    buf[n] = 0;
    fclose(f);
    DeleteFileA(p.c_str());
    cmd.valid = true;
    // ParseJsonFloat returns 0.0f for false-ish, !=0 for true.
    cmd.enabled = strstr(buf, "\"enabled\"") && strstr(buf, "true");
    McpLogf("Anaglyph command: enabled=%d", cmd.enabled ? 1 : 0);
    return cmd;
}

// ---------- Projection log ----------
//
// Captures the FOV and pose the app embedded in each xrEndFrame projection
// layer. The MCP can fetch the recent N frames via get_projection_log to
// diagnose: was the app's projection symmetric while the simulator was
// configured asymmetric? Did the rendered pose drift relative to the
// located pose?
struct ProjLogEntry {
    uint32_t frame = 0;
    // Pose embedded in projection-layer view 0 (left eye).
    float poseQx = 0, poseQy = 0, poseQz = 0, poseQw = 1;
    float posX = 0, posY = 0, posZ = 0;
    // FOV per eye (radians, OpenXR convention)
    float aL[2] = {0,0}, aR[2] = {0,0}, aU[2] = {0,0}, aD[2] = {0,0};
    // Image sub-rect per eye (left/right): offset_x, offset_y, extent_w, extent_h
    int32_t rectX[2] = {0,0}, rectY[2] = {0,0}, rectW[2] = {0,0}, rectH[2] = {0,0};
};
constexpr size_t PROJ_LOG_CAPACITY = 64;
inline ProjLogEntry  g_projLog[PROJ_LOG_CAPACITY];
inline size_t        g_projLogHead = 0;     // index of next slot to write
inline size_t        g_projLogCount = 0;    // number of valid entries (capped at capacity)

// Writes the current ring buffer to a JSON file the MCP server reads.
inline void DumpProjectionLog() {
    std::string p = GetSimulatorDataPath() + "\\projection_log.json";
    FILE* f = nullptr;
    if (fopen_s(&f, p.c_str(), "w") != 0 || !f) return;
    fprintf(f, "{\n  \"entries\": [\n");
    size_t n = g_projLogCount;
    // Walk oldest -> newest.
    size_t start = (g_projLogHead + PROJ_LOG_CAPACITY - n) % PROJ_LOG_CAPACITY;
    for (size_t i = 0; i < n; ++i) {
        const ProjLogEntry& e = g_projLog[(start + i) % PROJ_LOG_CAPACITY];
        fprintf(f, "    {\"frame\": %u, "
                "\"pose\": {\"x\": %.6f, \"y\": %.6f, \"z\": %.6f, "
                "\"qx\": %.6f, \"qy\": %.6f, \"qz\": %.6f, \"qw\": %.6f}, "
                "\"left_fov\":  {\"aL\": %.6f, \"aR\": %.6f, \"aU\": %.6f, \"aD\": %.6f}, "
                "\"right_fov\": {\"aL\": %.6f, \"aR\": %.6f, \"aU\": %.6f, \"aD\": %.6f}, "
                "\"left_rect\":  [%d, %d, %d, %d], "
                "\"right_rect\": [%d, %d, %d, %d]}%s\n",
                e.frame, e.posX, e.posY, e.posZ,
                e.poseQx, e.poseQy, e.poseQz, e.poseQw,
                e.aL[0], e.aR[0], e.aU[0], e.aD[0],
                e.aL[1], e.aR[1], e.aU[1], e.aD[1],
                e.rectX[0], e.rectY[0], e.rectW[0], e.rectH[0],
                e.rectX[1], e.rectY[1], e.rectW[1], e.rectH[1],
                (i + 1 < n) ? "," : "");
    }
    fprintf(f, "  ]\n}\n");
    fclose(f);
}

// MCP polls a ".dump_request" file to ask us to write the log.
inline bool CheckProjLogDumpRequest() {
    std::string p = GetSimulatorDataPath() + "\\projection_log_dump_request";
    if (GetFileAttributesA(p.c_str()) == INVALID_FILE_ATTRIBUTES) return false;
    DeleteFileA(p.c_str());
    return true;
}

// Controller pose control structure for MCP
// Allows setting right or left controller position/orientation and trigger
struct ControllerPoseCommand {
    bool valid = false;
    int hand = 1;           // 0=left, 1=right
    float posX = 0.2f;     // Position offset from head (head-local space)
    float posY = -0.3f;
    float posZ = -0.4f;
    float yaw = 0.0f;      // Yaw offset relative to head
    float pitch = -0.3f;   // Pitch offset relative to head
    float trigger = 0.0f;  // 0.0-1.0 trigger value
    bool triggerSet = false;
    int  buttonA = -1;     // -1=unchanged, 0=released, 1=pressed
};

// Check for controller pose command from MCP
// File format: {"hand": 1, "posX": 0.2, "posY": -0.3, "posZ": -0.4, "yaw": 0, "pitch": -0.3, "trigger": 0.0}
inline ControllerPoseCommand CheckControllerPoseCommand() {
    ControllerPoseCommand cmd;
    std::string cmdPath = GetSimulatorDataPath() + "\\controller_pose_command.json";
    FILE* f = nullptr;
    if (fopen_s(&f, cmdPath.c_str(), "r") == 0 && f) {
        char buf[512];
        size_t n = fread(buf, 1, sizeof(buf)-1, f);
        buf[n] = 0;
        fclose(f);

        cmd.valid = true;
        cmd.hand = (int)ParseJsonFloat(buf, "hand", 1.0f);
        cmd.posX = ParseJsonFloat(buf, "posX", 0.2f);
        cmd.posY = ParseJsonFloat(buf, "posY", -0.3f);
        cmd.posZ = ParseJsonFloat(buf, "posZ", -0.4f);
        cmd.yaw = ParseJsonFloat(buf, "yaw", 0.0f);
        cmd.pitch = ParseJsonFloat(buf, "pitch", -0.3f);
        cmd.trigger = ParseJsonFloat(buf, "trigger", -1.0f);
        cmd.triggerSet = (cmd.trigger >= 0.0f);
        if (!cmd.triggerSet) cmd.trigger = 0.0f;
        cmd.buttonA = (int)ParseJsonFloat(buf, "buttonA", -1.0f);

        // Delete the file after reading (one-shot command)
        DeleteFileA(cmdPath.c_str());
        McpLogf("Controller pose command: hand=%d pos(%.2f, %.2f, %.2f) yaw=%.2f pitch=%.2f trigger=%.1f",
                cmd.hand, cmd.posX, cmd.posY, cmd.posZ, cmd.yaw, cmd.pitch, cmd.trigger);
    }
    return cmd;
}

// Write acknowledgment that command was processed
inline void WriteCommandAck(const char* cmdType, bool success) {
    std::string ackPath = GetSimulatorDataPath() + "\\command_ack.json";
    FILE* f = nullptr;
    if (fopen_s(&f, ackPath.c_str(), "w") == 0 && f) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "{\n");
        fprintf(f, "  \"timestamp\": \"%04d-%02d-%02dT%02d:%02d:%02d.%03d\",\n",
                st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
        fprintf(f, "  \"command\": \"%s\",\n", cmdType);
        fprintf(f, "  \"success\": %s\n", success ? "true" : "false");
        fprintf(f, "}\n");
        fclose(f);
    }
}

} // namespace mcp
