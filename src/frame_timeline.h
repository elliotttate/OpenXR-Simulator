// Always-on per-frame timing feed for the frame-time viewer. One fixed-size
// record per xrEndFrame holding raw QPC timestamps and raw thread-cycle
// counters, published through a named shared-memory ring that another process
// reads without touching the game. Nothing is averaged, filtered or derived
// here: the reader computes intervals, headroom, lateness and missed refreshes,
// so a capture can be re-scored later against any refresh rate.
//
// Mapping Local\OpenXRSimulator.FrameTimeline.<pid>, all fields little-endian
// and naturally aligned:
//   [0, kHeaderBytes)                              TimelineHeader
//   [frameOffset,  + frameCapacity  * 192 bytes)   FrameRecord ring
//   [markerOffset, + markerCapacity * 128 bytes)   MarkerRecord ring
//
// Reading either ring: load its count (acquire), copy records [from, count),
// fence (acquire), load the count again. Record i survived the copy iff
// i + capacity > countAfter - the writer only starts reusing the slot of
// record i after publishing record i + capacity - 1. Every record also carries
// its own index. ReadRing below is the reference implementation.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX  // readers use std::min/std::max
#endif
#include <windows.h>
#include <intrin.h>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <new>
#include <string>
#include <vector>

namespace perf::timeline {

constexpr uint32_t kMagic = 0x54465258;  // "XRFT"
constexpr uint32_t kVersion = 1;
constexpr uint32_t kHeaderBytes = 4096;
constexpr uint32_t kDefaultFrameCapacity = 65536;  // ~12 minutes at 90 Hz
constexpr uint32_t kDefaultMarkerCapacity = 4096;

enum FrameFlags : uint32_t {
    kFrameStereoProjection = 1u << 0,  // a projection layer with two non-empty views
    kFrameNoProjection     = 1u << 1,  // 2D-only frame: loading screen, menu
    kFrameWaitMatched      = 1u << 2,  // displayTime is one this runtime's xrWaitFrame predicted
    kFrameBeginSeen        = 1u << 3,  // xrBeginFrame ran after the matched wait
    kFramePacingRebased    = 1u << 4,  // the matched wait restarted its tick schedule: the app fell over two periods behind it
    kFrameMirrorDue        = 1u << 5,  // the preview mirror updated this frame
    kFrameScreenshot       = 1u << 6,  // a screenshot was served from this frame
    kFrameBurst            = 1u << 7,  // a frame burst was recording
    kFrameSessionFocused   = 1u << 8,
};

// Zero means "not observed": every timestamp is a raw QPC value and the
// simulator never sees QPC 0. Cycle counters are QueryThreadCycleTime values,
// cumulative per thread, so the reader can take any span between two stamps
// made on the same thread (compare the thread ids) and convert it with
// TimelineHeader::tscFrequency.
struct FrameRecord {
    uint64_t index;                   // position in publish order; the reader checks it
    uint64_t frameId;                 // runtime frame number, as in the log and runtime_status.json

    // The xrWaitFrame whose predictedDisplayTime the app submitted (kFrameWaitMatched).
    uint64_t waitEnterQpc;
    uint64_t waitPaceQpc;             // simulator input/command handling done, pacing wait starts
    uint64_t waitTargetQpc;           // the tick the pacing wait aimed for
    uint64_t waitExitQpc;
    uint64_t beginQpc;                // xrBeginFrame
    uint64_t locateQpc;               // first xrLocateViews for this frame's displayTime

    // xrEndFrame stages.
    uint64_t endEnterQpc;
    uint64_t endProjectionQpc;        // projection layers composited
    uint64_t endOverlaysQpc;          // quad/cylinder layers composited
    uint64_t endPaintQpc;             // preview hand-off, flicker detection, paint
    uint64_t endExitQpc;              // status, deferred present, title

    int64_t  predictedDisplayTime;    // XrTime (ns) the matched wait returned
    int64_t  predictedDisplayPeriod;  // ns
    int64_t  submittedDisplayTime;    // XrFrameEndInfo::displayTime

