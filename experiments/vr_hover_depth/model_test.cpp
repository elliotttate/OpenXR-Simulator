#include "hover_model.h"
#include <cstdio>
#include <cstdlib>
#include <initializer_list>

#define EXPECT(x) do { if (!(x)) { std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); return 1; } } while (0)
int main() {
	hover_depth::Lift lift;
	for (int i=0; i<90; ++i) lift.update(true, false, 1.f/90.f);
	EXPECT(lift.meters > .029f && lift.meters <= .03001f);
	float previous = lift.meters;
	for (int i=0; i<20; ++i) {
		lift.update(true, true, 1.f/90.f);
		EXPECT(lift.meters <= previous && lift.meters >= hover_depth::pressedMeters);
		previous = lift.meters;
	}
	EXPECT(lift.meters < .007f);
	for (int i=0; i<120; ++i) lift.update(false, false, 1.f/90.f);
	EXPECT(lift.meters < .00001f);
	for (const float rate : {72.f, 90.f, 120.f}) {
		hover_depth::Lift a;
		for (int i=0; i<(int)rate; ++i) a.update(true, false, 1.f/rate);
		EXPECT(std::fabs(a.meters-hover_depth::hoverMeters) < .00001f);
	}
	EXPECT(hover_depth::button.contains(.6001f, .3501f));
	EXPECT(hover_depth::button.contains(.8499f, .6499f));
	EXPECT(!hover_depth::button.contains(.599f, .5f));
	hover_depth::Lift interrupted;
	for (int i=0; i<4; ++i) interrupted.update(true, false, 1.f/90.f);
	const float partial = interrupted.meters;
	interrupted.update(false, false, 1.f/90.f);
	EXPECT(interrupted.meters < partial && interrupted.meters > 0.f);
	interrupted.update(true, false, 1.f/90.f);
	EXPECT(interrupted.meters > 0.f && interrupted.meters < hover_depth::hoverMeters);
	const float unchanged = interrupted.meters;
	interrupted.update(true, true, 0.f);
	EXPECT(interrupted.meters == unchanged);
	std::puts("Hover depth model: motion, press, exit, refresh-rate independence, fixed hit bounds passed.");
}
