#include "viewer_app.h"

#include "capture_report.h"
#include "capture_session.h"
#include "imgui.h"
#include "implot.h"
#include "json.h"

#include <commdlg.h>
#include <dwmapi.h>
#include <shellapi.h>

#include <algorithm>
#include <cstdarg>
#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace viewer {

namespace an = perf::analysis;
namespace tl = perf::timeline;
using ui::Px;

namespace {

constexpr float kLiveWindows[] = {5.0f, 10.0f, 30.0f, 60.0f, 300.0f};
const char* const kLiveWindowLabels[] = {"5 s", "10 s", "30 s", "1 min", "5 min"};

const char* MarkerTag(uint32_t kind) {
    switch (tl::MarkerKind(kind)) {
        case tl::MarkerKind::Instance: return "instance";
        case tl::MarkerKind::SessionState: return "session";
        case tl::MarkerKind::SwapchainCreate: return "swapchain";
        case tl::MarkerKind::SwapchainDestroy: return "swapchain";
        case tl::MarkerKind::UiCommand: return "ui";
        case tl::MarkerKind::WindowResize: return "resize";
        case tl::MarkerKind::EndFrameReentry: return "reentry";
        case tl::MarkerKind::BurstStart: return "burst";
        case tl::MarkerKind::User: return "marker";
    }
    return "?";
}

std::string MarkerText(const tl::MarkerRecord& m) {
    char text[160];
    const char* detail = m.text;
    switch (tl::MarkerKind(m.kind)) {
        case tl::MarkerKind::Instance: snprintf(text, sizeof(text), "Instance created: %s", detail); break;
        case tl::MarkerKind::SessionState: snprintf(text, sizeof(text), "Session %s", detail); break;
        case tl::MarkerKind::SwapchainCreate:
            snprintf(text, sizeof(text), "Swapchain created, %lld x %lld, %s", (long long)m.a, (long long)m.b, detail);
            break;
        case tl::MarkerKind::SwapchainDestroy: snprintf(text, sizeof(text), "Swapchain destroyed"); break;
        case tl::MarkerKind::UiCommand:
            if (m.b == 1) snprintf(text, sizeof(text), "Preview key 0x%02llX", (unsigned long long)m.a);
            else snprintf(text, sizeof(text), "Preview menu command %lld", (long long)m.a);
            break;
        case tl::MarkerKind::WindowResize:
            snprintf(text, sizeof(text), "Preview window resized to %lld x %lld", (long long)m.a, (long long)m.b);
            break;
        case tl::MarkerKind::EndFrameReentry:
            snprintf(text, sizeof(text), "Reentrant xrEndFrame blocked (#%lld)", (long long)m.a);
            break;
        case tl::MarkerKind::BurstStart: snprintf(text, sizeof(text), "Frame burst, %lld frames", (long long)m.a); break;
        case tl::MarkerKind::User: snprintf(text, sizeof(text), "%s", detail); break;
        default: snprintf(text, sizeof(text), "Marker %u %s", m.kind, detail); break;
    }
    return text;
}

std::string SessionTitle(const SessionInfo& s) { return s.processName.empty() ? "unknown process" : s.processName; }

std::string SessionDetail(const SessionInfo& s) {
    char text[200];
    if (!s.applicationName.empty() && s.applicationName != s.processName) {
        snprintf(text, sizeof(text), "%s  \xC2\xB7  pid %lu", s.applicationName.c_str(), s.pid);
    } else {
        snprintf(text, sizeof(text), "pid %lu", s.pid);
    }
    return text;
}

std::string SettingsPath() {
    char base[MAX_PATH]{};
    const DWORD length = GetEnvironmentVariableA("LOCALAPPDATA", base, sizeof(base));
    if (!length || length >= sizeof(base)) return {};
    std::string dir = std::string(base) + "\\OpenXR-Simulator";
    CreateDirectoryA(dir.c_str(), nullptr);
    return dir + "\\frametime_viewer.ini";
}

// A number in semibold with its unit in smaller muted type on the same baseline.
void BigNumber(const char* number, const char* unit, float size = 26.0f) {
    const float y = ImGui::GetCursorPosY();
    ui::PushSemibold(size);
    const float numberHeight = ImGui::GetFontSize();
    ImGui::TextUnformatted(number);
    ui::PopFont();
    if (unit && *unit) {
        ImGui::SameLine(0.0f, Px(4.0f));
        ui::PushRegular(14.0f);
        const float unitHeight = ImGui::GetFontSize();
        ImGui::SetCursorPosY(y + (numberHeight - unitHeight) * 0.80f);
        ui::Muted("%s", unit);
        ui::PopFont();
    }
}

// One line of secondary information, optionally led by a status dot.
void SubLine(ImU32 dot, const char* fmt, ...) IM_FMTARGS(2);
void SubLine(ImU32 dot, const char* fmt, ...) {
    ui::PushRegular(ui::kSmallSize);
    if (dot) {
        ui::Dot(dot, 3.5f);
        ImGui::SameLine(0.0f, Px(6.0f));
    }
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
    ui::PopFont();
}

void KeyValue(const char* key, const char* fmt, ...) IM_FMTARGS(2);
void KeyValue(const char* key, const char* fmt, ...) {
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ui::Muted("%s", key);
    ImGui::TableNextColumn();
    va_list args;
    va_start(args, fmt);
    ImGui::TextV(fmt, args);
    va_end(args);
}

// Cursor to the right edge of the current line for content `width` wide.
void AlignRight(float width) {
    const float slack = ImGui::GetContentRegionAvail().x - width;
    if (slack > 0.0f) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + slack);
}

} // namespace

Settings LoadSettings() {
    Settings s;
    const std::string path = SettingsPath();
    FILE* file = path.empty() ? nullptr : fopen(path.c_str(), "r");
    if (!file) return s;
    char line[256];
    while (fgets(line, sizeof(line), file)) {
        char key[64];
        double value = 0.0;
        if (sscanf(line, "%63[^=]=%lf", key, &value) != 2) continue;
        const std::string k = key;
        if (k == "theme") s.theme = std::clamp(int(value), 0, 2);
        else if (k == "refresh_hz") s.refreshHz = float(value);
        else if (k == "hitch_budgets") s.hitchBudgets = float(std::clamp(value, 1.25, 10.0));
        else if (k == "live_seconds") s.liveSeconds = float(std::clamp(value, 1.0, 600.0));
        else if (k == "frame_axis") s.frameAxis = value != 0.0;
        else if (k == "log_y") s.logY = value != 0.0;
        else if (k == "show_interval") s.showInterval = value != 0.0;
        else if (k == "show_gpu") s.showGpu = value != 0.0;
        else if (k == "session_stats") s.sessionStats = value != 0.0;
        else if (k == "hitches_only") s.hitchesOnly = value != 0.0;
        else if (k == "window_x") s.windowX = int(value);
        else if (k == "window_y") s.windowY = int(value);
        else if (k == "window_w") s.windowW = int(value);
        else if (k == "window_h") s.windowH = int(value);
        else if (k == "maximized") s.maximized = value != 0.0;
    }
    fclose(file);
    return s;
}

void SaveSettings(const Settings& s) {
    const std::string path = SettingsPath();
    FILE* file = path.empty() ? nullptr : fopen(path.c_str(), "w");
    if (!file) return;
    fprintf(file, "theme=%d\nrefresh_hz=%g\nhitch_budgets=%g\nlive_seconds=%g\nframe_axis=%d\nlog_y=%d\n"
                  "show_interval=%d\nshow_gpu=%d\nsession_stats=%d\nhitches_only=%d\n"
                  "window_x=%d\nwindow_y=%d\nwindow_w=%d\nwindow_h=%d\nmaximized=%d\n",
            s.theme, s.refreshHz, s.hitchBudgets, s.liveSeconds, s.frameAxis, s.logY, s.showInterval, s.showGpu,
            s.sessionStats, s.hitchesOnly, s.windowX, s.windowY, s.windowW, s.windowH, s.maximized);
    fclose(file);
}

App::App(DWORD pid, HWND window, float dpiScale, const Settings& settings)
    : m_window(window), m_dpi(dpiScale), m_wantedPid(pid), m_settings(settings) {
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    m_qpcToSec = 1.0 / double(frequency.QuadPart);
    BeginFrame();
}

App::~App() {
    StopRecording();  // closing the window saves a recording in progress
    SaveSettings(m_settings);
}

double App::Clock() const { return double(tl::Qpc()) * m_qpcToSec; }
double App::SessionNow() const { return m_analyzer.Sec(tl::Qpc()); }
an::XAxis App::Axis() const { return m_settings.frameAxis ? an::XAxis::Frame : an::XAxis::Time; }
void App::SettingsChanged() { m_settingsChangedAt = Clock(); }

ImU32 App::ClassColor(an::FrameClass c) const {
    switch (c) {
        case an::FrameClass::Ok: return m_theme.onTime;
        case an::FrameClass::OverBudget: return m_theme.over;
        case an::FrameClass::Hitch: return m_theme.hitch;
        case an::FrameClass::Gap: return m_theme.gap;
    }
    return m_theme.gap;
}

double App::RightEdge() const {
    if (Axis() == an::XAxis::Frame) return double(View().Size());
    // Past the newest frame while the app is alive: a freeze shows as an
    // empty stretch growing in real time, not as nothing happening.
    if (!m_viewingCapture && (m_processAlive || View().Size() == 0)) return SessionNow();
    return View().Size() ? View().Metrics().back().endSec : 0.0;
}

double App::MarkerX(const tl::MarkerRecord& marker) const {
    const double sec = View().Sec(marker.qpc);
    if (Axis() == an::XAxis::Time) return sec;
    return double(View().LowerBound(an::XAxis::Time, sec));
}

void App::BeginFrame() {
    const double now = Clock();
    if (m_settings.theme == 0 && now - m_themeCheckedAt > 2.0) {
        m_systemDark = ui::SystemPrefersDark();
        m_themeCheckedAt = now;
    }
    const bool dark = m_settings.theme == 0 ? m_systemDark : m_settings.theme == 2;
    if (m_themeApplied && dark == m_theme.dark) return;
    m_theme = ui::MakeTheme(dark);
    ui::ApplyTheme(m_theme, m_dpi);
    m_themeApplied = true;
    const BOOL immersiveDark = dark ? TRUE : FALSE;
    DwmSetWindowAttribute(m_window, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &immersiveDark, sizeof(immersiveDark));
}

void App::AttachTo(DWORD pid) {
    StopRecording();  // saved under the process it came from
    if (!m_client.Attach(pid, m_error)) return;
    m_error.clear();
    m_wantedPid = pid;
    m_lostFrames = 0;
    m_live = true;
    m_selected = -1;
    ResetAnalyzer();
    SetPropW(m_window, L"OpenXRSim.Pid", reinterpret_cast<HANDLE>(uintptr_t(pid)));
    const TimelineHeader& h = *m_client.Header();
    char title[256];
    snprintf(title, sizeof(title), "Frame Timing - %s (pid %lu)",
             HeaderString(h.processName, sizeof(h.processName)).c_str(), pid);
    wchar_t wide[256];
    MultiByteToWideChar(CP_UTF8, 0, title, -1, wide, 256);
    SetWindowTextW(m_window, wide);
}

void App::ResetAnalyzer() {
    StopRecording();  // Clear or a restarted timeline: keep what was recorded
    const TimelineHeader& h = *m_client.Header();
    m_analyzer.Reset(h.qpcFrequency, h.createdQpc);
    m_analyzer.SetTscFrequency(h.tscFrequency.load(std::memory_order_relaxed));
    m_appliedHz = 0.0;
    ApplyBudget();
    m_lateRowsDirty = true;
    m_statsAt = -1.0;
    m_fitY = true;
}

// Every analyzer is scored at the target rate: the user's choice, else the
// rate whatever is on screen was recorded at. A baseline is scored like the
// view it is compared with.
void App::ApplyBudget() {
    const double chosen = m_settings.refreshHz;
    const double liveHz = chosen > 0.0 ? chosen
        : m_client.Attached() && m_client.Header()->refreshHz > 0.0 ? m_client.Header()->refreshHz : 90.0;
    const double viewHz = chosen > 0.0 || !m_viewingCapture ? liveHz : m_capture.header.refreshHz;
    if (viewHz == m_appliedHz && m_settings.hitchBudgets == m_appliedHitch) return;
    m_appliedHz = viewHz;
    m_appliedHitch = m_settings.hitchBudgets;
    m_analyzer.SetBudget(liveHz, m_settings.hitchBudgets);
    if (m_viewingCapture) m_captureAnalyzer.SetBudget(viewHz, m_settings.hitchBudgets);
    if (m_hasBaseline) {
        m_baselineAnalyzer.SetBudget(viewHz, m_settings.hitchBudgets);
        m_baselineStats = m_baselineAnalyzer.Stats(0, m_baselineAnalyzer.Size());
    }
    m_lateRowsDirty = true;
    m_statsAt = -1.0;
}

void App::Poll() {
    const double now = Clock();
    if (!m_client.Attached()) {
        if (now - m_lastDiscover > 1.0) {
            m_sessions = DiscoverSessions();
            m_lastDiscover = now;
            // Attach to the process asked for, or to the only one running.
            for (const SessionInfo& s : m_sessions) {
                if ((m_wantedPid && s.pid == m_wantedPid) ||
                    (!m_wantedPid && !m_autoAttached && m_sessions.size() == 1)) {
                    m_autoAttached = true;
                    AttachTo(s.pid);
                    break;
                }
            }
        }
        if (!m_client.Attached()) return;
    }

    m_newFrames.clear();
    m_newMarkers.clear();
    m_newGpu.clear();
    const auto read = m_client.Poll(m_newFrames, m_newMarkers, m_newGpu);
    if (read.restarted) ResetAnalyzer();
    m_lostFrames += read.lostFrames;
    const TimelineHeader& h = *m_client.Header();
    m_analyzer.SetTscFrequency(h.tscFrequency.load(std::memory_order_relaxed));
    ApplyBudget();
    m_analyzer.AddMarkers(m_newMarkers);
    m_analyzer.AddFrames(m_newFrames);
    m_analyzer.AddGpu(m_newGpu);
    if (!m_newFrames.empty() && !m_viewingCapture) m_lateRowsStale = true;
    m_processAlive = m_client.ProcessAlive();

    if (m_settingsChangedAt >= 0.0 && now - m_settingsChangedAt > 1.0) {
        SaveSettings(m_settings);
        m_settingsChangedAt = -1.0;
    }
}