    uint64_t waitEnterCycles;         // waitThreadId
    uint64_t waitExitCycles;          // waitThreadId
    uint64_t endEnterCycles;          // endThreadId
    uint64_t endExitCycles;           // endThreadId

    uint32_t waitThreadId;
    uint32_t endThreadId;
    uint32_t flags;                   // FrameFlags
    uint32_t layerCount;
    uint16_t projectionLayers;
    uint16_t quadLayers;
    uint16_t cylinderLayers;
    uint16_t otherLayers;
    uint64_t reserved;
};
static_assert(sizeof(FrameRecord) == 192, "FrameRecord is a shared-memory ABI");

// Events that explain a hitch next to it: what the simulator, the session or
// the user changed, and when.
enum class MarkerKind : uint32_t {
    Instance = 1,       // text: application name
    SessionState,       // a: XrSessionState
    SwapchainCreate,    // a: width, b: height, text: format, samples, array size
    SwapchainDestroy,
    UiCommand,          // a: menu command id, or the virtual key when b == 1
    WindowResize,       // a: client width, b: client height
    EndFrameReentry,    // a: blocked reentrant xrEndFrame calls so far
    BurstStart,         // a: frames
    User,               // text
};

struct MarkerRecord {
    uint64_t index;
    uint64_t qpc;
    uint64_t frameId;    // newest runtime frame published when the marker was written
    uint32_t kind;       // MarkerKind
    uint32_t threadId;
    int64_t  a;
    int64_t  b;
    char     text[80];   // UTF-8, always NUL-terminated
};
static_assert(sizeof(MarkerRecord) == 128, "MarkerRecord is a shared-memory ABI");

struct TimelineHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t headerBytes;
    uint32_t frameRecordBytes;
    uint32_t markerRecordBytes;
    uint32_t frameCapacity;
    uint32_t markerCapacity;
    uint32_t processId;
    uint64_t frameOffset;
    uint64_t markerOffset;
    uint64_t qpcFrequency;
    uint64_t createdQpc;
    double   refreshHz;                          // the simulated refresh rate pacing aims for
    std::atomic<uint64_t> generation;            // bumped each time a runtime attaches
    std::atomic<uint64_t> frameCount;            // FrameRecords published
    std::atomic<uint64_t> markerCount;           // MarkerRecords published
    std::atomic<uint64_t> lastEndExitQpc;        // heartbeat: newest xrEndFrame return
    std::atomic<uint64_t> waitActiveSinceQpc;    // nonzero while a thread is inside xrWaitFrame
    std::atomic<uint64_t> endActiveSinceQpc;     // nonzero while a thread is inside xrEndFrame
    std::atomic<uint64_t> tscFrequency;          // thread-cycle counter rate in Hz, 0 until calibrated
    char processName[64];                        // UTF-8 executable file name
    char applicationName[128];                   // XrApplicationInfo::applicationName
    // What the frames ran on: GPU and driver, render resolution, headset
    // profile, app swapchain, mirror rate. UTF-8 JSON, rewritten when any of it
    // changes; the sequence is odd while a rewrite is under way (ReadSessionInfo).
    std::atomic<uint32_t> sessionInfoSequence;
    uint32_t sessionInfoBytes;
    char sessionInfo[2048];
};
static_assert(sizeof(TimelineHeader) <= kHeaderBytes, "header must fit its page");

// The session info as of now, or "{}" if it is mid-rewrite for too long.
inline std::string ReadSessionInfo(const TimelineHeader& header) {
    for (int attempt = 0; attempt < 64; ++attempt) {
        const uint32_t before = header.sessionInfoSequence.load(std::memory_order_acquire);
        if (before & 1) continue;
        const uint32_t bytes = header.sessionInfoBytes;
        std::string copy(header.sessionInfo, bytes < sizeof(header.sessionInfo) ? bytes : 0);
        std::atomic_thread_fence(std::memory_order_acquire);
        if (header.sessionInfoSequence.load(std::memory_order_relaxed) == before) return copy.empty() ? "{}" : copy;
    }
    return "{}";
}
static_assert(std::atomic<uint64_t>::is_always_lock_free, "atomics in shared memory must be lock-free");

