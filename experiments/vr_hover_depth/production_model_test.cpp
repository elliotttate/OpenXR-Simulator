#include "PlayAll/Drivers/Game/HudButtonMotion.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>

using PlayAll::Game::VR::HudButtonBounds;
using PlayAll::Game::VR::HudButtonMotion;

static void require(bool condition, const char* message)
{
	if (!condition) { std::fprintf(stderr, "%s\n", message); std::exit(1); }
}

int main()
{
	const HudButtonBounds source(.6f, .35f, .85f, .65f);
	HudButtonMotion motion;
	require(motion.meters == 0.f, "Idle starts on the source plane");
	motion.Update(true, false, .135f);
	require(motion.meters > .0284f && motion.meters < .030f, "Hover reaches 95% of a real 30 mm lift within 135 ms");
	for (int i = 0; i < 90; ++i) motion.Update(true, false, 1.f / 90.f);
	require(std::fabs(motion.meters - .030f) < .000001f, "Held hover settles at 30 mm");
	const float hovered = motion.meters;
	motion.Update(true, true, .075f);
	require(motion.meters > .006f && motion.meters < .0073f, "Press approaches 6 mm without passing through the base");
	for (int i = 0; i < 90; ++i) motion.Update(true, true, 1.f / 90.f);
	require(std::fabs(motion.meters - .006f) < .000001f, "Held press settles at 6 mm");
	motion.Update(true, false, .025f);
	require(motion.meters > .006f && motion.meters < hovered, "An interrupted press returns toward hover from its current depth");
	for (int i = 0; i < 180; ++i)
	{
		const float previous = motion.meters;
		motion.Update(false, false, 1.f / 90.f);
		require(motion.meters >= 0.f && motion.meters <= previous, "Return never overshoots or reverses");
	}
	require(motion.meters < .0001f, "Return releases the transient layer below the 0.1 mm threshold");
	require(source.Contains(.6f, .35f) && source.Contains(.85f, .65f) && !source.Contains(.59f, .5f),
		"Physical movement preserves the full fixed source rectangle, including its edges");
	HudButtonMotion slow, fast;
	for (int i = 0; i < 9; ++i) slow.Update(true, false, 1.f / 30.f);
	for (int i = 0; i < 27; ++i) fast.Update(true, false, 1.f / 90.f);
	require(std::fabs(slow.meters - fast.meters) < .000001f, "Depth timing is independent of frame rate");
	const float unchanged = fast.meters;
	fast.Update(false, false, 0.f); fast.Update(false, false, -1.f);
	require(std::fabs(fast.meters - unchanged) < .000001f, "Paused or invalid negative elapsed time cannot teleport the face");
	std::puts("production HUD button motion: PASS");
}
