#include "flicker_detector.h"

#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace flicker {
namespace {

constexpr uint32_t kHistoryFrames = 10;
constexpr uint32_t kPostIncidentFrames = 12;
constexpr uint64_t kIncidentCooldownFrames = 300;

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
};

struct State {
    std::mutex mutex;
    uint64_t lastGeneration = 0;
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
    uint64_t lastPaintGeneration = 0;
    uint64_t anomalyCount = 0;
    uint64_t incidentCount = 0;
    uint64_t lastIncidentFrame = 0;
    uint32_t consecutiveMissingProjection = 0;
    uint32_t maxConsecutiveMissingProjection = 0;
    uint32_t lastLayerCount = 0;
    bool projectionStateInitialized = false;
    bool lastHadProjection = false;
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
    float lastSourceAlphaCoverage = 0.0f;
    std::string lastReason = "NONE";
    std::filesystem::path lastIncidentDirectory;
    std::filesystem::path activeIncidentDirectory;
    uint32_t postFramesRemaining = 0;
    uint64_t activeLastSavedFrame = 0;
    std::deque<Sample> history;
};

State& GetState() {
    static State state;
    return state;
}

UiState& GetUiState() {
    static UiState state;
    return state;
}

std::filesystem::path DataRoot() {
    wchar_t buffer[MAX_PATH] = {};
    const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, MAX_PATH);
    std::filesystem::path root = length > 0 && length < MAX_PATH ? buffer : L".";
    return root / L"OpenXR-Simulator";
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
    // Rename atomically without forcing a physical disk flush on the OpenXR
    // render thread. Readers still never observe a partial JSON file, while the
    // simulator avoids paying storage latency for status published several
    // times per second.
    MoveFileExW(temporary.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING);
}

uint8_t Luma(const uint8_t* bgra) {
    return (uint8_t)((19u * bgra[0] + 183u * bgra[1] + 54u * bgra[2]) >> 8);
}

Sample Downsample(const uint8_t* bgra, uint32_t width, uint32_t height, uint64_t frame) {
    Sample sample;
    sample.frame = frame;
    sample.width = std::min(width, 640u);
    sample.height = std::max(1u, (uint32_t)std::llround((double)height * sample.width / std::max(1u, width)));
    sample.height = std::min(sample.height, 360u);
    sample.bgra.resize((size_t)sample.width * sample.height * 4);

    double sum = 0.0, sumLeft = 0.0, sumRight = 0.0;
    uint64_t countLeft = 0, countRight = 0;
    for (uint32_t y = 0; y < sample.height; ++y) {
        const uint32_t sourceY = std::min(height - 1, (uint32_t)((uint64_t)y * height / sample.height));
        for (uint32_t x = 0; x < sample.width; ++x) {
            const uint32_t sourceX = std::min(width - 1, (uint32_t)((uint64_t)x * width / sample.width));
            const uint8_t* source = bgra + ((size_t)sourceY * width + sourceX) * 4;
            uint8_t* destination = sample.bgra.data() + ((size_t)y * sample.width + x) * 4;
            memcpy(destination, source, 4);
            const double value = Luma(source) / 255.0;
            sum += value;
            if (x < sample.width / 2) {
                sumLeft += value;
                ++countLeft;
            } else {
                sumRight += value;
                ++countRight;
            }
        }
    }
    const uint64_t pixels = (uint64_t)sample.width * sample.height;
    sample.mean = pixels ? (float)(sum / pixels) : 0.0f;
    sample.meanLeft = countLeft ? (float)(sumLeft / countLeft) : sample.mean;
    sample.meanRight = countRight ? (float)(sumRight / countRight) : sample.mean;
    return sample;
}

