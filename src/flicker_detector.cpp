#include "flicker_detector.h"

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace flicker {
namespace {

constexpr uint32_t kHistoryFrames = 10;
constexpr uint32_t kPostIncidentFrames = 12;
constexpr uint64_t kIncidentCooldownFrames = 300;

// Disk-lifecycle guards. A chronically-firing heuristic must not be able to
// fill the drive: each session stops writing new incident packets after this
// many, and the worker prunes older sessions' packets at startup.
constexpr uint64_t kMaxIncidentsPerSession = 40;
constexpr size_t kKeepRecentSessions = 5;
constexpr size_t kKeepRecentLogs = 10;

struct Sample {
    uint64_t frame = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    std::vector<uint8_t> bgra;
    float mean = 0.0f;
    float meanLeft = 0.0f;
    float meanRight = 0.0f;
    float temporal = 0.0f;
    float temporalLeft = 0.0f;
    float temporalRight = 0.0f;
    float brightFractionLeft = 0.0f;
    float brightFractionRight = 0.0f;
};

// The classifier metrics without the pixels: what the status JSON needs, small
// enough to copy on every sample without touching the allocator.
Sample MetricsOnly(const Sample& sample) {
    Sample metrics;
    metrics.frame = sample.frame;
    metrics.width = sample.width;
    metrics.height = sample.height;
    metrics.mean = sample.mean;
    metrics.meanLeft = sample.meanLeft;
    metrics.meanRight = sample.meanRight;
    metrics.temporal = sample.temporal;
    metrics.temporalLeft = sample.temporalLeft;
    metrics.temporalRight = sample.temporalRight;
    metrics.brightFractionLeft = sample.brightFractionLeft;
    metrics.brightFractionRight = sample.brightFractionRight;
    return metrics;
}

// RenderDoc's application API is append-only. Requesting 1.1.0 and declaring
// the stable pointer layout through TriggerMultiFrameCapture avoids a build-time
// dependency while still using the official RENDERDOC_GetAPI entry point.
using RenderDocSetCapturePath = void(__cdecl*)(const char*);
using RenderDocTriggerMultiFrameCapture = void(__cdecl*)(uint32_t);
struct RenderDocApi110 {
    void* getApiVersion;
    void* setCaptureOptionU32;
    void* setCaptureOptionF32;
    void* getCaptureOptionU32;
    void* getCaptureOptionF32;
    void* setFocusToggleKeys;
    void* setCaptureKeys;
    void* getOverlayBits;
    void* maskOverlayBits;
    void* removeHooks;
    void* unloadCrashHandler;
    RenderDocSetCapturePath setCaptureFilePathTemplate;
    void* getCaptureFilePathTemplate;
    void* getNumCaptures;
    void* getCapture;
    void* triggerCapture;
    void* isTargetControlConnected;
    void* launchReplayUi;
    void* setActiveWindow;
    void* startFrameCapture;
    void* isFrameCapturing;
    void* endFrameCapture;
    RenderDocTriggerMultiFrameCapture triggerMultiFrameCapture;
};
using RenderDocGetApi = int(__cdecl*)(int, void**);

struct State {
    std::mutex mutex;
    uint64_t lastGeneration = 0;
    uint64_t lastFrame = 0;
    uint64_t totalSubmissions = 0;
    uint64_t projectionSubmissions = 0;
    uint64_t missingProjectionSubmissions = 0;
    uint64_t projectionPresenceTransitions = 0;
    uint64_t previewSamples = 0;
    uint64_t visiblePreviewSamples = 0;
    uint64_t paintAttempts = 0;
    uint64_t successfulPaints = 0;
    uint64_t failedPaints = 0;
    uint64_t duplicateGenerationPaints = 0;
    uint64_t asymmetricBrightSamples = 0;
    uint64_t renderDocTriggerAttempts = 0;
    uint64_t renderDocTriggerSuccesses = 0;
    uint64_t lastRenderDocTriggerFrame = 0;
    uint64_t lastPaintGeneration = 0;
    uint64_t anomalyCount = 0;
    uint64_t incidentCount = 0;
    uint64_t lastIncidentFrame = 0;
    uint32_t consecutiveMissingProjection = 0;
    uint32_t maxConsecutiveMissingProjection = 0;
    uint32_t consecutiveAsymmetricBright = 0;
    uint32_t maxConsecutiveAsymmetricBright = 0;
    uint32_t lastLayerCount = 0;
    bool projectionStateInitialized = false;
    bool lastHadProjection = false;
    bool statusDirty = false;
    bool hasMetrics = false;
    Sample lastMetrics;
    std::string lastReason = "NONE";
    std::filesystem::path lastIncidentDirectory;
    std::filesystem::path activeIncidentDirectory;
    uint32_t postFramesRemaining = 0;
    uint64_t activeLastSavedFrame = 0;
    std::deque<Sample> history;
};

struct UiState {
    std::mutex mutex;
    uint64_t lastGeneration = 0;
    uint64_t lastFrame = 0;
    uint64_t observedFrames = 0;
    uint64_t quadSubmittedFrames = 0;
    uint64_t projectionRefreshFrames = 0;
    uint64_t freshReadbacks = 0;
    uint64_t freshCompositions = 0;
    uint64_t cachedCompositions = 0;
    uint64_t missingAfterProjection = 0;
    uint64_t compositionFailures = 0;
    uint64_t uiSamples = 0;
    uint64_t anomalyCount = 0;
    uint64_t incidentCount = 0;
    uint64_t lastIncidentFrame = 0;
    uint32_t consecutiveMissingAfterProjection = 0;
    uint32_t maxConsecutiveMissingAfterProjection = 0;
    uint32_t lastQuadLayers = 0;
    bool lastCacheValid = false;
    bool lastComposed = false;
    bool statusDirty = false;
    bool hasMetrics = false;
    Sample lastMetrics;
    float lastSourceAlphaCoverage = 0.0f;
    std::string lastReason = "NONE";
    std::filesystem::path lastIncidentDirectory;
    std::filesystem::path activeIncidentDirectory;
    uint32_t postFramesRemaining = 0;
    uint64_t activeLastSavedFrame = 0;
    std::deque<Sample> history;
};

// Leaked on purpose: the I/O worker is a detached thread that can outlive
// static destruction, so nothing it touches may ever be destructed.
State& GetState() {
    static State* state = new State;
    return *state;
}

UiState& GetUiState() {
    static UiState* state = new UiState;
    return *state;
}

const std::filesystem::path& DataRoot() {
    static const std::filesystem::path* root = []() {
        wchar_t buffer[MAX_PATH] = {};
        const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, MAX_PATH);
        std::filesystem::path base = length > 0 && length < MAX_PATH ? buffer : L".";
        return new std::filesystem::path(base / L"OpenXR-Simulator");
    }();
    return *root;
}

bool TryTriggerRenderDocCapture(State& state, uint64_t frame) {
    char enabled[8] = {};
    if (GetEnvironmentVariableA("BVR_RENDERDOC_AUTO_CAPTURE", enabled, sizeof(enabled)) == 0 ||
        enabled[0] == '0') return false;
    if (state.lastRenderDocTriggerFrame != 0 && frame - state.lastRenderDocTriggerFrame < 60)
        return false;

    ++state.renderDocTriggerAttempts;
    HMODULE module = GetModuleHandleW(L"renderdoc.dll");
    if (!module) return false;
    auto getApi = reinterpret_cast<RenderDocGetApi>(GetProcAddress(module, "RENDERDOC_GetAPI"));
    if (!getApi) return false;
    RenderDocApi110* api = nullptr;
    if (getApi(10100, reinterpret_cast<void**>(&api)) != 1 || !api ||
        !api->triggerMultiFrameCapture) return false;

    char captureTemplate[32768] = {};
    if (GetEnvironmentVariableA("BVR_RENDERDOC_CAPTURE_TEMPLATE", captureTemplate,
            sizeof(captureTemplate)) > 0 && api->setCaptureFilePathTemplate) {
        api->setCaptureFilePathTemplate(captureTemplate);
    }
    char frameCountText[32] = {};
    uint32_t frameCount = 3;
    if (GetEnvironmentVariableA("BVR_RENDERDOC_CAPTURE_FRAMES", frameCountText,
            sizeof(frameCountText)) > 0) {
        frameCount = std::clamp<uint32_t>(
            static_cast<uint32_t>(std::strtoul(frameCountText, nullptr, 10)), 1, 10);
    }
    api->triggerMultiFrameCapture(frameCount);
    state.lastRenderDocTriggerFrame = frame;
    ++state.renderDocTriggerSuccesses;
    return true;
}

uint64_t UnixTimeMs() {
    return (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string JsonEscape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (const char c : value) {
        switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default: out += c; break;
        }
    }
    return out;
}

void AtomicWrite(const std::filesystem::path& path, const std::string& contents) {
    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);
    const std::filesystem::path temporary =
        path.wstring() + L".tmp." + std::to_wstring(GetCurrentProcessId());
    {
        std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
        if (!file) return;
        file.write(contents.data(), (std::streamsize)contents.size());
        file.flush();
    }
    // Rename atomically without forcing a physical disk flush. Readers never
    // observe a partial JSON file.
    MoveFileExW(temporary.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING);
}