void App::UpdateStats(bool force) {
    const size_t size = View().Size();
    size_t begin = 0, end = size;
    if (!m_settings.sessionStats) {
        begin = View().LowerBound(Axis(), m_viewX0);
        end = std::min(size, View().LowerBound(Axis(), m_viewX1));
    }
    const double now = Clock();
    const bool rangeChanged = begin != m_statsBegin || end != m_statsEnd;
    const bool dataChanged = View().Revision() != m_statsRevision || &View() != m_statsSource;
    if (!force && !rangeChanged && !dataChanged) return;
    if (!force && !(rangeChanged && now - m_statsAt > 0.2) && now - m_statsAt < 0.5) return;
    m_statsAt = now;
    m_statsRevision = View().Revision();
    m_statsSource = &View();
    m_statsBegin = begin;
    m_statsEnd = end;
    m_stats = View().Stats(begin, end);
    m_worstIndex = -1;
    for (size_t i = begin; i < end; ++i) {
        const an::FrameMetrics& m = View().Metrics()[i];
        if (!m.gap && (m_worstIndex < 0 || m.costMs > View().Metrics()[size_t(m_worstIndex)].costMs)) m_worstIndex = int64_t(i);
    }
    m_histogramBinMs = View().BudgetMs() / 6.0;
    const size_t bins = size_t(std::ceil(View().HitchMs() * 1.5 / m_histogramBinMs)) + 1;
    View().Histogram(begin, end, m_histogramBinMs, bins, m_histogram);
    if (m_hasBaseline) {
        m_baselineAnalyzer.Histogram(0, m_baselineAnalyzer.Size(), m_histogramBinMs, bins, m_baselineHistogram);
    }
}

void App::CenterOn(double x) {
    const double width = std::max(1e-6, m_viewX1 - m_viewX0);
    m_viewX0 = x - width * 0.5;
    m_viewX1 = x + width * 0.5;
    m_applyView = true;
    m_live = false;
}

void App::Select(int64_t index, bool center) {
    if (index < 0 || size_t(index) >= View().Size()) return;
    m_selected = index;
    const double x0 = View().StartX(Axis(), size_t(index)), x1 = View().EndX(Axis(), size_t(index));
    if (center || x1 < m_viewX0 || x0 > m_viewX1) CenterOn((x0 + x1) * 0.5);
}

void App::HandleKeys() {
    if (ImGui::GetIO().WantTextInput) return;
    if (ImGui::IsKeyPressed(ImGuiKey_Space, false)) m_live = !m_live;
    if (ImGui::IsKeyPressed(ImGuiKey_Home, false)) m_live = true;
    if (ImGui::IsKeyPressed(ImGuiKey_F, false)) m_fitY = true;
    if (ImGui::IsKeyPressed(ImGuiKey_R, false) && !m_viewingCapture) {
        if (m_recording) StopRecording();
        else StartRecording();
    }
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false) && m_viewingCapture) CloseCapture();
    if (ImGui::IsKeyPressed(ImGuiKey_LeftArrow)) {
        Select(m_selected < 0 ? int64_t(View().LowerBound(Axis(), m_viewX1)) - 1 : m_selected - 1, false);
        m_live = false;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_RightArrow)) {
        Select(m_selected < 0 ? int64_t(View().LowerBound(Axis(), m_viewX0)) : m_selected + 1, false);
        m_live = false;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_N)) {
        const auto& late = View().LateFrames();
        const bool back = ImGui::GetIO().KeyShift;
        const int64_t from = m_selected >= 0 ? m_selected : int64_t(View().LowerBound(Axis(), m_viewX0));
        int64_t pick = -1;
        if (back) {
            auto it = std::lower_bound(late.begin(), late.end(), uint32_t(std::max<int64_t>(0, from)));
            if (it != late.begin()) pick = *(it - 1);
        } else {
            auto it = std::upper_bound(late.begin(), late.end(), uint32_t(std::max<int64_t>(0, from)));
            if (it != late.end()) pick = *it;
        }
        if (pick >= 0) Select(pick, true);
    }
}

void App::Draw() {
    const ImGuiViewport* viewport = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(viewport->WorkPos);
    ImGui::SetNextWindowSize(viewport->WorkSize);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(18.0f), Px(14.0f)));
    ImGui::Begin("##root", nullptr,
                 ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus |
                 ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    HandleKeys();
    DrawHeader();
    if (!m_client.Attached() && !m_viewingCapture) {
        DrawPicker();
        DrawToast();
        ImGui::End();
        return;
    }
    UpdateStats(false);
    ImGui::Dummy(ImVec2(0.0f, Px(2.0f)));
    DrawStats();
    const float gap = ImGui::GetStyle().ItemSpacing.y;
    const float available = ImGui::GetContentRegionAvail().y;
    const float bottom = std::max(Px(250.0f), available * 0.36f);
    DrawTimeline(std::max(Px(220.0f), available - bottom - gap));
    DrawBottom(ImGui::GetContentRegionAvail().y);
    DrawToast();
    ImGui::End();
}

