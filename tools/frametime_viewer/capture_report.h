// Machine-readable results of an analysis: the JSON summary the MCP tools and
// scripts consume, and a per-frame CSV for spreadsheets and other tools. Both
// come from the same Analyzer the viewer draws, so every number agrees.
#pragma once
#include "capture_file.h"
#include "frame_analysis.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

namespace perf::report {

inline const char* MarkerKindName(uint32_t kind) {
    switch (timeline::MarkerKind(kind)) {
        case timeline::MarkerKind::Instance: return "instance";
        case timeline::MarkerKind::SessionState: return "session_state";
        case timeline::MarkerKind::SwapchainCreate: return "swapchain_create";
        case timeline::MarkerKind::SwapchainDestroy: return "swapchain_destroy";
        case timeline::MarkerKind::UiCommand: return "ui_command";
        case timeline::MarkerKind::WindowResize: return "window_resize";
        case timeline::MarkerKind::EndFrameReentry: return "end_frame_reentry";
        case timeline::MarkerKind::BurstStart: return "burst_start";
        case timeline::MarkerKind::User: return "user";
    }
    return "unknown";
}

inline std::string Number(double value, const char* format = "%.3f") {
    if (!std::isfinite(value)) return "null";
    char text[64];
    snprintf(text, sizeof(text), format, value);
    return text;
}

// Summary of frames [begin, end). `extra` is spliced in as further top-level
// members (for example "capture": {...}); pass "" for none.
inline std::string SummaryJson(const analysis::Analyzer& a, size_t begin, size_t end, const std::string& extra,
                               size_t topFrames = 20) {
    using analysis::Cause;
    using analysis::FrameClass;
    end = std::min(end, a.Size());
    const analysis::RangeStats s = a.Stats(begin, end);
    size_t causes[6] = {};
    std::vector<uint32_t> worst;
    for (size_t i = begin; i < end; ++i) {
        const analysis::FrameMetrics& m = a.Metrics()[i];
        if (m.gap) continue;
        if (m.cls != FrameClass::Ok) ++causes[size_t(m.cause)];
        worst.push_back(uint32_t(i));
    }
    const size_t keep = std::min(topFrames, worst.size());
    std::partial_sort(worst.begin(), worst.begin() + keep, worst.end(), [&](uint32_t x, uint32_t y) {
        return a.Metrics()[x].costMs > a.Metrics()[y].costMs;
    });
    worst.resize(keep);

    std::string j = "{\n";
    j += "  \"target_hz\": " + Number(1000.0 / a.BudgetMs(), "%.6g") + ",\n";
    j += "  \"budget_ms\": " + Number(a.BudgetMs()) + ",\n";
    j += "  \"hitch_ms\": " + Number(a.HitchMs()) + ",\n";
    j += "  \"frames\": " + std::to_string(s.frames) + ",\n";
    j += "  \"seconds\": " + Number(s.seconds) + ",\n";
    j += "  \"idle_gaps\": " + std::to_string(s.idleGaps) + ",\n";
    j += "  \"delivered_fps\": " + Number(s.deliveredFps, "%.2f") + ",\n";
    j += "  \"frame_time_ms\": {\"mean\": " + Number(s.costMeanMs) + ", \"p50\": " + Number(s.costP50Ms) +
         ", \"p90\": " + Number(s.costP90Ms) + ", \"p99\": " + Number(s.costP99Ms) + ", \"p999\": " +
         Number(s.costP999Ms) + ", \"max\": " + Number(s.costMaxMs) + "},\n";
    j += "  \"interval_ms\": {\"p50\": " + Number(s.intervalP50Ms) + ", \"p99\": " + Number(s.intervalP99Ms) + "},\n";
    j += "  \"one_percent_low_fps\": " + Number(s.onePercentLowFps, "%.2f") + ",\n";
    j += "  \"headroom_p50_ms\": " + Number(s.headroomP50Ms) + ",\n";
    j += "  \"simulator_ms\": {\"p50\": " + Number(s.simulatorP50Ms) + ", \"p99\": " + Number(s.simulatorP99Ms) + "},\n";
    j += "  \"over_budget\": {\"frames\": " + std::to_string(s.overBudget) + ", \"percent\": " +
         Number(s.frames ? 100.0 * double(s.overBudget) / double(s.frames) : 0.0, "%.2f") + "},\n";
    j += "  \"hitches\": " + std::to_string(s.hitches) + ",\n";
    j += "  \"missed_refreshes\": " + std::to_string(s.missedRefreshes) + ",\n";
    j += "  \"late_causes\": {\"app_busy\": " + std::to_string(causes[size_t(Cause::AppBusy)]) +
         ", \"app_waiting\": " + std::to_string(causes[size_t(Cause::AppWaiting)]) +
         ", \"simulator\": " + std::to_string(causes[size_t(Cause::Simulator)]) +
         ", \"gpu\": " + std::to_string(causes[size_t(Cause::Gpu)]) +
         ", \"unknown\": " + std::to_string(causes[size_t(Cause::Unknown)]) + "},\n";
    if (s.gpuFrames) {
        j += "  \"gpu\": {\"api\": " + capture::JsonString(timeline::GpuApiName(a.GpuApi())) +
             ", \"frames\": " + std::to_string(s.gpuFrames) + ", \"over_budget\": " + std::to_string(s.gpuOverBudget) +
             ", \"p50_ms\": " + Number(s.gpuP50Ms) + ", \"p99_ms\": " + Number(s.gpuP99Ms) +
             ", \"max_ms\": " + Number(s.gpuMaxMs) + "},\n";
    } else {
        j += "  \"gpu\": null,\n";
    }
    j += "  \"worst_frames\": [";
    for (size_t k = 0; k < worst.size(); ++k) {
        const size_t i = worst[k];
        const analysis::FrameMetrics& m = a.Metrics()[i];
        j += k ? ",\n    " : "\n    ";
        j += "{\"frame\": " + std::to_string(a.Records()[i].frameId) + ", \"time_s\": " + Number(m.endSec) +
             ", \"frame_time_ms\": " + Number(m.costMs) + ", \"cpu_ms\": " + Number(m.cpuMs) +
             ", \"gpu_ms\": " + Number(m.gpuMs) + ", \"gpu_wait_ms\": " + Number(m.gpuWaitMs) +
             ", \"interval_ms\": " + Number(m.intervalMs) +
             ", \"missed\": " + std::to_string(m.missed) + ", \"class\": " +
             capture::JsonString(analysis::ClassName(m.cls)) + ", \"cause\": " +
             capture::JsonString(analysis::CauseName(m.cause)) + "}";
    }
    j += worst.empty() ? "],\n" : "\n  ],\n";
    // Events inside the range, the first 50.
    const double from = begin < end ? a.Metrics()[begin].startSec : 0.0;
    const double to = begin < end ? a.Metrics()[end - 1].endSec : 0.0;
    j += "  \"markers\": [";
    size_t listed = 0;
    for (const timeline::MarkerRecord& marker : a.Markers()) {
        const double t = a.Sec(marker.qpc);
        if (t < from || t > to || listed == 50) continue;
        j += listed++ ? ",\n    " : "\n    ";
        j += "{\"time_s\": " + Number(t) + ", \"kind\": " + capture::JsonString(MarkerKindName(marker.kind)) +
             ", \"a\": " + std::to_string(marker.a) + ", \"b\": " + std::to_string(marker.b) +
             ", \"text\": " + capture::JsonString(std::string(marker.text, strnlen(marker.text, sizeof(marker.text)))) + "}";
    }
    j += listed ? "\n  ]" : "]";
    if (!extra.empty()) j += ",\n" + extra;
    j += "\n}\n";
    return j;
}

// One row per frame: the derived numbers, then the raw stamps in milliseconds
// since the timeline began (empty when unobserved).
inline bool WriteCsv(const std::wstring& path, const analysis::Analyzer& a, size_t begin, size_t end, std::string& error) {
    FILE* file = nullptr;
    if (_wfopen_s(&file, path.c_str(), L"wb") != 0 || !file) {
        error = "cannot create the CSV file";
        return false;
    }
    fputs("frame,time_s,class,cause,frame_time_ms,cpu_ms,gpu_ms,gpu_latency_ms,gpu_wait_ms,interval_ms,missed_refreshes,headroom_ms,simulator_ms,"
          "app_span_ms,pacing_error_ms,submit_cpu_ms,layers,flags,wait_thread,submit_thread,"
          "wait_enter_ms,wait_pace_ms,wait_target_ms,wait_exit_ms,begin_ms,locate_ms,end_enter_ms,"
          "end_projection_ms,end_overlays_ms,end_paint_ms,end_exit_ms,predicted_display_time_ns,"
          "submitted_display_time_ns\n", file);
    auto stamp = [&](uint64_t qpc) { return qpc ? Number(a.Sec(qpc) * 1000.0, "%.4f") : std::string(); };
    auto value = [](double v) { return std::isfinite(v) ? Number(v, "%.4f") : std::string(); };
    end = std::min(end, a.Size());
    for (size_t i = begin; i < end; ++i) {
        const analysis::FrameMetrics& m = a.Metrics()[i];
        const timeline::FrameRecord& r = a.Records()[i];
        const std::string line =
            std::to_string(r.frameId) + "," + Number(m.endSec, "%.6f") + "," + analysis::ClassName(m.cls) + "," +
            analysis::CauseName(m.cause) + "," + value(m.costMs) + "," + value(m.cpuMs) + "," + value(m.gpuMs) + "," +
            value(m.gpuLatencyMs) + "," + value(m.gpuWaitMs) + "," + value(m.intervalMs) + "," +
            std::to_string(m.missed) + "," + value(m.blockedMs) + "," + value(m.simulatorMs) + "," +
            value(m.appSpanMs) + "," + value(m.pacingErrorMs) + "," + value(m.submitCpuMs) + "," +
            std::to_string(r.layerCount) + "," + std::to_string(r.flags) + "," + std::to_string(r.waitThreadId) + "," +
            std::to_string(r.endThreadId) + "," + stamp(r.waitEnterQpc) + "," + stamp(r.waitPaceQpc) + "," +
            stamp(r.waitTargetQpc) + "," + stamp(r.waitExitQpc) + "," + stamp(r.beginQpc) + "," + stamp(r.locateQpc) +
            "," + stamp(r.endEnterQpc) + "," + stamp(r.endProjectionQpc) + "," + stamp(r.endOverlaysQpc) + "," +
            stamp(r.endPaintQpc) + "," + stamp(r.endExitQpc) + "," + std::to_string(r.predictedDisplayTime) + "," +
            std::to_string(r.submittedDisplayTime) + "\n";
        fputs(line.c_str(), file);
    }
    if (fclose(file) != 0) {
        error = "writing the CSV failed";
        return false;
    }
    return true;
}

// Loads a capture into an analyzer scored at `refreshHz` (0: the rate it was
// recorded at) with hitches at `hitchBudgets` refresh budgets.
inline void Analyze(const capture::Capture& capture, analysis::Analyzer& a, double refreshHz, double hitchBudgets) {
    a.Reset(capture.header.qpcFrequency, capture.header.originQpc);
    a.SetTscFrequency(capture.header.tscFrequency);
    a.SetBudget(refreshHz > 0.0 ? refreshHz : capture.header.refreshHz, hitchBudgets);
    a.AddMarkers(capture.markers);
    a.AddFrames(capture.frames);
    a.AddGpu(capture.gpu);
}

} // namespace perf::report
