#include "ui_kit.h"

#include "implot.h"

#include <windows.h>

#include <algorithm>
#include <cfloat>
#include <cstdarg>
#include <cstdio>
#include <string>

namespace viewer::ui {

Fonts g_fonts;

namespace {

constexpr ImU32 Hex(unsigned rgb, unsigned alpha = 255) {
    return IM_COL32((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF, alpha);
}

ImVec4 V4(ImU32 c) { return ImGui::ColorConvertU32ToFloat4(c); }

std::string FontPath(const char* file) {
    char windows[MAX_PATH]{};
    if (!GetEnvironmentVariableA("WINDIR", windows, MAX_PATH)) return {};
    std::string path = std::string(windows) + "\\Fonts\\" + file;
    return GetFileAttributesA(path.c_str()) == INVALID_FILE_ATTRIBUTES ? std::string() : path;
}

void MergeIcons(ImGuiIO& io) {
    std::string icons = FontPath("SegoeIcons.ttf");  // Windows 11
    if (icons.empty()) icons = FontPath("segmdl2.ttf");  // Windows 10
    if (icons.empty()) return;
    ImFontConfig config;
    config.MergeMode = true;
    config.GlyphOffset = ImVec2(0.0f, 2.0f);
    io.Fonts->AddFontFromFileTTF(icons.c_str(), 0.0f, &config);
}

} // namespace

Theme MakeTheme(bool dark) {
    Theme t;
    t.dark = dark;
    if (dark) {
        t.page = Hex(0x0D0D0D);
        t.card = Hex(0x1A1A19);          // validated dark chart surface
        t.raised = Hex(0x242423);
        t.segment = Hex(0x343432);
        t.field = Hex(0x262624);
        t.fieldHover = Hex(0x302F2D);
        t.border = Hex(0xFFFFFF, 26);
        t.grid = Hex(0x2C2C2A);
        t.axis = Hex(0x383835);
        t.text = Hex(0xF0EFEC);
        t.textSecondary = Hex(0xC3C2B7);
        t.textMuted = Hex(0x898781);
        t.accent = Hex(0x3987E5);
        t.onTime = Hex(0x3987E5);
        t.over = Hex(0xC98500);
        t.hitch = Hex(0xD03B3B);
        t.gap = Hex(0x383835);
        t.twoD = Hex(0x898781);
        t.interval = Hex(0xC3C2B7, 170);
        t.gpu = Hex(0x199E70);           // validated against the class colors; always drawn as a labelled line
        t.simulator = Hex(0xC3C2B7);
    } else {
        t.page = Hex(0xF4F4F2);
        t.card = Hex(0xFCFCFB);          // validated light chart surface
        t.raised = Hex(0xFFFFFF);
        t.segment = Hex(0xFFFFFF);
        t.field = Hex(0xEDECE8);
        t.fieldHover = Hex(0xE4E3DE);
        t.border = Hex(0x0B0B0B, 26);
        t.grid = Hex(0xE1E0D9);
        t.axis = Hex(0xC3C2B7);
        t.text = Hex(0x0B0B0B);
        t.textSecondary = Hex(0x52514E);
        t.textMuted = Hex(0x898781);
        t.accent = Hex(0x2A78D6);
        t.onTime = Hex(0x2A78D6);
        t.over = Hex(0xEDA100);
        t.hitch = Hex(0xD03B3B);
        t.gap = Hex(0xC3C2B7, 140);
        t.twoD = Hex(0x898781);
        t.interval = Hex(0x52514E, 170);
        t.gpu = Hex(0x1BAF7A);
        t.simulator = Hex(0x52514E);
    }
    t.onAccent = Hex(0xFFFFFF);
    t.accentSoft = WithAlpha(t.accent, dark ? 0.22f : 0.14f);
    t.selection = WithAlpha(t.accent, dark ? 0.30f : 0.20f);
    t.good = Hex(0x0CA30C);
    return t;
}

void ApplyTheme(const Theme& t, float dpiScale) {
    ImGuiStyle style;  // fresh defaults, so sizes never compound across theme changes
    style.WindowPadding = ImVec2(16.0f, 14.0f);
    style.FramePadding = ImVec2(10.0f, 6.0f);
    style.ItemSpacing = ImVec2(10.0f, 8.0f);
    style.ItemInnerSpacing = ImVec2(6.0f, 6.0f);
    style.CellPadding = ImVec2(10.0f, 5.0f);
    style.WindowRounding = 0.0f;
    style.ChildRounding = 12.0f;
    style.FrameRounding = 8.0f;
    style.PopupRounding = 10.0f;
    style.ScrollbarRounding = 8.0f;
    style.GrabRounding = 8.0f;
    style.TabRounding = 6.0f;
    style.WindowBorderSize = 0.0f;
    style.ChildBorderSize = 1.0f;
    style.PopupBorderSize = 1.0f;
    style.FrameBorderSize = 0.0f;
    style.TabBorderSize = 0.0f;
    style.TabBarBorderSize = 1.0f;
    style.TabBarOverlineSize = 0.0f;
    style.ScrollbarSize = 10.0f;
    style.GrabMinSize = 12.0f;
    style.SelectableTextAlign = ImVec2(0.0f, 0.5f);

    ImVec4* c = style.Colors;
    const ImVec4 clear(0, 0, 0, 0);
    c[ImGuiCol_Text] = V4(t.text);
    c[ImGuiCol_TextDisabled] = V4(t.textMuted);
    c[ImGuiCol_WindowBg] = V4(t.page);
    c[ImGuiCol_ChildBg] = V4(t.card);
    c[ImGuiCol_PopupBg] = V4(t.raised);
    c[ImGuiCol_Border] = V4(t.border);
    c[ImGuiCol_BorderShadow] = clear;
    c[ImGuiCol_FrameBg] = V4(t.field);
    c[ImGuiCol_FrameBgHovered] = V4(t.fieldHover);
    c[ImGuiCol_FrameBgActive] = V4(t.fieldHover);
    c[ImGuiCol_TitleBg] = V4(t.page);
    c[ImGuiCol_TitleBgActive] = V4(t.page);
    c[ImGuiCol_TitleBgCollapsed] = V4(t.page);
    c[ImGuiCol_MenuBarBg] = V4(t.page);
    c[ImGuiCol_ScrollbarBg] = clear;
    c[ImGuiCol_ScrollbarGrab] = V4(WithAlpha(t.textMuted, 0.35f));
    c[ImGuiCol_ScrollbarGrabHovered] = V4(WithAlpha(t.textMuted, 0.55f));
    c[ImGuiCol_ScrollbarGrabActive] = V4(WithAlpha(t.textMuted, 0.75f));
    c[ImGuiCol_CheckMark] = V4(t.accent);
    c[ImGuiCol_SliderGrab] = V4(t.accent);
    c[ImGuiCol_SliderGrabActive] = V4(t.accent);
    c[ImGuiCol_Button] = V4(t.field);
    c[ImGuiCol_ButtonHovered] = V4(t.fieldHover);
    c[ImGuiCol_ButtonActive] = V4(t.fieldHover);
    c[ImGuiCol_Header] = V4(t.selection);
    c[ImGuiCol_HeaderHovered] = V4(WithAlpha(t.accent, 0.10f));
    c[ImGuiCol_HeaderActive] = V4(t.selection);
    c[ImGuiCol_Separator] = V4(t.border);
    c[ImGuiCol_SeparatorHovered] = V4(t.accent);
    c[ImGuiCol_SeparatorActive] = V4(t.accent);
    c[ImGuiCol_ResizeGrip] = clear;
    c[ImGuiCol_ResizeGripHovered] = clear;
    c[ImGuiCol_ResizeGripActive] = clear;
    c[ImGuiCol_Tab] = clear;
    c[ImGuiCol_TabHovered] = V4(t.field);
    c[ImGuiCol_TabSelected] = V4(t.field);
    c[ImGuiCol_TabSelectedOverline] = V4(t.accent);
    c[ImGuiCol_TabDimmed] = clear;
    c[ImGuiCol_TabDimmedSelected] = V4(t.field);
    c[ImGuiCol_TabDimmedSelectedOverline] = clear;
    c[ImGuiCol_TableHeaderBg] = clear;
    c[ImGuiCol_TableBorderStrong] = V4(t.border);
    c[ImGuiCol_TableBorderLight] = V4(t.border);
    c[ImGuiCol_TableRowBg] = clear;
    c[ImGuiCol_TableRowBgAlt] = V4(WithAlpha(t.text, t.dark ? 0.025f : 0.02f));
    c[ImGuiCol_TextSelectedBg] = V4(t.selection);
    c[ImGuiCol_NavCursor] = V4(t.accent);
    c[ImGuiCol_ModalWindowDimBg] = V4(WithAlpha(t.page, 0.6f));

    style.ScaleAllSizes(dpiScale);
    style.FontScaleDpi = dpiScale;
    style.FontSizeBase = kBodySize;
    ImGui::GetStyle() = style;

    ImPlotStyle& plot = ImPlot::GetStyle();
    plot.PlotBorderSize = 0.0f;
    plot.MinorAlpha = 0.0f;
    plot.MajorTickLen = ImVec2(0.0f, 0.0f);
    plot.MinorTickLen = ImVec2(0.0f, 0.0f);
    plot.MajorGridSize = ImVec2(1.0f, 1.0f);
    plot.PlotPadding = ImVec2(4.0f * dpiScale, 6.0f * dpiScale);
    plot.LabelPadding = ImVec2(6.0f * dpiScale, 4.0f * dpiScale);
    plot.LegendPadding = ImVec2(10.0f * dpiScale, 10.0f * dpiScale);
    plot.FitPadding = ImVec2(0.0f, 0.0f);
    ImVec4* p = plot.Colors;
    p[ImPlotCol_FrameBg] = clear;
    p[ImPlotCol_PlotBg] = V4(t.card);
    p[ImPlotCol_PlotBorder] = clear;
    p[ImPlotCol_LegendBg] = V4(t.raised);
    p[ImPlotCol_LegendBorder] = V4(t.border);
    p[ImPlotCol_LegendText] = V4(t.textSecondary);
    p[ImPlotCol_TitleText] = V4(t.text);
    p[ImPlotCol_InlayText] = V4(t.textSecondary);
    p[ImPlotCol_AxisText] = V4(t.textMuted);
    p[ImPlotCol_AxisGrid] = V4(t.grid);
    p[ImPlotCol_AxisTick] = clear;
    p[ImPlotCol_AxisBg] = clear;
    p[ImPlotCol_AxisBgHovered] = V4(WithAlpha(t.accent, 0.08f));
    p[ImPlotCol_AxisBgActive] = V4(WithAlpha(t.accent, 0.14f));
    p[ImPlotCol_Selection] = V4(t.accent);
    p[ImPlotCol_Crosshairs] = V4(t.textSecondary);
}

bool SystemPrefersDark() {
    DWORD light = 1, size = sizeof(light);
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                     L"AppsUseLightTheme", RRF_RT_REG_DWORD, nullptr, &light, &size) != ERROR_SUCCESS) {
        return true;
    }
    return light == 0;
}