void App::DrawHeader() {
    const ui::Theme& t = m_theme;
    const double now = SessionNow();

    // Where the app is right now, from the header's live state.
    ImU32 stateColor = t.textMuted;
    char state[160] = "Not attached";
    if (m_client.Attached()) {
        const TimelineHeader& h = *m_client.Header();
        const uint64_t inEnd = h.endActiveSinceQpc.load(std::memory_order_relaxed);
        const uint64_t inWait = h.waitActiveSinceQpc.load(std::memory_order_relaxed);
        const uint64_t lastExit = h.lastEndExitQpc.load(std::memory_order_relaxed);
        if (!m_processAlive) {
            snprintf(state, sizeof(state), "Process exited  \xC2\xB7  showing its last %zu frames", m_analyzer.Size());
        } else if (inEnd && now - m_analyzer.Sec(inEnd) > 0.1) {
            snprintf(state, sizeof(state), "Stuck inside xrEndFrame for %.1f s", now - m_analyzer.Sec(inEnd));
            stateColor = t.hitch;
        } else if (inWait && now - m_analyzer.Sec(inWait) > 0.25) {
            snprintf(state, sizeof(state), "Inside xrWaitFrame for %.1f s", now - m_analyzer.Sec(inWait));
            stateColor = t.hitch;
        } else if (!lastExit) {
            snprintf(state, sizeof(state), "Waiting for the first frame");
            stateColor = t.over;
        } else if (now - m_analyzer.Sec(lastExit) > 0.25) {
            snprintf(state, sizeof(state), "No frame for %.1f s", now - m_analyzer.Sec(lastExit));
            stateColor = t.over;
        } else {
            snprintf(state, sizeof(state), "Receiving  \xC2\xB7  %zu frames", m_analyzer.Size());
            stateColor = t.good;
        }
        if (m_lostFrames) {
            const size_t length = strlen(state);
            snprintf(state + length, sizeof(state) - length, "  \xC2\xB7  %llu missed by the viewer",
                     (unsigned long long)m_lostFrames);
        }
    }

    if (m_viewingCapture) {
        // The open capture, in the place of the session selector.
        json::Object meta(m_capture.metadata.c_str());
        const std::string label = meta.object("capture").string("label");
        const std::string title = label.empty() ? Narrow(m_capturePath.substr(m_capturePath.find_last_of(L"\\/") + 1)) : label;
        char detail[200];
        snprintf(detail, sizeof(detail), "%s  \xC2\xB7  %.1f s  \xC2\xB7  %s",
                 meta.object("process").string("name").c_str(), View().Size() ? View().Metrics().back().endSec - View().Metrics().front().endSec : 0.0,
                 LocalTimeText(meta.object("capture").string("started_utc")).c_str());
        const float height = ImGui::GetFrameHeight() + Px(6.0f);
        ui::PushSemibold(16.0f);
        const float titleWidth = ImGui::CalcTextSize(title.c_str()).x;
        ui::PopFont();
        const float iconWidth = ImGui::CalcTextSize(ICON_DOC).x;
        const float width = Px(14.0f) + iconWidth + Px(10.0f) + titleWidth + Px(10.0f) + ImGui::CalcTextSize(detail).x + Px(16.0f);
        const ImVec2 origin = ImGui::GetCursorScreenPos();
        if (ImGui::InvisibleButton("##capturechip", ImVec2(width, height))) ImGui::OpenPopup("##info");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Capture details");
        ImDrawList* draw = ImGui::GetWindowDrawList();
        draw->AddRectFilled(origin, ImVec2(origin.x + width, origin.y + height),
                            ImGui::IsItemHovered() ? t.fieldHover : t.field, height * 0.5f);
        const float mid = origin.y + height * 0.5f;
        float x = origin.x + Px(14.0f);
        draw->AddText(ImVec2(x, mid - ImGui::GetFontSize() * 0.5f), t.accent, ICON_DOC);
        x += iconWidth + Px(10.0f);
        ui::PushSemibold(16.0f);
        draw->AddText(ImVec2(x, mid - ImGui::GetFontSize() * 0.5f), t.text, title.c_str());
        ui::PopFont();
        x += titleWidth + Px(10.0f);
        draw->AddText(ImVec2(x, mid - ImGui::GetFontSize() * 0.5f), t.textMuted, detail);
        ImGui::SetCursorScreenPos(ImVec2(origin.x + width, origin.y));
        ImGui::Dummy(ImVec2(0.0f, height));
    } else {
    // Session selector: a field that opens the list of running sessions.
        SessionInfo current;
        if (m_client.Attached()) {
            const TimelineHeader& h = *m_client.Header();
            current.pid = m_client.Pid();
            current.processName = HeaderString(h.processName, sizeof(h.processName));
            current.applicationName = HeaderString(h.applicationName, sizeof(h.applicationName));
        }
        const std::string title = m_client.Attached() ? SessionTitle(current) : "Choose an OpenXR app";
        const std::string detail = m_client.Attached() ? SessionDetail(current) : "";
        const float height = ImGui::GetFrameHeight() + Px(6.0f);
        ui::PushSemibold(16.0f);
        const float titleWidth = ImGui::CalcTextSize(title.c_str()).x;
        ui::PopFont();
        const float detailWidth = ImGui::CalcTextSize(detail.c_str()).x;
        const float width = Px(12.0f) + Px(8.0f) + Px(10.0f) + titleWidth + (detail.empty() ? 0.0f : Px(10.0f) + detailWidth) +
                            Px(26.0f) + Px(12.0f);
        const ImVec2 origin = ImGui::GetCursorScreenPos();
        if (ImGui::InvisibleButton("##session", ImVec2(width, height))) {
            m_sessions = DiscoverSessions();
            m_lastDiscover = Clock();
            ImGui::OpenPopup("##sessions");
        }
        const bool hovered = ImGui::IsItemHovered();
        ImDrawList* draw = ImGui::GetWindowDrawList();
        draw->AddRectFilled(origin, ImVec2(origin.x + width, origin.y + height), hovered ? t.fieldHover : t.field, height * 0.5f);
        float x = origin.x + Px(14.0f);
        const float mid = origin.y + height * 0.5f;
        draw->AddCircleFilled(ImVec2(x + Px(4.0f), mid), Px(4.5f), m_client.Attached() ? stateColor : t.textMuted);
        x += Px(18.0f);
        ui::PushSemibold(16.0f);
        draw->AddText(ImVec2(x, mid - ImGui::GetFontSize() * 0.5f), t.text, title.c_str());
        ui::PopFont();
        x += titleWidth + Px(10.0f);
        if (!detail.empty()) {
            draw->AddText(ImVec2(x, mid - ImGui::GetFontSize() * 0.5f), t.textMuted, detail.c_str());
            x += detailWidth + Px(10.0f);
        }
        draw->AddText(ImVec2(origin.x + width - Px(28.0f), mid - ImGui::GetFontSize() * 0.5f), t.textSecondary, ICON_DOWN);
        ImGui::SetNextWindowPos(ImVec2(origin.x, origin.y + height + Px(6.0f)));
        ImGui::SetNextWindowSizeConstraints(ImVec2(std::max(width, Px(320.0f)), 0.0f), ImVec2(Px(640.0f), Px(480.0f)));
        if (ImGui::BeginPopup("##sessions")) {
            ui::Caption("Running OpenXR Simulator sessions");
            ImGui::Spacing();
            if (m_sessions.empty()) ui::Muted("None right now. Start an OpenXR app on the simulator runtime.");
            for (const SessionInfo& s : m_sessions) {
                const bool selected = m_client.Attached() && s.pid == m_client.Pid();
                ImGui::PushID(int(s.pid));
                if (ImGui::Selectable("##pick", selected, 0, ImVec2(0.0f, Px(40.0f))) && !selected) AttachTo(s.pid);
                ImGui::SameLine(Px(12.0f));
                ImGui::BeginGroup();
                ui::PushSemibold(ui::kBodySize);
                ImGui::TextUnformatted(SessionTitle(s).c_str());
                ui::PopFont();
                ui::PushRegular(ui::kSmallSize);
                ui::Muted("%s  \xC2\xB7  %llu frames  \xC2\xB7  %g Hz", SessionDetail(s).c_str(), (unsigned long long)s.frames, s.refreshHz);
                ui::PopFont();
                ImGui::EndGroup();
                ImGui::PopID();
            }
            ImGui::EndPopup();
        }
        ImGui::SetCursorScreenPos(ImVec2(origin.x + width, origin.y));
        ImGui::Dummy(ImVec2(0.0f, height));
    }

    const float rowY = ImGui::GetItemRectMin().y;
    const float rowHeight = ImGui::GetItemRectSize().y;
    auto centerY = [&] { ImGui::SetCursorScreenPos(ImVec2(ImGui::GetCursorScreenPos().x, rowY + (rowHeight - ImGui::GetFrameHeight()) * 0.5f)); };

    if (m_viewingCapture) {
        ImGui::SameLine(0.0f, Px(12.0f));
        centerY();
        if (ui::Pill("##back", m_client.Attached() ? ICON_BACK "  Back to live" : ICON_BACK "  Close capture", true, 0, t)) {
            CloseCapture();
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Esc");
    }
    if (m_client.Attached() && !m_viewingCapture) {
        ImGui::SameLine(0.0f, Px(12.0f));
        centerY();
        // A gently pulsing dot says "live" without shouting.
        const float pulse = float(std::fmod(Clock() * 0.9, 1.0));
        if (ui::Pill("##live", m_live ? "Live" : ICON_PLAY "  Resume", m_live,
                     m_live ? t.onAccent : 0, t, m_live ? pulse : -1.0f)) {
            m_live = !m_live;
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip(m_live ? "Pause to inspect (Space)" : "Follow the newest frames (Space or Home)");
        ImGui::SameLine(0.0f, Px(10.0f));
        centerY();
        int window = 1;
        for (int i = 0; i < 5; ++i) {
            if (std::abs(kLiveWindows[i] - m_settings.liveSeconds) < 0.01f) window = i;
        }
        if (ui::Segmented("##window", kLiveWindowLabels, 5, &window, t)) {
            m_settings.liveSeconds = kLiveWindows[window];
            m_live = true;
            SettingsChanged();
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("How much history the live view shows");

        // Recording: everything from the press until Stop goes into a capture file.
        ImGui::SameLine(0.0f, Px(10.0f));
        centerY();
        if (!m_recording) {
            if (ui::Pill("##record", "Record", false, t.hitch, t)) StartRecording();
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Save what happens from now on as a capture (R)");
        } else {
            const int elapsed = int(Clock() - m_recordStartedAt);
            char text[48];
            snprintf(text, sizeof(text), ICON_STOP "  Stop  %d:%02d", elapsed / 60, elapsed % 60);
            if (ui::Pill("##stop", text, true, 0, t, -1.0f, t.hitch)) StopRecording();
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Stop and save the capture (R)");
            ImGui::SameLine(0.0f, Px(8.0f));
            centerY();
            ImGui::SetNextItemWidth(Px(170.0f));
            ImGui::InputTextWithHint("##recordlabel", "label (optional)", m_recordLabel, sizeof(m_recordLabel));
        }
    }

    // Right side: live state and the theme switch.
    ImGui::SameLine();
    AlignRight(m_headerRightWidth);
    const float rightStart = ImGui::GetCursorPosX();
    centerY();
    ImGui::BeginGroup();
    if (m_client.Attached() && !m_viewingCapture) {
        ImGui::AlignTextToFramePadding();
        ui::Dot(stateColor, 4.0f, true);
        ImGui::SameLine(0.0f, Px(8.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(t.textSecondary));
        ImGui::TextUnformatted(state);
        ImGui::PopStyleColor();
        ImGui::SameLine(0.0f, Px(14.0f));
    }
    if (m_client.Attached() || m_viewingCapture) {
        if (ui::IconButton("##infobutton", ICON_INFO, m_viewingCapture ? "Capture details" : "Session details", t)) {
            ImGui::OpenPopup("##info");
        }
        ImGui::SameLine(0.0f, Px(2.0f));
    }
    if (ui::IconButton("##theme", t.dark ? ICON_MOON : ICON_SUN, "Theme", t)) ImGui::OpenPopup("##themes");
    if (ImGui::BeginPopup("##themes")) {
        const char* names[] = {"Follow Windows", "Light", "Dark"};
        for (int i = 0; i < 3; ++i) {
            if (ImGui::Selectable(names[i], m_settings.theme == i)) {
                m_settings.theme = i;
                m_themeCheckedAt = -10.0;
                SettingsChanged();
            }
        }
        ImGui::EndPopup();
    }
    ImGui::EndGroup();
    m_headerRightWidth = ImGui::GetItemRectSize().x;
    DrawInfoPopup();
    (void)rightStart;
    ImGui::SetCursorScreenPos(ImVec2(ImGui::GetWindowPos().x + ImGui::GetStyle().WindowPadding.x, rowY + rowHeight));
    ImGui::Dummy(ImVec2(0.0f, Px(4.0f)));
}

void App::DrawPicker() {
    const ui::Theme& t = m_theme;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const ImVec2 size(std::min(avail.x, Px(560.0f)), Px(260.0f));
    ImGui::SetCursorPos(ImVec2(ImGui::GetCursorPosX() + (avail.x - size.x) * 0.5f, ImGui::GetCursorPosY() + avail.y * 0.22f));
    if (ui::BeginCard("##picker", size)) {
        ImGui::Dummy(ImVec2(0.0f, Px(6.0f)));
        ui::PushSemibold(22.0f);
        ImGui::TextUnformatted(m_wantedPid ? "Waiting for the app" : "No app selected");
        ui::PopFont();
        ImGui::Spacing();
        ImGui::PushTextWrapPos(0.0f);
        if (m_wantedPid) {
            ui::Muted("Process %lu has not published a frame timeline yet. It appears once the app creates its "
                      "OpenXR instance on the simulator runtime.", m_wantedPid);
        } else if (m_sessions.empty()) {
            ui::Muted("No process is running the OpenXR Simulator. Start an OpenXR app on the simulator runtime "
                      "and it shows up here; the viewer attaches by itself when there is only one.");
        } else {
            ui::Muted("Pick the app to watch:");
        }
        ImGui::PopTextWrapPos();
        if (!m_error.empty()) {
            ImGui::Spacing();
            ui::Dot(t.hitch);
            ImGui::SameLine();
            ImGui::TextWrapped("%s", m_error.c_str());
        }
        ImGui::Spacing();
        if (ImGui::Button(ICON_DOC "  Open a saved capture")) {
            RefreshCaptureList();
            if (!m_captureList.empty()) OpenCapture(m_captureList.front().path);
            else Toast("No captures saved yet.");
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Opens the newest capture; the Captures tab lists the rest");
        ImGui::Spacing();
        for (const SessionInfo& s : m_sessions) {
            ImGui::PushID(int(s.pid));
            const std::string label = SessionTitle(s) + "   " + SessionDetail(s);
            if (ImGui::Button(label.c_str(), ImVec2(-1.0f, 0.0f))) AttachTo(s.pid);
            ImGui::PopID();
        }
    }
    ui::EndCard();
}

void App::DrawStats() {
    const ui::Theme& t = m_theme;
    const an::RangeStats& s = m_stats;
    const an::RangeStats& b = m_baselineStats;
    const double budget = View().BudgetMs(), hitch = View().HitchMs();

    ImGui::AlignTextToFramePadding();
    ui::Caption("STATISTICS");
    ImGui::SameLine(0.0f, Px(12.0f));
    int scope = m_settings.sessionStats ? 1 : 0;
    const char* scopes[] = {"Visible range", "Whole session"};
    if (ui::Segmented("##scope", scopes, 2, &scope, t)) {
        m_settings.sessionStats = scope == 1;
        SettingsChanged();
        UpdateStats(true);
    }
    ImGui::SameLine(0.0f, Px(12.0f));
    ImGui::AlignTextToFramePadding();
    if (s.idleGaps) ui::Muted("%.1f s  \xC2\xB7  %zu frames  \xC2\xB7  %zu idle gap%s left out", s.seconds, s.frames, s.idleGaps, s.idleGaps == 1 ? "" : "s");
    else ui::Muted("%.1f s  \xC2\xB7  %zu frames", s.seconds, s.frames);

    const int kCards = s.gpuFrames ? 9 : 8;
    const float gap = Px(10.0f);
    const float width = (ImGui::GetContentRegionAvail().x - gap * (kCards - 1)) / kCards;
    const float height = Px(92.0f);
    char number[32];
    int card = 0;
    auto openCard = [&](const char* label, const char* help) {
        if (card++) ImGui::SameLine(0.0f, gap);
        ImGui::PushID(card);
        ui::BeginCard("##stat", ImVec2(width, height));
        ui::PushRegular(ui::kSmallSize);
        ui::Muted("%s", label);
        ui::PopFont();
        if (help && ImGui::IsWindowHovered()) ImGui::SetTooltip("%s", help);
    };
    auto closeCard = [&] {
        ui::EndCard();
        ImGui::PopID();
    };
    auto severity = [&](double ms) -> ImU32 { return ms > hitch ? t.hitch : ms > budget + an::kBudgetToleranceMs ? t.over : 0; };

    openCard("Delivered", "Frames submitted per second. Idle gaps (a paused session) are left out.");
    snprintf(number, sizeof(number), "%.1f", s.deliveredFps);
    BigNumber(number, "fps");
    if (m_hasBaseline) Delta(s.deliveredFps, b.deliveredFps, true, "%.1f fps");
    else SubLine(0, "target %.0f Hz", 1000.0 / budget);
    closeCard();

    openCard("Frame time p50", "Median frame time with pacing removed: what each frame actually took.");
    snprintf(number, sizeof(number), "%.2f", s.costP50Ms);
    BigNumber(number, "ms");
    if (m_hasBaseline) Delta(s.costP50Ms, b.costP50Ms, false, "%.2f ms");
    else SubLine(severity(s.costP50Ms), "budget %.2f ms", budget);
    closeCard();

    openCard("Frame time p99", "99% of frames took at most this long.");
    snprintf(number, sizeof(number), "%.2f", s.costP99Ms);
    BigNumber(number, "ms");
    if (m_hasBaseline) Delta(s.costP99Ms, b.costP99Ms, false, "%.2f ms");
    else SubLine(severity(s.costP99Ms), "p99.9  %.2f ms", s.costP999Ms);
    closeCard();

    if (s.gpuFrames) {
        char help[200];
        snprintf(help, sizeof(help),
                 "GPU time per frame on the app's queue (%s), from xrBeginFrame to xrEndFrame. "
                 "99%% of frames' GPU work took at most this long.", tl::GpuApiName(View().GpuApi()));
        openCard("GPU p99", help);
        snprintf(number, sizeof(number), "%.2f", s.gpuP99Ms);
        BigNumber(number, "ms");
        if (m_hasBaseline && b.gpuFrames) Delta(s.gpuP99Ms, b.gpuP99Ms, false, "%.2f ms");
        else if (s.gpuOverBudget) SubLine(t.over, "p50 %.2f ms  \xC2\xB7  %zu late", s.gpuP50Ms, s.gpuOverBudget);
        else SubLine(severity(s.gpuP50Ms), "p50  %.2f ms", s.gpuP50Ms);
        closeCard();
    }

    openCard("Worst frame", nullptr);
    snprintf(number, sizeof(number), "%.1f", s.costMaxMs);
    BigNumber(number, "ms");
    const ImU32 worst = severity(s.costMaxMs);
    if (m_hasBaseline) Delta(s.costMaxMs, b.costMaxMs, false, "%.1f ms");
    else SubLine(worst, "%s", worst == t.hitch ? "hitch" : worst == t.over ? "over budget" : "within budget");
    closeCard();

    openCard("1% low", "1000 / mean frame time of the slowest 1% of frames.");
    snprintf(number, sizeof(number), "%.0f", s.onePercentLowFps);
    BigNumber(number, "fps");
    if (m_hasBaseline) Delta(s.onePercentLowFps, b.onePercentLowFps, true, "%.0f fps");
    else SubLine(0, "slowest 1%% of frames");
    closeCard();

    openCard("Over budget", "Frames a headset at the target rate would not have shown in time, hitches included.");
    snprintf(number, sizeof(number), "%zu", s.overBudget);
    BigNumber(number, "frames");
    const double overShare = s.frames ? 100.0 * double(s.overBudget) / double(s.frames) : 0.0;
    if (m_hasBaseline) {
        Delta(overShare, b.frames ? 100.0 * double(b.overBudget) / double(b.frames) : 0.0, false, "%.1f%% of frames",
              s.frames ? 100.0 / double(s.frames) : 0.0);
    } else {
        SubLine(s.overBudget ? t.over : t.good, "%.1f%%  \xC2\xB7  %zu refreshes missed", overShare, s.missedRefreshes);
    }
    closeCard();

    openCard("Hitches", "Frames costing at least the hitch threshold.");
    snprintf(number, sizeof(number), "%zu", s.hitches);
    BigNumber(number, "frames");
    // Captures differ in length, so hitches compare as a rate.
    auto perMinute = [](const an::RangeStats& r) { return r.seconds > 0.0 ? double(r.hitches) * 60.0 / r.seconds : 0.0; };
    if (m_hasBaseline) Delta(perMinute(s), perMinute(b), false, "%.1f per min", s.seconds > 0.0 ? 60.0 / s.seconds : 0.0);
    else SubLine(s.hitches ? t.hitch : t.good, "at least %.1f ms", hitch);
    closeCard();

    openCard("Headroom p50", "Median time the app sat blocked in xrWaitFrame waiting for its tick.");
    snprintf(number, sizeof(number), "%.2f", s.headroomP50Ms);
    BigNumber(number, "ms");
    if (m_hasBaseline) Delta(s.headroomP50Ms, b.headroomP50Ms, true, "%.2f ms");
    else SubLine(0, "simulator  %.2f ms", s.simulatorP50Ms);
    closeCard();
}

void App::DrawTimeline(float height) {
    if (ui::BeginCard("##timeline", ImVec2(0.0f, height))) {
        DrawTimelineHeader();
        ImGui::Dummy(ImVec2(0.0f, Px(2.0f)));
        DrawOverview(Px(40.0f));
        DrawMain(ImGui::GetContentRegionAvail().y);
    }
    ui::EndCard();
}

void App::DrawTimelineHeader() {
    const ui::Theme& t = m_theme;
    ImGui::AlignTextToFramePadding();
    ui::PushSemibold(16.0f);
    ImGui::TextUnformatted("Frame time");
    ui::PopFont();

    // Legend: every class named beside its color.
    auto key = [&](ImU32 color, const char* label, bool line = false) {
        ImGui::SameLine(0.0f, Px(16.0f));
        ImGui::AlignTextToFramePadding();
        line ? ui::LineKey(color, true) : ui::Swatch(color, true);
        ImGui::SameLine(0.0f, Px(6.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(t.textSecondary));
        ImGui::TextUnformatted(label);
        ImGui::PopStyleColor();
    };
    ImGui::SameLine(0.0f, Px(8.0f));
    key(t.onTime, "On time");
    key(t.over, "Over budget");
    key(t.hitch, "Hitch");
    key(t.twoD, "2D only");
    if (ShowGpu()) key(t.gpu, "GPU", true);
    if (m_settings.showInterval) key(t.interval, "Interval", true);

    // Controls, right-aligned.
    ImGui::SameLine();
    AlignRight(m_controlsWidth);
    ImGui::BeginGroup();
    ImGui::AlignTextToFramePadding();
    ui::Muted("Target");
    ImGui::SameLine(0.0f, Px(6.0f));
    const double runtimeHz = m_viewingCapture ? m_capture.header.refreshHz : m_client.Header()->refreshHz;
    char preview[48];
    snprintf(preview, sizeof(preview), "%g Hz%s", m_settings.refreshHz > 0.0f ? double(m_settings.refreshHz) : runtimeHz,
             m_settings.refreshHz > 0.0f ? "" : m_viewingCapture ? " (recorded)" : " (runtime)");
    if (ui::DropdownButton("##target", preview, Px(120.0f), t)) {
        char runtimeLabel[48];
        snprintf(runtimeLabel, sizeof(runtimeLabel), "%s  \xC2\xB7  %g Hz", m_viewingCapture ? "Recorded rate" : "Runtime rate", runtimeHz);
        if (ImGui::Selectable(runtimeLabel, m_settings.refreshHz <= 0.0f)) { m_settings.refreshHz = 0.0f; SettingsChanged(); }
        ImGui::Separator();
        for (float hz : {72.0f, 80.0f, 90.0f, 120.0f, 144.0f}) {
            char label[48];
            snprintf(label, sizeof(label), "%g Hz  \xC2\xB7  %.2f ms budget", hz, 1000.0 / hz);
            if (ImGui::Selectable(label, m_settings.refreshHz == hz)) { m_settings.refreshHz = hz; SettingsChanged(); }
        }
        ImGui::EndPopup();
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Score every frame against this refresh rate (%.2f ms budget)", View().BudgetMs());
    ImGui::SameLine(0.0f, Px(14.0f));
    ImGui::AlignTextToFramePadding();
    ui::Muted("Hitch at");
    ImGui::SameLine(0.0f, Px(6.0f));
    char hitchLabel[32];
    snprintf(hitchLabel, sizeof(hitchLabel), "%gx budget", m_settings.hitchBudgets);
    if (ui::DropdownButton("##hitch", hitchLabel, Px(110.0f), t)) {
        for (float budgets : {1.5f, 2.0f, 2.5f, 3.0f, 4.0f, 6.0f}) {
            char label[48];
            snprintf(label, sizeof(label), "%gx budget  \xC2\xB7  %.1f ms", budgets, View().BudgetMs() * budgets);
            if (ImGui::Selectable(label, m_settings.hitchBudgets == budgets)) { m_settings.hitchBudgets = budgets; SettingsChanged(); }
        }
        ImGui::EndPopup();
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Frames costing this many budgets or more are hitches (%.1f ms)", View().HitchMs());
    ApplyBudget();
    ImGui::SameLine(0.0f, Px(14.0f));
    int axis = m_settings.frameAxis ? 1 : 0;
    const char* axes[] = {"Time", "Frames"};
    if (ui::Segmented("##axis", axes, 2, &axis, t)) {
        // Keep looking at the same frames across the switch.
        const size_t first = View().LowerBound(Axis(), m_viewX0), last = View().LowerBound(Axis(), m_viewX1);
        m_settings.frameAxis = axis == 1;
        if (View().Size()) {
            const size_t a = std::min(first, View().Size() - 1), b = std::min(last, View().Size() - 1);
            m_viewX0 = View().StartX(Axis(), a);
            m_viewX1 = View().EndX(Axis(), b);
            m_applyView = true;
        }
        SettingsChanged();
    }
    ImGui::SameLine(0.0f, Px(8.0f));
    if (ui::Chip("Log scale", &m_settings.logY, t)) {
        m_fitY = true;
        SettingsChanged();
    }
    ImGui::SameLine(0.0f, Px(6.0f));
    if (ui::Chip("Interval", &m_settings.showInterval, t)) SettingsChanged();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Overlay the raw time between submissions, pacing included");
    if (View().GpuApi()) {
        ImGui::SameLine(0.0f, Px(6.0f));
        if (ui::Chip("GPU", &m_settings.showGpu, t)) SettingsChanged();
        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip("Overlay each frame's GPU time on the app's queue (%s)", tl::GpuApiName(View().GpuApi()));
        }
    }
    ImGui::SameLine(0.0f, Px(8.0f));
    if (ui::IconButton("##fit", ICON_FIT, "Fit the vertical axis (F)", t)) m_fitY = true;
    ImGui::SameLine(0.0f, Px(2.0f));
    if (!m_viewingCapture && ui::IconButton("##clear", ICON_CLEAR, "Clear and start from now", t)) {
        m_client.SkipToNow();
        ResetAnalyzer();
        m_selected = -1;
        m_live = true;
    }
    ImGui::EndGroup();
    m_controlsWidth = ImGui::GetItemRectSize().x;
}

void App::DrawBars(double x0, double x1, double y0, bool forOverview) {
    const auto& metrics = View().Metrics();
    if (metrics.empty()) return;
    const ui::Theme& t = m_theme;
    const an::XAxis axis = Axis();
    ImDrawList* draw = ImPlot::GetPlotDrawList();
    const ImVec2 plotPos = ImPlot::GetPlotPos();
    const ImVec2 plotSize = ImPlot::GetPlotSize();
    const float base = ImPlot::PlotToPixels(0.0, y0).y;
    const float strip = forOverview ? Px(2.0f) : Px(3.0f);
    const size_t first = View().LowerBound(axis, x0);
    const size_t last = std::min(metrics.size(), View().LowerBound(axis, x1) + 1);
    const int columns = std::max(1, int(plotSize.x));
    const float gapPx = Px(2.0f), round = Px(4.0f);

    ImPlot::PushPlotClipRect();
    std::vector<ImVec2> interval;
    // The GPU line breaks wherever a frame has no GPU time.
    const bool gpuLine = !forOverview && ShowGpu();
    std::vector<ImVec2> gpu;
    // Late bars go back over the GPU line: on a 2 px hitch the line would hide the red.
    struct Bar { ImVec2 a, b; ImU32 color; float rounding; };
    std::vector<Bar> late;
    auto isLate = [](an::FrameClass c) { return c == an::FrameClass::OverBudget || c == an::FrameClass::Hitch; };
    auto flushGpu = [&] {
        if (gpu.size() > 1) {
            // A surface ring keeps the line readable where it crosses the bars.
            draw->AddPolyline(gpu.data(), int(gpu.size()), ui::WithAlpha(t.card, 0.9f), ImDrawFlags_None, Px(4.5f));
            draw->AddPolyline(gpu.data(), int(gpu.size()), t.gpu, ImDrawFlags_None, Px(2.0f));
        } else if (gpu.size() == 1) {
            draw->AddCircleFilled(gpu[0], Px(2.5f), t.gpu);
        }
        gpu.clear();
    };
    auto addGpu = [&](float x, float ms) {
        if (std::isnan(ms)) flushGpu();
        else gpu.push_back(ImVec2(x, ImPlot::PlotToPixels(0.0, std::max(double(ms), y0)).y));
    };
    if (last > first && double(last - first) <= plotSize.x) {
        // Few enough to draw every frame at its true width: thin bars with a
        // surface gap between neighbours and a rounded data end.
        for (size_t i = first; i < last; ++i) {
            const an::FrameMetrics& m = metrics[i];
            const float left = ImPlot::PlotToPixels(View().StartX(axis, i), 0.0).x;
            const float right = ImPlot::PlotToPixels(View().EndX(axis, i), 0.0).x;
            if (m.gap) {
                draw->AddRectFilled(ImVec2(left, plotPos.y), ImVec2(right, base), ui::WithAlpha(t.gap, 0.35f));
                if (gpuLine) flushGpu();
                continue;
            }
            const float width = right - left;
            const float inset = width > Px(5.0f) ? gapPx * 0.5f : 0.0f;
            const float a = left + inset, b = std::max(a + 1.0f, right - inset);
            const float top = ImPlot::PlotToPixels(0.0, std::max(double(m.costMs), y0)).y;
            const bool hover = !forOverview && int64_t(i) == m_hovered;
            ImU32 color = ClassColor(m.cls);
            if (hover) color = ui::WithAlpha(color, 0.75f);
            draw->AddRectFilled(ImVec2(a, top), ImVec2(b, base), color, b - a > Px(6.0f) ? round : 0.0f,
                                ImDrawFlags_RoundCornersTop);
            if (gpuLine && isLate(m.cls)) late.push_back({ImVec2(a, top), ImVec2(b, base), color, b - a > Px(6.0f) ? round : 0.0f});
            if (View().Records()[i].flags & tl::kFrameNoProjection) {
                draw->AddRectFilled(ImVec2(a, base - strip), ImVec2(b, base), t.twoD);
            }
            if (!forOverview && int64_t(i) == m_selected) {
                draw->AddRect(ImVec2(a - Px(2.0f), top - Px(2.0f)), ImVec2(b + Px(2.0f), base), t.text,
                              Px(5.0f), ImDrawFlags_RoundCornersTop, Px(2.0f));
            }
            if (!forOverview && m_settings.showInterval) {
                interval.push_back(ImVec2((left + right) * 0.5f, ImPlot::PlotToPixels(0.0, std::max(double(m.intervalMs), y0)).y));
            }
            if (gpuLine) addGpu((left + right) * 0.5f, m.gpuMs);
        }
    } else {
        // One bar per pixel column, at the column's worst frame.
        std::vector<an::Column>& cols = forOverview ? m_overviewColumns : m_columns;
        View().Columns(axis, x0, x1, columns, cols);
        for (int c = 0; c < columns; ++c) {
            const an::Column& col = cols[size_t(c)];
            const float left = plotPos.x + float(c);
            if (col.noProjection) draw->AddRectFilled(ImVec2(left, base - strip), ImVec2(left + 1.0f, base), t.twoD);
            if (col.maxIndex == UINT32_MAX) {
                if (gpuLine) flushGpu();
                continue;
            }
            const float top = ImPlot::PlotToPixels(0.0, std::max(double(col.maxCostMs), y0)).y;
            draw->AddRectFilled(ImVec2(left, top), ImVec2(left + 1.0f, base - (col.noProjection ? strip : 0.0f)),
                                ClassColor(metrics[col.maxIndex].cls));
            if (gpuLine && isLate(metrics[col.maxIndex].cls)) {
                late.push_back({ImVec2(left, top), ImVec2(left + 1.0f, base - (col.noProjection ? strip : 0.0f)),
                                ClassColor(metrics[col.maxIndex].cls), 0.0f});
            }
            if (!forOverview && m_settings.showInterval) {
                interval.push_back(ImVec2(left + 0.5f, ImPlot::PlotToPixels(0.0, std::max(double(col.maxIntervalMs), y0)).y));
            }
            if (gpuLine) addGpu(left + 0.5f, col.maxGpuMs);
        }
        // Long stalls keep their true width.
        View().WideFrames(axis, x0, x1, 3.0 * (x1 - x0) / double(columns), m_wide);
        for (uint32_t i : m_wide) {
            const an::FrameMetrics& m = metrics[i];
            const ImVec2 a = ImPlot::PlotToPixels(View().StartX(axis, i), std::max(double(m.costMs), y0));
            const float b = ImPlot::PlotToPixels(View().EndX(axis, i), y0).x;
            draw->AddRectFilled(ImVec2(a.x + 1.0f, a.y), ImVec2(b - 1.0f, base), ui::WithAlpha(ClassColor(m.cls), 0.55f),
                                round, ImDrawFlags_RoundCornersTop);
        }
        if (!forOverview && m_selected >= 0 && size_t(m_selected) < metrics.size()) {
            const an::FrameMetrics& m = metrics[size_t(m_selected)];
            const float x = ImPlot::PlotToPixels(View().EndX(axis, size_t(m_selected)), 0.0).x;
            const float top = ImPlot::PlotToPixels(0.0, std::max(double(m.costMs), y0)).y;
            draw->AddRect(ImVec2(x - Px(3.0f), top - Px(3.0f)), ImVec2(x + Px(3.0f), base), t.text, Px(3.0f), 0, Px(2.0f));
        }
    }
    if (gpuLine) flushGpu();
    for (const Bar& bar : late) draw->AddRectFilled(bar.a, bar.b, bar.color, bar.rounding, ImDrawFlags_RoundCornersTop);
    if (interval.size() > 1) {
        draw->AddPolyline(interval.data(), int(interval.size()), t.interval, ImDrawFlags_None, Px(1.5f));
    }
    ImPlot::PopPlotClipRect();
}

void App::DrawReferenceLine(double y, ImU32 color, const char* label) {
    ImDrawList* draw = ImPlot::GetPlotDrawList();
    const ImVec2 pos = ImPlot::GetPlotPos(), size = ImPlot::GetPlotSize();
    const float py = ImPlot::PlotToPixels(0.0, y).y;
    if (py < pos.y || py > pos.y + size.y) return;
    ImPlot::PushPlotClipRect();
    draw->AddLine(ImVec2(pos.x, py), ImVec2(pos.x + size.x, py), ui::WithAlpha(color, 0.85f), Px(1.0f));
    ui::PushRegular(ui::kSmallSize);
    const ImVec2 text = ImGui::CalcTextSize(label);
    const ImVec2 at(pos.x + size.x - text.x - Px(10.0f), py - text.y - Px(3.0f));
    draw->AddRectFilled(ImVec2(at.x - Px(5.0f), at.y - Px(1.0f)), ImVec2(at.x + text.x + Px(5.0f), at.y + text.y + Px(1.0f)),
                        ui::WithAlpha(m_theme.card, 0.85f), Px(4.0f));
    draw->AddText(at, m_theme.textSecondary, label);
    ui::PopFont();
    ImPlot::PopPlotClipRect();
}

void App::DrawMarkers(double x0, double x1, bool interactive) {
    const ui::Theme& t = m_theme;
    ImDrawList* draw = ImPlot::GetPlotDrawList();
    const ImVec2 plotPos = ImPlot::GetPlotPos();
    const ImVec2 plotSize = ImPlot::GetPlotSize();
    const ImVec2 mouse = ImGui::GetMousePos();
    const tl::MarkerRecord* hovered = nullptr;
    float lastLabel = -1e9f;
    ImPlot::PushPlotClipRect();
    ui::PushRegular(12.0f);
    for (const tl::MarkerRecord& marker : View().Markers()) {
        const double x = MarkerX(marker);
        if (x < x0 || x > x1) continue;
        const float px = std::floor(ImPlot::PlotToPixels(x, 0.0).x) + 0.5f;
        const bool alarm = tl::MarkerKind(marker.kind) == tl::MarkerKind::EndFrameReentry;
        const ImU32 color = alarm ? t.hitch : t.textMuted;
        draw->AddLine(ImVec2(px, plotPos.y), ImVec2(px, plotPos.y + plotSize.y), ui::WithAlpha(color, interactive ? 0.45f : 0.30f), 1.0f);
        if (!interactive) continue;
        const char* tag = MarkerTag(marker.kind);
        const ImVec2 size = ImGui::CalcTextSize(tag);
        if (px - lastLabel > size.x + Px(16.0f)) {
            const ImVec2 at(px + Px(4.0f), plotPos.y + Px(4.0f));
            draw->AddRectFilled(ImVec2(at.x - Px(3.0f), at.y - Px(1.0f)), ImVec2(at.x + size.x + Px(3.0f), at.y + size.y + Px(1.0f)),
                                ui::WithAlpha(t.card, 0.9f), Px(3.0f));
            draw->AddText(at, alarm ? t.hitch : t.textMuted, tag);
            lastLabel = px;
        }
        if (ImPlot::IsPlotHovered() && std::abs(mouse.x - px) <= Px(3.0f)) hovered = &marker;
    }
    ui::PopFont();
    ImPlot::PopPlotClipRect();
    if (hovered) {
        ImGui::BeginTooltip();
        ImGui::TextUnformatted(MarkerText(*hovered).c_str());
        ui::PushRegular(ui::kSmallSize);
        ui::Muted("%.3f s  \xC2\xB7  after frame %llu", View().Sec(hovered->qpc), (unsigned long long)hovered->frameId);
        ui::PopFont();
        ImGui::EndTooltip();
        m_hovered = -1;
    }
}

void App::DrawOverview(float height) {
    const ui::Theme& t = m_theme;
    ImPlot::PushStyleColor(ImPlotCol_PlotBg, ImGui::ColorConvertU32ToFloat4(t.field));
    ImPlot::PushStyleVar(ImPlotStyleVar_PlotPadding, ImVec2(0.0f, 0.0f));
    if (ImPlot::BeginPlot("##overview", ImVec2(-1.0f, height),
                          ImPlotFlags_CanvasOnly | ImPlotFlags_NoInputs | ImPlotFlags_NoFrame)) {
        const double left = View().Size() ? View().StartX(Axis(), 0) : 0.0;
        const double right = std::max(left + 1e-3, RightEdge());
        const double top = std::max(View().BudgetMs() * 3.0, View().HitchMs() * 1.6);
        ImPlot::SetupAxes(nullptr, nullptr, ImPlotAxisFlags_NoDecorations, ImPlotAxisFlags_NoDecorations);
        ImPlot::SetupAxisLimits(ImAxis_X1, left, right, ImPlotCond_Always);
        ImPlot::SetupAxisLimits(ImAxis_Y1, 0.0, top, ImPlotCond_Always);
        ImPlot::SetupFinish();
        DrawBars(left, right, 0.0, true);
        DrawMarkers(left, right, false);

        // The main view's window onto the session.
        ImDrawList* draw = ImPlot::GetPlotDrawList();
        const ImVec2 pos = ImPlot::GetPlotPos(), size = ImPlot::GetPlotSize();
        float a = ImPlot::PlotToPixels(m_viewX0, 0.0).x, b = ImPlot::PlotToPixels(m_viewX1, 0.0).x;
        a = std::max(a, pos.x);
        b = std::max(std::min(b, pos.x + size.x), a + Px(3.0f));
        ImPlot::PushPlotClipRect();
        draw->AddRectFilled(ImVec2(a, pos.y), ImVec2(b, pos.y + size.y), ui::WithAlpha(t.accent, t.dark ? 0.16f : 0.10f), Px(4.0f));
        draw->AddRect(ImVec2(a, pos.y), ImVec2(b, pos.y + size.y), ui::WithAlpha(t.accent, 0.8f), Px(4.0f), 0, Px(1.5f));
        ImPlot::PopPlotClipRect();
        // NoInputs leaves ImPlot's own hover tracking off, so hit-test the canvas directly.
        if (ImGui::IsWindowHovered() && ImGui::IsMouseHoveringRect(pos, ImVec2(pos.x + size.x, pos.y + size.y))) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) CenterOn(ImPlot::PixelsToPlot(ImGui::GetMousePos()).x);
        }
        ImPlot::EndPlot();
    }
    ImPlot::PopStyleVar();
    ImPlot::PopStyleColor();
}

void App::DrawMain(float height) {
    const ui::Theme& t = m_theme;
    m_hovered = -1;
    if (!ImPlot::BeginPlot("##frametime", ImVec2(-1.0f, height), ImPlotFlags_NoLegend | ImPlotFlags_NoMenus | ImPlotFlags_NoMouseText)) return;
    const bool logY = m_settings.logY;
    ImPlot::SetupAxes(nullptr, nullptr, 0, logY ? 0 : ImPlotAxisFlags_LockMin);
    ImPlot::SetupAxisFormat(ImAxis_X1, m_settings.frameAxis ? "%g" : "%g s");
    ImPlot::SetupAxisFormat(ImAxis_Y1, "%g ms");
    if (logY) ImPlot::SetupAxisScale(ImAxis_Y1, ImPlotScale_Log10);
    const double right = RightEdge();
    if (m_live) {
        const double span = m_settings.frameAxis ? m_settings.liveSeconds * (1000.0 / View().BudgetMs())
                                                 : m_settings.liveSeconds;
        ImPlot::SetupAxisLimits(ImAxis_X1, right - span, right + span * 0.012, ImPlotCond_Always);
    } else if (m_applyView) {
        ImPlot::SetupAxisLimits(ImAxis_X1, m_viewX0, m_viewX1, ImPlotCond_Always);
        m_applyView = false;
    }
    const double floorY = logY ? 0.5 : 0.0;
    if (m_fitY || m_live) {
        // Room for everything on screen, never less than the hitch line.
        double top = View().HitchMs() * 1.35;
        const size_t first = View().LowerBound(Axis(), m_viewX0);
        const size_t last = std::min(View().Size(), View().LowerBound(Axis(), m_viewX1) + 1);
        for (size_t i = first; i < last; ++i) {
            if (!View().Metrics()[i].gap) top = std::max(top, double(View().Metrics()[i].costMs) * 1.12);
        }
        // Live, the axis jumps up at once (a spike is never clipped) and eases
        // back down after it scrolls away, so the bars do not jitter.
        if (m_fitY || m_yTop <= 0.0 || top > m_yTop) m_yTop = top;
        else m_yTop += (top - m_yTop) * 0.04;
        ImPlot::SetupAxisLimits(ImAxis_Y1, floorY, m_yTop, ImPlotCond_Always);
        m_fitY = false;
    } else {
        ImPlot::SetupAxisLimits(ImAxis_Y1, floorY, View().HitchMs() * 1.6, ImPlotCond_Once);
    }
    ImPlot::SetupFinish();

    const ImPlotRect limits = ImPlot::GetPlotLimits();
    m_viewX0 = limits.X.Min;
    m_viewX1 = limits.X.Max;
    const ImGuiIO& io = ImGui::GetIO();
    if (m_live && ImPlot::IsPlotHovered() &&
        (io.MouseWheel != 0.0f || ImGui::IsMouseDragging(ImGuiMouseButton_Left) || ImGui::IsMouseDragging(ImGuiMouseButton_Right))) {
        m_live = false;  // the user took the view
    }

    const double y0 = logY ? limits.Y.Min : 0.0;
    // Hover first, so the bar under the mouse is highlighted this frame.
    if (ImPlot::IsPlotHovered() && View().Size()) {
        const ImPlotPoint mouse = ImPlot::GetPlotMousePos();
        const size_t first = View().LowerBound(Axis(), limits.X.Min);
        const size_t last = std::min(View().Size(), View().LowerBound(Axis(), limits.X.Max) + 1);
        if (last > first && double(last - first) <= ImPlot::GetPlotSize().x) {
            const size_t i = View().LowerBound(Axis(), mouse.x);
            if (i < View().Size() && View().StartX(Axis(), i) <= mouse.x) m_hovered = int64_t(i);
        } else if (!m_columns.empty()) {
            const int c = int(ImGui::GetMousePos().x - ImPlot::GetPlotPos().x);
            if (c >= 0 && size_t(c) < m_columns.size() && m_columns[size_t(c)].maxIndex != UINT32_MAX) {
                m_hovered = m_columns[size_t(c)].maxIndex;
            }
        }
    }
    DrawBars(limits.X.Min, limits.X.Max, y0, false);

    char label[64];
    snprintf(label, sizeof(label), "budget  %.2f ms", View().BudgetMs());
    DrawReferenceLine(View().BudgetMs(), t.textSecondary, label);
    snprintf(label, sizeof(label), "hitch  %.1f ms", View().HitchMs());
    DrawReferenceLine(View().HitchMs(), t.hitch, label);
    DrawMarkers(limits.X.Min, limits.X.Max, true);

    if (m_hovered >= 0) {
        // Crosshair on the hovered frame.
        const ImVec2 pos = ImPlot::GetPlotPos(), size = ImPlot::GetPlotSize();
        const double x = (View().StartX(Axis(), size_t(m_hovered)) + View().EndX(Axis(), size_t(m_hovered))) * 0.5;
        const float px = std::floor(ImPlot::PlotToPixels(x, 0.0).x) + 0.5f;
        ImPlot::PushPlotClipRect();
        ImPlot::GetPlotDrawList()->AddLine(ImVec2(px, pos.y), ImVec2(px, pos.y + size.y), ui::WithAlpha(t.textSecondary, 0.35f), 1.0f);
        ImPlot::PopPlotClipRect();
        // A floor on the width: tooltips size to the previous frame's content,
        // so a wider line than the last frame's would otherwise clip for a frame.
        ImGui::SetNextWindowSizeConstraints(ImVec2(Px(230.0f), 0.0f), ImVec2(FLT_MAX, FLT_MAX));
        ImGui::BeginTooltip();
        FrameTooltip(size_t(m_hovered));
        ImGui::EndTooltip();
        const ImVec2 drag = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left, 0.0f);
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left) && std::abs(drag.x) < 3.0f && std::abs(drag.y) < 3.0f) {
            m_selected = m_hovered;
            m_live = false;
        }
    }
    ImPlot::EndPlot();
}

void App::FrameTooltip(size_t index) {
    const an::FrameMetrics& m = View().Metrics()[index];
    const tl::FrameRecord& r = View().Records()[index];
    ui::PushSemibold(ui::kBodySize);
    ImGui::Text("Frame %llu", (unsigned long long)r.frameId);
    ui::PopFont();
    ui::Dot(ClassColor(m.cls) | IM_COL32_A_MASK);
    ImGui::SameLine(0.0f, Px(6.0f));
    ImGui::TextUnformatted(an::ClassName(m.cls));
    if (m.cause != an::Cause::None) {
        ImGui::SameLine(0.0f, Px(6.0f));
        ui::Muted("\xC2\xB7  %s", an::CauseName(m.cause));
    }
    ImGui::Spacing();
    const float column = Px(92.0f);
    auto row = [&](const char* key, const char* fmt, double value) {
        ui::Muted("%s", key);
        ImGui::SameLine(column);
        ImGui::Text(fmt, value);
    };
    row("Frame time", "%.2f ms", m.costMs);
    row("CPU", "%.2f ms", m.cpuMs);
    if (!std::isnan(m.gpuMs)) row("GPU", "%.2f ms", m.gpuMs);
    if (m.gpuWaitMs > 0.0f) row("GPU wait", "%.2f ms", m.gpuWaitMs);
    if (m.missed) row("Missed", m.missed == 1 ? "%.0f refresh" : "%.0f refreshes", m.missed);
    row("Interval", "%.2f ms", m.intervalMs);
    row("Headroom", "%.2f ms", m.blockedMs);
    row("Simulator", "%.2f ms", m.simulatorMs);
    ImGui::Spacing();
    ui::PushRegular(ui::kSmallSize);
    ui::Muted("%.3f s  \xC2\xB7  click to inspect", m.endSec);
    ui::PopFont();
}

void App::DrawBottom(float height) {
    const float gap = Px(10.0f);
    const float width = ImGui::GetContentRegionAvail().x;
    const float histogram = std::floor(width * 0.29f), lists = std::floor(width * 0.39f);
    if (ui::BeginCard("##histogram", ImVec2(histogram, height))) DrawHistogram();
    ui::EndCard();
    ImGui::SameLine(0.0f, gap);
    if (ui::BeginCard("##lists", ImVec2(lists, height))) {
        if (ImGui::BeginTabBar("##tabs")) {
            char lateLabel[64];
            snprintf(lateLabel, sizeof(lateLabel), "Late frames  %zu###late", View().LateFrames().size());
            if (ImGui::BeginTabItem(lateLabel)) {
                DrawLateFrames();
                ImGui::EndTabItem();
            }
            char markerLabel[64];
            snprintf(markerLabel, sizeof(markerLabel), "Events  %zu###markers", View().Markers().size());
            if (ImGui::BeginTabItem(markerLabel)) {
                DrawMarkerList();
                ImGui::EndTabItem();
            }
            if (ImGui::BeginTabItem("Captures###captures")) {
                DrawCaptureList();
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
        }
    }
    ui::EndCard();
    ImGui::SameLine(0.0f, gap);
    if (ui::BeginCard("##inspector", ImVec2(0.0f, height), 0, true)) DrawInspector();
    ui::EndCard();
}

void App::DrawHistogram() {
    const ui::Theme& t = m_theme;
    ImGui::AlignTextToFramePadding();
    ui::PushSemibold(ui::kBodySize);
    ImGui::TextUnformatted("Distribution");
    ui::PopFont();
    ImGui::SameLine(0.0f, Px(8.0f));
    ImGui::AlignTextToFramePadding();
    ui::Muted("frame time, %s", m_settings.sessionStats ? "whole session" : "visible range");
    const size_t bins = m_histogram[0].size();
    if (!bins) return;
    std::vector<double> totals(bins), baseline;
    for (size_t b = 0; b < bins; ++b) totals[b] = m_histogram[0][b] + m_histogram[1][b] + m_histogram[2][b];
    // While comparing, each distribution is a share of its own frames: the two
    // need not be the same length.
    const bool compare = m_hasBaseline && m_baselineHistogram[0].size() == bins;
    if (compare) {
        ImGui::SameLine(0.0f, Px(12.0f));
        ImGui::AlignTextToFramePadding();
        ui::LineKey(t.text, true);
        ImGui::SameLine(0.0f, Px(6.0f));
        ImGui::AlignTextToFramePadding();
        ui::Muted("baseline");
        baseline.resize(bins);
        double count = 0.0, baselineCount = 0.0;
        for (size_t b = 0; b < bins; ++b) {
            baseline[b] = m_baselineHistogram[0][b] + m_baselineHistogram[1][b] + m_baselineHistogram[2][b];
            count += totals[b];
            baselineCount += baseline[b];
        }
        for (size_t b = 0; b < bins; ++b) {
            totals[b] = count > 0.0 ? 100.0 * totals[b] / count : 0.0;
            baseline[b] = baselineCount > 0.0 ? 100.0 * baseline[b] / baselineCount : 0.0;
        }
    }
    double peak = 1.0;
    for (size_t b = 0; b < bins; ++b) peak = std::max({peak, totals[b], compare ? baseline[b] : 0.0});
    if (!ImPlot::BeginPlot("##hist", ImVec2(-1.0f, -1.0f), ImPlotFlags_NoLegend | ImPlotFlags_NoMenus | ImPlotFlags_NoInputs | ImPlotFlags_NoMouseText)) return;
    ImPlot::SetupAxes(nullptr, nullptr, 0, 0);
    ImPlot::SetupAxisFormat(ImAxis_X1, "%g ms");
    ImPlot::SetupAxisFormat(ImAxis_Y1, compare ? "%g%%" : "%g");
    ImPlot::SetupAxisScale(ImAxis_Y1, ImPlotScale_SymLog);
    ImPlot::SetupAxisLimits(ImAxis_X1, 0.0, m_histogramBinMs * double(bins), ImPlotCond_Always);
    ImPlot::SetupAxisLimits(ImAxis_Y1, 0.0, peak * 1.6, ImPlotCond_Always);
    ImPlot::SetupFinish();

    // Bars colored by the class most of each bin's frames fall in.
    ImDrawList* draw = ImPlot::GetPlotDrawList();
    const ImU32 colors[3] = {t.onTime, t.over, t.hitch};
    const float base = ImPlot::PlotToPixels(0.0, 0.0).y;
    const ImVec2 mouse = ImGui::GetMousePos();
    int hoveredBin = -1;
    ImPlot::PushPlotClipRect();
    for (size_t b = 0; b < bins; ++b) {
        if (totals[b] <= 0.0) continue;
        int cls = 0;
        for (int c = 1; c < 3; ++c) if (m_histogram[c][b] > m_histogram[cls][b]) cls = c;
        const float a = ImPlot::PlotToPixels(double(b) * m_histogramBinMs, 0.0).x + Px(1.0f);
        const float e = ImPlot::PlotToPixels(double(b + 1) * m_histogramBinMs, 0.0).x - Px(1.0f);
        const float top = ImPlot::PlotToPixels(0.0, totals[b]).y;
        draw->AddRectFilled(ImVec2(a, top), ImVec2(std::max(a + 1.0f, e), base), colors[cls], e - a > Px(6.0f) ? Px(3.0f) : 0.0f,
                            ImDrawFlags_RoundCornersTop);
        if (ImPlot::IsPlotHovered() && mouse.x >= a - Px(1.0f) && mouse.x < e + Px(1.0f)) hoveredBin = int(b);
    }
    if (compare) {
        // The baseline as a stepped outline over the bars.
        std::vector<ImVec2> outline;
        for (size_t b = 0; b < bins; ++b) {
            const float y = ImPlot::PlotToPixels(0.0, baseline[b]).y;
            outline.push_back(ImVec2(ImPlot::PlotToPixels(double(b) * m_histogramBinMs, 0.0).x, y));
            outline.push_back(ImVec2(ImPlot::PlotToPixels(double(b + 1) * m_histogramBinMs, 0.0).x, y));
        }
        draw->AddPolyline(outline.data(), int(outline.size()), ui::WithAlpha(t.text, 0.85f), ImDrawFlags_None, Px(1.75f));
    }
    ImPlot::PopPlotClipRect();
    char label[48];
    // Reference lines, each labelled at the top; a label that would overlap one
    // already placed drops to the next row instead.
    struct Placed { float x0, x1; int row; };
    std::vector<Placed> placed;
    auto vline = [&](double x, ImU32 color, const char* text) {
        const ImVec2 pos = ImPlot::GetPlotPos(), size = ImPlot::GetPlotSize();
        const float px = std::floor(ImPlot::PlotToPixels(x, 0.0).x) + 0.5f;
        if (px < pos.x || px > pos.x + size.x) return;
        ImPlot::PushPlotClipRect();
        draw->AddLine(ImVec2(px, pos.y), ImVec2(px, pos.y + size.y), ui::WithAlpha(color, 0.8f), 1.0f);
        ui::PushRegular(12.0f);
        const ImVec2 extent = ImGui::CalcTextSize(text);
        float x0 = px + Px(4.0f);
        if (x0 + extent.x > pos.x + size.x) x0 = px - Px(4.0f) - extent.x;  // flip left at the right edge
        int row = 0;
        for (bool clash = true; clash && row < 4;) {
            clash = false;
            for (const Placed& p : placed) {
                if (p.row == row && x0 < p.x1 + Px(6.0f) && x0 + extent.x + Px(6.0f) > p.x0) {
                    clash = true;
                    ++row;
                    break;
                }
            }
        }
        placed.push_back({x0, x0 + extent.x, row});
        const ImVec2 at(x0, pos.y + Px(2.0f) + float(row) * (extent.y + Px(2.0f)));
        draw->AddRectFilled(ImVec2(at.x - Px(3.0f), at.y), ImVec2(at.x + extent.x + Px(3.0f), at.y + extent.y),
                            ui::WithAlpha(t.card, 0.85f), Px(3.0f));
        draw->AddText(at, t.textSecondary, text);
        ui::PopFont();
        ImPlot::PopPlotClipRect();
    };
    snprintf(label, sizeof(label), "p99 %.1f", m_stats.costP99Ms);
    vline(m_stats.costP99Ms, t.accent, label);
    vline(View().BudgetMs(), t.textSecondary, "budget");
    vline(View().HitchMs(), t.hitch, "hitch");
    if (hoveredBin >= 0) {
        const double lo = hoveredBin * m_histogramBinMs, hi = lo + m_histogramBinMs;
        ImGui::BeginTooltip();
        if (size_t(hoveredBin) + 1 == bins) ImGui::Text("%.1f ms or more", lo);
        else ImGui::Text("%.1f - %.1f ms", lo, hi);
        if (compare) {
            ui::Muted("%.2f%% of frames, baseline %.2f%%", totals[size_t(hoveredBin)], baseline[size_t(hoveredBin)]);
        } else {
            ui::Muted("%.0f frames", totals[size_t(hoveredBin)]);
        }
        ImGui::EndTooltip();
    }
    ImPlot::EndPlot();
}

void App::DrawLateFrames() {
    const ui::Theme& t = m_theme;
    ImGui::Dummy(ImVec2(0.0f, Px(2.0f)));
    if (ui::Chip("Hitches only", &m_settings.hitchesOnly, t)) {
        m_lateRowsDirty = true;
        SettingsChanged();
    }
    ImGui::SameLine(0.0f, Px(10.0f));
    ImGui::AlignTextToFramePadding();
    ui::Muted("N and Shift+N step through them");
    const ImGuiTableFlags flags = ImGuiTableFlags_ScrollY | ImGuiTableFlags_RowBg | ImGuiTableFlags_Sortable |
                                  ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_Resizable |
                                  ImGuiTableFlags_BordersInnerH;
    if (!ImGui::BeginTable("##late", 7, flags)) return;
    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("Time", ImGuiTableColumnFlags_DefaultSort, 0.9f, 0);
    ImGui::TableSetupColumn("Frame", 0, 0.9f, 1);
    ImGui::TableSetupColumn("Frame time", ImGuiTableColumnFlags_PreferSortDescending, 1.1f, 2);
    ImGui::TableSetupColumn("Interval", ImGuiTableColumnFlags_PreferSortDescending, 1.0f, 3);
    ImGui::TableSetupColumn("Missed", ImGuiTableColumnFlags_PreferSortDescending, 0.7f, 4);
    ImGui::TableSetupColumn("Class", 0, 1.2f, 5);
    ImGui::TableSetupColumn("Cause", 0, 1.1f, 6);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(t.textMuted));
    ImGui::TableHeadersRow();
    ImGui::PopStyleColor();

    const auto& late = View().LateFrames();
    const auto& metrics = View().Metrics();
    ImGuiTableSortSpecs* sort = ImGui::TableGetSortSpecs();
    // New frames rebuild the list at most twice a second: it is sorted, and a
    // GPU-bound app makes nearly every frame a row.
    if (m_lateRowsFor != late.size()) m_lateRowsStale = true;
    const bool due = m_lateRowsStale && Clock() - m_lateRowsAt >= 0.5;
    if (m_lateRowsDirty || due || (sort && sort->SpecsDirty)) {
        m_lateRows.clear();
        for (uint32_t i : late) {
            if (!m_settings.hitchesOnly || metrics[i].cls == an::FrameClass::Hitch) m_lateRows.push_back(i);
        }
        if (sort && sort->SpecsCount > 0) {
            const ImGuiTableColumnSortSpecs& spec = sort->Specs[0];
            const bool descending = spec.SortDirection == ImGuiSortDirection_Descending;
            auto key = [&](uint32_t i) -> double {
                switch (spec.ColumnUserID) {
                    case 2: return metrics[i].costMs;
                    case 3: return metrics[i].intervalMs;
                    case 4: return metrics[i].missed;
                    case 5: return double(metrics[i].cls);
                    case 6: return double(metrics[i].cause);
                    default: return double(i);
                }
            };
            std::stable_sort(m_lateRows.begin(), m_lateRows.end(), [&](uint32_t a, uint32_t b) {
                return descending ? key(a) > key(b) : key(a) < key(b);
            });
        }
        if (sort) sort->SpecsDirty = false;
        m_lateRowsDirty = false;
        m_lateRowsStale = false;
        m_lateRowsAt = Clock();
        m_lateRowsFor = late.size();
    }

    if (m_lateRows.empty()) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ui::Muted("%s", m_settings.hitchesOnly ? "No hitches yet." : "Every frame so far made its refresh.");
    }
    ImGuiListClipper clipper;
    clipper.Begin(int(m_lateRows.size()));
    while (clipper.Step()) {
        for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
            const uint32_t i = m_lateRows[size_t(row)];
            const an::FrameMetrics& m = metrics[i];
            ImGui::TableNextRow(0, Px(26.0f));
            ImGui::TableNextColumn();
            char label[48];
            snprintf(label, sizeof(label), "%.3f s##%u", m.endSec, i);
            if (ImGui::Selectable(label, int64_t(i) == m_selected, ImGuiSelectableFlags_SpanAllColumns)) Select(i, true);
            ImGui::TableNextColumn();
            ImGui::Text("%llu", (unsigned long long)View().Records()[i].frameId);
            ImGui::TableNextColumn();
            ImGui::Text("%.2f ms", m.costMs);
            ImGui::TableNextColumn();
            ImGui::Text("%.2f ms", m.intervalMs);
            ImGui::TableNextColumn();
            ImGui::Text("%u", unsigned(m.missed));
            ImGui::TableNextColumn();
            ui::Dot(ClassColor(m.cls), 3.5f);
            ImGui::SameLine(0.0f, Px(6.0f));
            ImGui::TextUnformatted(an::ClassName(m.cls));
            ImGui::TableNextColumn();
            ui::Muted("%s", an::CauseName(m.cause));
        }
    }
    ImGui::EndTable();
}

