#include "frame_timeline.h"

// These checks must also run in the Release configuration used for the DLL.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <atomic>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

using namespace perf::timeline;

struct Buffer {
    Buffer(uint32_t frames, uint32_t markers) : bytes(TimelineBytes(frames, markers)), storage(bytes / 8 + 1) {}
    void* data() { return storage.data(); }
    size_t bytes;
    std::vector<uint64_t> storage;  // 8-byte aligned, zeroed like a fresh mapping
};

// Every field of record `i` derives from `i`, so a record stitched together
// from two different publishes cannot pass.
FrameRecord Synthetic(uint64_t i) {
    FrameRecord r{};
    r.frameId = i * 3 + 1;
    r.waitEnterQpc = i * 7 + 2;
    r.endEnterQpc = i * 11 + 3;
    r.endExitQpc = i * 13 + 4;
    r.predictedDisplayTime = int64_t(i * 17 + 5);
    r.endExitCycles = i * 19 + 6;
    r.flags = uint32_t(i * 23 + 7);
    r.reserved = ~i;
    return r;
}

bool Consistent(const FrameRecord& r) {
    const uint64_t i = r.index;
    return r.frameId == i * 3 + 1 && r.waitEnterQpc == i * 7 + 2 && r.endEnterQpc == i * 11 + 3 &&
           r.endExitQpc == i * 13 + 4 && r.predictedDisplayTime == int64_t(i * 17 + 5) &&
           r.endExitCycles == i * 19 + 6 && r.flags == uint32_t(i * 23 + 7) && r.reserved == ~i;
}

void ReadsBackInOrder() {
    Buffer buffer(16, 8);
    Timeline timeline;
    assert(timeline.Attach(buffer.data(), buffer.bytes, 16, 8, 1234, 10000000, 99, 90.0));
    const TimelineHeader& h = *timeline.Header();
    assert(h.magic == kMagic && h.version == kVersion && h.generation == 1);
    assert(h.frameOffset == kHeaderBytes && h.markerOffset == kHeaderBytes + 16 * sizeof(FrameRecord));
    assert(h.processId == 1234 && h.createdQpc == 99 && h.refreshHz == 90.0);

    for (uint64_t i = 0; i < 10; ++i) timeline.PublishFrame(Synthetic(i));
    std::vector<FrameRecord> frames;
    ReadResult read = timeline.ReadFrames(0, frames);
    assert(read.next == 10 && read.lost == 0 && frames.size() == 10);
    for (uint64_t i = 0; i < 10; ++i) assert(frames[i].index == i && Consistent(frames[i]));
    assert(h.lastEndExitQpc == Synthetic(9).endExitQpc);

    frames.clear();
    read = timeline.ReadFrames(10, frames);
    assert(read.next == 10 && read.lost == 0 && frames.empty());

    // Too small for its own layout.
    Timeline tooSmall;
    assert(!tooSmall.Attach(buffer.data(), buffer.bytes, 64, 8, 1, 1, 1, 90.0));
}

void ReportsOverwrittenRecordsAsLost() {
    Buffer buffer(16, 8);
    Timeline timeline;
    assert(timeline.Attach(buffer.data(), buffer.bytes, 16, 8, 1, 1, 1, 90.0));
    for (uint64_t i = 0; i < 40; ++i) timeline.PublishFrame(Synthetic(i));

    // The slot of the oldest record is the one the next publish reuses, so a
    // reader only ever claims capacity - 1 of them.
    std::vector<FrameRecord> frames;
    ReadResult read = timeline.ReadFrames(0, frames);
    assert(read.next == 40 && read.lost == 25 && frames.size() == 15);
    for (size_t k = 0; k < frames.size(); ++k) {
        assert(frames[k].index == 25 + k && Consistent(frames[k]));
    }

    frames.clear();
    read = timeline.ReadFrames(38, frames);
    assert(read.lost == 0 && frames.size() == 2 && frames[0].index == 38);
}

