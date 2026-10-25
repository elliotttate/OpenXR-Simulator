// D3D11 / Shader Model 5.0. The front uses the native UI without relighting.
cbuffer State : register(b0) {
	float4 camera;       // eye x, camera z, tan(vertical FOV/2), aspect
	float4 model;        // yaw cosine/sine, local origin x/y
	float4 object;       // local origin z, object id, receiver shadow enable, extra hover
	float4 rect0;        // button shadow receiver bounds: center xy, half size xy
	float4 rect1;
}
Texture2D NativeFront : register(t0);
SamplerState NativeSampler : register(s0);
struct In { float3 p:POSITION; float3 n:NORMAL; float2 uv:TEXCOORD0; uint surface:TEXCOORD1; };
struct Interpolants {
	float4 p:SV_Position;
	float3 world:TEXCOORD0;
	float3 normal:TEXCOORD1;
	float2 uv:TEXCOORD2;
	float2 local:TEXCOORD3;
	nointerpolation uint surface:TEXCOORD4;
};
float3 turn(float3 v) { return float3(model.x*v.x+model.y*v.z,v.y,-model.y*v.x+model.x*v.z); }
Interpolants VS(In i) {
	Interpolants o;
	float3 local=i.p+float3(model.zw,object.x);
	o.world=turn(local);
	float depth=camera.y-o.world.z;
	const float nearZ=.05,farZ=10;
	o.p=float4((o.world.x-camera.x)/(camera.z*camera.w),o.world.y/camera.z,
		farZ/(farZ-nearZ)*depth-farZ*nearZ/(farZ-nearZ),depth);
	o.normal=turn(i.n); o.uv=i.uv; o.local=local.xy; o.surface=i.surface;
	return o;
}
float rectangleDistance(float2 p,float4 r) {
	float2 d=abs(p-r.xy)-r.zw;
	return length(max(d,0))+min(max(d.x,d.y),0);
}
float buttonShadow(float2 p,float4 r,float separation) {
	// An analytic rectangular area-light shadow on the actual panel plane.
	// Penumbra and directional offset grow with real button separation.
	const float2 lightSlope=float2(.35,-.5);
	float blur=.0008+separation*.15;
	r.xy+=lightSlope*separation;
	return .22*(1-smoothstep(-blur,blur,rectangleDistance(p,r)));
}
struct Targets { float4 color:SV_Target0; float4 geometry:SV_Target1; };
Targets PS(Interpolants i) {
	Targets o;
	if(i.surface==1) {
		o.color=NativeFront.Sample(NativeSampler,i.uv);
		clip(o.color.a-.001);
		if(object.z>0) {
			// The native up arrow is disabled; only the enabled down arrow lifts.
			float s=max(buttonShadow(i.local,rect0,.003),buttonShadow(i.local,rect1,.003+object.w));
			o.color.rgb*=1-s;
		}
	} else {
		float diffuse=.45+.55*saturate(dot(normalize(i.normal),normalize(float3(-.5,.7,1))));
		// Graphite matte shell: diffuse only, no specular shimmer on head motion.
		o.color=float4(float3(.018,.022,.028)*diffuse,1);
	}
	o.geometry=float4(i.world,object.y*8+i.surface);
	return o;
}