void App::DrawMarkerList() {
    const ui::Theme& t = m_theme;
    const ImGuiTableFlags flags = ImGuiTableFlags_ScrollY | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                                  ImGuiTableFlags_Resizable | ImGuiTableFlags_BordersInnerH;
    ImGui::Dummy(ImVec2(0.0f, Px(2.0f)));
    if (!ImGui::BeginTable("##markers", 3, flags)) return;
    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("Time", 0, 0.8f);
    ImGui::TableSetupColumn("After frame", 0, 0.8f);
    ImGui::TableSetupColumn("Event", 0, 3.2f);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(t.textMuted));
    ImGui::TableHeadersRow();
    ImGui::PopStyleColor();
    const auto& markers = View().Markers();
    ImGuiListClipper clipper;
    clipper.Begin(int(markers.size()));
    while (clipper.Step()) {
        for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
            const tl::MarkerRecord& m = markers[size_t(row)];
            ImGui::TableNextRow(0, Px(26.0f));
            ImGui::TableNextColumn();
            char label[48];
            snprintf(label, sizeof(label), "%.3f s##m%d", View().Sec(m.qpc), row);
            if (ImGui::Selectable(label, false, ImGuiSelectableFlags_SpanAllColumns)) CenterOn(MarkerX(m));
            ImGui::TableNextColumn();
            ImGui::Text("%llu", (unsigned long long)m.frameId);
            ImGui::TableNextColumn();
            const bool alarm = tl::MarkerKind(m.kind) == tl::MarkerKind::EndFrameReentry;
            ui::Dot(alarm ? t.hitch : t.textMuted, 3.0f);
            ImGui::SameLine(0.0f, Px(6.0f));
            ImGui::TextUnformatted(MarkerText(m).c_str());
        }
    }
    ImGui::EndTable();
}

