#pragma once
#include <cmath>

namespace hover_depth {
struct Rect {
	float left, top, right, bottom;
	bool contains(float x, float y) const { return x >= left && x <= right && y >= top && y <= bottom; }
};

// Interaction always uses this base-plane rectangle, independent of visual motion.
static constexpr Rect button{.60f, .35f, .85f, .65f};
static constexpr float hoverMeters = .030f;
static constexpr float pressedMeters = .006f;

struct Lift {
	float meters = 0.f;
	void update(bool hovered, bool pressed, float seconds) {
		const float target = hovered ? (pressed ? pressedMeters : hoverMeters) : 0.f;
		const float timeConstant = pressed ? .025f : hovered ? .045f : .035f;
		const float dt = seconds > 0.f ? seconds : 0.f;
		meters = target + (meters-target)*std::exp(-dt/timeConstant);
	}
};
}