Sample CropUi(const uint8_t* bgra, uint32_t width, uint32_t height,
              const int32_t rects[2][4], uint64_t frame) {
    Sample sample;
    sample.frame = frame;
    sample.width = 512;
    sample.height = 144;
    sample.bgra.assign((size_t)sample.width * sample.height * 4, 0);

    double sum[2] = {};
    uint64_t count[2] = {};
    for (int eye = 0; eye < 2; ++eye) {
        const int64_t rx0 = std::clamp<int64_t>(rects[eye][0], 0, width);
        const int64_t ry0 = std::clamp<int64_t>(rects[eye][1], 0, height);
        const int64_t rx1 = std::clamp<int64_t>((int64_t)rects[eye][0] + rects[eye][2], 0, width);
        const int64_t ry1 = std::clamp<int64_t>((int64_t)rects[eye][1] + rects[eye][3], 0, height);
        if (rx1 <= rx0 || ry1 <= ry0) continue;
        for (uint32_t y = 0; y < sample.height; ++y) {
            const uint32_t sourceY = (uint32_t)std::min<int64_t>(
                ry1 - 1, ry0 + (int64_t)y * (ry1 - ry0) / sample.height);
            for (uint32_t x = 0; x < 256; ++x) {
                const uint32_t sourceX = (uint32_t)std::min<int64_t>(
                    rx1 - 1, rx0 + (int64_t)x * (rx1 - rx0) / 256);
                const uint8_t* source = bgra + ((size_t)sourceY * width + sourceX) * 4;
                uint8_t* destination = sample.bgra.data() +
                    ((size_t)y * sample.width + eye * 256 + x) * 4;
                memcpy(destination, source, 4);
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

void WriteStatus(const State& state, const Sample* sample, uint64_t frame) {
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
             << ", \"temporalRight\": " << sample->temporalRight << " }";
    }
    json << "\n}\n";
    AtomicWrite(DataRoot() / L"flicker_status.json", json.str());
}

void BeginIncident(State& state, const std::string& reason, uint64_t frame, bool force = false) {
    if (reason != "MANUAL_CAPTURE") ++state.anomalyCount;
    state.lastReason = reason;
    if (!state.activeIncidentDirectory.empty() ||
        (!force && state.lastIncidentFrame != 0 && frame - state.lastIncidentFrame < kIncidentCooldownFrames)) return;

    state.lastIncidentFrame = frame;
    ++state.incidentCount;
    state.activeIncidentDirectory = DataRoot() / L"flicker_incidents" /
        (L"session_" + std::to_wstring(GetCurrentProcessId())) /
        (L"incident_" + std::to_wstring(frame));
    state.lastIncidentDirectory = state.activeIncidentDirectory;
    state.postFramesRemaining = kPostIncidentFrames;
    state.activeLastSavedFrame = 0;
    std::error_code ec;
    std::filesystem::create_directories(state.activeIncidentDirectory, ec);
    for (const auto& historySample : state.history) {
        SaveSample(state.activeIncidentDirectory, historySample);
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
    AtomicWrite(state.activeIncidentDirectory / L"incident.json", incident.str());
    AtomicWrite(state.activeIncidentDirectory / L"LLM_REVIEW.md",
        "# OpenXR Simulator visible-preview flicker incident\n\n"
        "This packet was captured from the simulator's fully composed CPU preview surface, after projection and quad layers. "
        "It does not rely on the application's game window or pre-compositor textures.\n\n"
        "Run `analyze_openxr_flicker.py` on this directory. Review the paired `color_L`/`color_R` frames for flashes, missing-eye frames, alternation, and frozen output. "
        "The trigger and submission context are in `incident.json` and `%LOCALAPPDATA%\\OpenXR-Simulator\\flicker_status.json`.\n");
}

void WriteUiStatus(const UiState& state, const Sample* sample, uint64_t frame) {
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
    AtomicWrite(DataRoot() / L"ui_flicker_status.json", json.str());
}

void BeginUiIncident(UiState& state, const std::string& reason, uint64_t frame, bool force = false) {
    if (reason != "MANUAL_UI_CAPTURE") ++state.anomalyCount;
    state.lastReason = reason;
    if (!state.activeIncidentDirectory.empty() ||
        (!force && state.lastIncidentFrame != 0 && frame - state.lastIncidentFrame < kIncidentCooldownFrames)) return;

    state.lastIncidentFrame = frame;
    ++state.incidentCount;
    state.activeIncidentDirectory = DataRoot() / L"ui_flicker_incidents" /
        (L"session_" + std::to_wstring(GetCurrentProcessId())) /
        (L"incident_" + std::to_wstring(frame));
    state.lastIncidentDirectory = state.activeIncidentDirectory;
    state.postFramesRemaining = kPostIncidentFrames;
    state.activeLastSavedFrame = 0;
    std::error_code ec;
    std::filesystem::create_directories(state.activeIncidentDirectory, ec);
    for (const auto& historySample : state.history) {
        SaveSample(state.activeIncidentDirectory, historySample);
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
    AtomicWrite(state.activeIncidentDirectory / L"incident.json", incident.str());
    AtomicWrite(state.activeIncidentDirectory / L"LLM_REVIEW.md",
        "# OpenXR Simulator UI-only flicker incident\n\n"
        "Every paired image in this packet is cropped to the submitted quad-layer rectangle in each eye. "
        "World motion outside the UI is intentionally excluded.\n\n"
        "Review `color_L` and `color_R` for UI presence/absence alternation. The structural trigger and "
        "quad readback/composition counters are in `incident.json` and `ui_flicker_status.json`.\n");
}

} // namespace

void ObserveSubmission(uint64_t frame, uint32_t projectionLayers, uint32_t totalLayers) {
    State& state = GetState();
    std::lock_guard<std::mutex> guard(state.mutex);
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
    if (state.totalSubmissions % 15 == 0) {
        const std::filesystem::path request = DataRoot() / L"flicker_capture_request.json";
        if (GetFileAttributesW(request.c_str()) != INVALID_FILE_ATTRIBUTES && DeleteFileW(request.c_str())) {
            BeginIncident(state, "MANUAL_CAPTURE", frame, true);
        }
    }
    if (state.totalSubmissions % 15 == 0 || !hasProjection) {
        WriteStatus(state, state.history.empty() ? nullptr : &state.history.back(), frame);
    }
}

void ObservePreview(const uint8_t* bgra, uint32_t width, uint32_t height,
                    uint64_t generation, uint64_t frame) {
    if (!bgra || width < 4 || height < 2) return;
    State& state = GetState();
    std::lock_guard<std::mutex> guard(state.mutex);
    if (generation == 0 || generation == state.lastGeneration) return;
    state.lastGeneration = generation;

    Sample current = Downsample(bgra, width, height, frame);
    if (!state.history.empty()) CalculateTemporal(current, state.history.back());
    ++state.previewSamples;

    std::string reason;
    if (!state.history.empty()) {
        const Sample& previous = state.history.back();
        const bool blankNow = current.mean < 0.015f || current.mean > 0.985f;
        const bool blankBefore = previous.mean < 0.015f || previous.mean > 0.985f;
        const float meanJump = std::abs(current.mean - previous.mean);
        const float eyeAsymmetry = std::abs(current.temporalLeft - current.temporalRight);
        // Black-to-visible is the expected startup transition. A return to a
        // blank frame after several visible samples is the actionable case.
        if (blankNow && !blankBefore && state.visiblePreviewSamples >= 5)
            reason = "VISIBLE_TO_BLANK_FRAME";
        else if (current.temporal > 0.16f) reason = "LARGE_VISIBLE_TEMPORAL_JUMP";
        else if (meanJump > 0.10f && current.temporal > 0.08f) reason = "VISIBLE_LUMA_FLASH";
        else if (std::max(current.temporalLeft, current.temporalRight) > 0.12f && eyeAsymmetry > 0.08f)
            reason = "ASYMMETRIC_EYE_FLASH";
        else if (state.history.size() >= 2 && current.temporal > 0.08f &&
                 TemporalBetween(current, state.history[state.history.size() - 2]) < 0.03f)
            reason = "ALTERNATING_VISIBLE_FRAMES";
    }
    if (current.mean >= 0.015f && current.mean <= 0.985f) {
        ++state.visiblePreviewSamples;
    }
    if (!reason.empty()) BeginIncident(state, reason, frame);

    state.history.push_back(current);
    while (state.history.size() > kHistoryFrames) state.history.pop_front();

    if (!state.activeIncidentDirectory.empty() && state.activeLastSavedFrame != current.frame) {
        SaveSample(state.activeIncidentDirectory, current);
        state.activeLastSavedFrame = current.frame;
        if (state.postFramesRemaining > 0) --state.postFramesRemaining;
        if (state.postFramesRemaining == 0) state.activeIncidentDirectory.clear();
    }
    if (state.previewSamples % 15 == 0 || !reason.empty()) {
        WriteStatus(state, &current, frame);
    }
}

void ObservePaint(uint64_t generation, bool paintedPreview) {
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
            BeginIncident(state, "PREVIEW_PAINT_FAILURE", generation);
        }
    }
    if (!paintedPreview || state.paintAttempts % 15 == 0) {
        WriteStatus(state, state.history.empty() ? nullptr : &state.history.back(), generation);
    }
}

void ObserveUi(const uint8_t* bgra, uint32_t width, uint32_t height,
               uint64_t generation, uint64_t frame, const UiFrameInfo& info) {
    UiState& state = GetUiState();
    std::lock_guard<std::mutex> guard(state.mutex);
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
        current = CropUi(bgra, width, height, info.rects, frame);
        if (!state.history.empty()) CalculateTemporal(current, state.history.back());
        ++state.uiSamples;
        if (reason.empty() && !state.history.empty()) {
            const float asymmetry = std::abs(current.temporalLeft - current.temporalRight);
            if (state.history.size() >= 2 && current.temporal > 0.055f &&
                TemporalBetween(current, state.history[state.history.size() - 2]) < 0.025f) {
                reason = "ALTERNATING_UI_REGION";
            } else if (current.temporal > 0.18f) {
                reason = "LARGE_UI_REGION_FLASH";
            } else if (std::max(current.temporalLeft, current.temporalRight) > 0.12f && asymmetry > 0.08f) {
                reason = "ASYMMETRIC_UI_EYE_FLASH";
            }
        }
    }

    if (!reason.empty()) BeginUiIncident(state, reason, frame);
    if (!current.bgra.empty()) {
        state.history.push_back(current);
        while (state.history.size() > kHistoryFrames) state.history.pop_front();
        if (!state.activeIncidentDirectory.empty() && state.activeLastSavedFrame != current.frame) {
            SaveSample(state.activeIncidentDirectory, current);
            state.activeLastSavedFrame = current.frame;
            if (state.postFramesRemaining > 0) --state.postFramesRemaining;
            if (state.postFramesRemaining == 0) state.activeIncidentDirectory.clear();
        }
    }

    if (state.observedFrames % 15 == 0) {
        const std::filesystem::path request = DataRoot() / L"ui_flicker_capture_request.json";
        if (GetFileAttributesW(request.c_str()) != INVALID_FILE_ATTRIBUTES && DeleteFileW(request.c_str())) {
            BeginUiIncident(state, "MANUAL_UI_CAPTURE", frame, true);
        }
    }
    if (state.observedFrames % 15 == 0 || !reason.empty()) {
        const Sample* sample = !current.bgra.empty() ? &current :
            (state.history.empty() ? nullptr : &state.history.back());
        WriteUiStatus(state, sample, frame);
    }
}

} // namespace flicker
