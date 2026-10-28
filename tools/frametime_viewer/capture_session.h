// Turning a stretch of a live timeline into a capture: where captures live,
// what they are called, and the metadata that makes one self-explaining
// (process, session settings from the runtime, this machine, the label).
#pragma once
#include "capture_file.h"
#include "timeline_client.h"

#include <windows.h>

#include <string>
#include <vector>

namespace viewer {

inline std::wstring CapturesDirectory() {
    wchar_t base[MAX_PATH]{};
    const DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", base, MAX_PATH);
    if (!length || length >= MAX_PATH) return L".";
    std::wstring dir = std::wstring(base) + L"\\OpenXR-Simulator";
    CreateDirectoryW(dir.c_str(), nullptr);
    dir += L"\\captures";
    CreateDirectoryW(dir.c_str(), nullptr);
    return dir;
}

inline std::wstring Widen(const std::string& text) {
    if (text.empty()) return {};
    const int length = MultiByteToWideChar(CP_UTF8, 0, text.c_str(), int(text.size()), nullptr, 0);
    std::wstring out(size_t(length), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.c_str(), int(text.size()), out.data(), length);
    return out;
}

inline std::string Narrow(const std::wstring& text) {
    if (text.empty()) return {};
    const int length = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), int(text.size()), nullptr, 0, nullptr, nullptr);
    std::string out(size_t(length), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.c_str(), int(text.size()), out.data(), length, nullptr, nullptr);
    return out;
}

// "<process>_<yyyy-mm-dd_hh-mm-ss>[_<label>].xrft", safe on any file system.
// A path in `directory` not taken yet: CaptureFileName has one-second resolution.
inline std::wstring UniqueCapturePath(const std::wstring& directory, const std::wstring& name) {
    const size_t dot = name.rfind(L'.');
    const std::wstring stem = name.substr(0, dot), extension = dot == std::wstring::npos ? L"" : name.substr(dot);
    std::wstring path = directory + L"\\" + name;
    for (int n = 2; n < 1000 && GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES; ++n) {
        path = directory + L"\\" + stem + L"-" + std::to_wstring(n) + extension;
    }
    return path;
}