uint8_t Luma(const uint8_t* bgra) {
    return (uint8_t)((19u * bgra[0] + 183u * bgra[1] + 54u * bgra[2]) >> 8);
}

// `pitchBytes` is the distance between source rows: the D3D12 readback the
// caller maps is 256-byte aligned, and sampling it in place is what removed the
// full-frame repack the detector used to force on the render thread.
Sample Downsample(const uint8_t* bgra, uint32_t width, uint32_t height,
                  uint32_t pitchBytes, uint64_t frame) {
    Sample sample;
    sample.frame = frame;
    // Detection runs on the application's xrEndFrame thread. A 320-wide signal
    // retains far more spatial detail than the temporal/luma classifier needs
    // while keeping sampling and history comparisons cheap.
    sample.width = std::min(width, 320u);
    sample.height = std::max(1u, (uint32_t)std::llround((double)height * sample.width / std::max(1u, width)));
    sample.height = std::min(sample.height, 180u);
    sample.bgra.resize((size_t)sample.width * sample.height * 4);

    // The horizontal sampling pattern is identical for every row.
    uint32_t sourceOffsets[320];
    for (uint32_t x = 0; x < sample.width; ++x) {
        sourceOffsets[x] =
            std::min(width - 1, (uint32_t)((uint64_t)x * width / sample.width)) * 4;
    }

    double sum = 0.0, sumLeft = 0.0, sumRight = 0.0;
    uint64_t countLeft = 0, countRight = 0, brightLeft = 0, brightRight = 0;
    const uint32_t halfWidth = sample.width / 2;
    for (uint32_t y = 0; y < sample.height; ++y) {
        const uint32_t sourceY = std::min(height - 1, (uint32_t)((uint64_t)y * height / sample.height));
        const uint8_t* sourceRow = bgra + (size_t)sourceY * pitchBytes;
        uint8_t* destinationRow = sample.bgra.data() + (size_t)y * sample.width * 4;
        for (uint32_t x = 0; x < sample.width; ++x) {
            const uint8_t* source = sourceRow + sourceOffsets[x];
            memcpy(destinationRow + (size_t)x * 4, source, 4);
            const uint8_t luma = Luma(source);
            const double value = luma / 255.0;
            sum += value;
            if (x < halfWidth) {
                sumLeft += value;
                ++countLeft;
                if (luma >= 245) ++brightLeft;
            } else {
                sumRight += value;
                ++countRight;
                if (luma >= 245) ++brightRight;
            }
        }
    }
    const uint64_t pixels = (uint64_t)sample.width * sample.height;
    sample.mean = pixels ? (float)(sum / pixels) : 0.0f;
    sample.meanLeft = countLeft ? (float)(sumLeft / countLeft) : sample.mean;
    sample.meanRight = countRight ? (float)(sumRight / countRight) : sample.mean;
    sample.brightFractionLeft = countLeft ? (float)brightLeft / countLeft : 0.0f;
    sample.brightFractionRight = countRight ? (float)brightRight / countRight : 0.0f;
    return sample;
}

