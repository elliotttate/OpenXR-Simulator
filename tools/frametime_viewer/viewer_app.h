// The frame-time viewer's window contents: session picker, live frame-time
// graph, statistics, distribution, late-frame list and frame inspector.
#pragma once
#include "capture_file.h"
#include "frame_analysis.h"
#include "timeline_client.h"
#include "ui_kit.h"

#include <string>
#include <vector>

namespace viewer {

struct Settings {
    int theme = 0;              // 0 follows Windows, 1 light, 2 dark
    float refreshHz = 0.0f;     // judge frames against this rate; 0 uses the runtime's own
    float hitchBudgets = 2.0f;  // a frame costing this many budgets is a hitch
    float liveSeconds = 10.0f;  // how much history the live view shows
    bool frameAxis = false;     // x axis in frames rather than seconds
    bool logY = false;
    bool showInterval = false;  // overlay the raw submission interval
    bool showGpu = true;        // overlay each frame's GPU time, when the runtime measures it
    bool sessionStats = false;  // statistics over the whole session rather than the visible range
    bool hitchesOnly = false;
    int windowX = 0, windowY = 0, windowW = 0, windowH = 0;
    bool maximized = false;
};

Settings LoadSettings();
void SaveSettings(const Settings& settings);

class App {
public:
    App(DWORD pid, HWND window, float dpiScale, const Settings& settings);
    ~App();

    // Pulls new records. Cheap; also runs while the window is minimized so
    // nothing is missed.
    void Poll();
    // Before ImGui::NewFrame: applies a theme change, which rebuilds the style.
    void BeginFrame();
    // One full-window UI frame.
    void Draw();

    // Shows a saved capture instead of the live session (which keeps being
    // recorded in the background). From the command line, drag-and-drop or the list.
    void OpenCapture(const std::wstring& path);
    // Compares what is on screen with a saved capture (--baseline).
    void CompareWith(const std::wstring& path) { SetBaseline(path); }

    Settings& UserSettings() { return m_settings; }
    const ui::Theme& Theme() const { return m_theme; }

private:
    void AttachTo(DWORD pid);
    void ResetAnalyzer();
    void ApplyBudget();
    void UpdateStats(bool force);
    void Select(int64_t index, bool center);
    void CenterOn(double x);
    void HandleKeys();
    void SettingsChanged();

    // What the plots and statistics show: the live session or an opened capture.
    perf::analysis::Analyzer& View() { return m_viewingCapture ? m_captureAnalyzer : m_analyzer; }
    const perf::analysis::Analyzer& View() const { return m_viewingCapture ? m_captureAnalyzer : m_analyzer; }
    void CloseCapture();
    void StartRecording();
    void StopRecording();
    void SetBaseline(const std::wstring& path);
    void ClearBaseline();
    void ExportCsv(const std::wstring& capturePath);
    void RefreshCaptureList();
    void Toast(const std::string& text, const std::wstring& reveal = L"");
    void ResetViewState();

    void DrawHeader();
    void DrawPicker();
    void DrawStats();
    void DrawTimeline(float height);
    void DrawTimelineHeader();
    void DrawOverview(float height);
    void DrawMain(float height);
    void DrawBars(double x0, double x1, double y0, bool forOverview);
    void DrawReferenceLine(double y, ImU32 color, const char* label);
    void DrawMarkers(double x0, double x1, bool interactive);
    void DrawBottom(float height);
    void DrawHistogram();
    void DrawLateFrames();
    void DrawMarkerList();
    void DrawInspector();
    void FrameTooltip(size_t index);
    void DrawCaptureList();
    void DrawToast();
    void DrawInfoPopup();
    // `oneEvent`: for counts, what a single frame more or less changes the
    // value by; a change no bigger than that is noise, not a difference.
    void Delta(double current, double baseline, bool higherIsBetter, const char* format, double oneEvent = 0.0);

