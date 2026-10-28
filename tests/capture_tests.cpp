#include "capture_report.h"
#include "json.h"

// These checks must also run in the Release configuration used for the DLL.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

namespace {

using namespace perf;

constexpr uint64_t kQpc = 10000000;
constexpr uint64_t kTsc = 3000000000;
constexpr uint64_t kOrigin = 5000000;

uint64_t Ticks(double ms) { return uint64_t(std::llround(ms * double(kQpc) / 1000.0)); }

// The simulator's 90 Hz pacing around a single-threaded app (see frame_analysis_tests).
std::vector<timeline::FrameRecord> Frames(const std::vector<double>& workMs) {
    const uint64_t period = Ticks(1000.0 / 90.0);
    uint64_t now = kOrigin, tick = kOrigin, cycles = 0;
    std::vector<timeline::FrameRecord> out;
    for (size_t i = 0; i < workMs.size(); ++i) {
        timeline::FrameRecord r{};
        r.index = i;
        r.frameId = i + 1;
        r.waitThreadId = r.endThreadId = 7;
        r.flags = timeline::kFrameWaitMatched | timeline::kFrameBeginSeen | timeline::kFrameStereoProjection;
        r.waitEnterQpc = r.waitPaceQpc = now;
        r.waitEnterCycles = cycles;
        r.waitTargetQpc = tick;
        if (now < tick) now = tick;
        r.waitExitQpc = r.beginQpc = now;
        r.waitExitCycles = cycles;
        tick += period;
        if (now > tick + period) tick = now;
        now += Ticks(workMs[i]);
        cycles += uint64_t(workMs[i] * double(kTsc) / 1000.0);
        r.endEnterQpc = now;
        r.endEnterCycles = cycles;
        now += Ticks(0.2);
        cycles += uint64_t(0.2 * double(kTsc) / 1000.0);
        r.endExitQpc = now;
        r.endExitCycles = cycles;
        out.push_back(r);
    }
    return out;
}

std::wstring TempPath(const wchar_t* name) {
    wchar_t dir[MAX_PATH];
    GetTempPathW(MAX_PATH, dir);
    return std::wstring(dir) + L"simxr-capture-test-" + std::to_wstring(GetCurrentProcessId()) + L"-" + name;
}

capture::Capture Sample() {
    std::vector<double> work(300, 5.0);
    work[100] = 30.0;  // a hitch
    work[200] = 15.0;  // over budget
    capture::Capture c;
    c.header = capture::MakeHeader(kQpc, kOrigin, kTsc, 90.0);
    c.metadata = "{\"capture\": {\"label\": " + capture::JsonString("quote \" backslash \\ tab\t") +
                 ", \"frames\": 300}, \"process\": {\"name\": \"game.exe\"}}";
    c.frames = Frames(work);
    timeline::MarkerRecord marker{};
    marker.kind = uint32_t(timeline::MarkerKind::User);
    marker.qpc = c.frames[150].endExitQpc;
    marker.frameId = 151;
    snprintf(marker.text, sizeof(marker.text), "boss fight");
    c.markers.push_back(marker);
    // The GPU busy 4 ms of every frame, 12 ms on frame 250.
    for (const timeline::FrameRecord& f : c.frames) {
        timeline::GpuRecord g{};
        g.frameId = f.frameId;
        g.tickFrequency = 1000000000ull;
        g.beginTicks = f.waitExitQpc * 100;
        g.endTicks = g.beginTicks + (f.frameId == 250 ? 12000000ull : 4000000ull);
        g.api = uint32_t(timeline::GpuApi::Vulkan);
        c.gpu.push_back(g);
    }
    return c;
}

void RoundTrips() {
    capture::Capture written = Sample();
    const std::wstring path = TempPath(L"roundtrip.xrft");
    std::string error;
    assert(capture::WriteCapture(path, written, error));

    capture::Capture read;
    assert(capture::ReadCapture(path, read, error));
    assert(std::memcmp(&read.header, &written.header, sizeof(capture::CaptureHeader)) == 0);
    assert(read.metadata == written.metadata);
    assert(read.frames.size() == 300 && read.markers.size() == 1);
    assert(std::memcmp(read.frames.data(), written.frames.data(), 300 * sizeof(timeline::FrameRecord)) == 0);
    assert(std::string(read.markers[0].text) == "boss fight");
    assert(read.gpu.size() == 300 && read.header.gpuCount == 300 && read.header.gpuRecordBytes == sizeof(timeline::GpuRecord));
    assert(std::memcmp(read.gpu.data(), written.gpu.data(), 300 * sizeof(timeline::GpuRecord)) == 0);

    // A capture without GPU timing (as every capture before it was) reads with none.
    capture::Capture plain = Sample();
    plain.gpu.clear();
    assert(capture::WriteCapture(path, plain, error) && capture::ReadCapture(path, read, error));
    assert(read.gpu.empty() && read.header.gpuCount == 0 && read.frames.size() == 300);

    // The metadata is valid JSON, escapes included.
    json::Object meta(read.metadata.c_str());
    assert(meta.object("capture").string("label") == "quote \" backslash \\ tab\t");
    assert(meta.object("process").string("name") == "game.exe");

    capture::Capture listed;
    assert(capture::ReadCapture(path, listed, error, true));
    assert(listed.metadata == written.metadata && listed.frames.empty() && listed.header.frameCount == 300);
    DeleteFileW(path.c_str());
}

void RejectsDamagedFiles() {
    capture::Capture c = Sample();
    const std::wstring path = TempPath(L"damaged.xrft");
    std::string error;
    assert(capture::WriteCapture(path, c, error));

    // Cut off the last record: the header promises more than the file holds.
    {
        std::ifstream in(path, std::ios::binary);
        std::string bytes((std::istreambuf_iterator<char>(in)), {});
        in.close();
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out.write(bytes.data(), std::streamsize(bytes.size() - 100));
    }
    capture::Capture read;
    assert(!capture::ReadCapture(path, read, error) && error.find("truncated") != std::string::npos);

    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out << "this is not a capture at all, just text";
    }
    assert(!capture::ReadCapture(path, read, error) && error.find("not a frame-timing capture") != std::string::npos);

