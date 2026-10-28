// A recorded stretch of a frame timeline, saved to disk (.xrft):
//
//   CaptureHeader     fixed, little-endian (below)
//   metadata          metadataBytes of UTF-8 JSON: what was captured, from
//                     which process, on what hardware and settings
//   frames            frameCount x FrameRecord (frame_timeline.h)
//   markers           markerCount x MarkerRecord
//   gpu               gpuCount x GpuRecord (when the runtime timed the GPU)
//
// The records are exactly what the runtime published, so a capture is scored
// the same way as the live view and can be re-scored later against any
// refresh rate. The numbers analysis needs are in the binary header; the JSON
// is description only.
#pragma once
#include "frame_timeline.h"

#include <cstdio>
#include <io.h>
#include <new>
#include <string>
#include <vector>

namespace perf::capture {

using timeline::FrameRecord;
using timeline::GpuRecord;
using timeline::MarkerRecord;

constexpr char kMagic[8] = {'X', 'R', 'F', 'T', 'C', 'A', 'P', '\0'};
constexpr uint32_t kVersion = 1;
constexpr const wchar_t* kExtension = L".xrft";

struct CaptureHeader {
    char     magic[8];
    uint32_t version;
    uint32_t headerBytes;
    uint32_t frameRecordBytes;
    uint32_t markerRecordBytes;
    uint64_t frameCount;
    uint64_t markerCount;
    uint64_t metadataBytes;
    uint64_t qpcFrequency;
    uint64_t originQpc;     // the live timeline's creation: capture times match the live view's
    uint64_t tscFrequency;  // thread-cycle rate, 0 if never calibrated
    double   refreshHz;     // the rate the runtime paced at
    uint64_t gpuCount;      // 0 in captures from before GPU timing: those were reserved, zero
    uint64_t gpuRecordBytes;
    uint64_t reserved[2];
};
static_assert(sizeof(CaptureHeader) == 112, "CaptureHeader is a file format");

struct Capture {
    CaptureHeader header{};
    std::string metadata;  // JSON
    std::vector<FrameRecord> frames;
    std::vector<MarkerRecord> markers;
    std::vector<GpuRecord> gpu;
};

// JSON string literal for `text`, quotes included.
inline std::string JsonString(const std::string& text) {
    std::string out = "\"";
    for (const unsigned char c : text) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char escaped[8];
                    snprintf(escaped, sizeof(escaped), "\\u%04x", c);
                    out += escaped;
                } else {
                    out += char(c);
                }
        }
    }
    return out + "\"";
}

inline CaptureHeader MakeHeader(uint64_t qpcFrequency, uint64_t originQpc, uint64_t tscFrequency, double refreshHz) {
    CaptureHeader h{};
    std::memcpy(h.magic, kMagic, sizeof(kMagic));
    h.version = kVersion;
    h.headerBytes = sizeof(CaptureHeader);
    h.frameRecordBytes = sizeof(FrameRecord);
    h.markerRecordBytes = sizeof(MarkerRecord);
    h.qpcFrequency = qpcFrequency;
    h.originQpc = originQpc;
    h.tscFrequency = tscFrequency;
    h.refreshHz = refreshHz;
    return h;
}

