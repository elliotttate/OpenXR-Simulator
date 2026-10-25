#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <wincodec.h>
#include <wrl/client.h>
#include "panel_mesh.h"
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>

using Microsoft::WRL::ComPtr;
using namespace panel_volume;
namespace fs=std::filesystem;
static void check(HRESULT hr,const char* operation) {
	if(FAILED(hr)) { char text[256]; std::snprintf(text,sizeof(text),"%s: HRESULT %08lx",operation,(unsigned long)hr); throw std::runtime_error(text); }
}
struct Image { unsigned w=0,h=0; std::vector<unsigned char> bytes; };
struct Crop { unsigned x,y,w,h; };
static ComPtr<IWICImagingFactory> wic;
static Image readImage(const fs::path& path) {
	ComPtr<IWICBitmapDecoder> decoder; ComPtr<IWICBitmapFrameDecode> frame; ComPtr<IWICFormatConverter> converter;
	check(wic->CreateDecoderFromFilename(path.c_str(),nullptr,GENERIC_READ,WICDecodeMetadataCacheOnLoad,&decoder),"Read native PNG");
	check(decoder->GetFrame(0,&frame),"PNG frame");
	check(wic->CreateFormatConverter(&converter),"PNG converter");
	check(converter->Initialize(frame.Get(),GUID_WICPixelFormat32bppRGBA,WICBitmapDitherTypeNone,nullptr,0,WICBitmapPaletteTypeCustom),"RGBA converter");
	Image image; check(converter->GetSize(&image.w,&image.h),"PNG dimensions"); image.bytes.resize((size_t)image.w*image.h*4);
	check(converter->CopyPixels(nullptr,image.w*4,(UINT)image.bytes.size(),image.bytes.data()),"PNG pixels"); return image;
}
static Image cropImage(const Image& image,Crop c) {
	if(c.x+c.w>image.w || c.y+c.h>image.h) throw std::runtime_error("Native crop exceeds source image");
	Image result{c.w,c.h,std::vector<unsigned char>((size_t)c.w*c.h*4)};
	for(unsigned y=0;y<c.h;++y) std::copy_n(image.bytes.data()+((size_t)(y+c.y)*image.w+c.x)*4,c.w*4,result.bytes.data()+(size_t)y*c.w*4);
	return result;
}
static void writeImage(const Image& image,const fs::path& path) {
	ComPtr<IWICStream> stream; ComPtr<IWICBitmapEncoder> encoder; ComPtr<IWICBitmapFrameEncode> frame;
	check(wic->CreateStream(&stream),"PNG stream"); check(stream->InitializeFromFilename(path.c_str(),GENERIC_WRITE),"PNG output path");
	check(wic->CreateEncoder(GUID_ContainerFormatPng,nullptr,&encoder),"PNG encoder"); check(encoder->Initialize(stream.Get(),WICBitmapEncoderNoCache),"PNG encoder init");
	check(encoder->CreateNewFrame(&frame,nullptr),"PNG new frame"); check(frame->Initialize(nullptr),"PNG frame init"); check(frame->SetSize(image.w,image.h),"PNG size");
	// WIC's PNG encoder negotiates BGRA; passing RGBA to that negotiated format
	// silently exchanges native amber and blue. Convert explicitly and roundtrip-test.
	WICPixelFormatGUID format=GUID_WICPixelFormat32bppBGRA; check(frame->SetPixelFormat(&format),"PNG format");
	if(!IsEqualGUID(format,GUID_WICPixelFormat32bppBGRA))throw std::runtime_error("PNG encoder did not accept BGRA");
	auto bgra=image.bytes;for(size_t i=0;i<bgra.size();i+=4)std::swap(bgra[i],bgra[i+2]);
	check(frame->WritePixels(image.h,image.w*4,(UINT)bgra.size(),bgra.data()),"PNG write"); check(frame->Commit(),"PNG frame commit"); check(encoder->Commit(),"PNG commit");
}
struct PixelGeometry { float x,y,z,kind; };
struct Frame { Image color; std::vector<PixelGeometry> geometry; };
struct Rect { float x,y,w,h; };
struct Constants { float camera[4],model[4],object[4],rect0[4],rect1[4]; };
static_assert(sizeof(Constants)%16==0,"D3D11 CB alignment");
static constexpr unsigned width=1280,height=800;
static constexpr float cameraDistance=1.3f,tanHalfFov=.445228685f,ipd=.064f;
static constexpr float panelWidth=1.25f,restMeters=.003f,hoverMeters=.030f;
static constexpr Crop panelCrop{105,61,1901,971};
static constexpr std::array<Crop,2> buttonCrops{{{1772,74,87,79},{1903,74,88,79}}};
static const float panelHeight=panelWidth*panelCrop.h/panelCrop.w;
static Rect nativeRect(Crop c) {
	const float scale=panelWidth/panelCrop.w;
	return {(float(c.x-panelCrop.x)+c.w*.5f)*scale-panelWidth*.5f,
		panelHeight*.5f-(float(c.y-panelCrop.y)+c.h*.5f)*scale,c.w*scale,c.h*scale};
}
struct Renderer {
	ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> context;
	ComPtr<ID3D11Texture2D> target,diagnostic,depth,colorReadback,diagnosticReadback;
	ComPtr<ID3D11RenderTargetView> rtv,geometryRtv; ComPtr<ID3D11DepthStencilView> dsv;
	ComPtr<ID3D11VertexShader> vs; ComPtr<ID3D11PixelShader> ps;
	ComPtr<ID3D11InputLayout> layout; ComPtr<ID3D11Buffer> cb;
	ComPtr<ID3D11SamplerState> sampler; ComPtr<ID3D11RasterizerState> rasterizer;
	ComPtr<ID3D11DepthStencilState> depthState;
	ComPtr<ID3D11ShaderResourceView> originalPanel,backingPanel; std::array<ComPtr<ID3D11ShaderResourceView>,2> buttons;
	void texture(const Image& image,ComPtr<ID3D11ShaderResourceView>& srv) {
		D3D11_TEXTURE2D_DESC desc{}; desc.Width=image.w; desc.Height=image.h; desc.MipLevels=desc.ArraySize=1;
		desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB; desc.SampleDesc.Count=1; desc.Usage=D3D11_USAGE_IMMUTABLE; desc.BindFlags=D3D11_BIND_SHADER_RESOURCE;
		D3D11_SUBRESOURCE_DATA data{image.bytes.data(),image.w*4,0}; ComPtr<ID3D11Texture2D> tex;
		check(device->CreateTexture2D(&desc,&data,&tex),"Native texture"); check(device->CreateShaderResourceView(tex.Get(),nullptr,&srv),"Native texture SRV");
	}
	void init(const Image& native,const fs::path& shader) {
		const D3D_FEATURE_LEVEL requested[]={D3D_FEATURE_LEVEL_11_0}; D3D_FEATURE_LEVEL actual{};
		// WARP is deliberate and enforced. No hardware adapter, HWND, OpenXR runtime,
		// simulator DLL, game process, or RenderDoc replay is touched by this probe.
		check(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,requested,1,D3D11_SDK_VERSION,&device,&actual,&context),"WARP device");
		if(actual!=D3D_FEATURE_LEVEL_11_0) throw std::runtime_error("SM5 feature level unavailable");
		D3D11_TEXTURE2D_DESC desc{}; desc.Width=width; desc.Height=height; desc.MipLevels=desc.ArraySize=1; desc.SampleDesc.Count=1;
		desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB; desc.Usage=D3D11_USAGE_DEFAULT; desc.BindFlags=D3D11_BIND_RENDER_TARGET;
		check(device->CreateTexture2D(&desc,nullptr,&target),"Color target"); check(device->CreateRenderTargetView(target.Get(),nullptr,&rtv),"Color RTV");
		desc.Format=DXGI_FORMAT_R32G32B32A32_FLOAT; check(device->CreateTexture2D(&desc,nullptr,&diagnostic),"Geometry target"); check(device->CreateRenderTargetView(diagnostic.Get(),nullptr,&geometryRtv),"Geometry RTV");
		desc.Format=DXGI_FORMAT_D32_FLOAT; desc.BindFlags=D3D11_BIND_DEPTH_STENCIL;
		check(device->CreateTexture2D(&desc,nullptr,&depth),"Depth target"); check(device->CreateDepthStencilView(depth.Get(),nullptr,&dsv),"Depth view");
		desc.Usage=D3D11_USAGE_STAGING; desc.BindFlags=0; desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ; desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
		check(device->CreateTexture2D(&desc,nullptr,&colorReadback),"Color readback"); desc.Format=DXGI_FORMAT_R32G32B32A32_FLOAT;
		check(device->CreateTexture2D(&desc,nullptr,&diagnosticReadback),"Geometry readback");
		ComPtr<ID3DBlob> vb,pb,error;
		HRESULT result=D3DCompileFromFile(shader.c_str(),nullptr,D3D_COMPILE_STANDARD_FILE_INCLUDE,"VS","vs_5_0",D3DCOMPILE_ENABLE_STRICTNESS,0,&vb,&error);
		if(FAILED(result) && error) std::fprintf(stderr,"%s\n",(const char*)error->GetBufferPointer()); check(result,"SM5 VS");
		result=D3DCompileFromFile(shader.c_str(),nullptr,D3D_COMPILE_STANDARD_FILE_INCLUDE,"PS","ps_5_0",D3DCOMPILE_ENABLE_STRICTNESS,0,&pb,&error);
		if(FAILED(result) && error) std::fprintf(stderr,"%s\n",(const char*)error->GetBufferPointer()); check(result,"SM5 PS");
		check(device->CreateVertexShader(vb->GetBufferPointer(),vb->GetBufferSize(),nullptr,&vs),"Vertex shader");
		check(device->CreatePixelShader(pb->GetBufferPointer(),pb->GetBufferSize(),nullptr,&ps),"Pixel shader");
		const D3D11_INPUT_ELEMENT_DESC elements[]={{"POSITION",0,DXGI_FORMAT_R32G32B32_FLOAT,0,0,D3D11_INPUT_PER_VERTEX_DATA,0},
			{"NORMAL",0,DXGI_FORMAT_R32G32B32_FLOAT,0,12,D3D11_INPUT_PER_VERTEX_DATA,0},{"TEXCOORD",0,DXGI_FORMAT_R32G32_FLOAT,0,24,D3D11_INPUT_PER_VERTEX_DATA,0},
			{"TEXCOORD",1,DXGI_FORMAT_R32_UINT,0,32,D3D11_INPUT_PER_VERTEX_DATA,0}};
		static_assert(sizeof(Vertex)==36,"Native input layout");
		check(device->CreateInputLayout(elements,4,vb->GetBufferPointer(),vb->GetBufferSize(),&layout),"Input layout");
		D3D11_BUFFER_DESC bd{}; bd.ByteWidth=sizeof(Constants); bd.Usage=D3D11_USAGE_DEFAULT; bd.BindFlags=D3D11_BIND_CONSTANT_BUFFER; check(device->CreateBuffer(&bd,nullptr,&cb),"Constants");
		D3D11_SAMPLER_DESC sd{}; sd.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR; sd.AddressU=sd.AddressV=sd.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP; sd.MaxLOD=D3D11_FLOAT32_MAX;
		check(device->CreateSamplerState(&sd,&sampler),"Native sampler");
		D3D11_RASTERIZER_DESC rd{}; rd.FillMode=D3D11_FILL_SOLID; rd.CullMode=D3D11_CULL_NONE; rd.DepthClipEnable=TRUE;
		check(device->CreateRasterizerState(&rd,&rasterizer),"Rasterizer");
		D3D11_DEPTH_STENCIL_DESC ds{}; ds.DepthEnable=TRUE; ds.DepthWriteMask=D3D11_DEPTH_WRITE_MASK_ALL; ds.DepthFunc=D3D11_COMPARISON_LESS_EQUAL;
		check(device->CreateDepthStencilState(&ds,&depthState),"Depth state");
		Image front=cropImage(native,panelCrop); texture(front,originalPanel);
		// Equivalent to suppressing each original HudNode before drawing a promoted
		// button: restore the exact native header backing beneath those rectangles.
		for(unsigned b=0;b<buttonCrops.size();++b) {
			const Crop c=buttonCrops[b]; texture(cropImage(native,c),buttons[b]);
			for(unsigned y=c.y-panelCrop.y;y<c.y-panelCrop.y+c.h;++y) for(unsigned x=c.x-panelCrop.x;x<c.x-panelCrop.x+c.w;++x) {
				unsigned char* pixel=front.bytes.data()+((size_t)y*front.w+x)*4;
				pixel[0]=8;pixel[1]=11;pixel[2]=14;pixel[3]=255;
			}
		}
		texture(front,backingPanel);
	}
	void draw(const Mesh& mesh,ID3D11ShaderResourceView* texture,const Constants& constants) {
		ComPtr<ID3D11Buffer> vertices; D3D11_BUFFER_DESC bd{}; bd.ByteWidth=(UINT)(mesh.size()*sizeof(Vertex)); bd.Usage=D3D11_USAGE_IMMUTABLE; bd.BindFlags=D3D11_BIND_VERTEX_BUFFER;
		D3D11_SUBRESOURCE_DATA data{mesh.data(),0,0}; check(device->CreateBuffer(&bd,&data,&vertices),"Panel vertices");
		const UINT stride=sizeof(Vertex),offset=0; ID3D11Buffer* vertex=vertices.Get(); context->IASetVertexBuffers(0,1,&vertex,&stride,&offset);
		context->UpdateSubresource(cb.Get(),0,nullptr,&constants,0,0); context->PSSetShaderResources(0,1,&texture); context->Draw((UINT)mesh.size(),0);
	}
	Frame render(float thickness,float yaw,float eye,bool withButtons=false,bool shadows=false,float hover=0) {
		const float clear[]={.055f,.065f,.08f,1},zero[]={0,0,0,0}; context->ClearRenderTargetView(rtv.Get(),clear); context->ClearRenderTargetView(geometryRtv.Get(),zero); context->ClearDepthStencilView(dsv.Get(),D3D11_CLEAR_DEPTH,1,0);
		ID3D11RenderTargetView* targets[]={rtv.Get(),geometryRtv.Get()}; context->OMSetRenderTargets(2,targets,dsv.Get()); context->OMSetDepthStencilState(depthState.Get(),0);
		D3D11_VIEWPORT viewport{0,0,(float)width,(float)height,0,1}; context->RSSetViewports(1,&viewport); context->RSSetState(rasterizer.Get());
		context->IASetInputLayout(layout.Get()); context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST); context->VSSetShader(vs.Get(),nullptr,0); context->PSSetShader(ps.Get(),nullptr,0);
		ID3D11Buffer* constant=cb.Get(); context->VSSetConstantBuffers(0,1,&constant); context->PSSetConstantBuffers(0,1,&constant); ID3D11SamplerState* samp=sampler.Get();context->PSSetSamplers(0,1,&samp);
		const float angle=yaw*3.14159265358979323846f/180.f;
		Constants c{{eye,cameraDistance,tanHalfFov,float(width)/height},{std::cos(angle),std::sin(angle),0,0},{0,1,shadows?1.f:0.f,hover},{},{}};
		const Rect r0=nativeRect(buttonCrops[0]),r1=nativeRect(buttonCrops[1]);
		const float rects[8]={r0.x,r0.y,r0.w*.5f,r0.h*.5f,r1.x,r1.y,r1.w*.5f,r1.h*.5f}; std::copy_n(rects,4,c.rect0);std::copy_n(rects+4,4,c.rect1);
		Plaque panel{panelWidth,panelHeight,thickness,.0006f}; draw(thickness>0?shell(panel):flat(panel),withButtons?backingPanel.Get():originalPanel.Get(),c);
		if(withButtons) for(unsigned i=0;i<buttonCrops.size();++i) {
			const Rect r=nativeRect(buttonCrops[i]); c.model[2]=r.x;c.model[3]=r.y;c.object[0]=restMeters+(i==1?hover:0);c.object[1]=float(i+2);c.object[2]=0;
			const Plaque button{r.w,r.h,thickness>0?std::min(thickness,.005f):0,.0004f}; draw(thickness>0?shell(button):flat(button),buttons[i].Get(),c);
		}
		context->OMSetRenderTargets(0,nullptr,nullptr); ID3D11ShaderResourceView* nullSrv=nullptr;context->PSSetShaderResources(0,1,&nullSrv);
		context->CopyResource(colorReadback.Get(),target.Get());context->CopyResource(diagnosticReadback.Get(),diagnostic.Get());
		Frame f{{width,height,std::vector<unsigned char>((size_t)width*height*4)},std::vector<PixelGeometry>((size_t)width*height)};
		D3D11_MAPPED_SUBRESOURCE mapped{}; check(context->Map(colorReadback.Get(),0,D3D11_MAP_READ,0,&mapped),"Color map");
		for(unsigned y=0;y<height;++y)std::copy_n((unsigned char*)mapped.pData+(size_t)y*mapped.RowPitch,width*4,f.color.bytes.data()+(size_t)y*width*4);context->Unmap(colorReadback.Get(),0);
		check(context->Map(diagnosticReadback.Get(),0,D3D11_MAP_READ,0,&mapped),"Geometry map");
		for(unsigned y=0;y<height;++y)std::copy_n((PixelGeometry*)((unsigned char*)mapped.pData+(size_t)y*mapped.RowPitch),width,f.geometry.data()+(size_t)y*width);context->Unmap(diagnosticReadback.Get(),0);
		return f;
	}
};
static unsigned surface(const PixelGeometry& p) { return (unsigned)std::lround(p.kind)%8; }
static unsigned objectId(const PixelGeometry& p) { return (unsigned)std::lround(p.kind)/8; }
static unsigned countSides(const Frame& f) { return (unsigned)std::count_if(f.geometry.begin(),f.geometry.end(),[](auto p){return surface(p)==3;}); }
static unsigned outsideFlat(const Frame& f,const Frame& flatFrame) {
	unsigned n=0;for(size_t i=0;i<f.geometry.size();++i)n+=surface(f.geometry[i])!=0 && surface(flatFrame.geometry[i])==0;return n;
}
static unsigned differentPixels(const Frame& a,const Frame& b) {
	unsigned n=0; for(size_t i=0;i<a.geometry.size();++i)n+=std::memcmp(a.color.bytes.data()+i*4,b.color.bytes.data()+i*4,4)!=0;return n;
}
static std::array<float,2> project(PixelGeometry p,float eye) {
	const float d=cameraDistance-p.z;
	return {width*.5f*(1+(p.x-eye)/(d*tanHalfFov*float(width)/height)),height*.5f*(1-p.y/(d*tanHalfFov))};
}
struct StereoResult { unsigned checked=0,matched=0;float maxReprojectionError=0; };
static StereoResult stereo(const Frame& left,const Frame& right) {
	StereoResult result;
	for(unsigned y=2;y<height-2;y+=5)for(unsigned x=2;x<width-2;++x) {
		const PixelGeometry p=left.geometry[(size_t)y*width+x];if(surface(p)!=3 || objectId(p)!=1)continue;
		const auto source=project(p,-ipd*.5f),target=project(p,ipd*.5f);
		result.maxReprojectionError=std::max(result.maxReprojectionError,std::max(std::fabs(source[0]-(x+.5f)),std::fabs(source[1]-(y+.5f))));
		const int tx=(int)std::floor(target[0]),ty=(int)std::floor(target[1]);if(tx<2||tx>=int(width)-2||ty<2||ty>=int(height)-2)continue;
		++result.checked; float nearest=1e6f;
		for(int yy=ty-1;yy<=ty+1;++yy)for(int xx=tx-1;xx<=tx+1;++xx) {
			const auto q=right.geometry[(size_t)yy*width+xx];if(q.kind!=p.kind)continue;
			const float dd=(p.x-q.x)*(p.x-q.x)+(p.y-q.y)*(p.y-q.y)+(p.z-q.z)*(p.z-q.z);nearest=std::min(nearest,dd);
		}
		result.matched+=nearest<.003f*.003f;
	}
	return result;
}
static Image contactSheet(const std::array<Frame,6>& frames) {
	Image image{width*3,height*2,std::vector<unsigned char>((size_t)width*3*height*2*4)};
	for(unsigned tile=0;tile<frames.size();++tile)for(unsigned y=0;y<height;++y)
		std::copy_n(frames[tile].color.bytes.data()+(size_t)y*width*4,width*4,image.bytes.data()+((size_t)(y+(tile/3)*height)*image.w+(tile%3)*width)*4);
	return image;
}
static void require(bool value,const char* text) { if(!value)throw std::runtime_error(text); }
static void nativeColorGate(const Frame& frame,const Image& native) {
	// Independent absolute-color checks prevent a shared channel-order or face
	// relighting bug from passing the relative flat/shell pixel comparison.
	for(const std::array<unsigned,2> point : {std::array<unsigned,2>{115,75},{1000,280},{1000,450}}) {
		const float x=(float(point[0]-panelCrop.x)+.5f)/panelCrop.w*panelWidth-panelWidth*.5f;
		const float y=panelHeight*.5f-(float(point[1]-panelCrop.y)+.5f)/panelCrop.h*panelHeight;
		const auto projected=project({x,y,0,9},0);const unsigned px=(unsigned)projected[0],py=(unsigned)projected[1];
		const unsigned char* a=native.bytes.data()+((size_t)point[1]*native.w+point[0])*4;
		const unsigned char* b=frame.color.bytes.data()+((size_t)py*width+px)*4;
		for(unsigned channel=0;channel<4;++channel) require(std::abs(int(a[channel])-int(b[channel]))<=1,"Native front color changed");
	}
}
static int run(const fs::path& nativePath,const fs::path& output,bool flatControl,bool stereoControl,const fs::path& shader) {
	fs::create_directories(output);const Image native=readImage(nativePath);
	require(native.w==2100 && native.h==1181,"Expected the documented 2100x1181 native statistics HUD export");
	const Image nativePanel=cropImage(native,panelCrop);writeImage(nativePanel,output/L"native-panel-front.png");
	require(nativePanel.bytes==readImage(output/L"native-panel-front.png").bytes,"Native PNG roundtrip changed colors");
	Renderer renderer;renderer.init(native,shader);
	std::ofstream report(output/L"results.json");report<<"{\n  \"driver\":\"WARP\",\n  \"shaderModel\":\"5.0\",\n  \"ipdMeters\":0.064,\n  \"buttonRestMeters\":0.003,\n  \"additionalHoverMeters\":0.030,\n  \"presets\":[\n";
	const Frame frontFlat=renderer.render(0,0,0);
	nativeColorGate(frontFlat,native);
	writeImage(frontFlat.color,output/L"front-flat-control.png");
	require(frontFlat.color.bytes==readImage(output/L"front-flat-control.png").bytes,"Rendered PNG roundtrip changed colors");
	std::array<Frame,6> comparison;
	comparison[0]=renderer.render(0,0,0,true,true);comparison[3]=renderer.render(0,60,0,true,true);
	for(unsigned index=0;index<2;++index) {
		const float t=flatControl?0.f:index==0?.004f:.008f;
		const std::wstring name=index==0?L"4mm":L"8mm";
		const Frame front=renderer.render(t,0,0);
		const unsigned frontDifference=differentPixels(frontFlat,front);
		require(frontDifference==0,"Front-on pixel preservation failed");writeImage(front.color,output/(L"front-"+name+L".png"));
		unsigned totalOutside=0,totalSide=0,stereoChecked=0,stereoMatched=0;float reprojectionError=0;
		for(const float angle : {-60.f,60.f}) {
			const Frame left=renderer.render(t,angle,-ipd*.5f),right=renderer.render(t,angle,stereoControl?-ipd*.5f:ipd*.5f);
			const Frame flatLeft=renderer.render(0,angle,-ipd*.5f),flatRight=renderer.render(0,angle,ipd*.5f);
			const unsigned sides=countSides(left)+countSides(right),outside=outsideFlat(left,flatLeft)+outsideFlat(right,flatRight);
			std::printf("%ls yaw %.0f: sides=%u, silhouette pixels outside flat=%u\n",name.c_str(),angle,sides,outside);std::fflush(stdout);
			require(sides>50,"No visible side depth: flat control must fail here");require(outside>30,"No geometric silhouette extension beyond the flat control");
			const StereoResult both=stereo(left,right);stereoChecked+=both.checked;stereoMatched+=both.matched;reprojectionError=std::max(reprojectionError,both.maxReprojectionError);
			require(both.checked>10 && both.matched>both.checked*.9f,"Stereo world-space correspondence failed");require(both.maxReprojectionError<.002f,"Shader eye transform disagrees with independent projection");
			totalOutside+=outside;totalSide+=sides;
			const std::wstring side=angle<0?L"minus60":L"plus60";
			writeImage(left.color,output/(name+L"-"+side+L"-left.png"));writeImage(right.color,output/(name+L"-"+side+L"-right.png"));
		}
		comparison[index+1]=renderer.render(t,0,0,true,true);comparison[index+4]=renderer.render(t,60,0,true,true);
		report<<"    {\"panelThicknessMeters\":"<<(index==0?.004:.008)<<",\"frontChangedPixels\":"<<frontDifference<<",\"sidePixels\":"<<totalSide<<",\"outsideFlatPixels\":"<<totalOutside<<",\"stereoChecked\":"<<stereoChecked<<",\"stereoMatched\":"<<stereoMatched<<",\"maxReprojectionErrorPixels\":"<<reprojectionError<<"}"<<(index==0?",":"")<<"\n";
	}
	const unsigned raisedPresetDifference=differentPixels(comparison[1],comparison[2]);
	const unsigned raisedBodyDifference=differentPixels(comparison[0],comparison[1]);
	require(raisedPresetDifference==0,"Changing 4mm to 8mm body thickness changed the head-on raised-button scene");
	const Frame resting=renderer.render(.008f,0,0,true,false),hovered=renderer.render(.008f,0,0,true,false,hoverMeters),shadowed=renderer.render(.008f,0,0,true,true);
	unsigned restingPixels=0,hoveredPixels=0;float restMin=1,restMax=-1,hoverMin=1,hoverMax=-1;
	for(const auto p:resting.geometry)if(surface(p)==1 && objectId(p)>1){restMin=std::min(restMin,p.z);restMax=std::max(restMax,p.z);++restingPixels;}
	for(const auto p:hovered.geometry)if(surface(p)==1 && objectId(p)==3){hoverMin=std::min(hoverMin,p.z);hoverMax=std::max(hoverMax,p.z);++hoveredPixels;}
	for(const auto p:hovered.geometry)if(surface(p)==1 && objectId(p)==2)require(std::fabs(p.z-restMeters)<.000001f,"Disabled button lifted on hover");
	require(restingPixels>100 && hoveredPixels>100,"Raised native buttons missing");
	require(std::fabs(restMin-restMeters)<.000001f && std::fabs(restMax-restMeters)<.000001f,"Button rest is not actual 3mm geometry");
	require(std::fabs(hoverMin-restMeters-hoverMeters)<.000001f && std::fabs(hoverMax-restMeters-hoverMeters)<.000001f,"Hover is not actual additional 30mm geometry");
	unsigned maxBoundsDifference=0;
	for(unsigned b=0;b<buttonCrops.size();++b) {
		const Rect r=nativeRect(buttonCrops[b]);const auto tl=project({r.x-r.w*.5f,r.y+r.h*.5f,0,0},0),br=project({r.x+r.w*.5f,r.y-r.h*.5f,0,0},0);
		const std::array<int,4> original{{(int)std::ceil(tl[0]-.5f),(int)std::ceil(tl[1]-.5f),(int)std::ceil(br[0]-.5f)-1,(int)std::ceil(br[1]-.5f)-1}};
		std::array<int,4> actual{{int(width),int(height),-1,-1}};
		for(unsigned y=0;y<height;++y)for(unsigned x=0;x<width;++x) {
			const auto p=resting.geometry[(size_t)y*width+x];if(surface(p)!=1 || objectId(p)!=b+2)continue;
			actual[0]=std::min(actual[0],int(x));actual[1]=std::min(actual[1],int(y));actual[2]=std::max(actual[2],int(x));actual[3]=std::max(actual[3],int(y));
		}
		for(unsigned edge=0;edge<4;++edge)maxBoundsDifference=std::max(maxBoundsDifference,(unsigned)std::abs(actual[edge]-original[edge]));
	}
	require(maxBoundsDifference<=1,"3mm resting elevation changed head-on button bounds by more than one pixel");
	const unsigned shadowChanged=differentPixels(resting,shadowed);require(shadowChanged>20,"Soft contact shadow not visible");
	unsigned shadowOutside=0;std::array<bool,256> shadowValues{};
	for(size_t i=0;i<resting.geometry.size();++i)if(std::memcmp(resting.color.bytes.data()+i*4,shadowed.color.bytes.data()+i*4,4)!=0) {
		const auto p=resting.geometry[i];bool inBounds=false;
		for(const Crop crop:buttonCrops) {const Rect r=nativeRect(crop);inBounds|=std::fabs(p.x-r.x)<r.w*.5f+.007f && std::fabs(p.y-r.y)<r.h*.5f+.007f;}
		shadowOutside+=!inBounds || objectId(p)!=1 || surface(p)!=1;shadowValues[shadowed.color.bytes[i*4+2]]=true;
	}
	require(shadowOutside==0,"Shadow changed pixels outside its receiver mask");
	require(std::count(shadowValues.begin(),shadowValues.end(),true)>2,"Shadow has no soft falloff");
	writeImage(resting.color,output/L"raised-rest-no-shadow.png");writeImage(shadowed.color,output/L"raised-rest-soft-shadow.png");writeImage(hovered.color,output/L"raised-hover-additional-30mm.png");
	writeImage(renderer.render(.008f,60,0,true,true,hoverMeters).color,output/L"raised-hover-oblique-soft-shadow.png");
	writeImage(contactSheet(comparison),output/L"comparison.png");
	report<<"  ],\n  \"nativeColorAndPngRoundtrip\":true,\n  \"headOnRaisedScene4vs8ChangedPixels\":"<<raisedPresetDifference<<",\n  \"headOnRaisedBodiesVsFrontsOnlyChangedPixels\":"<<raisedBodyDifference<<",\n  \"headOnButtonBoundsMaxDeltaPixels\":"<<maxBoundsDifference<<",\n  \"shadowChangedPixels\":"<<shadowChanged<<",\n  \"shadowOutsideReceiverMask\":"<<shadowOutside<<",\n  \"buttonRestZ\":["<<restMin<<","<<restMax<<"],\n  \"enabledButtonHoverZ\":["<<hoverMin<<","<<hoverMax<<"],\n  \"disabledButtonStaysAtRest\":true,\n  \"passed\":true\n}\n";
	std::puts("PASS: exact front-on pixels, real side silhouette, two-eye correspondence, raised native buttons, physical hover, bounded soft shadow.");return 0;
}
int wmain(int argc,wchar_t** argv) {
	try {
		check(CoInitializeEx(nullptr,COINIT_MULTITHREADED),"COM");check(CoCreateInstance(CLSID_WICImagingFactory,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&wic)),"WIC");
		if(argc<3) {std::fputs("Usage: ui_panel_volume_probe <native-statistics-hud.png> <output-dir> [--flat-control|--stereo-control]\nRun from experiments/ui_panel_volume so panel.hlsl resolves.\n",stderr);return 2;}
		const bool flatControl=argc>3 && std::wstring(argv[3])==L"--flat-control",stereoControl=argc>3 && std::wstring(argv[3])==L"--stereo-control";
		return run(argv[1],argv[2],flatControl,stereoControl,L"panel.hlsl");
	} catch(const std::exception& e) {std::fprintf(stderr,"FAIL: %s\n",e.what());return 1;}
}