inline size_t TimelineBytes(uint32_t frameCapacity, uint32_t markerCapacity) {
    return kHeaderBytes + size_t(frameCapacity) * sizeof(FrameRecord) +
           size_t(markerCapacity) * sizeof(MarkerRecord);
}

struct ReadResult {
    uint64_t next = 0;  // the `from` to pass next time
    uint64_t lost = 0;  // records in [from, next) the writer overwrote before they were read
};

// Appends to `out` every record of [from, published) that survived the copy.
template <class Record>
inline ReadResult ReadRing(const std::atomic<uint64_t>& count, const Record* ring,
                           uint32_t capacity, uint64_t from, std::vector<Record>& out) {
    ReadResult result;
    const uint64_t published = count.load(std::memory_order_acquire);
    // Only a timeline formatted afresh under the reader can move the count back.
    if (from > published) from = 0;
    const uint64_t oldest = published >= capacity ? published - capacity + 1 : 0;
    const uint64_t first = from < oldest ? oldest : from;
    const size_t base = out.size();
    out.resize(base + size_t(published - first));
    for (uint64_t i = first; i < published; ++i) {
        std::memcpy(&out[base + size_t(i - first)], &ring[i % capacity], sizeof(Record));
    }
    std::atomic_thread_fence(std::memory_order_acquire);
    const uint64_t after = count.load(std::memory_order_relaxed);
    const uint64_t survived = after >= capacity ? after - capacity + 1 : 0;
    size_t kept = base;
    for (uint64_t i = first; i < published; ++i) {
        const size_t at = base + size_t(i - first);
        if (i < survived || out[at].index != i) continue;
        if (kept != at) out[kept] = out[at];
        ++kept;
    }
    out.resize(kept);
    result.next = published;
    result.lost = (published - from) - uint64_t(kept - base);
    return result;
}

// The ring itself, over memory the caller owns: a file mapping in the runtime,
// a plain buffer in tests.
class Timeline {
public:
    // Formats `memory` as an empty timeline or, when it already holds one with
    // exactly this layout (the runtime DLL was reloaded while a viewer kept the
    // mapping open), continues it under a new generation so record indices
    // never go back.
    bool Attach(void* memory, size_t bytes, uint32_t frameCapacity, uint32_t markerCapacity,
                uint32_t processId, uint64_t qpcFrequency, uint64_t nowQpc, double refreshHz) {
        m_header = nullptr;
        if (!memory || frameCapacity == 0 || markerCapacity == 0 ||
            bytes < TimelineBytes(frameCapacity, markerCapacity)) return false;
        auto* header = static_cast<TimelineHeader*>(memory);
        const uint64_t frameOffset = kHeaderBytes;
        const uint64_t markerOffset = frameOffset + uint64_t(frameCapacity) * sizeof(FrameRecord);
        const bool compatible =
            header->magic == kMagic && header->version == kVersion &&
            header->headerBytes == kHeaderBytes &&
            header->frameRecordBytes == sizeof(FrameRecord) &&
            header->markerRecordBytes == sizeof(MarkerRecord) &&
            header->frameCapacity == frameCapacity && header->markerCapacity == markerCapacity &&
            header->frameOffset == frameOffset && header->markerOffset == markerOffset;
        if (!compatible) {
            std::memset(memory, 0, kHeaderBytes);
            header = new (memory) TimelineHeader{};
            header->headerBytes = kHeaderBytes;
            header->frameRecordBytes = sizeof(FrameRecord);
            header->markerRecordBytes = sizeof(MarkerRecord);
            header->frameCapacity = frameCapacity;
            header->markerCapacity = markerCapacity;
            header->frameOffset = frameOffset;
            header->markerOffset = markerOffset;
            header->createdQpc = nowQpc;
            header->version = kVersion;
        }
        header->processId = processId;
        header->qpcFrequency = qpcFrequency;
        header->refreshHz = refreshHz;
        header->waitActiveSinceQpc.store(0, std::memory_order_relaxed);
        header->endActiveSinceQpc.store(0, std::memory_order_relaxed);
        header->generation.fetch_add(1, std::memory_order_relaxed);
        // Readers identify a timeline by its magic: written last, released.
        std::atomic_thread_fence(std::memory_order_release);
        header->magic = kMagic;
        m_frames = reinterpret_cast<FrameRecord*>(static_cast<char*>(memory) + frameOffset);
        m_markers = reinterpret_cast<MarkerRecord*>(static_cast<char*>(memory) + markerOffset);
        m_header = header;
        return true;
    }

