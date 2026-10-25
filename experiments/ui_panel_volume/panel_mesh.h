#pragma once
#include <array>
#include <cmath>
#include <vector>

namespace panel_volume {
struct Vec3 { float x, y, z; };
inline Vec3 operator+(Vec3 a, Vec3 b) { return {a.x+b.x,a.y+b.y,a.z+b.z}; }
inline Vec3 operator-(Vec3 a, Vec3 b) { return {a.x-b.x,a.y-b.y,a.z-b.z}; }
inline Vec3 operator*(Vec3 a, float s) { return {a.x*s,a.y*s,a.z*s}; }
inline float dot(Vec3 a, Vec3 b) { return a.x*b.x+a.y*b.y+a.z*b.z; }
inline Vec3 cross(Vec3 a, Vec3 b) { return {a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x}; }
inline Vec3 normalized(Vec3 a) { const float n=std::sqrt(dot(a,a)); return a*(1.f/n); }
enum class Surface : unsigned { Front=1, Bevel=2, Side=3, Back=4 };
struct Vertex { Vec3 position, normal; float u,v; unsigned surface; };
using Mesh = std::vector<Vertex>;
struct Plaque { float width=.9f, height=.46f, thickness=.004f, bevel=.0006f; };
inline void triangle(Mesh& m, Vec3 a, Vec3 b, Vec3 c, Surface kind, const Plaque& p) {
	const Vec3 n=normalized(cross(b-a,c-a));
	for (const Vec3 v : {a,b,c}) m.push_back({v,n,v.x/p.width+.5f,.5f-v.y/p.height,(unsigned)kind});
}
inline void quad(Mesh& m, Vec3 a, Vec3 b, Vec3 c, Vec3 d, Surface kind, const Plaque& p) {
	triangle(m,a,b,c,kind,p); triangle(m,a,c,d,kind,p);
}
inline Mesh flat(const Plaque& p) {
	Mesh m;
	quad(m,{-p.width/2,-p.height/2,0},{p.width/2,-p.height/2,0},
		{p.width/2,p.height/2,0},{-p.width/2,p.height/2,0},Surface::Front,p);
	return m;
}
inline Mesh shell(const Plaque& p) {
	// The original face and its full UV rectangle are immutable. The shell goes
	// backwards, inside that silhouette. Its small rear chamfer is invisible
	// head-on, unlike a conventional front bevel which would restyle the border.
	Mesh m=flat(p);
	const float x=p.width*.5f,y=p.height*.5f;
	const std::array<Vec3,4> front{{{-x,-y,0},{x,-y,0},{x,y,0},{-x,y,0}}};
	std::array<Vec3,4> shoulder,back;
	for(unsigned i=0;i<4;++i) {
		shoulder[i]={front[i].x,front[i].y,-p.thickness+p.bevel};
		back[i]={front[i].x*(x-p.bevel)/x,front[i].y*(y-p.bevel)/y,-p.thickness};
	}
	for(unsigned i=0;i<4;++i) {
		const unsigned j=(i+1)%4;
		quad(m,front[i],shoulder[i],shoulder[j],front[j],Surface::Side,p);
		quad(m,shoulder[i],back[i],back[j],shoulder[j],Surface::Bevel,p);
	}
	quad(m,back[0],back[3],back[2],back[1],Surface::Back,p);
	return m;
}
}