void App::DrawInspector() {
    const ui::Theme& t = m_theme;
    // A pinned frame, or else the worst one in view: the panel is never empty
    // while there is something worth explaining.
    const bool pinned = m_selected >= 0 && size_t(m_selected) < View().Size();
    const int64_t shown = pinned ? m_selected : m_worstIndex;
    ImGui::AlignTextToFramePadding();
    ui::Caption(pinned ? "SELECTED FRAME" : "WORST FRAME IN VIEW");
    ImGui::SameLine();
    AlignRight(ImGui::GetFrameHeight() * (pinned ? 3.0f : 1.0f) + Px(4.0f));
    if (pinned) {
        if (ui::IconButton("##prev", ICON_LEFT, "Previous frame (Left)", t)) Select(m_selected - 1, false);
        ImGui::SameLine(0.0f, Px(2.0f));
        if (ui::IconButton("##next", ICON_RIGHT, "Next frame (Right)", t)) Select(m_selected + 1, false);
        ImGui::SameLine(0.0f, Px(2.0f));
    }
    ui::IconButton("##help", "?", nullptr, t);
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ui::PushSemibold(ui::kBodySize);
        ImGui::TextUnformatted("Keyboard and mouse");
        ui::PopFont();
        ImGui::Spacing();
        const char* keys[][2] = {
            {"Space", "live / pause"}, {"Home", "back to live"}, {"Left, Right", "previous / next frame"},
            {"N, Shift+N", "next / previous late frame"}, {"F", "fit the vertical axis"},
            {"Wheel", "zoom"}, {"Drag", "pan; right-drag zooms to a box"}, {"Click", "pin a frame here"},
        };
        for (const auto& k : keys) {
            ui::Muted("%s", k[0]);
            ImGui::SameLine(Px(96.0f));
            ImGui::TextUnformatted(k[1]);
        }
        ImGui::EndTooltip();
    }
    if (shown < 0) {
        ImGui::Spacing();
        ImGui::PushTextWrapPos(0.0f);
        ui::Muted("No frames in view yet. Click a bar in the graph, or a row in the list, to break that frame down.");
        ImGui::PopTextWrapPos();
        return;
    }
    const size_t i = size_t(shown);
    const an::FrameMetrics& m = View().Metrics()[i];
    const tl::FrameRecord& r = View().Records()[i];

    ImGui::AlignTextToFramePadding();
    ui::PushSemibold(18.0f);
    ImGui::Text("Frame %llu", (unsigned long long)r.frameId);
    ui::PopFont();
    ImGui::SameLine(0.0f, Px(10.0f));
    ImGui::AlignTextToFramePadding();
    ui::Dot(ClassColor(m.cls) | IM_COL32_A_MASK, 4.0f, true);
    ImGui::SameLine(0.0f, Px(6.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(an::ClassName(m.cls));
    if (m.cause != an::Cause::None) {
        ImGui::SameLine(0.0f, Px(6.0f));
        ImGui::AlignTextToFramePadding();
        ui::Muted("\xC2\xB7  %s", an::CauseName(m.cause));
    }
    ui::PushRegular(ui::kSmallSize);
    ui::Muted("%.3f s into the session", m.endSec);
    ui::PopFont();
    ImGui::Spacing();

    // The frame's life on one time axis: what the CPU did and, below it, when
    // the GPU worked on the frame.
    const bool matched = (r.flags & tl::kFrameWaitMatched) != 0;
    const uint64_t start = matched ? r.waitEnterQpc : r.endEnterQpc;
    const bool gpuLane = !std::isnan(m.gpuStartSec) && !std::isnan(m.gpuEndSec);
    const double startSec = View().Sec(start);
    const double endSec = gpuLane ? std::max(View().Sec(r.endExitQpc), m.gpuEndSec) : View().Sec(r.endExitQpc);
    const double total = (endSec - startSec) * 1000.0;
    const ImU32 blocked = ui::WithAlpha(t.textMuted, 0.35f);
    if (total > 0.0) {
        ImDrawList* draw = ImGui::GetWindowDrawList();
        const ImVec2 top = ImGui::GetCursorScreenPos();
        const float height = Px(14.0f), laneGap = Px(5.0f);
        // With a GPU lane each bar is named, the names sharing one column.
        const float labelWidth = gpuLane ? Px(34.0f) : 0.0f;
        const float width = ImGui::GetContentRegionAvail().x - labelWidth;
        auto track = [&](float y, const char* name) {
            if (gpuLane) {
                ui::PushRegular(ui::kSmallSize);
                draw->AddText(ImVec2(top.x, y + (height - ImGui::GetFontSize()) * 0.5f), t.textMuted, name);
                ui::PopFont();
            }
            const ImVec2 origin(top.x + labelWidth, y);
            draw->AddRectFilled(origin, ImVec2(origin.x + width, origin.y + height), t.field, height * 0.5f);
            return origin;
        };
        auto x = [&](const ImVec2& origin, double sec) { return origin.x + float((sec - startSec) * 1000.0 / total) * width; };
        auto sec = [&](uint64_t qpc) { return qpc ? View().Sec(qpc) : NAN; };
        // True while the mouse is over the segment; `name` is its tooltip, if any.
        auto segment = [&](const ImVec2& origin, double a, double b, ImU32 color, const char* name) {
            if (std::isnan(a) || std::isnan(b) || b <= a) return false;
            const float x0 = x(origin, a), x1 = x(origin, b);
            const ImDrawFlags corners = (x0 <= origin.x + 0.5f ? ImDrawFlags_RoundCornersLeft : 0) |
                                        (x1 >= origin.x + width - 0.5f ? ImDrawFlags_RoundCornersRight : 0);
            draw->AddRectFilled(ImVec2(x0, origin.y), ImVec2(std::max(x0 + 1.0f, x1), origin.y + height), color,
                                corners ? height * 0.5f : 0.0f, corners ? corners : ImDrawFlags_RoundCornersNone);
            const bool hovered = ImGui::IsMouseHoveringRect(ImVec2(x0, origin.y), ImVec2(std::max(x0 + 2.0f, x1), origin.y + height));
            if (hovered && name) ImGui::SetTooltip("%s\n%.3f ms", name, (b - a) * 1000.0);
            return hovered;
        };
        const ImVec2 cpu = track(top.y, "CPU");
        if (matched) {
            segment(cpu, sec(r.waitEnterQpc), sec(r.waitPaceQpc), t.simulator, "Simulator input and commands, in xrWaitFrame");
            segment(cpu, sec(r.waitPaceQpc), sec(r.waitExitQpc), blocked, "Blocked by pacing (headroom)");
            segment(cpu, sec(r.waitExitQpc), sec(r.endEnterQpc), ClassColor(m.cls), "App: xrWaitFrame return to xrEndFrame");
        }
        segment(cpu, sec(r.endEnterQpc), sec(r.endExitQpc), t.simulator, "Simulator: xrEndFrame (composite, preview, status)");
        // App time the GPU was still on the previous frame, tinted toward the GPU.
        const ImVec4 field = ImGui::ColorConvertU32ToFloat4(t.field), tint = ImGui::ColorConvertU32ToFloat4(t.gpu);
        const ImU32 waiting = ImGui::ColorConvertFloat4ToU32(ImVec4(field.x + (tint.x - field.x) * 0.45f, field.y + (tint.y - field.y) * 0.45f,
                                                                    field.z + (tint.z - field.z) * 0.45f, 1.0f));
        const bool waited = i > 0 && m.gpuWaitMs > 0.0f;
        if (waited) {
            double from = std::max(sec(View().Records()[i - 1].endExitQpc), startSec);
            if (matched) from = std::max(from, sec(r.waitExitQpc));
            if (segment(cpu, from, std::min(View().Metrics()[i - 1].gpuEndSec, sec(r.endEnterQpc)), waiting, nullptr)) {
                ImGui::SetTooltip("App waiting for the GPU to finish the previous frame\n%.3f ms off the CPU, left out of this frame's time",
                                  m.gpuWaitMs);
            }
        }
        if (gpuLane) {
            const ImVec2 gpu = track(top.y + height + laneGap, "GPU");
            if (segment(gpu, std::max(m.gpuStartSec, startSec), m.gpuEndSec, t.gpu, nullptr)) {
                if (std::isnan(m.gpuLatencyMs)) ImGui::SetTooltip("GPU: the app's work on its queue\n%.3f ms", m.gpuMs);
                else ImGui::SetTooltip("GPU: the app's work on its queue\n%.3f ms, done %.3f ms after xrEndFrame", m.gpuMs, m.gpuLatencyMs);
            }
            // Where the app submitted: GPU work right of this line is latency.
            const float submit = std::floor(x(cpu, sec(r.endEnterQpc))) + 0.5f;
            draw->AddLine(ImVec2(submit, cpu.y - Px(2.0f)), ImVec2(submit, gpu.y + height + Px(2.0f)), t.textSecondary, Px(1.0f));
        }
        ImGui::Dummy(ImVec2(labelWidth + width, gpuLane ? height * 2.0f + laneGap : height));
        ui::PushRegular(ui::kSmallSize);
        auto legend = [&](ImU32 color, const char* name) {
            ui::Dot(color, 3.5f);
            ImGui::SameLine(0.0f, Px(5.0f));
            ui::Muted("%s", name);
            ImGui::SameLine(0.0f, Px(12.0f));
        };
        legend(ClassColor(m.cls), "app");
        legend(blocked, "pacing");
        legend(t.simulator, "simulator");
        if (gpuLane) legend(t.gpu, "GPU");
        if (waited) legend(waiting, "GPU wait");
        ui::Muted("%.2f ms in all", total);
        ui::PopFont();
    }
    ImGui::Spacing();

    if (ImGui::BeginTable("##metrics", 4, ImGuiTableFlags_SizingStretchProp)) {
        auto cell = [](const char* name, const char* fmt, double value) {
            ImGui::TableNextColumn();
            ui::Muted("%s", name);
            ImGui::TableNextColumn();
            if (std::isnan(value)) ui::Muted("-");
            else ImGui::Text(fmt, value);
        };
        const bool gpu = View().GpuApi() != 0;
        cell("Frame time", "%.2f ms", m.costMs);
        cell("Budget", "%.2f ms", View().BudgetMs());
        cell("CPU time", "%.2f ms", m.cpuMs);
        if (gpu) cell("GPU time", "%.2f ms", m.gpuMs);
        cell("Interval", "%.2f ms", m.intervalMs);
        cell("Missed", "%.0f", m.missed);
        cell("Headroom", "%.2f ms", m.blockedMs);
        cell("App span", "%.2f ms", m.appSpanMs);
        cell("Simulator", "%.2f ms", m.simulatorMs);
        cell("Pacing error", "%.2f ms", m.pacingErrorMs);
        cell("Submit CPU", "%.2f ms", m.submitCpuMs);
        cell("Submit busy", "%.0f%%", std::isnan(m.submitOnCpu) ? NAN : m.submitOnCpu * 100.0);
        if (gpu) {
            cell("GPU wait", "%.2f ms", m.gpuWaitMs);
            cell("GPU latency", "%.2f ms", m.gpuLatencyMs);
        }
        ImGui::EndTable();
    }

    ImGui::Spacing();
    if (ImGui::CollapsingHeader("Timestamps")) {
        const uint64_t base = matched ? r.waitEnterQpc : r.endEnterQpc;
        const struct { const char* name; uint64_t qpc; } stamps[] = {
            {"xrWaitFrame enter", r.waitEnterQpc}, {"pacing starts", r.waitPaceQpc},
            {"pacing target tick", r.waitTargetQpc}, {"xrWaitFrame return", r.waitExitQpc},
            {"xrBeginFrame", r.beginQpc}, {"xrLocateViews (first)", r.locateQpc},
            {"xrEndFrame enter", r.endEnterQpc}, {"projection composited", r.endProjectionQpc},
            {"overlays composited", r.endOverlaysQpc}, {"preview paint done", r.endPaintQpc},
            {"xrEndFrame return", r.endExitQpc},
        };
        if (ImGui::BeginTable("##stamps", 2, ImGuiTableFlags_SizingStretchProp)) {
            for (const auto& s : stamps) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn();
                ui::Muted("%s", s.name);
                ImGui::TableNextColumn();
                if (s.qpc) ImGui::Text("%+.3f ms", (double(s.qpc) - double(base)) * 1000.0 / double(View().QpcFrequency()));
                else ui::Muted("-");
            }
            if (gpuLane) {
                const struct { const char* name; double sec; } gpuStamps[] = {
                    {"GPU starts the frame", m.gpuStartSec}, {"GPU finishes the frame", m.gpuEndSec},
                };
                for (const auto& s : gpuStamps) {
                    ImGui::TableNextRow();
                    ImGui::TableNextColumn();
                    ui::Muted("%s", s.name);
                    ImGui::TableNextColumn();
                    ImGui::Text("%+.3f ms", (s.sec - View().Sec(base)) * 1000.0);
                }
            }
            ImGui::EndTable();
        }
    }
    if (ImGui::CollapsingHeader("Submission")) {
        ImGui::Text("%u layers: %u projection, %u quad, %u cylinder, %u other", r.layerCount, r.projectionLayers,
                    r.quadLayers, r.cylinderLayers, r.otherLayers);
        ui::Muted("Waits on thread %u, submits on thread %u", r.waitThreadId, r.endThreadId);
        if (!matched) {
            ui::Dot(t.over);
            ImGui::SameLine(0.0f, Px(6.0f));
            ImGui::TextUnformatted("displayTime matched no xrWaitFrame prediction");
        }
        std::string flags;
        auto flag = [&](uint32_t bit, const char* name) {
            if (r.flags & bit) flags += flags.empty() ? name : std::string(", ") + name;
        };
        flag(tl::kFrameStereoProjection, "stereo");
        flag(tl::kFrameNoProjection, "2D only");
        flag(tl::kFrameMirrorDue, "mirror updated");
        flag(tl::kFrameScreenshot, "screenshot");
        flag(tl::kFrameBurst, "burst");
        flag(tl::kFrameSessionFocused, "focused");
        flag(tl::kFramePacingRebased, "pacing re-based");
        ImGui::PushTextWrapPos(0.0f);
        ui::Muted("Flags: %s", flags.empty() ? "none" : flags.c_str());
        ImGui::PopTextWrapPos();
    }
}