Sample CropUi(const uint8_t* bgra, uint32_t width, uint32_t height, uint32_t pitchBytes,
              const int32_t rects[2][4], uint64_t frame) {
    Sample sample;
    sample.frame = frame;
    // The UI detector only needs presence/absence and coarse temporal shape.
    // Keep paired eye crops, but avoid scanning the full quad rectangle on
    // every composed diagnostic preview update.
    sample.width = 256;
    sample.height = 72;
    sample.bgra.assign((size_t)sample.width * sample.height * 4, 0);

    double sum[2] = {};
    uint64_t count[2] = {};
    for (int eye = 0; eye < 2; ++eye) {
        const int64_t rx0 = std::clamp<int64_t>(rects[eye][0], 0, width);
        const int64_t ry0 = std::clamp<int64_t>(rects[eye][1], 0, height);
        const int64_t rx1 = std::clamp<int64_t>((int64_t)rects[eye][0] + rects[eye][2], 0, width);
        const int64_t ry1 = std::clamp<int64_t>((int64_t)rects[eye][1] + rects[eye][3], 0, height);
        if (rx1 <= rx0 || ry1 <= ry0) continue;
        uint32_t sourceOffsets[128];
        for (uint32_t x = 0; x < 128; ++x) {
            sourceOffsets[x] = (uint32_t)std::min<int64_t>(
                rx1 - 1, rx0 + (int64_t)x * (rx1 - rx0) / 128) * 4;
        }
        for (uint32_t y = 0; y < sample.height; ++y) {
            const uint32_t sourceY = (uint32_t)std::min<int64_t>(
                ry1 - 1, ry0 + (int64_t)y * (ry1 - ry0) / sample.height);
            const uint8_t* sourceRow = bgra + (size_t)sourceY * pitchBytes;
            uint8_t* destinationRow = sample.bgra.data() +
                ((size_t)y * sample.width + (size_t)eye * 128) * 4;
            for (uint32_t x = 0; x < 128; ++x) {
                const uint8_t* source = sourceRow + sourceOffsets[x];
                memcpy(destinationRow + (size_t)x * 4, source, 4);
                sum[eye] += Luma(source) / 255.0;
                ++count[eye];
            }
        }
    }
    sample.meanLeft = count[0] ? (float)(sum[0] / count[0]) : 0.0f;
    sample.meanRight = count[1] ? (float)(sum[1] / count[1]) : 0.0f;
    sample.mean = (sample.meanLeft + sample.meanRight) * 0.5f;
    return sample;
}

void CalculateTemporal(Sample& current, const Sample& previous) {
    if (current.width != previous.width || current.height != previous.height ||
        current.bgra.size() != previous.bgra.size()) return;
    double total = 0.0, left = 0.0, right = 0.0;
    uint64_t countLeft = 0, countRight = 0;
    for (uint32_t y = 0; y < current.height; ++y) {
        for (uint32_t x = 0; x < current.width; ++x) {
            const size_t offset = ((size_t)y * current.width + x) * 4;
            const double delta = std::abs((int)Luma(current.bgra.data() + offset) -
                                          (int)Luma(previous.bgra.data() + offset)) / 255.0;
            total += delta;
            if (x < current.width / 2) {
                left += delta;
                ++countLeft;
            } else {
                right += delta;
                ++countRight;
            }
        }
    }
    const uint64_t pixels = (uint64_t)current.width * current.height;
    current.temporal = pixels ? (float)(total / pixels) : 0.0f;
    current.temporalLeft = countLeft ? (float)(left / countLeft) : current.temporal;
    current.temporalRight = countRight ? (float)(right / countRight) : current.temporal;
}

float TemporalBetween(const Sample& first, const Sample& second) {
    if (first.width != second.width || first.height != second.height ||
        first.bgra.size() != second.bgra.size() || first.bgra.empty()) return 1.0f;
    double total = 0.0;
    const size_t pixels = first.bgra.size() / 4;
    for (size_t i = 0; i < pixels; ++i) {
        total += std::abs((int)Luma(first.bgra.data() + i * 4) -
                          (int)Luma(second.bgra.data() + i * 4)) / 255.0;
    }
    return (float)(total / pixels);
}

#pragma pack(push, 1)
struct BmpHeader {
    uint16_t magic = 0x4D42;
    uint32_t fileSize = 0;
    uint16_t reserved1 = 0;
    uint16_t reserved2 = 0;
    uint32_t pixelOffset = 54;
    uint32_t infoSize = 40;
    int32_t width = 0;
    int32_t height = 0;
    uint16_t planes = 1;
    uint16_t bitsPerPixel = 24;
    uint32_t compression = 0;
    uint32_t imageSize = 0;
    int32_t ppmX = 2835;
    int32_t ppmY = 2835;
    uint32_t colors = 0;
    uint32_t importantColors = 0;
};
#pragma pack(pop)

void WriteBmpRegion(const std::filesystem::path& path, const Sample& sample,
                    uint32_t startX, uint32_t regionWidth) {
    if (sample.bgra.empty() || regionWidth == 0 || startX + regionWidth > sample.width) return;
    const uint32_t rowSize = ((regionWidth * 3 + 3) / 4) * 4;
    BmpHeader header;
    header.width = (int32_t)regionWidth;
    header.height = (int32_t)sample.height;
    header.imageSize = rowSize * sample.height;
    header.fileSize = header.pixelOffset + header.imageSize;
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    if (!file) return;
    file.write((const char*)&header, sizeof(header));
    std::vector<uint8_t> row(rowSize, 0);
    for (int32_t y = (int32_t)sample.height - 1; y >= 0; --y) {
        for (uint32_t x = 0; x < regionWidth; ++x) {
            const uint8_t* pixel = sample.bgra.data() + ((size_t)y * sample.width + startX + x) * 4;
            row[x * 3 + 0] = pixel[0];
            row[x * 3 + 1] = pixel[1];
            row[x * 3 + 2] = pixel[2];
        }
        file.write((const char*)row.data(), row.size());
    }
}