void ConcurrentReadsNeverTear() {
    // A tiny ring and a writer that never pauses: readers are lapped constantly,
    // which is exactly when a torn or overwritten record could slip through.
    constexpr uint32_t kCapacity = 64;
    constexpr uint64_t kRecords = 400000;
    Buffer buffer(kCapacity, 8);
    Timeline timeline;
    assert(timeline.Attach(buffer.data(), buffer.bytes, kCapacity, 8, 1, 1, 1, 90.0));

    std::atomic<bool> done{false};
    std::atomic<uint64_t> torn{0}, delivered{0}, lost{0}, outOfOrder{0};
    auto reader = [&] {
        uint64_t next = 0;
        std::vector<FrameRecord> frames;
        while (true) {
            const bool last = done.load();
            frames.clear();
            const ReadResult read = timeline.ReadFrames(next, frames);
            uint64_t expect = next;
            for (const FrameRecord& r : frames) {
                if (!Consistent(r)) ++torn;
                if (r.index < expect) ++outOfOrder;
                expect = r.index + 1;
            }
            delivered += frames.size();
            lost += read.lost;
            next = read.next;
            if (last) break;
        }
        assert(next == kRecords);
    };
    std::thread readers[] = {std::thread(reader), std::thread(reader)};
    for (uint64_t i = 0; i < kRecords; ++i) timeline.PublishFrame(Synthetic(i));
    done = true;
    for (auto& t : readers) t.join();

    assert(torn == 0);
    assert(outOfOrder == 0);
    // Each reader accounts for every record exactly once: read or reported lost.
    assert(delivered + lost == 2 * kRecords);
    std::cout << "  concurrent: " << delivered << " delivered, " << lost << " lapped, 0 torn\n";
}

void ReattachContinuesOrReformats() {
    Buffer buffer(16, 8);
    {
        Timeline first;
        assert(first.Attach(buffer.data(), buffer.bytes, 16, 8, 1, 1, 50, 90.0));
        for (uint64_t i = 0; i < 5; ++i) first.PublishFrame(Synthetic(i));
        first.PublishMarker(MarkerKind::User, 7, 1, 0, 0, "before reload");
    }
    // A DLL reload rejoins the same mapping: indices carry on, history stays.
    Timeline second;
    assert(second.Attach(buffer.data(), buffer.bytes, 16, 8, 2, 1, 60, 120.0));
    const TimelineHeader& h = *second.Header();
    assert(h.generation == 2 && h.frameCount == 5 && h.markerCount == 1);
    assert(h.createdQpc == 50 && h.processId == 2 && h.refreshHz == 120.0);
    second.PublishFrame(Synthetic(5));
    std::vector<FrameRecord> frames;
    second.ReadFrames(0, frames);
    assert(frames.size() == 6 && frames[5].index == 5 && Consistent(frames[5]));

    // A different layout cannot be continued, so it starts over.
    Timeline third;
    assert(third.Attach(buffer.data(), buffer.bytes, 8, 8, 3, 1, 70, 90.0));
    assert(third.Header()->generation == 1 && third.Header()->frameCount == 0);
    assert(third.Header()->createdQpc == 70);
}

void MarkersFromManyThreads() {
    Buffer buffer(16, 8192);
    Timeline timeline;
    assert(timeline.Attach(buffer.data(), buffer.bytes, 16, 8192, 1, 1, 1, 90.0));
    FrameRecord frame = Synthetic(0);
    timeline.PublishFrame(frame);

    std::vector<std::thread> writers;
    for (int t = 0; t < 4; ++t) {
        writers.emplace_back([&timeline, t] {
            for (int i = 0; i < 1000; ++i) {
                timeline.PublishMarker(MarkerKind::UiCommand, uint64_t(i + 1), uint32_t(t), t, i, "menu");
            }
        });
    }
    for (auto& w : writers) w.join();

    std::vector<MarkerRecord> markers;
    const ReadResult read = timeline.ReadMarkers(0, markers);
    assert(read.next == 4000 && read.lost == 0 && markers.size() == 4000);
    int perThread[4] = {};
    for (size_t k = 0; k < markers.size(); ++k) {
        const MarkerRecord& m = markers[k];
        assert(m.index == k);
        assert(m.kind == uint32_t(MarkerKind::UiCommand));
        assert(m.frameId == frame.frameId);
        assert(std::string(m.text) == "menu");
        assert(m.a == int64_t(m.threadId) && m.b == perThread[m.threadId]);
        ++perThread[m.threadId];
    }

    const std::string longText(200, 'x');
    timeline.PublishMarker(MarkerKind::User, 1, 1, 0, 0, longText.c_str());
    markers.clear();
    timeline.ReadMarkers(4000, markers);
    assert(markers.size() == 1 && std::string(markers[0].text) == longText.substr(0, 79));
}

WaitSample Wait(int64_t displayTime, uint64_t enterQpc) {
    WaitSample w;
    w.enterQpc = enterQpc;
    w.predictedDisplayTime = displayTime;
    return w;
}