void App::ResetViewState() {
    m_selected = -1;
    m_hovered = -1;
    m_worstIndex = -1;
    m_fitY = true;
    m_yTop = 0.0;
    m_lateRowsDirty = true;
    m_statsAt = -1.0;
}

void App::OpenCapture(const std::wstring& path) {
    perf::capture::Capture capture;
    std::string error;
    if (!perf::capture::ReadCapture(path, capture, error)) {
        Toast("Could not open " + Narrow(path) + ": " + error);
        return;
    }
    if (m_recording) StopRecording();
    m_capture = std::move(capture);
    m_capturePath = path;
    m_viewingCapture = true;
    perf::report::Analyze(m_capture, m_captureAnalyzer, m_settings.refreshHz, m_settings.hitchBudgets);
    m_appliedHz = 0.0;
    ApplyBudget();
    ResetViewState();
    m_live = false;
    if (m_captureAnalyzer.Size()) {  // the whole capture
        m_viewX0 = m_captureAnalyzer.StartX(Axis(), 0);
        m_viewX1 = m_captureAnalyzer.EndX(Axis(), m_captureAnalyzer.Size() - 1);
        m_applyView = true;
    }
    SetWindowTextW(m_window, (L"Frame Timing - " + path.substr(path.find_last_of(L"\\/") + 1)).c_str());
}