void LoadFonts() {
    ImGuiIO& io = ImGui::GetIO();
    const std::string regular = FontPath("segoeui.ttf");
    const std::string semibold = FontPath("seguisb.ttf");
    if (!regular.empty()) {
        g_fonts.regular = io.Fonts->AddFontFromFileTTF(regular.c_str(), kBodySize);
        MergeIcons(io);
    } else {
        g_fonts.regular = io.Fonts->AddFontDefault();
    }
    if (!semibold.empty()) {
        g_fonts.semibold = io.Fonts->AddFontFromFileTTF(semibold.c_str(), kBodySize);
        MergeIcons(io);
    }
}

void PushSemibold(float size) { ImGui::PushFont(g_fonts.semibold ? g_fonts.semibold : g_fonts.regular, size); }
void PushRegular(float size) { ImGui::PushFont(g_fonts.regular, size); }
float Dpi() { return ImGui::GetStyle().FontScaleDpi; }

ImU32 WithAlpha(ImU32 color, float alpha) {
    const unsigned a = unsigned(((color >> IM_COL32_A_SHIFT) & 0xFF) * alpha + 0.5f);
    return (color & ~IM_COL32_A_MASK) | (ImU32(a > 255 ? 255 : a) << IM_COL32_A_SHIFT);
}