void SaveSample(const std::filesystem::path& directory, const Sample& sample) {
    std::error_code ec;
    std::filesystem::create_directories(directory, ec);
    const uint32_t leftWidth = sample.width / 2;
    const uint32_t rightWidth = sample.width - leftWidth;
    WriteBmpRegion(directory / ("frame" + std::to_string(sample.frame) + "_color_L.bmp"), sample, 0, leftWidth);
    WriteBmpRegion(directory / ("frame" + std::to_string(sample.frame) + "_color_R.bmp"), sample, leftWidth, rightWidth);
    WriteBmpRegion(directory / ("frame" + std::to_string(sample.frame) + "_preview.bmp"), sample, 0, sample.width);
}

// ---------------------------------------------------------------------------
// I/O worker
//
// The detector observes on the application's render thread, but every file it
// produces is written here: incident BMP packets, incident/status JSON, and
// the manual capture-request polls. Lock order is always a state mutex first,
// then the worker mutex — never the reverse.
// ---------------------------------------------------------------------------

struct PendingFile {
    std::filesystem::path path;
    std::string contents;
};

struct PendingSample {
    std::filesystem::path directory;
    Sample sample;
};

struct IoWorker {
    std::mutex mutex;
    std::condition_variable wake;
    std::vector<PendingFile> files;
    std::deque<PendingSample> samples;
};

IoWorker& GetWorker() {
    static IoWorker* worker = new IoWorker;
    return *worker;
}

void QueueFile(std::filesystem::path path, std::string contents) {
    IoWorker& worker = GetWorker();
    {
        std::lock_guard<std::mutex> guard(worker.mutex);
        worker.files.push_back(PendingFile{ std::move(path), std::move(contents) });
    }
    worker.wake.notify_one();
}

void QueueSample(const std::filesystem::path& directory, const Sample& sample) {
    IoWorker& worker = GetWorker();
    {
        std::lock_guard<std::mutex> guard(worker.mutex);
        // Bound the backlog if storage cannot keep up; dropping a post-incident
        // frame beats growing without limit on the app's memory.
        if (worker.samples.size() >= 64) return;
        worker.samples.push_back(PendingSample{ directory, sample });
    }
    worker.wake.notify_one();
}

std::string BuildStatusJson(const State& state, const Sample* sample, uint64_t frame) {
    std::ostringstream json;
    json << std::fixed << std::setprecision(6)
         << "{\n  \"schemaVersion\": 1,\n  \"timestampUnixMs\": " << UnixTimeMs()
         << ",\n  \"pid\": " << GetCurrentProcessId()
         << ",\n  \"frame\": " << frame
         << ",\n  \"totalSubmissions\": " << state.totalSubmissions
         << ",\n  \"projectionSubmissions\": " << state.projectionSubmissions
         << ",\n  \"missingProjectionSubmissions\": " << state.missingProjectionSubmissions
         << ",\n  \"projectionPresenceTransitions\": " << state.projectionPresenceTransitions
         << ",\n  \"consecutiveMissingProjection\": " << state.consecutiveMissingProjection
         << ",\n  \"maxConsecutiveMissingProjection\": " << state.maxConsecutiveMissingProjection
         << ",\n  \"lastLayerCount\": " << state.lastLayerCount
         << ",\n  \"previewSamples\": " << state.previewSamples
         << ",\n  \"visiblePreviewSamples\": " << state.visiblePreviewSamples
         << ",\n  \"paintAttempts\": " << state.paintAttempts
         << ",\n  \"successfulPaints\": " << state.successfulPaints
         << ",\n  \"failedPaints\": " << state.failedPaints
         << ",\n  \"duplicateGenerationPaints\": " << state.duplicateGenerationPaints
         << ",\n  \"asymmetricBrightSamples\": " << state.asymmetricBrightSamples
         << ",\n  \"consecutiveAsymmetricBright\": " << state.consecutiveAsymmetricBright
         << ",\n  \"maxConsecutiveAsymmetricBright\": " << state.maxConsecutiveAsymmetricBright
         << ",\n  \"renderDocTriggerAttempts\": " << state.renderDocTriggerAttempts
         << ",\n  \"renderDocTriggerSuccesses\": " << state.renderDocTriggerSuccesses
         << ",\n  \"lastRenderDocTriggerFrame\": " << state.lastRenderDocTriggerFrame
         << ",\n  \"anomalyCount\": " << state.anomalyCount
         << ",\n  \"incidentCount\": " << state.incidentCount
         << ",\n  \"lastReason\": \"" << JsonEscape(state.lastReason) << "\""
         << ",\n  \"lastIncidentDirectory\": \"" << JsonEscape(state.lastIncidentDirectory.string()) << "\"";
    if (sample) {
        json << ",\n  \"preview\": { \"mean\": " << sample->mean
             << ", \"meanLeft\": " << sample->meanLeft
             << ", \"meanRight\": " << sample->meanRight
             << ", \"temporal\": " << sample->temporal
             << ", \"temporalLeft\": " << sample->temporalLeft
             << ", \"temporalRight\": " << sample->temporalRight
             << ", \"brightFractionLeft\": " << sample->brightFractionLeft
             << ", \"brightFractionRight\": " << sample->brightFractionRight << " }";
    }
    json << "\n}\n";
    return json.str();
}