void JoinsSubmissionsToTheirWait() {
    FrameJoiner joiner;
    WaitSample out;

    // Plain loop: wait, begin, locate, end.
    joiner.OnWait(Wait(1000, 1));
    joiner.OnBegin(2);
    joiner.OnLocate(1000, 3);
    joiner.OnLocate(1000, 4);  // only the first sample of the pose counts
    assert(joiner.TakeForEnd(1000, out));
    assert(out.enterQpc == 1 && out.beginQpc == 2 && out.locateQpc == 3);
    assert(joiner.Pending() == 0);

    // Pipelined: the game thread waits for N+1 before the render thread ends N.
    joiner.OnWait(Wait(2000, 10));
    joiner.OnBegin(11);
    joiner.OnWait(Wait(3000, 12));
    assert(joiner.TakeForEnd(2000, out) && out.enterQpc == 10 && out.beginQpc == 11);
    joiner.OnBegin(13);
    assert(joiner.TakeForEnd(3000, out) && out.enterQpc == 12 && out.beginQpc == 13);

    // A frame the app waited for but never submitted is dropped with the match.
    joiner.OnWait(Wait(4000, 20));
    joiner.OnWait(Wait(5000, 21));
    assert(joiner.TakeForEnd(5000, out) && out.enterQpc == 21);
    assert(joiner.Pending() == 0);
    assert(!joiner.TakeForEnd(4000, out));

    // A display time no wait predicted (an app inventing its own) matches
    // nothing and leaves the pending waits in place.
    joiner.OnWait(Wait(6000, 30));
    assert(!joiner.TakeForEnd(5999, out));
    assert(joiner.Pending() == 1);
    assert(joiner.TakeForEnd(6000, out) && out.enterQpc == 30);

    // Bounded: an app that never submits cannot grow the table.
    for (int i = 0; i < 20; ++i) joiner.OnWait(Wait(7000 + i, 40 + i));
    assert(joiner.Pending() == FrameJoiner::kCapacity);
    assert(!joiner.TakeForEnd(7000, out));
    assert(joiner.TakeForEnd(7019, out) && out.enterQpc == 59);
}

void RecorderPublishesThroughTheMapping() {
    const std::wstring name = L"Local\\OpenXRSimulator.FrameTimelineTest." +
                              std::to_wstring(GetCurrentProcessId());
    Recorder recorder;
    // Before Open every entry point is a no-op.
    WaitSample idle;
    recorder.WaitEnter(idle);
    recorder.Marker(MarkerKind::User);
    assert(!recorder.Enabled() && idle.enterQpc == 0);

    assert(recorder.Open(90.0, 256, 64, name.c_str()));
    recorder.SetApplicationName("frame_timeline_tests");
    recorder.Marker(MarkerKind::SessionState, 5);

    // A viewer in another process maps the same name read-only.
    HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, name.c_str());
    assert(mapping);
    const void* view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
    assert(view);
    const auto* header = static_cast<const TimelineHeader*>(view);
    assert(header->magic == kMagic && header->frameCapacity == 256 && header->markerCapacity == 64);
    assert(std::string(header->processName) == "frame_timeline_tests.exe");
    assert(std::string(header->applicationName) == "frame_timeline_tests");
    const auto* frames = reinterpret_cast<const FrameRecord*>(
        static_cast<const char*>(view) + header->frameOffset);

    // Wait on one thread, submit on another, as UE does.
    WaitSample wait;
    std::thread game([&] {
        recorder.WaitEnter(wait);
        assert(header->waitActiveSinceQpc == wait.enterQpc);
        recorder.WaitPace(wait);
        Sleep(2);
        recorder.WaitExit(wait, Qpc(), Qpc(), 123456789, 11111111, true);
        assert(header->waitActiveSinceQpc == 0);
    });
    game.join();
    recorder.Begin();
    recorder.Locate(123456789);

    FrameRecord frame{};
    frame.frameId = 1;
    frame.flags = kFrameStereoProjection;
    frame.layerCount = 2;
    recorder.EndEnter(frame, Qpc());
    assert(header->endActiveSinceQpc == frame.endEnterQpc);
    frame.endProjectionQpc = frame.endOverlaysQpc = frame.endPaintQpc = Qpc();
    recorder.EndExit(frame, 123456789, Qpc());
    assert(header->endActiveSinceQpc == 0);

    std::vector<FrameRecord> read;
    ReadRing(header->frameCount, frames, header->frameCapacity, 0, read);
    assert(read.size() == 1);
    const FrameRecord& r = read[0];
    assert(r.frameId == 1 && r.layerCount == 2);
    assert(r.flags == (kFrameStereoProjection | kFrameWaitMatched | kFrameBeginSeen | kFramePacingRebased));
    assert(r.waitThreadId != 0 && r.endThreadId == GetCurrentThreadId() && r.waitThreadId != r.endThreadId);
    assert(r.predictedDisplayTime == 123456789 && r.submittedDisplayTime == 123456789);
    assert(r.predictedDisplayPeriod == 11111111);
    assert(r.waitEnterQpc <= r.waitPaceQpc && r.waitPaceQpc < r.waitExitQpc);
    assert(r.waitExitQpc <= r.beginQpc && r.beginQpc <= r.locateQpc && r.locateQpc <= r.endEnterQpc);
    assert(r.endEnterQpc <= r.endPaintQpc && r.endPaintQpc <= r.endExitQpc);
    assert(r.waitExitCycles >= r.waitEnterCycles && r.endExitCycles >= r.endEnterCycles);
    assert(header->lastEndExitQpc == r.endExitQpc);

    // A submission whose displayTime this runtime never predicted is still
    // recorded, just without wait data.
    FrameRecord stale{};
    stale.frameId = 2;
    recorder.EndEnter(stale, Qpc());
    recorder.EndExit(stale, 42, Qpc());
    read.clear();
    ReadRing(header->frameCount, frames, header->frameCapacity, 1, read);
    assert(read.size() == 1 && read[0].flags == 0 && read[0].waitEnterQpc == 0);

    // Session info: what the frames ran on, readable whole or not at all.
    assert(ReadSessionInfo(*header) == "{}");
    recorder.SetSessionInfo("{\"gpu\": {\"name\": \"Test GPU\"}}");
    assert(ReadSessionInfo(*header) == "{\"gpu\": {\"name\": \"Test GPU\"}}");
    assert(header->sessionInfoSequence % 2 == 0);
    recorder.SetSessionInfo(std::string(4096, 'x'));  // too big for its slot: published as nothing
    assert(ReadSessionInfo(*header) == "{}");

    const auto* markers = reinterpret_cast<const MarkerRecord*>(
        static_cast<const char*>(view) + header->markerOffset);
    std::vector<MarkerRecord> markerRead;
    ReadRing(header->markerCount, markers, header->markerCapacity, 0, markerRead);
    assert(markerRead.size() == 1 && markerRead[0].kind == uint32_t(MarkerKind::SessionState));
    assert(markerRead[0].a == 5 && markerRead[0].frameId == 0);

    UnmapViewOfFile(view);
    CloseHandle(mapping);
}