    TimelineHeader* Header() const { return m_header; }
    const FrameRecord* Frames() const { return m_frames; }
    const MarkerRecord* Markers() const { return m_markers; }

    // One writer at a time: the runtime publishes only from inside xrEndFrame,
    // whose reentrancy guard already serializes callers.
    void PublishFrame(const FrameRecord& record) {
        const uint64_t index = m_header->frameCount.load(std::memory_order_relaxed);
        // Keeps this slot's overwrite after the previous publish: a reader that
        // sees any of it must also see the count that discards the old record.
        std::atomic_thread_fence(std::memory_order_release);
        FrameRecord stamped = record;
        stamped.index = index;
        std::memcpy(&m_frames[index % m_header->frameCapacity], &stamped, sizeof(FrameRecord));
        m_header->frameCount.store(index + 1, std::memory_order_release);
        m_header->lastEndExitQpc.store(record.endExitQpc, std::memory_order_relaxed);
    }

    // Any thread. Markers are rare, so a lock is cheaper than getting clever.
    void PublishMarker(MarkerKind kind, uint64_t qpc, uint32_t threadId, int64_t a, int64_t b,
                       const char* text) {
        MarkerRecord record{};
        record.qpc = qpc;
        record.kind = static_cast<uint32_t>(kind);
        record.threadId = threadId;
        record.a = a;
        record.b = b;
        if (text) {
            size_t length = std::strlen(text);
            if (length >= sizeof(record.text)) {
                // Cut at a character boundary, not inside a UTF-8 sequence.
                length = sizeof(record.text) - 1;
                while (length > 0 && (static_cast<unsigned char>(text[length]) & 0xC0) == 0x80) --length;
            }
            std::memcpy(record.text, text, length);
        }
        std::lock_guard<std::mutex> lock(m_markerMutex);
        const uint64_t frames = m_header->frameCount.load(std::memory_order_acquire);
        record.frameId = frames ? m_frames[(frames - 1) % m_header->frameCapacity].frameId : 0;
        const uint64_t index = m_header->markerCount.load(std::memory_order_relaxed);
        std::atomic_thread_fence(std::memory_order_release);
        MarkerRecord& slot = m_markers[index % m_header->markerCapacity];
        record.index = index;
        std::memcpy(&slot, &record, sizeof(MarkerRecord));
        m_header->markerCount.store(index + 1, std::memory_order_release);
    }

    ReadResult ReadFrames(uint64_t from, std::vector<FrameRecord>& out) const {
        return ReadRing(m_header->frameCount, m_frames, m_header->frameCapacity, from, out);
    }
    ReadResult ReadMarkers(uint64_t from, std::vector<MarkerRecord>& out) const {
        return ReadRing(m_header->markerCount, m_markers, m_header->markerCapacity, from, out);
    }

private:
    TimelineHeader* m_header = nullptr;
    FrameRecord* m_frames = nullptr;
    MarkerRecord* m_markers = nullptr;
    std::mutex m_markerMutex;
};

// What one xrWaitFrame call saw. The app serializes its xrWaitFrame calls
// (the session is externally synchronized there), so the caller keeps this on
// its stack until the call returns.
struct WaitSample {
    uint64_t enterQpc = 0;
    uint64_t paceQpc = 0;
    uint64_t targetQpc = 0;
    uint64_t exitQpc = 0;
    uint64_t beginQpc = 0;
    uint64_t locateQpc = 0;
    int64_t  predictedDisplayTime = 0;
    int64_t  predictedDisplayPeriod = 0;
    uint64_t enterCycles = 0;
    uint64_t exitCycles = 0;
    uint32_t threadId = 0;
    bool     rebased = false;
};