void App::CloseCapture() {
    m_viewingCapture = false;
    m_capture = perf::capture::Capture{};
    m_capturePath.clear();
    m_captureAnalyzer.Reset(1, 0);
    m_appliedHz = 0.0;
    ApplyBudget();
    ResetViewState();
    m_live = true;
    if (m_client.Attached()) {
        const TimelineHeader& h = *m_client.Header();
        SetWindowTextW(m_window, (L"Frame Timing - " + Widen(HeaderString(h.processName, sizeof(h.processName))) +
                                  L" (pid " + std::to_wstring(m_client.Pid()) + L")").c_str());
    } else {
        SetWindowTextW(m_window, L"Frame Timing - OpenXR Simulator");
    }
}

void App::StartRecording() {
    if (!m_client.Attached() || m_viewingCapture || m_recording) return;
    m_recording = true;
    // The newest frame comes along: it gives the first recorded one its interval.
    m_recordFrom = m_analyzer.Size() ? m_analyzer.Size() - 1 : 0;
    m_recordStartedAt = Clock();
    m_recordStartedUtc = UtcNow();
    m_recordLostFrom = m_lostFrames;
    m_recordLabel[0] = '\0';
}

void App::StopRecording() {
    if (!m_recording) return;
    m_recording = false;
    const auto& records = m_analyzer.Records();
    std::vector<FrameRecord> frames(records.begin() + std::min(m_recordFrom, records.size()), records.end());
    if (frames.size() < 2) {
        Toast("Nothing was recorded: no frames arrived.");
        return;
    }
    CaptureDescription description;
    description.label = m_recordLabel;
    description.recordedBy = "viewer";
    description.startedUtc = m_recordStartedUtc;
    description.pid = m_client.Pid();
    description.lostFrames = m_lostFrames - m_recordLostFrom;
    const TimelineHeader& h = *m_client.Header();
    std::vector<GpuRecord> gpu;
    for (size_t i = std::min(m_recordFrom, records.size()); i < records.size(); ++i) {
        if (m_analyzer.Gpu(i).frameId) gpu.push_back(m_analyzer.Gpu(i));
    }
    perf::capture::Capture capture = MakeCapture(h, description, std::move(frames), m_analyzer.Markers(), gpu);
    const std::wstring path = UniqueCapturePath(
        CapturesDirectory(), CaptureFileName(HeaderString(h.processName, sizeof(h.processName)), m_recordLabel));
    std::string error;
    if (!perf::capture::WriteCapture(path, capture, error)) {
        Toast("Could not save the capture: " + error);
        return;
    }
    char text[160];
    snprintf(text, sizeof(text), "Saved %zu frames (%.1f s) as a capture", capture.frames.size(), Clock() - m_recordStartedAt);
    Toast(text, path);
    m_captureListAt = -10.0;
}

