// The viewer's design system: light and dark theme tokens, the fonts, and the
// handful of custom widgets (cards, segmented controls, chips, pills) that
// stock Dear ImGui does not draw the way this tool wants.
//
// Frame classes are status colors, not series colors: on-time frames take the
// calm accent blue, late frames the warning amber and critical red. All three
// pass the dataviz palette checks against their own surface in each theme
// (CVD and normal-vision separation, lightness band); the light amber sits
// below 3:1 contrast, so a class is always named next to its color too.
#pragma once
#include "imgui.h"

#define ICON_PLAY     "\xEE\x9D\xA8"  // U+E768, Segoe Fluent Icons
#define ICON_PAUSE    "\xEE\x9D\xA9"  // U+E769
#define ICON_SUN      "\xEE\x9C\x86"  // U+E706
#define ICON_MOON     "\xEE\x9C\x88"  // U+E708
#define ICON_CLEAR    "\xEE\xA2\x94"  // U+E894
#define ICON_FIT      "\xEE\xA6\xA6"  // U+E9A6
#define ICON_LEFT     "\xEE\x9D\xAB"  // U+E76B
#define ICON_RIGHT    "\xEE\x9D\xAC"  // U+E76C
#define ICON_WARNING  "\xEE\x9E\xBA"  // U+E7BA
#define ICON_ERROR    "\xEE\xA8\xB9"  // U+EA39
#define ICON_CHECK    "\xEE\x9C\xBE"  // U+E73E
#define ICON_DOWN     "\xEE\x9C\x8D"  // U+E70D
#define ICON_INFO     "\xEE\xA5\x86"  // U+E946
#define ICON_FOLDER   "\xEE\xA0\xB8"  // U+E838
#define ICON_DOC      "\xEE\xA2\xA5"  // U+E8A5
#define ICON_STOP     "\xEE\x9C\x9A"  // U+E71A
#define ICON_BACK     "\xEE\x9C\xAB"  // U+E72B
#define ICON_COMPARE  "\xEE\xA2\xAB"  // U+E8AB

namespace viewer::ui {

struct Theme {
    bool dark = true;
    // Surfaces and lines.
    ImU32 page, card, raised, segment, field, fieldHover, border, grid, axis;
    // Ink.
    ImU32 text, textSecondary, textMuted, onAccent;
    // Interaction.
    ImU32 accent, accentSoft, selection;
    // Data.
    ImU32 onTime, over, hitch, gap, twoD, interval, gpu, simulator, good;
};

Theme MakeTheme(bool dark);
// Rebuilds the ImGui and ImPlot styles from the tokens. Sizes are authored at
// 96 DPI and scaled by `dpiScale`.
void ApplyTheme(const Theme& theme, float dpiScale);
// Windows' "app mode" setting (Settings > Personalization > Colors).
bool SystemPrefersDark();

struct Fonts {
    ImFont* regular = nullptr;
    ImFont* semibold = nullptr;
};
extern Fonts g_fonts;
// Loads Segoe UI, Segoe UI Semibold and the Fluent icon font merged into both.
void LoadFonts();

constexpr float kBodySize = 15.0f;
constexpr float kSmallSize = 13.0f;

void PushSemibold(float size);
void PushRegular(float size);
inline void PopFont() { ImGui::PopFont(); }
float Dpi();
inline float Px(float v) { return v * Dpi(); }

ImU32 WithAlpha(ImU32 color, float alpha);

// A rounded surface with a hairline border; content inside is padded. Cards
// clip by default; `scroll` lets one scroll when its content runs long.
bool BeginCard(const char* id, const ImVec2& size, ImGuiWindowFlags flags = 0, bool scroll = false);
void EndCard();

// Mutually exclusive options in one rounded track. Returns true on change.
bool Segmented(const char* id, const char* const* labels, int count, int* index, const Theme& t);
// A pill that toggles. Returns true on change.
bool Chip(const char* label, bool* value, const Theme& t);
// A borderless square button holding one icon.
bool IconButton(const char* id, const char* icon, const char* tooltip, const Theme& t);
// A pill-shaped button; `filled` uses the accent, or `fill` when given. A
// `pulse` in [0, 1] rings the dot.
bool Pill(const char* id, const char* text, bool filled, ImU32 dot, const Theme& t, float pulse = -1.0f, ImU32 fill = 0);
// A field showing `text` and a chevron. Returns true while its popup is open:
// fill it, then call ImGui::EndPopup().
bool DropdownButton(const char* id, const char* text, float minWidth, const Theme& t);

// Inline marks for legends, lists and status text. `frameRow` centers them on
// a row of framed widgets (after AlignTextToFramePadding) rather than on text.
void Dot(ImU32 color, float radius = 4.0f, bool frameRow = false);
void Swatch(ImU32 color, bool frameRow = false);
void LineKey(ImU32 color, bool frameRow = false);
void Muted(const char* fmt, ...) IM_FMTARGS(1);
void Caption(const char* text);  // small uppercase-free section label in muted ink

} // namespace viewer::ui