// Pairs each xrEndFrame with the xrWaitFrame that produced its displayTime.
// UE-style engines wait on the game thread and submit on the render thread a
// frame or more later, so neither call order nor thread identity says which
// wait a submission belongs to - but the app hands the predicted display time
// back in XrFrameEndInfo, and that is an exact key.
class FrameJoiner {
public:
    static constexpr size_t kCapacity = 8;

    void OnWait(const WaitSample& wait) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_count == kCapacity) Erase(0);  // never ended: the oldest goes
        m_pending[m_count++] = wait;
    }

    // xrBeginFrame belongs to the most recent xrWaitFrame.
    void OnBegin(uint64_t qpc) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_count && !m_pending[m_count - 1].beginQpc) m_pending[m_count - 1].beginQpc = qpc;
    }

    void OnLocate(int64_t displayTime, uint64_t qpc) {
        std::lock_guard<std::mutex> lock(m_mutex);
        for (size_t i = 0; i < m_count; ++i) {
            if (m_pending[i].predictedDisplayTime == displayTime) {
                if (!m_pending[i].locateQpc) m_pending[i].locateQpc = qpc;
                return;
            }
        }
    }

    // Removes and returns the wait that predicted `displayTime`, along with
    // every older one: their frames were discarded, never submitted. A time no
    // wait predicted leaves the table alone.
    bool TakeForEnd(int64_t displayTime, WaitSample& out) {
        std::lock_guard<std::mutex> lock(m_mutex);
        for (size_t i = 0; i < m_count; ++i) {
            if (m_pending[i].predictedDisplayTime != displayTime) continue;
            out = m_pending[i];
            const size_t remove = i + 1;
            for (size_t j = remove; j < m_count; ++j) m_pending[j - remove] = m_pending[j];
            m_count -= remove;
            return true;
        }
        return false;
    }

    size_t Pending() const {
        std::lock_guard<std::mutex> lock(m_mutex);
        return m_count;
    }

private:
    void Erase(size_t at) {
        for (size_t j = at + 1; j < m_count; ++j) m_pending[j - 1] = m_pending[j];
        --m_count;
    }

    mutable std::mutex m_mutex;
    WaitSample m_pending[kCapacity];
    size_t m_count = 0;
};

inline uint64_t Qpc() {
    LARGE_INTEGER value;
    QueryPerformanceCounter(&value);
    return uint64_t(value.QuadPart);
}

inline uint64_t ThreadCycles() {
    ULONG64 cycles = 0;
    QueryThreadCycleTime(GetCurrentThread(), &cycles);
    return cycles;
}

inline std::wstring MappingName(uint32_t processId) {
    return L"Local\\OpenXRSimulator.FrameTimeline." + std::to_wstring(processId);
}

// The runtime's side: owns the mapping and stamps the frame loop. Every entry
// point is a no-op until Open succeeds.
class Recorder {
public:
    Recorder() = default;
    Recorder(const Recorder&) = delete;
    Recorder& operator=(const Recorder&) = delete;
    ~Recorder() { Close(); }