bool BeginCard(const char* id, const ImVec2& size, ImGuiWindowFlags flags, bool scroll) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(Px(16.0f), Px(12.0f)));
    if (!scroll) flags |= ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse;
    const bool open = ImGui::BeginChild(id, size, ImGuiChildFlags_Borders | ImGuiChildFlags_AlwaysUseWindowPadding, flags);
    ImGui::PopStyleVar();
    return open;
}

void EndCard() { ImGui::EndChild(); }

bool Segmented(const char* id, const char* const* labels, int count, int* index, const Theme& t) {
    ImGui::PushID(id);
    const ImGuiStyle& style = ImGui::GetStyle();
    const float height = ImGui::GetFrameHeight(), inset = Px(3.0f), pad = style.FramePadding.x;
    float widths[16];
    float total = inset * 2.0f;
    for (int i = 0; i < count && i < 16; ++i) {
        widths[i] = ImGui::CalcTextSize(labels[i]).x + pad * 2.0f;
        total += widths[i];
    }
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    ImDrawList* draw = ImGui::GetWindowDrawList();
    draw->AddRectFilled(origin, ImVec2(origin.x + total, origin.y + height), t.field, style.FrameRounding);
    bool changed = false;
    float x = origin.x + inset;
    for (int i = 0; i < count && i < 16; ++i) {
        ImGui::SetCursorScreenPos(ImVec2(x, origin.y));
        ImGui::PushID(i);
        if (ImGui::InvisibleButton("##segment", ImVec2(widths[i], height)) && *index != i) {
            *index = i;
            changed = true;
        }
        const bool hovered = ImGui::IsItemHovered();
        ImGui::PopID();
        const ImVec2 a(x, origin.y + inset), b(x + widths[i], origin.y + height - inset);
        if (*index == i) {
            draw->AddRectFilled(a, b, t.segment, style.FrameRounding - inset);
            if (!t.dark) draw->AddRect(a, b, t.border, style.FrameRounding - inset);
        }
        const ImVec2 size = ImGui::CalcTextSize(labels[i]);
        draw->AddText(ImVec2(x + (widths[i] - size.x) * 0.5f, origin.y + (height - size.y) * 0.5f),
                      *index == i || hovered ? t.text : t.textSecondary, labels[i]);
        x += widths[i];
    }
    ImGui::SetCursorScreenPos(origin);
    ImGui::Dummy(ImVec2(total, height));
    ImGui::PopID();
    return changed;
}