    ImU32 ClassColor(perf::analysis::FrameClass c) const;
    perf::analysis::XAxis Axis() const;
    bool ShowGpu() const { return m_settings.showGpu && View().GpuApi() != 0; }
    double RightEdge() const;      // newest x: now in seconds, or the frame count
    double MarkerX(const perf::timeline::MarkerRecord& marker) const;
    double SessionNow() const;
    double Clock() const;

    HWND m_window;
    float m_dpi;
    DWORD m_wantedPid;
    bool m_autoAttached = false;
    Settings m_settings;
    double m_settingsChangedAt = -1.0;
    ui::Theme m_theme;
    bool m_themeApplied = false;
    double m_themeCheckedAt = -10.0;
    bool m_systemDark = true;

    TimelineClient m_client;
    perf::analysis::Analyzer m_analyzer;  // the live session

    // An opened capture.
    bool m_viewingCapture = false;
    perf::capture::Capture m_capture;
    std::wstring m_capturePath;
    perf::analysis::Analyzer m_captureAnalyzer;

    // A capture to compare against.
    bool m_hasBaseline = false;
    perf::capture::Capture m_baseline;
    std::wstring m_baselinePath;
    perf::analysis::Analyzer m_baselineAnalyzer;
    perf::analysis::RangeStats m_baselineStats;
    std::vector<double> m_baselineHistogram[3];

    // Recording the live session.
    bool m_recording = false;
    size_t m_recordFrom = 0;
    uint64_t m_recordLostFrom = 0;
    double m_recordStartedAt = 0.0;
    std::string m_recordStartedUtc;
    char m_recordLabel[64] = "";

    struct CaptureEntry {
        std::wstring path;
        std::string file, label, process, application, started;
        double seconds = 0.0, refreshHz = 0.0;
        uint64_t frames = 0;
    };
    std::vector<CaptureEntry> m_captureList;
    double m_captureListAt = -10.0;

    std::string m_toast;
    std::wstring m_toastReveal;
    double m_toastAt = -10.0;
    std::vector<SessionInfo> m_sessions;
    double m_lastDiscover = -10.0;
    std::string m_error;
    bool m_processAlive = true;
    uint64_t m_lostFrames = 0;
    double m_appliedHz = 0.0, m_appliedHitch = 0.0;
    double m_qpcToSec = 0.0;

    bool m_live = true;
    bool m_applyView = false;
    double m_viewX0 = 0.0, m_viewX1 = 10.0;
    bool m_fitY = true;
    double m_yTop = 0.0;  // the main plot's live y-axis top, eased
    int64_t m_selected = -1;
    int64_t m_hovered = -1;
    int64_t m_worstIndex = -1;  // costliest frame in the statistics range
    float m_controlsWidth = 0.0f;
    float m_headerRightWidth = 0.0f;

    perf::analysis::RangeStats m_stats;
    std::vector<double> m_histogram[3];
    double m_histogramBinMs = 1.0;
    double m_statsAt = -1.0;
    size_t m_statsBegin = 0, m_statsEnd = 0;

    std::vector<perf::timeline::FrameRecord> m_newFrames;
    std::vector<perf::timeline::MarkerRecord> m_newMarkers;
    std::vector<perf::timeline::GpuRecord> m_newGpu;
    std::vector<perf::analysis::Column> m_columns;          // main plot, kept for hover
    std::vector<perf::analysis::Column> m_overviewColumns;
    std::vector<uint32_t> m_wide;
    std::vector<uint32_t> m_lateRows;
    size_t m_lateRowsFor = SIZE_MAX;
    bool m_lateRowsDirty = true;    // rebuild now: filter, sort or source changed
    bool m_lateRowsStale = false;   // new frames: rebuild when due
    double m_lateRowsAt = -10.0;
    uint64_t m_statsRevision = ~0ull;
    const void* m_statsSource = nullptr;
};

} // namespace viewer
