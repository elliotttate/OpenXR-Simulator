// Real scene UI: rasterize the menu plane into each projection image. No quad
// composition layer is submitted in this mode, so targeting must use view rays
// or an explicit application-provided surface.
#pragma once
#include <d3dcompiler.h>

struct WorldUiRenderer {
    Microsoft::WRL::ComPtr<ID3D12RootSignature> root;
    Microsoft::WRL::ComPtr<ID3D12PipelineState> pipeline;
    bool Init(ID3D12Device* device) {
        static const char* source=R"HLSL(
cbuffer State : register(b0) { float4 cameraQ; float4 cameraP; float4 tans; float4 state; }
float3 rotate(float4 q,float3 v) { return v+2*cross(q.xyz,cross(q.xyz,v)+q.w*v); }
struct Vertex { float4 p:SV_Position; float2 uv:TEXCOORD0; };
Vertex VS(uint id:SV_VertexID) {
    const float2 uv[6]={float2(0,0),float2(1,0),float2(0,1),float2(0,1),float2(1,0),float2(1,1)};
    Vertex o; o.uv=uv[id];
    float x=(o.uv.x-.5)*1.6,y=(.5-o.uv.y)*.9;
    float3 world=float3(.32+cos(.22)*x,1.52+y,-2-sin(.22)*x);
    float3 v=rotate(float4(-cameraQ.xyz,cameraQ.w),world-cameraP.xyz);
    float w=-v.z;
    o.p=float4((2*v.x+v.z*(tans.y+tans.x))/(tans.y-tans.x),
               (2*v.y+v.z*(tans.z+tans.w))/(tans.z-tans.w),.5*w,w);
    return o;
}
float4 PS(Vertex i):SV_Target {
    if (i.uv.x>=.60 && i.uv.x<=.85 && i.uv.y>=.35 && i.uv.y<=.65)
        return state.y>0 ? float4(.05,.8,.15,1):state.x>0 ? float4(.95,.65,.02,1):float4(.05,.25,.9,1);
    if (state.z>0 && i.uv.x>=.60 && i.uv.x<=.85 && i.uv.y>=.25 && i.uv.y<=.31)
        return float4(.8,.8,.8,1);
    return float4(.025,.03,.04,1);
}
)HLSL";
        Microsoft::WRL::ComPtr<ID3DBlob> vs,ps,error,signature;
        if (FAILED(D3DCompile(source,strlen(source),nullptr,nullptr,nullptr,"VS","vs_5_0",0,0,&vs,&error)) ||
            FAILED(D3DCompile(source,strlen(source),nullptr,nullptr,nullptr,"PS","ps_5_0",0,0,&ps,&error))) {
            if (error) printf("World UI shader: %s\n",(const char*)error->GetBufferPointer());
            return false;
        }
        D3D12_ROOT_PARAMETER param{}; param.ParameterType=D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        param.Constants.Num32BitValues=16; param.ShaderVisibility=D3D12_SHADER_VISIBILITY_ALL;
        D3D12_ROOT_SIGNATURE_DESC desc{}; desc.NumParameters=1; desc.pParameters=&param;
        if (FAILED(D3D12SerializeRootSignature(&desc,D3D_ROOT_SIGNATURE_VERSION_1,&signature,&error)) ||
            FAILED(device->CreateRootSignature(0,signature->GetBufferPointer(),signature->GetBufferSize(),IID_PPV_ARGS(&root)))) return false;
        D3D12_GRAPHICS_PIPELINE_STATE_DESC p{};
        p.pRootSignature=root.Get(); p.VS={vs->GetBufferPointer(),vs->GetBufferSize()}; p.PS={ps->GetBufferPointer(),ps->GetBufferSize()};
        p.BlendState.RenderTarget[0].RenderTargetWriteMask=D3D12_COLOR_WRITE_ENABLE_ALL;
        p.RasterizerState.FillMode=D3D12_FILL_MODE_SOLID; p.RasterizerState.CullMode=D3D12_CULL_MODE_NONE;
        p.RasterizerState.DepthClipEnable=TRUE; p.SampleMask=UINT_MAX;
        p.PrimitiveTopologyType=D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE; p.NumRenderTargets=1;
        p.RTVFormats[0]=DXGI_FORMAT_R8G8B8A8_UNORM_SRGB; p.SampleDesc.Count=1;
        return SUCCEEDED(device->CreateGraphicsPipelineState(&p,IID_PPV_ARGS(&pipeline)));
    }
    void Draw(ID3D12GraphicsCommandList* cmd,D3D12_CPU_DESCRIPTOR_HANDLE rtv,const XrRect2Di& r,const XrView& view,const UiFixture& ui) {
        const auto& q=view.pose.orientation; const auto& v=view.pose.position;
        const float constants[16]{q.x,q.y,q.z,q.w,v.x,v.y,v.z,0,
            tanf(view.fov.angleLeft),tanf(view.fov.angleRight),tanf(view.fov.angleUp),tanf(view.fov.angleDown),
            ui.hovered ? 1.0f:0.0f,ui.clicks ? 1.0f:0.0f,ui.hoverFrames>=8 ? 1.0f:0.0f,0};
        cmd->SetPipelineState(pipeline.Get()); cmd->SetGraphicsRootSignature(root.Get());
        cmd->SetGraphicsRoot32BitConstants(0,16,constants,0); cmd->OMSetRenderTargets(1,&rtv,FALSE,nullptr);
        D3D12_VIEWPORT viewport{(float)r.offset.x,(float)r.offset.y,(float)r.extent.width,(float)r.extent.height,0,1};
        D3D12_RECT scissor{r.offset.x,r.offset.y,r.offset.x+r.extent.width,r.offset.y+r.extent.height};
        cmd->RSSetViewports(1,&viewport); cmd->RSSetScissorRects(1,&scissor);
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST); cmd->DrawInstanced(6,1,0,0);
    }
};