void App::SetBaseline(const std::wstring& path) {
    perf::capture::Capture baseline;
    std::string error;
    if (!perf::capture::ReadCapture(path, baseline, error)) {
        Toast("Could not open " + Narrow(path) + ": " + error);
        return;
    }
    m_baseline = std::move(baseline);
    m_baselinePath = path;
    m_hasBaseline = true;
    // Scored like what it is compared with.
    perf::report::Analyze(m_baseline, m_baselineAnalyzer, m_appliedHz, m_settings.hitchBudgets);
    m_baselineStats = m_baselineAnalyzer.Stats(0, m_baselineAnalyzer.Size());
    m_statsAt = -1.0;
}

void App::ClearBaseline() {
    m_hasBaseline = false;
    m_baseline = perf::capture::Capture{};
    m_baselinePath.clear();
    m_statsAt = -1.0;
}

void App::ExportCsv(const std::wstring& capturePath) {
    perf::capture::Capture capture;
    std::string error;
    perf::analysis::Analyzer a;
    if (!perf::capture::ReadCapture(capturePath, capture, error)) {
        Toast("Could not open the capture: " + error);
        return;
    }
    perf::report::Analyze(capture, a, m_settings.refreshHz, m_settings.hitchBudgets);
    const size_t dot = capturePath.find_last_of(L'.');
    const std::wstring out = (dot == std::wstring::npos ? capturePath : capturePath.substr(0, dot)) + L".csv";
    if (!perf::report::WriteCsv(out, a, 0, a.Size(), error)) {
        Toast("Could not write the CSV: " + error);
        return;
    }
    Toast("Exported " + std::to_string(a.Size()) + " frames to CSV", out);
}

void App::RefreshCaptureList() {
    m_captureListAt = Clock();
    struct Found { CaptureEntry entry; uint64_t written; };
    std::vector<Found> found;
    const std::wstring dir = CapturesDirectory();
    WIN32_FIND_DATAW data{};
    HANDLE search = FindFirstFileW((dir + L"\\*" + perf::capture::kExtension).c_str(), &data);
    if (search != INVALID_HANDLE_VALUE) {
        do {
            Found f;
            f.entry.path = dir + L"\\" + data.cFileName;
            f.entry.file = Narrow(data.cFileName);
            f.written = (uint64_t(data.ftLastWriteTime.dwHighDateTime) << 32) | data.ftLastWriteTime.dwLowDateTime;
            perf::capture::Capture header;
            std::string error;
            if (!perf::capture::ReadCapture(f.entry.path, header, error, true)) continue;
            json::Object meta(header.metadata.c_str());
            f.entry.label = meta.object("capture").string("label");
            f.entry.started = meta.object("capture").string("started_utc");
            f.entry.seconds = meta.object("capture").number("seconds", 0.0);
            f.entry.process = meta.object("process").string("name");
            f.entry.application = meta.object("process").string("application");
            f.entry.frames = header.header.frameCount;
            f.entry.refreshHz = header.header.refreshHz;
            found.push_back(std::move(f));
        } while (FindNextFileW(search, &data));
        FindClose(search);
    }
    std::sort(found.begin(), found.end(), [](const Found& a, const Found& b) { return a.written > b.written; });
    m_captureList.clear();
    for (Found& f : found) m_captureList.push_back(std::move(f.entry));
}

void App::Toast(const std::string& text, const std::wstring& reveal) {
    m_toast = text;
    m_toastReveal = reveal;
    m_toastAt = Clock();
}

void App::DrawToast() {
    const double age = Clock() - m_toastAt;
    if (m_toast.empty() || age > 7.0) return;
    const ui::Theme& t = m_theme;
    const ImGuiViewport* viewport = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(viewport->WorkPos.x + viewport->WorkSize.x - Px(24.0f),
                                   viewport->WorkPos.y + viewport->WorkSize.y - Px(24.0f)), ImGuiCond_Always, ImVec2(1.0f, 1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha, float(std::clamp((7.0 - age) / 0.5, 0.0, 1.0)));
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImGui::ColorConvertU32ToFloat4(t.raised));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, Px(10.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 1.0f);
    ImGui::Begin("##toast", nullptr, ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_AlwaysAutoResize |
                                          ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoNav |
                                          ImGuiWindowFlags_NoSavedSettings);
    ui::Dot(m_toastReveal.empty() ? t.over : t.good);
    ImGui::SameLine(0.0f, Px(8.0f));
    ImGui::PushTextWrapPos(Px(420.0f));
    ImGui::TextUnformatted(m_toast.c_str());
    ImGui::PopTextWrapPos();
    if (!m_toastReveal.empty()) {
        ui::PushRegular(ui::kSmallSize);
        ui::Muted("%s", Narrow(m_toastReveal).c_str());
        ui::PopFont();
        if (ImGui::SmallButton(ICON_FOLDER "  Show in folder")) {
            const std::wstring arguments = L"/select,\"" + m_toastReveal + L"\"";
            ShellExecuteW(nullptr, L"open", L"explorer.exe", arguments.c_str(), nullptr, SW_SHOWNORMAL);
        }
        if (m_toastReveal.size() > 5 && m_toastReveal.compare(m_toastReveal.size() - 5, 5, perf::capture::kExtension) == 0) {
            ImGui::SameLine();
            if (ImGui::SmallButton(ICON_DOC "  Open")) OpenCapture(m_toastReveal);
        }
    }
    if (ImGui::IsWindowHovered()) m_toastAt = std::max(m_toastAt, Clock() - 3.0);  // stays while pointed at
    ImGui::End();
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor();
}

void App::DrawCaptureList() {
    const ui::Theme& t = m_theme;
    if (Clock() - m_captureListAt > 3.0) RefreshCaptureList();
    ImGui::Dummy(ImVec2(0.0f, Px(2.0f)));
    if (ImGui::Button(ICON_DOC "  Open file...")) {
        wchar_t file[MAX_PATH] = L"";
        const std::wstring dir = CapturesDirectory();
        OPENFILENAMEW dialog{sizeof(dialog)};
        dialog.hwndOwner = m_window;
        dialog.lpstrFilter = L"Frame-timing captures (*.xrft)\0*.xrft\0All files\0*.*\0";
        dialog.lpstrFile = file;
        dialog.nMaxFile = MAX_PATH;
        dialog.lpstrInitialDir = dir.c_str();
        dialog.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;
        if (GetOpenFileNameW(&dialog)) OpenCapture(file);
    }
    ImGui::SameLine();
    if (ImGui::Button(ICON_FOLDER "  Folder")) {
        ShellExecuteW(nullptr, L"open", CapturesDirectory().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
    }
    if (m_hasBaseline) {
        ImGui::SameLine(0.0f, Px(12.0f));
        bool on = true;
        const std::string name = "Baseline: " + Narrow(m_baselinePath.substr(m_baselinePath.find_last_of(L"\\/") + 1));
        if (ui::Chip(name.c_str(), &on, t)) ClearBaseline();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Comparing everything with this capture. Click to stop comparing.");
    } else {
        ImGui::SameLine(0.0f, Px(12.0f));
        ImGui::AlignTextToFramePadding();
        ui::Muted("Compare picks a baseline for the statistics and distribution");
    }
    const ImGuiTableFlags flags = ImGuiTableFlags_ScrollY | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp |
                                  ImGuiTableFlags_Resizable | ImGuiTableFlags_BordersInnerH;
    if (!ImGui::BeginTable("##captures", 5, flags)) return;
    ImGui::TableSetupScrollFreeze(0, 1);
    ImGui::TableSetupColumn("Capture", 0, 2.2f);
    ImGui::TableSetupColumn("Recorded", 0, 1.3f);
    ImGui::TableSetupColumn("Length", 0, 0.7f);
    ImGui::TableSetupColumn("Frames", 0, 0.7f);
    ImGui::TableSetupColumn("##actions", 0, 1.9f);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(t.textMuted));
    ImGui::TableHeadersRow();
    ImGui::PopStyleColor();
    if (m_captureList.empty()) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ui::Muted("No captures yet. Press Record while watching an app.");
    }
    for (size_t row = 0; row < m_captureList.size(); ++row) {
        const CaptureEntry& c = m_captureList[row];
        ImGui::PushID(int(row));
        ImGui::TableNextRow(0, Px(30.0f));
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(c.label.empty() ? c.file.c_str() : c.label.c_str());
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s\n%s  \xC2\xB7  %s", c.file.c_str(), c.process.c_str(), c.application.c_str());
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ui::Muted("%s", LocalTimeText(c.started).c_str());
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::Text("%.1f s", c.seconds);
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::Text("%llu", (unsigned long long)c.frames);
        ImGui::TableNextColumn();
        const bool open = m_viewingCapture && m_capturePath == c.path;
        if (ImGui::SmallButton(open ? "Viewing" : "Open") && !open) OpenCapture(c.path);
        ImGui::SameLine();
        const bool isBaseline = m_hasBaseline && m_baselinePath == c.path;
        if (ImGui::SmallButton(isBaseline ? "Baseline" : "Compare")) {
            if (isBaseline) ClearBaseline();
            else SetBaseline(c.path);
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip(isBaseline ? "Stop comparing with this capture" : "Compare what is on screen with this capture");
        ImGui::SameLine();
        if (ImGui::SmallButton("CSV")) ExportCsv(c.path);
        ImGui::PopID();
    }
    ImGui::EndTable();
}

void App::DrawInfoPopup() {
    ImGui::SetNextWindowSizeConstraints(ImVec2(Px(380.0f), 0.0f), ImVec2(Px(560.0f), FLT_MAX));
    if (!ImGui::BeginPopup("##info")) return;
    const std::string session = m_viewingCapture || !m_client.Attached() ? std::string("{}")
                                                                          : perf::timeline::ReadSessionInfo(*m_client.Header());
    json::Object meta(m_viewingCapture ? m_capture.metadata.c_str() : "{}");
    json::Object s = m_viewingCapture ? meta.object("session") : json::Object(session.c_str());
    const float column = Px(150.0f);
    auto row = [&](const char* key, const std::string& value) {
        if (value.empty()) return;
        ui::Muted("%s", key);
        ImGui::SameLine(column);
        ImGui::TextUnformatted(value.c_str());
    };
    auto number = [](double v, const char* format) {
        char text[64];
        snprintf(text, sizeof(text), format, v);
        return std::string(text);
    };
    if (m_viewingCapture) {
        json::Object c = meta.object("capture");
        ui::PushSemibold(ui::kBodySize);
        ImGui::TextUnformatted("Capture");
        ui::PopFont();
        row("File", Narrow(m_capturePath));
        row("Label", c.string("label"));
        row("Recorded", LocalTimeText(c.string("started_utc")));
        row("Length", number(c.number("seconds", 0.0), "%.1f s"));
        row("Frames", number(c.number("frames", 0.0), "%.0f"));
        row("Lost frames", number(c.number("lost_frames", 0.0), "%.0f"));
        row("Recorded by", c.string("recorded_by"));
        row("Paced at", number(m_capture.header.refreshHz, "%g Hz"));
        row("Process", meta.object("process").string("name"));
        row("Application", meta.object("process").string("application"));
        ImGui::Separator();
    }
    ui::PushSemibold(ui::kBodySize);
    ImGui::TextUnformatted("Session");
    ui::PopFont();
    json::Object gpu = s.object("gpu"), render = s.object("render"), swapchain = s.object("app_swapchain");
    row("Simulator build", s.object("simulator").string("build"));
    row("Refresh rate", s.has("simulator") ? number(s.object("simulator").number("refresh_hz", 0.0), "%g Hz") : "");
    row("Graphics API", s.string("graphics_api"));
    row("GPU", gpu.string("name"));
    row("Driver", gpu.string("driver"));
    if (const uint32_t api = View().GpuApi()) {
        bool calibrated = false;
        for (size_t i = View().Size(); i-- > 0;) {
            if (View().Gpu(i).frameId) {
                calibrated = (View().Gpu(i).flags & tl::kGpuCalibrated) != 0;
                break;
            }
        }
        row("GPU timing", std::string(tl::GpuApiName(api)) + (calibrated ? " timestamps on the CPU clock" : " durations only"));
    } else {
        row("GPU timing", "not measured");
    }
    row("VRAM", gpu.has("vram_mb") ? number(gpu.number("vram_mb", 0.0), "%.0f MB") : "");
    if (render.valid()) {
        row("Render per eye", number(render.number("width_per_eye", 0.0), "%.0f") + " x " +
                                  number(render.number("height_per_eye", 0.0), "%.0f"));
        row("Headset profile", render.string("headset"));
        row("IPD", number(render.number("ipd_mm", 0.0), "%.0f mm"));
    }
    if (swapchain.valid() && swapchain.number("width", 0.0) > 0.0) {
        row("App swapchain", number(swapchain.number("width", 0.0), "%.0f") + " x " + number(swapchain.number("height", 0.0), "%.0f") +
                                 ", format " + number(swapchain.number("format", 0.0), "%.0f") + ", " +
                                 number(swapchain.number("samples", 0.0), "%.0fx"));
    }
    json::Object preview = s.object("preview");
    if (preview.valid()) {
        const std::string mirror = preview.string("mirror_fps");
        row("Mirror rate", mirror.empty() ? number(preview.number("mirror_fps", 0.0), "%.0f fps") : mirror);
    }
    if (!s.has("graphics_api")) ui::Muted("The runtime has not described this session yet.");
    if (m_viewingCapture && meta.has("system")) {
        json::Object sys = meta.object("system");
        ImGui::Separator();
        ui::PushSemibold(ui::kBodySize);
        ImGui::TextUnformatted("Machine");
        ui::PopFont();
        row("CPU", sys.string("cpu"));
        row("Logical processors", number(sys.number("logical_processors", 0.0), "%.0f"));
        row("Memory", number(sys.number("memory_gb", 0.0), "%.0f GB"));
        row("Windows", sys.string("os"));
    }
    ImGui::EndPopup();
}

// The sub-line of a statistics card while comparing: the change against the
// baseline, its direction named by an arrow and colored by whether it helps.
void App::Delta(double current, double baseline, bool higherIsBetter, const char* format, double oneEvent) {
    const ui::Theme& t = m_theme;
    const double change = current - baseline;
    const bool same = std::abs(change) <= std::max({1e-9, std::abs(baseline) * 0.01, oneEvent * 1.0001});
    const bool better = same ? false : ((change > 0.0) == higherIsBetter);
    char amount[48];
    snprintf(amount, sizeof(amount), format, std::abs(change));
    char text[96];
    if (same) snprintf(text, sizeof(text), "same as baseline");
    else snprintf(text, sizeof(text), "%s %s vs baseline", change > 0.0 ? "\xE2\x96\xB2" : "\xE2\x96\xBC", amount);
    ui::PushRegular(ui::kSmallSize);
    ui::Dot(same ? t.textMuted : better ? t.good : t.hitch, 3.5f);
    ImGui::SameLine(0.0f, Px(6.0f));
    ui::Muted("%s", text);
    ui::PopFont();
}

} // namespace viewer