std::string BuildUiStatusJson(const UiState& state, const Sample* sample, uint64_t frame) {
    std::ostringstream json;
    json << std::fixed << std::setprecision(6)
         << "{\n  \"schemaVersion\": 1,\n  \"captureSource\": \"openxr-simulator-ui-quad\""
         << ",\n  \"timestampUnixMs\": " << UnixTimeMs()
         << ",\n  \"pid\": " << GetCurrentProcessId()
         << ",\n  \"frame\": " << frame
         << ",\n  \"observedFrames\": " << state.observedFrames
         << ",\n  \"quadSubmittedFrames\": " << state.quadSubmittedFrames
         << ",\n  \"projectionRefreshFrames\": " << state.projectionRefreshFrames
         << ",\n  \"freshReadbacks\": " << state.freshReadbacks
         << ",\n  \"freshCompositions\": " << state.freshCompositions
         << ",\n  \"cachedCompositions\": " << state.cachedCompositions
         << ",\n  \"missingAfterProjection\": " << state.missingAfterProjection
         << ",\n  \"compositionFailures\": " << state.compositionFailures
         << ",\n  \"consecutiveMissingAfterProjection\": " << state.consecutiveMissingAfterProjection
         << ",\n  \"maxConsecutiveMissingAfterProjection\": " << state.maxConsecutiveMissingAfterProjection
         << ",\n  \"uiSamples\": " << state.uiSamples
         << ",\n  \"lastQuadLayers\": " << state.lastQuadLayers
         << ",\n  \"cacheValid\": " << (state.lastCacheValid ? "true" : "false")
         << ",\n  \"composedLastFrame\": " << (state.lastComposed ? "true" : "false")
         << ",\n  \"sourceAlphaCoverage\": " << state.lastSourceAlphaCoverage
         << ",\n  \"anomalyCount\": " << state.anomalyCount
         << ",\n  \"incidentCount\": " << state.incidentCount
         << ",\n  \"lastReason\": \"" << JsonEscape(state.lastReason) << "\""
         << ",\n  \"lastIncidentDirectory\": \"" << JsonEscape(state.lastIncidentDirectory.string()) << "\"";
    if (sample) {
        json << ",\n  \"ui\": { \"mean\": " << sample->mean
             << ", \"meanLeft\": " << sample->meanLeft
             << ", \"meanRight\": " << sample->meanRight
             << ", \"temporal\": " << sample->temporal
             << ", \"temporalLeft\": " << sample->temporalLeft
             << ", \"temporalRight\": " << sample->temporalRight << " }";
    }
    json << "\n}\n";
    return json.str();
}

// Caller holds state.mutex. Everything this produces goes through the worker
// queue: an incident's history packet is ~30 small BMPs and the render thread
// must not pay for them.
void BeginIncident(State& state, const std::string& reason, uint64_t frame, bool force = false) {
    if (reason != "MANUAL_CAPTURE") ++state.anomalyCount;
    state.lastReason = reason;
    state.statusDirty = true;
    if (reason != "MANUAL_CAPTURE") TryTriggerRenderDocCapture(state, frame);
    if (!state.activeIncidentDirectory.empty() ||
        (!force && state.lastIncidentFrame != 0 && frame - state.lastIncidentFrame < kIncidentCooldownFrames)) return;
    if (state.incidentCount >= kMaxIncidentsPerSession) return;

    state.lastIncidentFrame = frame;
    ++state.incidentCount;
    state.activeIncidentDirectory = DataRoot() / L"flicker_incidents" /
        (L"session_" + std::to_wstring(GetCurrentProcessId())) /
        (L"incident_" + std::to_wstring(frame));
    state.lastIncidentDirectory = state.activeIncidentDirectory;
    state.postFramesRemaining = kPostIncidentFrames;
    state.activeLastSavedFrame = 0;
    for (const auto& historySample : state.history) {
        QueueSample(state.activeIncidentDirectory, historySample);
        state.activeLastSavedFrame = historySample.frame;
    }

    std::ostringstream incident;
    incident << "{\n  \"schemaVersion\": 1,\n  \"pid\": " << GetCurrentProcessId()
             << ",\n  \"triggerFrame\": " << frame
             << ",\n  \"timestampUnixMs\": " << UnixTimeMs()
             << ",\n  \"reason\": \"" << JsonEscape(reason) << "\""
             << ",\n  \"totalSubmissions\": " << state.totalSubmissions
             << ",\n  \"projectionSubmissions\": " << state.projectionSubmissions
             << ",\n  \"missingProjectionSubmissions\": " << state.missingProjectionSubmissions
             << ",\n  \"projectionPresenceTransitions\": " << state.projectionPresenceTransitions
             << ",\n  \"consecutiveMissingProjection\": " << state.consecutiveMissingProjection
             << ",\n  \"lastLayerCount\": " << state.lastLayerCount
             << ",\n  \"previewSamples\": " << state.previewSamples
             << ",\n  \"paintAttempts\": " << state.paintAttempts
             << ",\n  \"successfulPaints\": " << state.successfulPaints
             << ",\n  \"failedPaints\": " << state.failedPaints << "\n}\n";
    QueueFile(state.activeIncidentDirectory / L"incident.json", incident.str());
    QueueFile(state.activeIncidentDirectory / L"LLM_REVIEW.md",
        "# OpenXR Simulator visible-preview flicker incident\n\n"
        "This packet was captured from the simulator's fully composed CPU preview surface, after projection and quad layers. "
        "It does not rely on the application's game window or pre-compositor textures.\n\n"
        "Run `analyze_openxr_flicker.py` on this directory. Review the paired `color_L`/`color_R` frames for flashes, missing-eye frames, alternation, and frozen output. "
        "The trigger and submission context are in `incident.json` and `%LOCALAPPDATA%\\OpenXR-Simulator\\flicker_status.json`.\n");
}