inline std::wstring CaptureFileName(const std::string& process, const std::string& label) {
    auto clean = [](const std::string& text) {
        std::string out;
        for (const char c : text) {
            if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_') out += c;
            else if (c == ' ' || c == '.') out += '-';
        }
        return out.substr(0, 48);
    };
    std::string stem = clean(process);
    const size_t exe = stem.rfind("-exe");
    if (exe != std::string::npos && exe + 4 == stem.size()) stem.resize(exe);
    SYSTEMTIME t;
    GetLocalTime(&t);
    char stamp[32];
    snprintf(stamp, sizeof(stamp), "%04u-%02u-%02u_%02u-%02u-%02u", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
    std::string name = (stem.empty() ? std::string("capture") : stem) + "_" + stamp;
    if (!clean(label).empty()) name += "_" + clean(label);
    return Widen(name) + perf::capture::kExtension;
}

inline std::string UtcNow() {
    SYSTEMTIME t;
    GetSystemTime(&t);
    char text[32];
    snprintf(text, sizeof(text), "%04u-%02u-%02uT%02u:%02u:%02uZ", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
    return text;
}

// "2026-10-28T13:59:40Z" as this machine's local time: "Oct 28, 09:59:40",
// with the year when it is not this one. Unparseable text comes back as is.
inline std::string LocalTimeText(const std::string& utc) {
    SYSTEMTIME t{};
    unsigned year, month, day, hour, minute, second;
    if (sscanf(utc.c_str(), "%4u-%2u-%2uT%2u:%2u:%2uZ", &year, &month, &day, &hour, &minute, &second) != 6) return utc;
    t.wYear = WORD(year);
    t.wMonth = WORD(month);
    t.wDay = WORD(day);
    t.wHour = WORD(hour);
    t.wMinute = WORD(minute);
    t.wSecond = WORD(second);
    SYSTEMTIME local{}, now{};
    if (!SystemTimeToTzSpecificLocalTime(nullptr, &t, &local)) return utc;
    GetLocalTime(&now);
    static const char* months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    char text[48];
    if (local.wYear == now.wYear) {
        snprintf(text, sizeof(text), "%s %u, %02u:%02u:%02u", months[(local.wMonth + 11) % 12], local.wDay, local.wHour,
                 local.wMinute, local.wSecond);
    } else {
        snprintf(text, sizeof(text), "%s %u %u, %02u:%02u", months[(local.wMonth + 11) % 12], local.wDay, local.wYear,
                 local.wHour, local.wMinute);
    }
    return text;
}

inline std::string RegistryString(HKEY root, const wchar_t* key, const wchar_t* value) {
    wchar_t buffer[256]{};
    DWORD size = sizeof(buffer);
    if (RegGetValueW(root, key, value, RRF_RT_REG_SZ, nullptr, buffer, &size) != ERROR_SUCCESS) return {};
    std::string out = Narrow(buffer);
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

// This machine, as far as frame times care: CPU, memory, Windows build.
inline std::string SystemJson() {
    const wchar_t* cpuKey = L"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0";
    const wchar_t* osKey = L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion";
    const std::string cpu = RegistryString(HKEY_LOCAL_MACHINE, cpuKey, L"ProcessorNameString");
    const std::string build = RegistryString(HKEY_LOCAL_MACHINE, osKey, L"CurrentBuild");
    const std::string release = RegistryString(HKEY_LOCAL_MACHINE, osKey, L"DisplayVersion");
    DWORD ubr = 0, size = sizeof(ubr);
    RegGetValueW(HKEY_LOCAL_MACHINE, osKey, L"UBR", RRF_RT_REG_DWORD, nullptr, &ubr, &size);
    MEMORYSTATUSEX memory{sizeof(memory)};
    GlobalMemoryStatusEx(&memory);
    SYSTEM_INFO system{};
    GetSystemInfo(&system);
    const int buildNumber = atoi(build.c_str());
    char os[96];
    snprintf(os, sizeof(os), "Windows %s %s (build %s.%lu)", buildNumber >= 22000 ? "11" : "10", release.c_str(),
             build.c_str(), ubr);
    char json[512];
    snprintf(json, sizeof(json), "{\"cpu\": %s, \"logical_processors\": %lu, \"memory_gb\": %.1f, \"os\": %s}",
             perf::capture::JsonString(cpu).c_str(), system.dwNumberOfProcessors,
             double(memory.ullTotalPhys) / (1024.0 * 1024.0 * 1024.0), perf::capture::JsonString(os).c_str());
    return json;
}

struct CaptureDescription {
    std::string label;
    std::string recordedBy;      // "viewer" or "recorder"
    std::string startedUtc;
    DWORD pid = 0;
    uint64_t lostFrames = 0;
};

// Builds a capture of `frames` (and the markers that fall among them) taken
// from the timeline `header` describes.
inline perf::capture::Capture MakeCapture(const TimelineHeader& header, const CaptureDescription& d,
                                          std::vector<FrameRecord> frames, const std::vector<MarkerRecord>& allMarkers,
                                          const std::vector<GpuRecord>& allGpu) {
    using perf::capture::JsonString;
    perf::capture::Capture c;
    c.header = perf::capture::MakeHeader(header.qpcFrequency, header.createdQpc,
                                         header.tscFrequency.load(std::memory_order_relaxed), header.refreshHz);
    c.frames = std::move(frames);
    if (!c.frames.empty()) {
        const uint64_t from = c.frames.front().endEnterQpc, to = c.frames.back().endExitQpc;
        for (const MarkerRecord& m : allMarkers) {
            if (m.qpc >= from && m.qpc <= to) c.markers.push_back(m);
        }
        const uint64_t firstId = c.frames.front().frameId, lastId = c.frames.back().frameId;
        for (const GpuRecord& g : allGpu) {
            if (g.frameId >= firstId && g.frameId <= lastId) c.gpu.push_back(g);
        }
    }
    const double seconds = c.frames.size() > 1
        ? double(c.frames.back().endEnterQpc - c.frames.front().endEnterQpc) / double(header.qpcFrequency)
        : 0.0;
    char numbers[160];
    snprintf(numbers, sizeof(numbers), "\"seconds\": %.3f, \"frames\": %zu, \"markers\": %zu, \"lost_frames\": %llu",
             seconds, c.frames.size(), c.markers.size(), (unsigned long long)d.lostFrames);
    c.metadata = "{\n  \"capture\": {\"label\": " + JsonString(d.label) + ", \"started_utc\": " + JsonString(d.startedUtc) +
                 ", \"recorded_by\": " + JsonString(d.recordedBy) + ", " + numbers + "},\n" +
                 "  \"process\": {\"name\": " + JsonString(HeaderString(header.processName, sizeof(header.processName))) +
                 ", \"application\": " + JsonString(HeaderString(header.applicationName, sizeof(header.applicationName))) +
                 ", \"pid\": " + std::to_string(d.pid) + "},\n" +
                 "  \"session\": " + perf::timeline::ReadSessionInfo(header) + ",\n" +
                 "  \"system\": " + SystemJson() + "\n}\n";
    return c;
}

} // namespace viewer
