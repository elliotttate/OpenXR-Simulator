#include "headless.h"

#include "capture_report.h"
#include "capture_session.h"

#include <cstdio>
#include <cwchar>
#include <map>
#include <string>
#include <vector>

namespace viewer {

namespace {

namespace an = perf::analysis;
namespace cap = perf::capture;
namespace rep = perf::report;

struct Args {
    std::wstring mode;
    std::map<std::wstring, std::wstring> options;  // --name value
    std::vector<std::wstring> positional;

    std::wstring Get(const wchar_t* name, const wchar_t* fallback = L"") const {
        const auto it = options.find(name);
        return it == options.end() ? fallback : it->second;
    }
    double Number(const wchar_t* name, double fallback) const {
        const auto it = options.find(name);
        return it == options.end() ? fallback : wcstod(it->second.c_str(), nullptr);
    }
};

constexpr const wchar_t* kModes[] = {L"--list-sessions", L"--live-summary", L"--record", L"--analyze", L"--export-csv"};
constexpr const wchar_t* kValueOptions[] = {L"--pid", L"--seconds", L"--out", L"--label", L"--stop-file",
                                            L"--json", L"--hz", L"--hitch-budgets", L"--top"};

bool Parse(int argc, wchar_t** argv, Args& args) {
    for (int i = 1; i < argc; ++i) {
        const std::wstring arg = argv[i];
        bool isMode = false;
        for (const wchar_t* mode : kModes) isMode |= arg == mode;
        bool takesValue = false;
        for (const wchar_t* option : kValueOptions) takesValue |= arg == option;
        if (isMode) {
            args.mode = arg;
        } else if (takesValue && i + 1 < argc) {
            args.options[arg] = argv[++i];
        } else {
            args.positional.push_back(arg);
        }
    }
    return !args.mode.empty();
}

// The result goes to --json if given, else stdout - the parent's pipe, or the
// console the command was typed into.
bool Emit(const Args& args, const std::string& text) {
    const std::wstring path = args.Get(L"--json");
    if (!path.empty()) {
        FILE* file = nullptr;
        if (_wfopen_s(&file, path.c_str(), L"wb") != 0 || !file) return false;
        fwrite(text.data(), 1, text.size(), file);
        return fclose(file) == 0;
    }
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (!out || out == INVALID_HANDLE_VALUE) {
        if (AttachConsole(ATTACH_PARENT_PROCESS)) {
            out = CreateFileW(L"CONOUT$", GENERIC_WRITE, FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        }
    }
    DWORD written = 0;
    return out && out != INVALID_HANDLE_VALUE && WriteFile(out, text.data(), DWORD(text.size()), &written, nullptr);
}

int Fail(const Args& args, const std::string& message, int code = 3) {
    Emit(args, "{\"error\": " + cap::JsonString(message) + "}\n");
    return code;
}

void Score(const Args& args, double& hz, double& hitchBudgets) {
    hz = args.Number(L"--hz", 0.0);
    hitchBudgets = args.Number(L"--hitch-budgets", 2.0);
}

int ListSessions(const Args& args) {
    std::string j = "{\"sessions\": [";
    bool first = true;
    for (const SessionInfo& s : DiscoverSessions()) {
        j += first ? "\n  " : ",\n  ";
        first = false;
        char numbers[96];
        snprintf(numbers, sizeof(numbers), "\"pid\": %lu, \"refresh_hz\": %g, \"frames\": %llu", s.pid, s.refreshHz,
                 (unsigned long long)s.frames);
        j += std::string("{") + numbers + ", \"process\": " + cap::JsonString(s.processName) +
             ", \"application\": " + cap::JsonString(s.applicationName) + "}";
    }
    j += first ? "]}\n" : "\n]}\n";
    return Emit(args, j) ? 0 : 3;
}

bool AttachWithRetry(TimelineClient& client, DWORD pid, std::string& error, double waitSeconds) {
    const ULONGLONG deadline = GetTickCount64() + ULONGLONG(waitSeconds * 1000.0);
    while (!client.Attach(pid, error)) {
        if (GetTickCount64() >= deadline) return false;
        Sleep(200);
    }
    return true;
}

std::string ProcessJson(const TimelineHeader& h, DWORD pid) {
    return "{\"name\": " + cap::JsonString(HeaderString(h.processName, sizeof(h.processName))) +
           ", \"application\": " + cap::JsonString(HeaderString(h.applicationName, sizeof(h.applicationName))) +
           ", \"pid\": " + std::to_string(pid) + "}";
}

int LiveSummary(const Args& args) {
    const DWORD pid = DWORD(args.Number(L"--pid", 0));
    if (!pid) return Fail(args, "--live-summary needs --pid", 2);
    TimelineClient client;
    std::string error;
    if (!client.Attach(pid, error)) return Fail(args, error);
    std::vector<FrameRecord> frames;
    std::vector<MarkerRecord> markers;
    std::vector<GpuRecord> gpu;
    client.Poll(frames, markers, gpu);  // everything still in the ring
    const TimelineHeader& h = *client.Header();
    double hz = 0.0, hitchBudgets = 2.0;
    Score(args, hz, hitchBudgets);
    an::Analyzer a;
    a.Reset(h.qpcFrequency, h.createdQpc);
    a.SetTscFrequency(h.tscFrequency.load());
    a.SetBudget(hz > 0.0 ? hz : h.refreshHz, hitchBudgets);
    a.AddMarkers(markers);
    a.AddFrames(frames);
    a.AddGpu(gpu);
    const double seconds = args.Number(L"--seconds", 10.0);
    const double end = a.Size() ? a.Metrics().back().endSec : 0.0;
    const size_t begin = a.LowerBound(an::XAxis::Time, end - seconds);
    const std::string extra = "  \"process\": " + ProcessJson(h, pid) + ",\n  \"session\": " +
                              perf::timeline::ReadSessionInfo(h) + ",\n  \"process_alive\": " +
                              (client.ProcessAlive() ? "true" : "false");
    return Emit(args, rep::SummaryJson(a, begin, a.Size(), extra, size_t(args.Number(L"--top", 10)))) ? 0 : 3;
}

int Record(const Args& args) {
    const DWORD pid = DWORD(args.Number(L"--pid", 0));
    if (!pid) return Fail(args, "--record needs --pid", 2);
    TimelineClient client;
    std::string error;
    if (!AttachWithRetry(client, pid, error, 10.0)) return Fail(args, error);
    const std::wstring stopFile = args.Get(L"--stop-file");
    const double limit = args.Number(L"--seconds", 600.0);
    const std::string label = Narrow(args.Get(L"--label"));
    CaptureDescription description;
    description.label = label;
    description.recordedBy = "recorder";
    description.startedUtc = UtcNow();
    description.pid = pid;

    client.SkipToNow(1);
    std::vector<FrameRecord> frames;
    std::vector<MarkerRecord> markers;
    std::vector<GpuRecord> gpu;
    const ULONGLONG started = GetTickCount64();
    bool stopped = false;
    while (!stopped) {
        Sleep(100);
        stopped = (!stopFile.empty() && GetFileAttributesW(stopFile.c_str()) != INVALID_FILE_ATTRIBUTES) ||
                  double(GetTickCount64() - started) / 1000.0 >= limit || !client.ProcessAlive();
        const auto read = client.Poll(frames, markers, gpu);  // one more read after the stop condition
        if (read.restarted) return Fail(args, "the app's frame timeline restarted during the recording");
        description.lostFrames += read.lostFrames;
    }
    if (!stopFile.empty()) DeleteFileW(stopFile.c_str());
    // The GPU finishes the last frames after the CPU submitted them: wait for
    // their results, without keeping the frames that arrived meanwhile.
    const size_t stoppedAt = frames.size();
    Sleep(150);
    client.Poll(frames, markers, gpu);
    frames.resize(stoppedAt);

    cap::Capture capture = MakeCapture(*client.Header(), description, std::move(frames), markers, gpu);
    std::wstring out = args.Get(L"--out");
    if (out.empty()) out = UniqueCapturePath(CapturesDirectory(), CaptureFileName(HeaderString(client.Header()->processName, 64), label));
    if (!cap::WriteCapture(out, capture, error)) return Fail(args, error);
    char numbers[128];
    snprintf(numbers, sizeof(numbers), "\"frames\": %zu, \"markers\": %zu, \"lost_frames\": %llu",
             capture.frames.size(), capture.markers.size(), (unsigned long long)description.lostFrames);
    return Emit(args, "{\"path\": " + cap::JsonString(Narrow(out)) + ", " + numbers + "}\n") ? 0 : 3;
}

bool Load(const Args& args, cap::Capture& capture, an::Analyzer& a, std::string& error) {
    if (args.positional.empty()) {
        error = "name a capture file";
        return false;
    }
    if (!cap::ReadCapture(args.positional[0], capture, error)) return false;
    double hz = 0.0, hitchBudgets = 2.0;
    Score(args, hz, hitchBudgets);
    rep::Analyze(capture, a, hz, hitchBudgets);
    return true;
}

int Analyze(const Args& args) {
    cap::Capture capture;
    an::Analyzer a;
    std::string error;
    if (!Load(args, capture, a, error)) return Fail(args, error);
    const std::string metadata = capture.metadata.empty() ? "{}" : capture.metadata;
    const std::string extra = "  \"capture\": {\"path\": " + cap::JsonString(Narrow(args.positional[0])) +
                              ", \"recorded_at_hz\": " + rep::Number(capture.header.refreshHz, "%g") +
                              ", \"metadata\": " + metadata + "}";
    return Emit(args, rep::SummaryJson(a, 0, a.Size(), extra, size_t(args.Number(L"--top", 20)))) ? 0 : 3;
}

int ExportCsv(const Args& args) {
    cap::Capture capture;
    an::Analyzer a;
    std::string error;
    if (!Load(args, capture, a, error)) return Fail(args, error);
    std::wstring out = args.Get(L"--out");
    if (out.empty()) {
        out = args.positional[0];
        const size_t dot = out.find_last_of(L'.');
        out = (dot == std::wstring::npos ? out : out.substr(0, dot)) + L".csv";
    }
    if (!rep::WriteCsv(out, a, 0, a.Size(), error)) return Fail(args, error);
    return Emit(args, "{\"path\": " + cap::JsonString(Narrow(out)) + ", \"rows\": " + std::to_string(a.Size()) + "}\n") ? 0 : 3;
}

} // namespace

bool RunHeadless(int argc, wchar_t** argv, int& exitCode) {
    Args args;
    if (!Parse(argc, argv, args)) return false;
    if (args.mode == L"--list-sessions") exitCode = ListSessions(args);
    else if (args.mode == L"--live-summary") exitCode = LiveSummary(args);
    else if (args.mode == L"--record") exitCode = Record(args);
    else if (args.mode == L"--analyze") exitCode = Analyze(args);
    else exitCode = ExportCsv(args);
    return true;
}

} // namespace viewer
