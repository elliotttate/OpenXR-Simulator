#include "gpu_frame_timer.h"

// These checks must also run in the Release configuration used for the DLL.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
#include <vector>

namespace {

using namespace perf;
using Microsoft::WRL::ComPtr;

void SlotRingRules() {
    gpu::SlotRing ring;
    // Plain frames: begin, end, collect.
    const int a = ring.OpenForBegin();
    assert(a == 0 && ring.OpenForBegin() == 0);  // a second xrBeginFrame restarts the same slot
    assert(ring.OpenForEnd() == 0);
    ring.Close(a, 7);
    assert(ring.Oldest() == 0 && ring[0].frameId == 7 && ring[0].begun);
    ring.Free(0);
    assert(ring.Oldest() == -1);
    // A frame with no begin still gets an end slot.
    const int b = ring.OpenForEnd();
    assert(b == 1 && !ring[b].begun);
    ring.Close(b, 8);
    // A GPU a whole ring behind: new frames go untimed instead of waiting.
    for (uint64_t f = 9; f < 9 + gpu::kSlots - 1; ++f) ring.Close(ring.OpenForBegin(), f);
    assert(ring.OpenForBegin() == -1 && ring.OpenForEnd() == -1);
    assert(ring[ring.Oldest()].frameId == 8);  // oldest first
}

// Repeated large buffer copies: real GPU work of a size that scales.
struct CopyLoad {
    static constexpr UINT64 kBytes = 64ull << 20;
    ComPtr<ID3D12Resource> a, b;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;

    bool Init(ID3D12Device* device) {
        D3D12_HEAP_PROPERTIES heap{D3D12_HEAP_TYPE_DEFAULT};
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = kBytes;
        desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        return SUCCEEDED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COMMON,
                                                         nullptr, IID_PPV_ARGS(&a))) &&
               SUCCEEDED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COMMON,
                                                         nullptr, IID_PPV_ARGS(&b))) &&
               SUCCEEDED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator))) &&
               SUCCEEDED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr,
                                                   IID_PPV_ARGS(&list))) &&
               SUCCEEDED(list->Close());
    }

    void Submit(ID3D12CommandQueue* queue, int copies) {
        allocator->Reset();
        list->Reset(allocator.Get(), nullptr);
        for (int i = 0; i < copies; ++i) list->CopyBufferRegion(i % 2 ? a.Get() : b.Get(), 0, i % 2 ? b.Get() : a.Get(), 0, kBytes);
        list->Close();
        ID3D12CommandList* lists[] = {list.Get()};
        queue->ExecuteCommandLists(1, lists);
    }
};

void WaitIdle(ID3D12Device* device, ID3D12CommandQueue* queue) {
    ComPtr<ID3D12Fence> fence;
    device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence));
    HANDLE done = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    queue->Signal(fence.Get(), 1);
    fence->SetEventOnCompletion(1, done);
    WaitForSingleObject(done, 10000);
    CloseHandle(done);
}

double Ms(const timeline::GpuRecord& r) { return double(r.endTicks - r.beginTicks) * 1000.0 / double(r.tickFrequency); }

void D3D12TimesRealWork() {
    ComPtr<ID3D12Device> device;
    if (FAILED(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) {
        std::cout << "  no D3D12 device here: skipped\n";
        return;
    }
    D3D12_COMMAND_QUEUE_DESC qd{D3D12_COMMAND_LIST_TYPE_DIRECT};
    ComPtr<ID3D12CommandQueue> queue;
    assert(SUCCEEDED(device->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue))));
    CopyLoad load;
    assert(load.Init(device.Get()));
    gpu::D3D12Timer timer;
    assert(timer.Init(device.Get(), queue.Get()));

    auto frame = [&](uint64_t id, int copies) {
        const uint64_t before = timeline::Qpc();
        timer.Begin();
        load.Submit(queue.Get(), copies);
        timer.End(id);
        WaitIdle(device.Get(), queue.Get());
        const uint64_t after = timeline::Qpc();
        std::vector<timeline::GpuRecord> out;
        timer.Collect(out);
        assert(out.size() == 1 && out[0].frameId == id && out[0].api == uint32_t(timeline::GpuApi::D3D12));
        const timeline::GpuRecord r = out[0];
        assert(r.beginTicks && r.endTicks > r.beginTicks);
        // Calibrated onto QPC: both moments fall inside the CPU window the frame ran in.
        const uint64_t slack = gpu::QpcFrequency() / 1000;  // 1 ms
        assert(r.flags & timeline::kGpuCalibrated);
        assert(r.beginQpc + slack >= before && r.endQpc <= after + slack && r.beginQpc <= r.endQpc);
        return r;
    };
    frame(1, 0);  // warm-up
    const double idle = Ms(frame(2, 0));
    const double busy = Ms(frame(3, 24));
    std::cout << "  D3D12: empty frame " << idle << " ms, 24 x 64 MB copies " << busy << " ms on the GPU\n";
    assert(busy > idle + 0.5);

    // A ring of frames ends untimed rather than blocking when nothing collects.
    for (uint64_t f = 10; f < 10 + 2 * gpu::kSlots; ++f) {
        timer.Begin();
        timer.End(f);
    }
    WaitIdle(device.Get(), queue.Get());
    std::vector<timeline::GpuRecord> out;
    timer.Collect(out);
    assert(out.size() == gpu::kSlots && out.front().frameId == 10);
    timer.Shutdown();
}

void D3D11TimesRealWork() {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &device,
                                 nullptr, &context))) {
        std::cout << "  no D3D11 device here: skipped\n";
        return;
    }
    D3D11_BUFFER_DESC desc{};
    desc.ByteWidth = 64u << 20;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Buffer> a, b;
    assert(SUCCEEDED(device->CreateBuffer(&desc, nullptr, &a)) && SUCCEEDED(device->CreateBuffer(&desc, nullptr, &b)));
    gpu::D3D11Timer timer;
    assert(timer.Init(device.Get(), context.Get()));

    auto frame = [&](uint64_t id, int copies) {
        timer.Begin();
        for (int i = 0; i < copies; ++i) context->CopyResource(i % 2 ? a.Get() : b.Get(), i % 2 ? b.Get() : a.Get());
        timer.End(id);
        context->Flush();
        std::vector<timeline::GpuRecord> out;
        for (int tries = 0; tries < 2000 && out.empty(); ++tries) {
            timer.Collect(out);
            if (out.empty()) Sleep(1);
        }
        assert(out.size() == 1 && out[0].frameId == id);
        assert(out[0].endTicks >= out[0].beginTicks && !(out[0].flags & timeline::kGpuCalibrated));
        return out[0];
    };
    frame(1, 0);
    const double idle = Ms(frame(2, 0));
    const double busy = Ms(frame(3, 24));
    std::cout << "  D3D11: empty frame " << idle << " ms, 24 x 64 MB copies " << busy << " ms on the GPU\n";
    assert(busy > idle + 0.5);
}

} // namespace

int main() {
    SlotRingRules();
    D3D12TimesRealWork();
    D3D11TimesRealWork();
    std::cout << "gpu frame timer tests passed\n";
    return 0;
}