// Caller holds state.mutex.
void BeginUiIncident(UiState& state, const std::string& reason, uint64_t frame, bool force = false) {
    if (reason != "MANUAL_UI_CAPTURE") ++state.anomalyCount;
    state.lastReason = reason;
    state.statusDirty = true;
    if (!state.activeIncidentDirectory.empty() ||
        (!force && state.lastIncidentFrame != 0 && frame - state.lastIncidentFrame < kIncidentCooldownFrames)) return;
    if (state.incidentCount >= kMaxIncidentsPerSession) return;

    state.lastIncidentFrame = frame;
    ++state.incidentCount;
    state.activeIncidentDirectory = DataRoot() / L"ui_flicker_incidents" /
        (L"session_" + std::to_wstring(GetCurrentProcessId())) /
        (L"incident_" + std::to_wstring(frame));
    state.lastIncidentDirectory = state.activeIncidentDirectory;
    state.postFramesRemaining = kPostIncidentFrames;
    state.activeLastSavedFrame = 0;
    for (const auto& historySample : state.history) {
        QueueSample(state.activeIncidentDirectory, historySample);
        state.activeLastSavedFrame = historySample.frame;
    }
    std::ostringstream incident;
    incident << "{\n  \"schemaVersion\": 1,\n  \"captureSource\": \"openxr-simulator-ui-quad\""
             << ",\n  \"pid\": " << GetCurrentProcessId()
             << ",\n  \"triggerFrame\": " << frame
             << ",\n  \"timestampUnixMs\": " << UnixTimeMs()
             << ",\n  \"reason\": \"" << JsonEscape(reason) << "\""
             << ",\n  \"quadSubmittedFrames\": " << state.quadSubmittedFrames
             << ",\n  \"projectionRefreshFrames\": " << state.projectionRefreshFrames
             << ",\n  \"freshReadbacks\": " << state.freshReadbacks
             << ",\n  \"freshCompositions\": " << state.freshCompositions
             << ",\n  \"cachedCompositions\": " << state.cachedCompositions
             << ",\n  \"missingAfterProjection\": " << state.missingAfterProjection
             << ",\n  \"compositionFailures\": " << state.compositionFailures << "\n}\n";
    QueueFile(state.activeIncidentDirectory / L"incident.json", incident.str());
    QueueFile(state.activeIncidentDirectory / L"LLM_REVIEW.md",
        "# OpenXR Simulator UI-only flicker incident\n\n"
        "Every paired image in this packet is cropped to the submitted quad-layer rectangle in each eye. "
        "World motion outside the UI is intentionally excluded.\n\n"
        "Review `color_L` and `color_R` for UI presence/absence alternation. The structural trigger and "
        "quad readback/composition counters are in `incident.json` and `ui_flicker_status.json`.\n");
}

// Old sessions' incident packets and log files, pruned once per process from
// the worker. Without this the incident roots grow without bound (2.8 GB was
// observed before the cap existed).
void PruneStaleArtifacts() {
    namespace fs = std::filesystem;
    std::error_code ec;
    const std::wstring ownSession = L"session_" + std::to_wstring(GetCurrentProcessId());
    const wchar_t* const incidentRoots[] = { L"flicker_incidents", L"ui_flicker_incidents" };
    for (const wchar_t* rootName : incidentRoots) {
        std::vector<std::pair<fs::file_time_type, fs::path>> sessions;
        fs::directory_iterator it(DataRoot() / rootName, ec);
        for (; !ec && it != fs::directory_iterator(); it.increment(ec)) {
            if (!it->is_directory(ec)) continue;
            if (it->path().filename().wstring() == ownSession) continue;
            sessions.emplace_back(fs::last_write_time(it->path(), ec), it->path());
        }
        std::sort(sessions.begin(), sessions.end(),
                  [](const auto& a, const auto& b) { return a.first > b.first; });
        for (size_t i = kKeepRecentSessions; i < sessions.size(); ++i) {
            std::error_code removeError;
            fs::remove_all(sessions[i].second, removeError);
        }
    }

    const std::wstring ownLog =
        L"openxr_simulator." + std::to_wstring(GetCurrentProcessId()) + L".log";
    std::vector<std::pair<fs::file_time_type, fs::path>> logs;
    ec.clear();
    fs::directory_iterator logIt(DataRoot(), ec);
    for (; !ec && logIt != fs::directory_iterator(); logIt.increment(ec)) {
        const std::wstring name = logIt->path().filename().wstring();
        if (name.rfind(L"openxr_simulator.", 0) != 0 ||
            name.size() < 4 || name.compare(name.size() - 4, 4, L".log") != 0) continue;
        if (name == ownLog) continue;
        logs.emplace_back(fs::last_write_time(logIt->path(), ec), logIt->path());
    }
    std::sort(logs.begin(), logs.end(),
              [](const auto& a, const auto& b) { return a.first > b.first; });
    for (size_t i = kKeepRecentLogs; i < logs.size(); ++i) {
        std::error_code removeError;
        fs::remove(logs[i].second, removeError);
    }
}

void FlushDirtyStatus() {
    static ULONGLONG lastWriteMs = 0;
    static ULONGLONG lastUiWriteMs = 0;
    const ULONGLONG now = GetTickCount64();

    std::string statusJson;
    {
        State& state = GetState();
        std::lock_guard<std::mutex> guard(state.mutex);
        if (state.statusDirty && now - lastWriteMs >= 250) {
            statusJson = BuildStatusJson(state, state.hasMetrics ? &state.lastMetrics : nullptr,
                                         state.lastFrame);
            state.statusDirty = false;
            lastWriteMs = now;
        }
    }
    if (!statusJson.empty()) AtomicWrite(DataRoot() / L"flicker_status.json", statusJson);

    std::string uiJson;
    {
        UiState& state = GetUiState();
        std::lock_guard<std::mutex> guard(state.mutex);
        if (state.statusDirty && now - lastUiWriteMs >= 250) {
            uiJson = BuildUiStatusJson(state, state.hasMetrics ? &state.lastMetrics : nullptr,
                                       state.lastFrame);
            state.statusDirty = false;
            lastUiWriteMs = now;
        }
    }
    if (!uiJson.empty()) AtomicWrite(DataRoot() / L"ui_flicker_status.json", uiJson);
}