    // Creates this process's mapping, or rejoins it after a DLL reload. Call
    // from an ordinary runtime entry point, never DllMain.
    bool Open(double refreshHz, uint32_t frameCapacity = kDefaultFrameCapacity,
              uint32_t markerCapacity = kDefaultMarkerCapacity,
              const wchar_t* name = nullptr) {
        std::lock_guard<std::mutex> lock(m_lifecycle);
        if (m_enabled.load()) return true;
        const size_t bytes = TimelineBytes(frameCapacity, markerCapacity);
        const uint32_t processId = GetCurrentProcessId();
        const std::wstring mappingName = name ? std::wstring(name) : MappingName(processId);
        m_mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                       DWORD(uint64_t(bytes) >> 32), DWORD(bytes & 0xffffffffu),
                                       mappingName.c_str());
        if (!m_mapping) return false;
        m_view = MapViewOfFile(m_mapping, FILE_MAP_ALL_ACCESS, 0, 0, bytes);
        LARGE_INTEGER frequency;
        QueryPerformanceFrequency(&frequency);
        m_qpcFrequency = uint64_t(frequency.QuadPart);
        const uint64_t now = Qpc();
        if (!m_view || !m_timeline.Attach(m_view, bytes, frameCapacity, markerCapacity,
                                          processId, m_qpcFrequency, now, refreshHz)) {
            CloseLocked();
            return false;
        }
        WriteProcessName(*m_timeline.Header());
        m_calibrationQpc = now;
        m_calibrationTsc = __rdtsc();
        m_enabled.store(true, std::memory_order_release);
        return true;
    }

    void Close() {
        std::lock_guard<std::mutex> lock(m_lifecycle);
        CloseLocked();
    }

    bool Enabled() const { return m_enabled.load(std::memory_order_acquire); }
    const Timeline& Ring() const { return m_timeline; }

    void SetApplicationName(const char* name) {
        if (!Enabled() || !name) return;
        TimelineHeader& header = *m_timeline.Header();
        std::memset(header.applicationName, 0, sizeof(header.applicationName));
        size_t length = std::strlen(name);
        if (length >= sizeof(header.applicationName)) length = sizeof(header.applicationName) - 1;
        std::memcpy(header.applicationName, name, length);
    }

    // One writer at a time (the runtime publishes from inside xrEndFrame).
    void SetSessionInfo(const std::string& json) {
        if (!Enabled()) return;
        TimelineHeader& header = *m_timeline.Header();
        const size_t bytes = json.size() < sizeof(header.sessionInfo) ? json.size() : 0;
        const uint32_t sequence = header.sessionInfoSequence.load(std::memory_order_relaxed);
        header.sessionInfoSequence.store(sequence + 1, std::memory_order_relaxed);
        std::atomic_thread_fence(std::memory_order_release);
        std::memcpy(header.sessionInfo, json.data(), bytes);
        header.sessionInfoBytes = uint32_t(bytes);
        header.sessionInfoSequence.store(sequence + 2, std::memory_order_release);
    }

    // xrWaitFrame: entry, after the simulator's own input and command handling,
    // and once the frame state is filled in.
    void WaitEnter(WaitSample& wait) {
        if (!Enabled()) return;
        wait.enterQpc = Qpc();
        wait.enterCycles = ThreadCycles();
        wait.threadId = GetCurrentThreadId();
        m_timeline.Header()->waitActiveSinceQpc.store(wait.enterQpc, std::memory_order_relaxed);
    }
    void WaitPace(WaitSample& wait) {
        if (!Enabled()) return;
        wait.paceQpc = Qpc();
    }
    void WaitExit(WaitSample& wait, uint64_t targetQpc, uint64_t exitQpc,
                  int64_t predictedDisplayTime, int64_t predictedDisplayPeriod, bool rebased) {
        if (!Enabled()) return;
        wait.targetQpc = targetQpc;
        wait.exitQpc = exitQpc;
        wait.exitCycles = ThreadCycles();
        wait.predictedDisplayTime = predictedDisplayTime;
        wait.predictedDisplayPeriod = predictedDisplayPeriod;
        wait.rebased = rebased;
        m_joiner.OnWait(wait);
        m_timeline.Header()->waitActiveSinceQpc.store(0, std::memory_order_relaxed);
    }

    void Begin() {
        if (Enabled()) m_joiner.OnBegin(Qpc());
    }

    void Locate(int64_t displayTime) {
        if (Enabled()) m_joiner.OnLocate(displayTime, Qpc());
    }

    // xrEndFrame entry. `frame` collects the stage stamps, flags and layer
    // counts until EndExit publishes it.
    void EndEnter(FrameRecord& frame, uint64_t enterQpc) {
        if (!Enabled()) return;
        frame.endEnterQpc = enterQpc;
        frame.endEnterCycles = ThreadCycles();
        frame.endThreadId = GetCurrentThreadId();
        m_timeline.Header()->endActiveSinceQpc.store(enterQpc, std::memory_order_relaxed);
    }

    void EndExit(FrameRecord& frame, int64_t submittedDisplayTime, uint64_t exitQpc) {
        if (!Enabled()) return;
        frame.endExitQpc = exitQpc;
        frame.endExitCycles = ThreadCycles();
        frame.submittedDisplayTime = submittedDisplayTime;
        WaitSample wait;
        if (m_joiner.TakeForEnd(submittedDisplayTime, wait)) {
            frame.flags |= kFrameWaitMatched;
            if (wait.beginQpc) frame.flags |= kFrameBeginSeen;
            if (wait.rebased) frame.flags |= kFramePacingRebased;
            frame.waitEnterQpc = wait.enterQpc;
            frame.waitPaceQpc = wait.paceQpc;
            frame.waitTargetQpc = wait.targetQpc;
            frame.waitExitQpc = wait.exitQpc;
            frame.beginQpc = wait.beginQpc;
            frame.locateQpc = wait.locateQpc;
            frame.predictedDisplayTime = wait.predictedDisplayTime;
            frame.predictedDisplayPeriod = wait.predictedDisplayPeriod;
            frame.waitEnterCycles = wait.enterCycles;
            frame.waitExitCycles = wait.exitCycles;
            frame.waitThreadId = wait.threadId;
        }
        m_timeline.PublishFrame(frame);
        TimelineHeader& header = *m_timeline.Header();
        header.endActiveSinceQpc.store(0, std::memory_order_relaxed);
        if ((frame.frameId & 255) == 0) Calibrate(header, exitQpc);
    }

    void Marker(MarkerKind kind, int64_t a = 0, int64_t b = 0, const char* text = nullptr) {
        if (Enabled()) m_timeline.PublishMarker(kind, Qpc(), GetCurrentThreadId(), a, b, text);
    }