// The reader converts QueryThreadCycleTime spans with a TSC rate measured
// against QPC. That only holds if the thread cycle counter runs on the TSC, so
// check it: a thread spinning flat out accrues about its wall time in cycles.
void ThreadCyclesRunOnTheTsc() {
    const std::wstring name = L"Local\\OpenXRSimulator.FrameTimelineCalibration." +
                              std::to_wstring(GetCurrentProcessId());
    Recorder recorder;
    assert(recorder.Open(90.0, 256, 64, name.c_str()));

    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    const uint64_t qpc0 = Qpc(), tsc0 = __rdtsc(), cycles0 = ThreadCycles();
    while (Qpc() - qpc0 < uint64_t(frequency.QuadPart) * 6 / 10) {}
    const uint64_t qpc1 = Qpc(), tsc1 = __rdtsc(), cycles1 = ThreadCycles();
    const double seconds = double(qpc1 - qpc0) / double(frequency.QuadPart);
    const double tscHz = double(tsc1 - tsc0) / seconds;
    const double busy = double(cycles1 - cycles0) / tscHz / seconds;
    std::cout << "  thread cycles cover " << busy * 100.0 << "% of a spinning thread's wall time\n";
    // Preemption can only lower it; a counter at the core clock would overshoot.
    assert(busy > 0.5 && busy < 1.05);

    // Calibration runs on every 256th frame once half a second has passed.
    FrameRecord frame{};
    frame.frameId = 256;
    recorder.EndEnter(frame, Qpc());
    recorder.EndExit(frame, 0, Qpc());
    const double calibrated = double(recorder.Ring().Header()->tscFrequency.load());
    assert(calibrated > tscHz * 0.99 && calibrated < tscHz * 1.01);
}

} // namespace

int main() {
    ReadsBackInOrder();
    ReportsOverwrittenRecordsAsLost();
    ConcurrentReadsNeverTear();
    ReattachContinuesOrReformats();
    MarkersFromManyThreads();
    JoinsSubmissionsToTheirWait();
    RecorderPublishesThroughTheMapping();
    ThreadCyclesRunOnTheTsc();
    std::cout << "frame timeline tests passed\n";
    return 0;
}