bool Chip(const char* label, bool* value, const Theme& t) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float height = ImGui::GetFrameHeight();
    const float check = *value ? ImGui::CalcTextSize(ICON_CHECK).x + style.ItemInnerSpacing.x : 0.0f;
    const ImVec2 text = ImGui::CalcTextSize(label);
    const ImVec2 size(text.x + check + style.FramePadding.x * 2.0f, height);
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const bool pressed = ImGui::InvisibleButton(label, size);
    if (pressed) *value = !*value;
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* draw = ImGui::GetWindowDrawList();
    const ImVec2 end(origin.x + size.x, origin.y + size.y);
    draw->AddRectFilled(origin, end, *value ? t.accentSoft : (hovered ? t.fieldHover : t.field), height * 0.5f);
    if (*value) draw->AddRect(origin, end, WithAlpha(t.accent, 0.55f), height * 0.5f);
    float x = origin.x + style.FramePadding.x;
    const float y = origin.y + (height - text.y) * 0.5f;
    if (*value) {
        draw->AddText(ImVec2(x, y), t.accent, ICON_CHECK);
        x += check;
    }
    draw->AddText(ImVec2(x, y), *value || hovered ? t.text : t.textSecondary, label);
    return pressed;
}

bool IconButton(const char* id, const char* icon, const char* tooltip, const Theme& t) {
    const float size = ImGui::GetFrameHeight();
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const bool pressed = ImGui::InvisibleButton(id, ImVec2(size, size));
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* draw = ImGui::GetWindowDrawList();
    if (hovered) draw->AddRectFilled(origin, ImVec2(origin.x + size, origin.y + size), t.field, ImGui::GetStyle().FrameRounding);
    const ImVec2 glyph = ImGui::CalcTextSize(icon);
    draw->AddText(ImVec2(origin.x + (size - glyph.x) * 0.5f, origin.y + (size - glyph.y) * 0.5f),
                  hovered ? t.text : t.textSecondary, icon);
    if (tooltip && hovered) ImGui::SetTooltip("%s", tooltip);
    return pressed;
}