private:
    void CloseLocked() {
        m_enabled.store(false, std::memory_order_release);
        if (m_view) UnmapViewOfFile(m_view);
        if (m_mapping) CloseHandle(m_mapping);
        m_view = nullptr;
        m_mapping = nullptr;
    }

    static void WriteProcessName(TimelineHeader& header) {
        wchar_t path[MAX_PATH]{};
        const DWORD length = GetModuleFileNameW(nullptr, path, MAX_PATH);
        if (!length || length >= MAX_PATH) return;
        const wchar_t* name = path;
        for (const wchar_t* c = path; *c; ++c) {
            if (*c == L'\\' || *c == L'/') name = c + 1;
        }
        std::memset(header.processName, 0, sizeof(header.processName));
        WideCharToMultiByte(CP_UTF8, 0, name, -1, header.processName,
                            int(sizeof(header.processName) - 1), nullptr, nullptr);
    }

    // QueryThreadCycleTime counts in TSC ticks. Their rate is measured against
    // QPC over the whole run, so it only gets more exact.
    void Calibrate(TimelineHeader& header, uint64_t nowQpc) {
        const uint64_t elapsedQpc = nowQpc - m_calibrationQpc;
        if (nowQpc <= m_calibrationQpc || elapsedQpc < m_qpcFrequency / 2) return;
        const double ticks = double(__rdtsc() - m_calibrationTsc);
        const double seconds = double(elapsedQpc) / double(m_qpcFrequency);
        header.tscFrequency.store(uint64_t(ticks / seconds), std::memory_order_relaxed);
    }

    std::mutex m_lifecycle;
    std::atomic<bool> m_enabled{false};
    HANDLE m_mapping = nullptr;
    void* m_view = nullptr;
    Timeline m_timeline;
    FrameJoiner m_joiner;
    uint64_t m_qpcFrequency = 1;
    uint64_t m_calibrationQpc = 0;
    uint64_t m_calibrationTsc = 0;
};

} // namespace perf::timeline