void PollManualRequests() {
    static ULONGLONG lastPollMs = 0;
    const ULONGLONG now = GetTickCount64();
    if (now - lastPollMs < 500) return;
    lastPollMs = now;

    const std::filesystem::path request = DataRoot() / L"flicker_capture_request.json";
    if (GetFileAttributesW(request.c_str()) != INVALID_FILE_ATTRIBUTES && DeleteFileW(request.c_str())) {
        State& state = GetState();
        std::lock_guard<std::mutex> guard(state.mutex);
        BeginIncident(state, "MANUAL_CAPTURE", state.lastFrame, true);
    }
    const std::filesystem::path renderDocRequest = DataRoot() / L"renderdoc_capture_request.json";
    if (GetFileAttributesW(renderDocRequest.c_str()) != INVALID_FILE_ATTRIBUTES &&
        DeleteFileW(renderDocRequest.c_str())) {
        State& state = GetState();
        std::lock_guard<std::mutex> guard(state.mutex);
        TryTriggerRenderDocCapture(state, state.lastFrame);
    }
    const std::filesystem::path uiRequest = DataRoot() / L"ui_flicker_capture_request.json";
    if (GetFileAttributesW(uiRequest.c_str()) != INVALID_FILE_ATTRIBUTES && DeleteFileW(uiRequest.c_str())) {
        UiState& state = GetUiState();
        std::lock_guard<std::mutex> guard(state.mutex);
        BeginUiIncident(state, "MANUAL_UI_CAPTURE", state.lastFrame, true);
    }
}

void WorkerMain() {
    IoWorker& worker = GetWorker();
    bool pruned = false;
    for (;;) {
        std::vector<PendingFile> files;
        std::deque<PendingSample> samples;
        {
            std::unique_lock<std::mutex> lock(worker.mutex);
            worker.wake.wait_for(lock, std::chrono::milliseconds(250));
            files.swap(worker.files);
            samples.swap(worker.samples);
        }
        for (PendingFile& file : files) AtomicWrite(file.path, file.contents);
        for (PendingSample& pending : samples) SaveSample(pending.directory, pending.sample);
        FlushDirtyStatus();
        PollManualRequests();
        // Prune after the first service cycle, not before it: clearing a large
        // backlog can take minutes, and fresh status output must not wait on it.
        if (!pruned) {
            pruned = true;
            PruneStaleArtifacts();
        }
    }
}

void EnsureWorker() {
    static std::atomic<bool> started{false};
    if (started.load(std::memory_order_acquire)) return;
    IoWorker& worker = GetWorker();
    std::lock_guard<std::mutex> guard(worker.mutex);
    if (started.load(std::memory_order_relaxed)) return;
    // The worker is detached and every singleton it touches is leaked, so the
    // one remaining hazard is the module's code disappearing underneath it.
    // Pin the DLL: a runtime that has started diagnostics stays resident.
    HMODULE self = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                       reinterpret_cast<LPCWSTR>(&WorkerMain), &self);
    std::thread(&WorkerMain).detach();
    started.store(true, std::memory_order_release);
}

} // namespace

void ObserveSubmission(uint64_t frame, uint32_t projectionLayers, uint32_t totalLayers) {
    EnsureWorker();
    State& state = GetState();
    std::lock_guard<std::mutex> guard(state.mutex);
    state.lastFrame = frame;
    ++state.totalSubmissions;
    state.lastLayerCount = totalLayers;
    const bool hasProjection = projectionLayers > 0;
    if (hasProjection) {
        ++state.projectionSubmissions;
        state.consecutiveMissingProjection = 0;
    } else {
        ++state.missingProjectionSubmissions;
        ++state.consecutiveMissingProjection;
        state.maxConsecutiveMissingProjection = std::max(
            state.maxConsecutiveMissingProjection, state.consecutiveMissingProjection);
    }
    if (state.projectionStateInitialized && state.lastHadProjection != hasProjection) {
        ++state.projectionPresenceTransitions;
        // Ignore startup, but once projection has been visible, a missing projection
        // submission is compositor-level flicker evidence even if the window retains
        // the previous DIB for a moment.
        if (!hasProjection && state.projectionSubmissions >= 5) {
            BeginIncident(state, "MISSING_PROJECTION_LAYER", frame);
        }
    }
    state.projectionStateInitialized = true;
    state.lastHadProjection = hasProjection;
    // Marking dirty is all that happens on the frame path; the worker rate-limits
    // the actual writes, so a long 2D-only stretch (menus, loading) no longer
    // turns into one file write per frame.
    if (state.totalSubmissions % 15 == 0 || !hasProjection) {
        state.statusDirty = true;
    }
}

void ObservePreview(const uint8_t* bgra, uint32_t width, uint32_t height,
                    uint32_t pitchBytes, uint64_t generation, uint64_t frame) {
    if (!bgra || width < 4 || height < 2) return;
    if (pitchBytes == 0) pitchBytes = width * 4;
    EnsureWorker();
    State& state = GetState();
    std::lock_guard<std::mutex> guard(state.mutex);
    if (generation == 0 || generation == state.lastGeneration) return;
    state.lastGeneration = generation;

    Sample current = Downsample(bgra, width, height, pitchBytes, frame);
    if (!state.history.empty()) CalculateTemporal(current, state.history.back());
    ++state.previewSamples;

    std::string reason;
    const float brightMaximum = std::max(
        current.brightFractionLeft, current.brightFractionRight);
    const float brightMinimum = std::min(
        current.brightFractionLeft, current.brightFractionRight);
    const bool asymmetricBright = brightMaximum >= 0.001f &&
        brightMaximum - brightMinimum >= 0.0009f &&
        brightMinimum <= brightMaximum * 0.35f;
    if (asymmetricBright) {
        ++state.asymmetricBrightSamples;
        ++state.consecutiveAsymmetricBright;
        state.maxConsecutiveAsymmetricBright = std::max(
            state.maxConsecutiveAsymmetricBright, state.consecutiveAsymmetricBright);
        if (state.consecutiveAsymmetricBright == 2)
            reason = "ASYMMETRIC_BRIGHT_BREAKTHROUGH";
    } else {
        state.consecutiveAsymmetricBright = 0;
    }
    if (!state.history.empty()) {
        const Sample& previous = state.history.back();
        const bool blankNow = current.mean < 0.015f || current.mean > 0.985f;
        const bool blankBefore = previous.mean < 0.015f || previous.mean > 0.985f;
        const float eyeAsymmetry = std::abs(current.temporalLeft - current.temporalRight);
        // Black-to-visible is the expected startup transition. A return to a
        // blank frame after several visible samples is the actionable case.
        if (reason.empty() && blankNow && !blankBefore && state.visiblePreviewSamples >= 5)
            reason = "VISIBLE_TO_BLANK_FRAME";
        else if (reason.empty() && std::max(current.temporalLeft, current.temporalRight) > 0.12f && eyeAsymmetry > 0.08f)
            reason = "ASYMMETRIC_EYE_FLASH";
        else if (reason.empty() && state.history.size() >= 2 && current.temporal > 0.08f &&
                 TemporalBetween(current, state.history[state.history.size() - 2]) < 0.03f)
            reason = "ALTERNATING_VISIBLE_FRAMES";
    }
    if (current.mean >= 0.015f && current.mean <= 0.985f) {
        ++state.visiblePreviewSamples;
    }
    if (!reason.empty()) BeginIncident(state, reason, frame);

    if (!state.activeIncidentDirectory.empty() && state.activeLastSavedFrame != current.frame) {
        QueueSample(state.activeIncidentDirectory, current);
        state.activeLastSavedFrame = current.frame;
        if (state.postFramesRemaining > 0) --state.postFramesRemaining;
        if (state.postFramesRemaining == 0) state.activeIncidentDirectory.clear();
    }
    if (state.previewSamples % 15 == 0 || !reason.empty()) {
        state.statusDirty = true;
    }
    state.lastMetrics = MetricsOnly(current);
    state.hasMetrics = true;

    state.history.push_back(std::move(current));
    while (state.history.size() > kHistoryFrames) state.history.pop_front();
}

