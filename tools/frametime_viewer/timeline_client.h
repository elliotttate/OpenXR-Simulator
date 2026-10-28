// The viewer's side of the frame timeline: find processes publishing one,
// attach read-only, and pull new records each UI frame. The mapping stays
// mapped after the game exits, so the last records of a crash or hang survive.
#pragma once
#include "frame_timeline.h"

#include <tlhelp32.h>

#include <string>
#include <vector>

namespace viewer {

using perf::timeline::FrameRecord;
using perf::timeline::GpuRecord;
using perf::timeline::MarkerRecord;
using perf::timeline::TimelineHeader;

struct SessionInfo {
    DWORD pid = 0;
    std::string processName;
    std::string applicationName;
    double refreshHz = 0.0;
    uint64_t frames = 0;
};

// Validates a mapped header against the layout this viewer was built for.
inline bool HeaderUsable(const TimelineHeader& h) {
    return h.magic == perf::timeline::kMagic && h.version == perf::timeline::kVersion &&
           h.headerBytes == perf::timeline::kHeaderBytes &&
           h.frameRecordBytes == sizeof(FrameRecord) && h.markerRecordBytes == sizeof(MarkerRecord) &&
           h.frameCapacity > 0 && h.markerCapacity > 0 && h.frameOffset == perf::timeline::kHeaderBytes &&
           h.gpuRecordBytes == sizeof(GpuRecord) && h.gpuCapacity == h.frameCapacity;
}

inline std::string HeaderString(const char* text, size_t capacity) {
    return std::string(text, strnlen(text, capacity));
}

// Every process with a timeline, newest last. Opening a mapping by name per
// process costs one syscall each, so this is cheap enough to run every second.
inline std::vector<SessionInfo> DiscoverSessions() {
    std::vector<SessionInfo> sessions;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return sessions;
    PROCESSENTRY32W entry{sizeof(entry)};
    for (BOOL ok = Process32FirstW(snapshot, &entry); ok; ok = Process32NextW(snapshot, &entry)) {
        HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE,
                                          perf::timeline::MappingName(entry.th32ProcessID).c_str());
        if (!mapping) continue;
        if (const void* view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, perf::timeline::kHeaderBytes)) {
            const auto& h = *static_cast<const TimelineHeader*>(view);
            if (HeaderUsable(h)) {
                SessionInfo info;
                info.pid = entry.th32ProcessID;
                info.processName = HeaderString(h.processName, sizeof(h.processName));
                info.applicationName = HeaderString(h.applicationName, sizeof(h.applicationName));
                info.refreshHz = h.refreshHz;
                info.frames = h.frameCount.load(std::memory_order_relaxed);
                sessions.push_back(info);
            }
            UnmapViewOfFile(view);
        }
        CloseHandle(mapping);
    }
    CloseHandle(snapshot);
    return sessions;
}

class TimelineClient {
public:
    TimelineClient() = default;
    TimelineClient(const TimelineClient&) = delete;
    TimelineClient& operator=(const TimelineClient&) = delete;
    ~TimelineClient() { Detach(); }

