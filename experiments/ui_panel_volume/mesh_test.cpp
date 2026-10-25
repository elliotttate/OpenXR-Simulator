#include "panel_mesh.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>

#define EXPECT(x) do { if (!(x)) { std::fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#x); return 1; } } while(0)
int main(int argc,char** argv) {
	using namespace panel_volume;
	const Plaque p;
	const bool negative=argc>1 && std::string(argv[1])=="--flat-control";
	const Mesh m=negative ? flat(p) : shell(p);
	float lo=0,hi=0; unsigned sides=0,bevels=0,fronts=0;
	for (const Vertex& v:m) {
		lo=std::min(lo,v.position.z); hi=std::max(hi,v.position.z);
		sides+=v.surface==(unsigned)Surface::Side;
		bevels+=v.surface==(unsigned)Surface::Bevel;
		fronts+=v.surface==(unsigned)Surface::Front;
		EXPECT(std::fabs(dot(v.normal,v.normal)-1.f)<.00001f);
		if(v.surface==(unsigned)Surface::Front) {
			EXPECT(v.position.z==0.f); EXPECT(v.u>=0 && v.u<=1 && v.v>=0 && v.v<=1);
		}
		EXPECT(std::fabs(v.position.x)<=p.width*.5f);
		EXPECT(std::fabs(v.position.y)<=p.height*.5f);
	}
	EXPECT(sides>0); EXPECT(bevels>0); EXPECT(fronts==6);
	EXPECT(std::fabs(lo+p.thickness)<.000001f); EXPECT(hi==0.f);
	const Mesh control=flat(p);
	for(size_t i=0;i<control.size();++i) {
		EXPECT(m[i].position.x==control[i].position.x && m[i].position.y==control[i].position.y && m[i].position.z==control[i].position.z);
		EXPECT(m[i].u==control[i].u && m[i].v==control[i].v);
	}
	std::puts("PASS: real side vertices, small bevel, full native front UVs, physical thickness.");
}