// Writes to a temporary file first, so a reader never sees half a capture.
inline bool WriteCapture(const std::wstring& path, Capture& capture, std::string& error) {
    capture.header.frameCount = capture.frames.size();
    capture.header.markerCount = capture.markers.size();
    capture.header.gpuCount = capture.gpu.size();
    capture.header.gpuRecordBytes = capture.gpu.empty() ? 0 : sizeof(GpuRecord);
    capture.header.metadataBytes = capture.metadata.size();
    // Unique, so two writers of one name cannot share a half-written file.
    const std::wstring temporary = path + L"." + std::to_wstring(GetCurrentProcessId()) + L"-" +
                                   std::to_wstring(GetTickCount64()) + L".partial";
    FILE* file = nullptr;
    if (_wfopen_s(&file, temporary.c_str(), L"wb") != 0 || !file) {
        error = "cannot create the capture file";
        return false;
    }
    bool ok = fwrite(&capture.header, sizeof(CaptureHeader), 1, file) == 1;
    if (ok && !capture.metadata.empty()) ok = fwrite(capture.metadata.data(), capture.metadata.size(), 1, file) == 1;
    if (ok && !capture.frames.empty()) {
        ok = fwrite(capture.frames.data(), sizeof(FrameRecord), capture.frames.size(), file) == capture.frames.size();
    }
    if (ok && !capture.markers.empty()) {
        ok = fwrite(capture.markers.data(), sizeof(MarkerRecord), capture.markers.size(), file) == capture.markers.size();
    }
    if (ok && !capture.gpu.empty()) {
        ok = fwrite(capture.gpu.data(), sizeof(GpuRecord), capture.gpu.size(), file) == capture.gpu.size();
    }
    ok = ok && fflush(file) == 0 && _commit(_fileno(file)) == 0;  // on disk before it takes the name
    ok = (fclose(file) == 0) && ok;
    if (!ok || !MoveFileExW(temporary.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING)) {
        DeleteFileW(temporary.c_str());
        error = ok ? "cannot move the capture into place" : "writing the capture failed (disk full?)";
        return false;
    }
    return true;
}

// `headerOnly` stops after the metadata: enough to list captures quickly.
inline bool ReadCapture(const std::wstring& path, Capture& capture, std::string& error, bool headerOnly = false) {
    capture = Capture{};
    FILE* file = nullptr;
    if (_wfopen_s(&file, path.c_str(), L"rb") != 0 || !file) {
        error = "cannot open the file";
        return false;
    }
    auto fail = [&](const char* message) {
        fclose(file);
        error = message;
        return false;
    };
    CaptureHeader& h = capture.header;
    if (fread(&h, sizeof(CaptureHeader), 1, file) != 1 || std::memcmp(h.magic, kMagic, sizeof(kMagic)) != 0) {
        return fail("not a frame-timing capture");
    }
    if (h.version != kVersion || h.headerBytes != sizeof(CaptureHeader) ||
        h.frameRecordBytes != sizeof(FrameRecord) || h.markerRecordBytes != sizeof(MarkerRecord)) {
        return fail("capture written by an incompatible version");
    }
    _fseeki64(file, 0, SEEK_END);
    const uint64_t size = uint64_t(_ftelli64(file));
    if (h.gpuCount && h.gpuRecordBytes != sizeof(GpuRecord)) return fail("capture written by an incompatible version");
    // Each count against the bytes left, so a damaged count cannot overflow the sum.
    uint64_t left = size > sizeof(CaptureHeader) ? size - sizeof(CaptureHeader) : 0;
    auto take = [&](uint64_t count, uint64_t bytes) {
        if (count > left / bytes) return false;
        left -= count * bytes;
        return true;
    };
    if (h.metadataBytes > (16u << 20) || !take(h.metadataBytes, 1) || !take(h.frameCount, sizeof(FrameRecord)) ||
        !take(h.markerCount, sizeof(MarkerRecord)) || !take(h.gpuCount, sizeof(GpuRecord))) {
        return fail("capture is truncated or damaged");
    }
    _fseeki64(file, sizeof(CaptureHeader), SEEK_SET);
    capture.metadata.resize(size_t(h.metadataBytes));
    if (h.metadataBytes && fread(capture.metadata.data(), size_t(h.metadataBytes), 1, file) != 1) {
        return fail("capture metadata is unreadable");
    }
    if (!headerOnly) {
        try {
            capture.frames.resize(size_t(h.frameCount));
            capture.markers.resize(size_t(h.markerCount));
            capture.gpu.resize(size_t(h.gpuCount));
        } catch (const std::bad_alloc&) {
            return fail("capture is too large to open");
        }
        if ((h.frameCount && fread(capture.frames.data(), sizeof(FrameRecord), size_t(h.frameCount), file) != h.frameCount) ||
            (h.markerCount && fread(capture.markers.data(), sizeof(MarkerRecord), size_t(h.markerCount), file) != h.markerCount) ||
            (h.gpuCount && fread(capture.gpu.data(), sizeof(GpuRecord), size_t(h.gpuCount), file) != h.gpuCount)) {
            return fail("capture records are unreadable");
        }
    }
    fclose(file);
    return true;
}

} // namespace perf::capture