    bool Attach(DWORD pid, std::string& error) {
        Detach();
        m_mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, perf::timeline::MappingName(pid).c_str());
        if (!m_mapping) {
            error = "No frame timeline for process " + std::to_string(pid) +
                    " (is it running the OpenXR Simulator?)";
            return false;
        }
        m_view = MapViewOfFile(m_mapping, FILE_MAP_READ, 0, 0, 0);
        if (!m_view || !HeaderUsable(*Header())) {
            error = "Process " + std::to_string(pid) + " publishes a frame timeline this viewer cannot read";
            Detach();
            return false;
        }
        MEMORY_BASIC_INFORMATION region{};
        const size_t needed = perf::timeline::TimelineBytes(Header()->frameCapacity, Header()->markerCapacity);
        if (!VirtualQuery(m_view, &region, sizeof(region)) || region.RegionSize < needed) {
            error = "Frame timeline of process " + std::to_string(pid) + " is truncated";
            Detach();
            return false;
        }
        m_viewBytes = region.RegionSize;
        m_process = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        m_pid = pid;
        m_generation = Header()->generation.load(std::memory_order_relaxed);
        m_nextFrame = 0;
        m_nextMarker = 0;
        m_nextGpu = 0;
        return true;
    }

    void Detach() {
        if (m_view) UnmapViewOfFile(m_view);
        if (m_mapping) CloseHandle(m_mapping);
        if (m_process) CloseHandle(m_process);
        m_view = nullptr;
        m_mapping = nullptr;
        m_process = nullptr;
        m_pid = 0;
    }

    bool Attached() const { return m_view != nullptr; }
    DWORD Pid() const { return m_pid; }
    const TimelineHeader* Header() const { return static_cast<const TimelineHeader*>(m_view); }

    // Starts reading from whatever is published next, dropping history except
    // the newest `keepFrames` (one gives the first new frame its interval).
    void SkipToNow(uint64_t keepFrames = 0) {
        if (!Attached()) return;
        const uint64_t frames = Header()->frameCount.load(std::memory_order_acquire);
        m_nextFrame = frames > keepFrames ? frames - keepFrames : 0;
        m_nextMarker = Header()->markerCount.load(std::memory_order_acquire);
        m_nextGpu = Header()->gpuCount.load(std::memory_order_acquire);
    }

    struct PollResult {
        uint64_t lostFrames = 0;
        bool restarted = false;  // the runtime reformatted the timeline: drop what was read before
    };

    // Appends every record published since the last poll. Markers first, so a
    // frame always arrives after the markers that explain it; GPU results last,
    // a few frames behind the frames they describe.
    PollResult Poll(std::vector<FrameRecord>& frames, std::vector<MarkerRecord>& markers,
                    std::vector<GpuRecord>& gpu) {
        PollResult result;
        if (!Attached()) return result;
        const TimelineHeader& h = *Header();
        const auto* base = static_cast<const char*>(m_view);
        // The layout is read once per poll and checked every time, not only at
        // attach: a runtime can re-format its mapping in place.
        const uint32_t frameCapacity = h.frameCapacity, markerCapacity = h.markerCapacity, gpuCapacity = h.gpuCapacity;
        const uint64_t frameOffset = h.frameOffset, markerOffset = h.markerOffset, gpuOffset = h.gpuOffset;
        if (!HeaderUsable(h) || !frameCapacity || !markerCapacity || gpuCapacity != frameCapacity ||
            frameOffset != perf::timeline::kHeaderBytes ||
            markerOffset != frameOffset + uint64_t(frameCapacity) * sizeof(FrameRecord) ||
            gpuOffset != markerOffset + uint64_t(markerCapacity) * sizeof(MarkerRecord) ||
            perf::timeline::TimelineBytes(frameCapacity, markerCapacity) > m_viewBytes) {
            Detach();
            return result;
        }
        const uint64_t frameCount = h.frameCount.load(std::memory_order_acquire);
        if (frameCount < m_nextFrame) {
            result.restarted = true;
            m_nextFrame = 0;
            m_nextMarker = 0;
            m_nextGpu = 0;
        }
        m_generation = h.generation.load(std::memory_order_relaxed);
        const auto markerRead = perf::timeline::ReadRing(
            h.markerCount, reinterpret_cast<const MarkerRecord*>(base + markerOffset),
            markerCapacity, m_nextMarker, markers);
        m_nextMarker = markerRead.next;
        const auto frameRead = perf::timeline::ReadRing(
            h.frameCount, reinterpret_cast<const FrameRecord*>(base + frameOffset),
            frameCapacity, m_nextFrame, frames);
        m_nextFrame = frameRead.next;
        result.lostFrames = frameRead.lost;
        m_nextGpu = perf::timeline::ReadRing(h.gpuCount, reinterpret_cast<const GpuRecord*>(base + gpuOffset),
            gpuCapacity, m_nextGpu, gpu).next;
        return result;
    }

    bool ProcessAlive() const {
        return m_process && WaitForSingleObject(m_process, 0) == WAIT_TIMEOUT;
    }

    uint64_t Generation() const { return m_generation; }

private:
    HANDLE m_mapping = nullptr;
    HANDLE m_process = nullptr;
    const void* m_view = nullptr;
    size_t m_viewBytes = 0;
    DWORD m_pid = 0;
    uint64_t m_generation = 0;
    uint64_t m_nextFrame = 0;
    uint64_t m_nextMarker = 0;
    uint64_t m_nextGpu = 0;
};

} // namespace viewer