bool DropdownButton(const char* id, const char* text, float minWidth, const Theme& t) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float height = ImGui::GetFrameHeight();
    const ImVec2 label = ImGui::CalcTextSize(text);
    const float chevron = ImGui::CalcTextSize(ICON_DOWN).x;
    const float width = std::max(minWidth, label.x + chevron + style.FramePadding.x * 2.0f + Px(10.0f));
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const bool pressed = ImGui::InvisibleButton(id, ImVec2(width, height));
    const bool hovered = ImGui::IsItemHovered() || ImGui::IsPopupOpen(id);
    ImDrawList* draw = ImGui::GetWindowDrawList();
    draw->AddRectFilled(origin, ImVec2(origin.x + width, origin.y + height), hovered ? t.fieldHover : t.field, style.FrameRounding);
    const float y = origin.y + (height - label.y) * 0.5f;
    draw->AddText(ImVec2(origin.x + style.FramePadding.x, y), t.text, text);
    draw->AddText(ImVec2(origin.x + width - style.FramePadding.x - chevron, y), t.textSecondary, ICON_DOWN);
    if (pressed) ImGui::OpenPopup(id);
    ImGui::SetNextWindowPos(ImVec2(origin.x, origin.y + height + Px(4.0f)));
    ImGui::SetNextWindowSizeConstraints(ImVec2(width, 0.0f), ImVec2(FLT_MAX, FLT_MAX));
    return ImGui::BeginPopup(id);
}

bool Pill(const char* id, const char* text, bool filled, ImU32 dot, const Theme& t, float pulse, ImU32 fillColor) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float height = ImGui::GetFrameHeight();
    const float dotSpace = dot ? Px(14.0f) : 0.0f;
    const ImVec2 label = ImGui::CalcTextSize(text);
    const ImVec2 size(label.x + dotSpace + style.FramePadding.x * 2.2f, height);
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const bool pressed = ImGui::InvisibleButton(id, size);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* draw = ImGui::GetWindowDrawList();
    const ImVec2 end(origin.x + size.x, origin.y + size.y);
    const ImU32 strong = fillColor ? fillColor : t.accent;
    ImU32 fill = filled ? strong : (hovered ? t.fieldHover : t.field);
    if (filled && hovered) fill = WithAlpha(strong, 0.88f);
    draw->AddRectFilled(origin, end, fill, height * 0.5f);
    float x = origin.x + style.FramePadding.x * 1.1f;
    if (dot) {
        const ImVec2 center(x + Px(4.0f), origin.y + height * 0.5f);
        if (pulse >= 0.0f) {
            // A ring that grows and fades: "receiving" without blinking text.
            draw->AddCircleFilled(center, Px(4.0f) * (1.0f + 0.9f * pulse), WithAlpha(dot, 0.45f * (1.0f - pulse)));
        }
        draw->AddCircleFilled(center, Px(4.0f), dot);
        x += dotSpace;
    }
    draw->AddText(ImVec2(x, origin.y + (height - label.y) * 0.5f), filled ? t.onAccent : t.text, text);
    return pressed;
}

void Dot(ImU32 color, float radius, bool frameRow) {
    const float r = Px(radius);
    const float height = frameRow ? ImGui::GetFrameHeight() : ImGui::GetTextLineHeight();
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(r * 2.0f, height));
    ImGui::GetWindowDrawList()->AddCircleFilled(ImVec2(origin.x + r, origin.y + height * 0.5f), r, color);
}

void Swatch(ImU32 color, bool frameRow) {
    const float s = Px(10.0f);
    const float height = frameRow ? ImGui::GetFrameHeight() : ImGui::GetTextLineHeight();
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(s, height));
    const float y = origin.y + (height - s) * 0.5f;
    ImGui::GetWindowDrawList()->AddRectFilled(ImVec2(origin.x, y), ImVec2(origin.x + s, y + s), color, Px(2.5f));
}

void LineKey(ImU32 color, bool frameRow) {
    const float w = Px(14.0f);
    const float height = frameRow ? ImGui::GetFrameHeight() : ImGui::GetTextLineHeight();
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, height));
    const float y = origin.y + height * 0.5f;
    ImGui::GetWindowDrawList()->AddLine(ImVec2(origin.x, y), ImVec2(origin.x + w, y), color, Px(2.0f));
}

void Muted(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextV(fmt, args);
    ImGui::PopStyleColor();
    va_end(args);
}

void Caption(const char* text) {
    PushSemibold(kSmallSize);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
    PopFont();
}

} // namespace viewer::ui