    capture::Capture future = Sample();
    future.header.version = capture::kVersion + 1;
    assert(capture::WriteCapture(path, future, error));
    assert(!capture::ReadCapture(path, read, error) && error.find("incompatible") != std::string::npos);
    DeleteFileW(path.c_str());

    assert(!capture::ReadCapture(TempPath(L"missing.xrft"), read, error));
}

void SummaryMatchesTheAnalyzer() {
    const capture::Capture c = Sample();
    analysis::Analyzer a;
    report::Analyze(c, a, 0.0, 2.0);
    const std::string summary = report::SummaryJson(a, 0, a.Size(), "  \"capture\": {\"label\": \"x\"}", 5);
    json::Object j(summary.c_str());
    assert(j.valid());
    const analysis::RangeStats s = a.Stats(0, a.Size());
    assert(j.number("frames", -1) == int(s.frames) && s.frames == 299);
    assert(j.number("hitches", -1) == 1);
    // Two late on the CPU, and frame 250 late on the GPU alone.
    assert(j.object("over_budget").number("frames", -1) == 3);
    assert(j.object("late_causes").number("gpu", -1) == 1);
    assert(std::abs(j.object("frame_time_ms").number("max", 0.0) - 30.2) < 0.01);
    assert(std::abs(j.number("target_hz", 0.0) - 90.0) < 1e-6);
    assert(j.object("late_causes").number("app_busy", -1) == 2);
    assert(j.object("capture").string("label") == "x");
    // The worst frame leads the list.
    assert(summary.find("\"worst_frames\": [\n    {\"frame\": 101,") != std::string::npos);
    assert(summary.find("\"kind\": \"user\"") != std::string::npos && summary.find("boss fight") != std::string::npos);
    json::Object gpu = j.object("gpu");
    assert(gpu.string("api") == "Vulkan" && gpu.number("frames", 0) == 299 && gpu.number("over_budget", -1) == 1);
    assert(std::abs(gpu.number("p50_ms", 0.0) - 4.0) < 0.01 && std::abs(gpu.number("max_ms", 0.0) - 12.0) < 0.01);

    // Re-scored at 120 Hz from the same capture.
    analysis::Analyzer fast;
    report::Analyze(c, fast, 120.0, 2.0);
    json::Object k(report::SummaryJson(fast, 0, fast.Size(), "").c_str());
    assert(std::abs(k.number("target_hz", 0.0) - 120.0) < 1e-6);
}

void CsvHasARowPerFrame() {
    const capture::Capture c = Sample();
    analysis::Analyzer a;
    report::Analyze(c, a, 0.0, 2.0);
    const std::wstring path = TempPath(L"frames.csv");
    std::string error;
    assert(report::WriteCsv(path, a, 0, a.Size(), error));
    std::ifstream in(path);
    std::string line;
    size_t rows = 0;
    std::getline(in, line);
    assert(line.rfind("frame,time_s,class,cause,frame_time_ms", 0) == 0);
    std::string hitchRow;
    while (std::getline(in, line)) {
        if (rows == 100) hitchRow = line;
        ++rows;
    }
    assert(rows == 300);
    assert(hitchRow.rfind("101,", 0) == 0 && hitchRow.find(",hitch,app busy,30.2") != std::string::npos);
    in.close();
    DeleteFileW(path.c_str());
}

} // namespace

int main() {
    RoundTrips();
    RejectsDamagedFiles();
    SummaryMatchesTheAnalyzer();
    CsvHasARowPerFrame();
    std::cout << "capture tests passed\n";
    return 0;
}