void ObservePaint(uint64_t generation, bool paintedPreview) {
    EnsureWorker();
    State& state = GetState();
    std::lock_guard<std::mutex> guard(state.mutex);
    ++state.paintAttempts;
    if (paintedPreview) {
        ++state.successfulPaints;
        if (generation != 0 && generation == state.lastPaintGeneration) {
            ++state.duplicateGenerationPaints;
        }
        state.lastPaintGeneration = generation;
    } else {
        ++state.failedPaints;
        if (state.successfulPaints >= 5) {
            BeginIncident(state, "PREVIEW_PAINT_FAILURE", state.lastFrame);
        }
    }
    if (!paintedPreview || state.paintAttempts % 15 == 0) {
        state.statusDirty = true;
    }
}

void ObserveUi(const uint8_t* bgra, uint32_t width, uint32_t height,
               uint32_t pitchBytes, uint64_t generation, uint64_t frame,
               const UiFrameInfo& info) {
    if (pitchBytes == 0) pitchBytes = width * 4;
    EnsureWorker();
    UiState& state = GetUiState();
    std::lock_guard<std::mutex> guard(state.mutex);
    state.lastFrame = frame;
    ++state.observedFrames;
    state.lastQuadLayers = info.quadLayers;
    state.lastCacheValid = info.cacheValid;
    state.lastComposed = info.composed;
    state.lastSourceAlphaCoverage = info.sourceAlphaCoverage;
    if (info.quadLayers > 0) ++state.quadSubmittedFrames;
    if (info.projectionRefreshed && info.quadLayers > 0) ++state.projectionRefreshFrames;
    if (info.freshReadback) ++state.freshReadbacks;
    if (info.composed && info.freshReadback) ++state.freshCompositions;
    if (info.composed && info.cachedPixelsUsed) ++state.cachedCompositions;

    std::string reason;
    if (info.quadLayers > 0 && info.projectionRefreshed) {
        if (!info.composed) {
            ++state.missingAfterProjection;
            ++state.consecutiveMissingAfterProjection;
            state.maxConsecutiveMissingAfterProjection = std::max(
                state.maxConsecutiveMissingAfterProjection,
                state.consecutiveMissingAfterProjection);
            reason = info.cacheValid ? "UI_NOT_RECOMPOSED_AFTER_PROJECTION" : "UI_CACHE_UNAVAILABLE_AFTER_PROJECTION";
        } else {
            state.consecutiveMissingAfterProjection = 0;
        }
    }
    if (info.quadLayers > 0 && info.projectionRefreshed && info.cacheValid &&
        !info.composed && info.freshReadback) {
        ++state.compositionFailures;
        reason = "UI_COMPOSITION_FAILED";
    }

    Sample current;
    const bool validRects = info.rects[0][2] > 0 && info.rects[0][3] > 0 &&
                            info.rects[1][2] > 0 && info.rects[1][3] > 0;
    if (bgra && width >= 4 && height >= 2 && validRects && generation != 0 &&
        generation != state.lastGeneration) {
        state.lastGeneration = generation;
        current = CropUi(bgra, width, height, pitchBytes, info.rects, frame);
        if (!state.history.empty()) CalculateTemporal(current, state.history.back());
        ++state.uiSamples;
        if (reason.empty() && !state.history.empty()) {
            const float asymmetry = std::abs(current.temporalLeft - current.temporalRight);
            if (state.history.size() >= 2 && current.temporal > 0.055f &&
                TemporalBetween(current, state.history[state.history.size() - 2]) < 0.025f) {
                reason = "ALTERNATING_UI_REGION";
            } else if (std::max(current.temporalLeft, current.temporalRight) > 0.12f && asymmetry > 0.08f) {
                reason = "ASYMMETRIC_UI_EYE_FLASH";
            }
        }
    }

    if (!reason.empty()) BeginUiIncident(state, reason, frame);
    if (!current.bgra.empty()) {
        if (!state.activeIncidentDirectory.empty() && state.activeLastSavedFrame != current.frame) {
            QueueSample(state.activeIncidentDirectory, current);
            state.activeLastSavedFrame = current.frame;
            if (state.postFramesRemaining > 0) --state.postFramesRemaining;
            if (state.postFramesRemaining == 0) state.activeIncidentDirectory.clear();
        }
        state.lastMetrics = MetricsOnly(current);
        state.hasMetrics = true;
        state.history.push_back(std::move(current));
        while (state.history.size() > kHistoryFrames) state.history.pop_front();
    }

    if (state.observedFrames % 15 == 0 || !reason.empty()) {
        state.statusDirty = true;
    }
}

} // namespace flicker
