// GPU timestamps around each frame, on the app's own queue or context: one in
// xrBeginFrame, one in xrEndFrame behind everything the app submitted for the
// frame. Results come back a few frames later, once the GPU has reached them,
// as timeline::GpuRecords. Nothing here ever waits on the GPU.
//
// Each timer keeps a ring of slots. Begin opens the next slot, End closes it
// with the frame's id, Collect hands back closed slots whose timestamps are in.
// If the GPU falls a whole ring behind, frames go untimed rather than stall.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "frame_timeline.h"

#include <d3d11.h>
#include <d3d12.h>
#include <wrl/client.h>

#include <mutex>
#include <vector>

namespace perf::gpu {

using Microsoft::WRL::ComPtr;
using timeline::GpuRecord;

constexpr uint32_t kSlots = 16;

// The slot ring every API shares. Not thread-safe by itself; timers lock.
class SlotRing {
public:
    enum class State : uint8_t { Free, Open, Pending };
    struct Slot {
        State state = State::Free;
        bool begun = false;
        uint64_t frameId = 0;
        uint64_t fence = 0;  // D3D12: the value signalled after the end timestamp
    };

    // The slot for a new frame's begin marker, or -1 when the GPU is a ring behind.
    int OpenForBegin() {
        if (m_open >= 0) return m_open;  // xrBeginFrame twice: the frame restarts
        if (m_slots[m_next].state != State::Free) return -1;
        m_open = int(m_next);
        m_slots[m_next] = Slot{State::Open, true};
        return m_open;
    }

    // The slot an end marker goes in: the open one, or a fresh one without a begin.
    int OpenForEnd() {
        if (m_open >= 0) return m_open;
        if (m_slots[m_next].state != State::Free) return -1;
        m_slots[m_next] = Slot{State::Open, false};
        return int(m_next);
    }

    void Close(int slot, uint64_t frameId, uint64_t fence = 0) {
        Slot& s = m_slots[size_t(slot)];
        s.state = State::Pending;
        s.frameId = frameId;
        s.fence = fence;
        m_open = -1;
        m_next = (uint32_t(slot) + 1) % kSlots;
    }

    // The oldest pending slot, or -1.
    int Oldest() const {
        for (uint32_t k = 0; k < kSlots; ++k) {
            const uint32_t i = (m_next + k) % kSlots;
            if (m_slots[i].state == State::Pending) return int(i);
        }
        return -1;
    }

    void Free(int slot) { m_slots[size_t(slot)] = Slot{}; }
    const Slot& operator[](int slot) const { return m_slots[size_t(slot)]; }

private:
    Slot m_slots[kSlots];
    uint32_t m_next = 0;
    int m_open = -1;
};

// Maps GPU ticks onto QPC from a (gpu, cpu) pair sampled at the same moment.
struct Calibration {
    bool valid = false;
    uint64_t gpuTicks = 0, cpuQpc = 0, tickFrequency = 1, qpcFrequency = 1;

    uint64_t ToQpc(uint64_t ticks) const {
        const double delta = (double(ticks) - double(gpuTicks)) * double(qpcFrequency) / double(tickFrequency);
        const double qpc = double(cpuQpc) + delta;
        return qpc > 0.0 ? uint64_t(qpc) : 0;
    }
};

inline uint64_t QpcFrequency() {
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    return uint64_t(f.QuadPart);
}

inline GpuRecord MakeRecord(timeline::GpuApi api, uint64_t frameId, uint64_t beginTicks, uint64_t endTicks,
                            uint64_t tickFrequency, const Calibration& calibration) {
    GpuRecord r{};
    r.frameId = frameId;
    r.beginTicks = beginTicks;
    r.endTicks = endTicks;
    r.tickFrequency = tickFrequency;
    r.api = uint32_t(api);
    if (calibration.valid) {
        r.flags |= timeline::kGpuCalibrated;
        r.beginQpc = beginTicks ? calibration.ToQpc(beginTicks) : 0;
        r.endQpc = calibration.ToQpc(endTicks);
    }
    return r;
}

// D3D12: tiny pre-recorded command lists on the app's queue write a timestamp
// and resolve it into a persistently mapped readback buffer; a fence says when
// the end one has landed. The queue calibrates its clock against QPC directly.
class D3D12Timer {
public:
    ~D3D12Timer() { Shutdown(false); }  // at DLL unload: never wait under the loader lock

    bool Init(ID3D12Device* device, ID3D12CommandQueue* queue) {
        Shutdown();
        if (!device || !queue) return false;
        const D3D12_COMMAND_QUEUE_DESC desc = queue->GetDesc();
        if (desc.Type != D3D12_COMMAND_LIST_TYPE_DIRECT && desc.Type != D3D12_COMMAND_LIST_TYPE_COMPUTE) return false;
        if (FAILED(queue->GetTimestampFrequency(&m_tickFrequency)) || !m_tickFrequency) return false;

        D3D12_QUERY_HEAP_DESC heap{D3D12_QUERY_HEAP_TYPE_TIMESTAMP, kSlots * 2, 0};
        D3D12_HEAP_PROPERTIES readback{D3D12_HEAP_TYPE_READBACK};
        D3D12_RESOURCE_DESC buffer{};
        buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        buffer.Width = kSlots * 2 * sizeof(uint64_t);
        buffer.Height = buffer.DepthOrArraySize = buffer.MipLevels = 1;
        buffer.SampleDesc.Count = 1;
        buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(device->CreateQueryHeap(&heap, IID_PPV_ARGS(&m_heap))) ||
            FAILED(device->CreateCommittedResource(&readback, D3D12_HEAP_FLAG_NONE, &buffer,
                                                   D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&m_readback))) ||
            FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&m_fence))) ||
            FAILED(device->CreateCommandAllocator(desc.Type, IID_PPV_ARGS(&m_allocator)))) {
            Shutdown();
            return false;
        }
        for (uint32_t q = 0; q < kSlots * 2; ++q) {
            ComPtr<ID3D12GraphicsCommandList> list;
            if (FAILED(device->CreateCommandList(0, desc.Type, m_allocator.Get(), nullptr, IID_PPV_ARGS(&list)))) {
                Shutdown();
                return false;
            }
            list->EndQuery(m_heap.Get(), D3D12_QUERY_TYPE_TIMESTAMP, q);
            list->ResolveQueryData(m_heap.Get(), D3D12_QUERY_TYPE_TIMESTAMP, q, 1, m_readback.Get(), q * sizeof(uint64_t));
            if (FAILED(list->Close())) {
                Shutdown();
                return false;
            }
            m_lists[q] = list;
        }
        const D3D12_RANGE all{0, SIZE_T(buffer.Width)};
        void* mapped = nullptr;
        if (FAILED(m_readback->Map(0, &all, &mapped))) {
            Shutdown();
            return false;
        }
        m_ticks = static_cast<const uint64_t*>(mapped);
        m_queue = queue;
        m_qpcFrequency = QpcFrequency();
        Calibrate();
        return true;
    }

    bool Active() const { return m_queue != nullptr; }

    void Begin() {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (!m_queue) return;
        const int slot = m_slots.OpenForBegin();
        if (slot < 0) return;
        ID3D12CommandList* lists[] = {m_lists[size_t(slot) * 2].Get()};
        m_queue->ExecuteCommandLists(1, lists);
    }

    void End(uint64_t frameId) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (!m_queue) return;
        const int slot = m_slots.OpenForEnd();
        if (slot < 0) return;
        ID3D12CommandList* lists[] = {m_lists[size_t(slot) * 2 + 1].Get()};
        m_queue->ExecuteCommandLists(1, lists);
        m_queue->Signal(m_fence.Get(), ++m_fenceValue);
        m_slots.Close(slot, frameId, m_fenceValue);
    }

    void Collect(std::vector<GpuRecord>& out) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (!m_queue) return;
        const uint64_t completed = m_fence->GetCompletedValue();
        for (int slot = m_slots.Oldest(); slot >= 0 && m_slots[slot].fence <= completed; slot = m_slots.Oldest()) {
            const auto& s = m_slots[slot];
            const uint64_t begin = s.begun ? m_ticks[size_t(slot) * 2] : 0;
            out.push_back(MakeRecord(timeline::GpuApi::D3D12, s.frameId, begin, m_ticks[size_t(slot) * 2 + 1],
                                     m_tickFrequency, m_calibration));
            m_slots.Free(slot);
        }
        if (timeline::Qpc() - m_calibration.cpuQpc > m_qpcFrequency) Calibrate();  // about once a second
    }

    // `wait`: give work still on the GPU up to half a second to finish. Anything
    // still in flight after that is leaked rather than released under the GPU.
    void Shutdown(bool wait = true) {
        std::lock_guard<std::mutex> lock(m_mutex);
        bool inFlight = m_queue && m_fence && m_fence->GetCompletedValue() < m_fenceValue;
        if (inFlight && wait) {
            HANDLE done = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            if (done && SUCCEEDED(m_fence->SetEventOnCompletion(m_fenceValue, done))) WaitForSingleObject(done, 500);
            if (done) CloseHandle(done);
            inFlight = m_fence->GetCompletedValue() < m_fenceValue;
        }
        if (inFlight) {
            for (auto& list : m_lists) list.Detach();
            m_allocator.Detach();
            m_fence.Detach();
            m_readback.Detach();
            m_heap.Detach();
        } else if (m_readback && m_ticks) {
            m_readback->Unmap(0, nullptr);
        }
        m_ticks = nullptr;
        for (auto& list : m_lists) list.Reset();
        m_allocator.Reset();
        m_fence.Reset();
        m_readback.Reset();
        m_heap.Reset();
        m_queue = nullptr;
        m_slots = SlotRing{};
        m_fenceValue = 0;
    }

private:
    void Calibrate() {
        UINT64 gpu = 0, cpu = 0;
        if (SUCCEEDED(m_queue->GetClockCalibration(&gpu, &cpu))) {
            m_calibration = Calibration{true, gpu, cpu, m_tickFrequency, m_qpcFrequency};
        }
    }

    std::mutex m_mutex;
    ID3D12CommandQueue* m_queue = nullptr;  // the app's; it outlives the session
    ComPtr<ID3D12QueryHeap> m_heap;
    ComPtr<ID3D12Resource> m_readback;
    ComPtr<ID3D12Fence> m_fence;
    ComPtr<ID3D12CommandAllocator> m_allocator;
    ComPtr<ID3D12GraphicsCommandList> m_lists[kSlots * 2];
    const uint64_t* m_ticks = nullptr;
    uint64_t m_fenceValue = 0;
    uint64_t m_tickFrequency = 0, m_qpcFrequency = 1;
    Calibration m_calibration;
    SlotRing m_slots;
};

// D3D11: timestamp queries in a disjoint bracket on the app's immediate
// context, read back without flushing. D3D11 cannot relate its GPU clock to
// QPC, so these give GPU durations only.
class D3D11Timer {
public:
    bool Init(ID3D11Device* device, ID3D11DeviceContext* context) {
        *this = D3D11Timer{};
        if (!device || !context) return false;
        D3D11_QUERY_DESC disjoint{D3D11_QUERY_TIMESTAMP_DISJOINT, 0};
        D3D11_QUERY_DESC stamp{D3D11_QUERY_TIMESTAMP, 0};
        for (uint32_t s = 0; s < kSlots; ++s) {
            if (FAILED(device->CreateQuery(&disjoint, &m_disjoint[s])) || FAILED(device->CreateQuery(&stamp, &m_begin[s])) ||
                FAILED(device->CreateQuery(&stamp, &m_end[s]))) {
                *this = D3D11Timer{};
                return false;
            }
        }
        m_context = context;
        return true;
    }

    bool Active() const { return m_context != nullptr; }

    void Begin() {
        if (!m_context) return;
        const int slot = m_slots.OpenForBegin();
        if (slot < 0) return;
        m_context->Begin(m_disjoint[slot].Get());
        m_context->End(m_begin[slot].Get());
    }

    // A frame with no xrBeginFrame has no disjoint bracket, so it goes untimed.
    void End(uint64_t frameId) {
        if (!m_context) return;
        const int slot = m_slots.OpenForEnd();
        if (slot < 0) return;
        if (!m_slots[slot].begun) {
            m_slots.Free(slot);
            return;
        }
        m_context->End(m_end[slot].Get());
        m_context->End(m_disjoint[slot].Get());
        m_slots.Close(slot, frameId);
    }

    void Collect(std::vector<GpuRecord>& out) {
        if (!m_context) return;
        for (int slot = m_slots.Oldest(); slot >= 0; slot = m_slots.Oldest()) {
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint{};
            UINT64 begin = 0, end = 0;
            if (m_context->GetData(m_disjoint[slot].Get(), &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK ||
                m_context->GetData(m_begin[slot].Get(), &begin, sizeof(begin), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK ||
                m_context->GetData(m_end[slot].Get(), &end, sizeof(end), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                break;  // not there yet
            }
            if (!disjoint.Disjoint && disjoint.Frequency) {  // a clock change voids the pair
                out.push_back(MakeRecord(timeline::GpuApi::D3D11, m_slots[slot].frameId, begin, end, disjoint.Frequency,
                                         Calibration{}));
            }
            m_slots.Free(slot);
        }
    }

private:
    ID3D11DeviceContext* m_context = nullptr;  // the app's immediate context
    ComPtr<ID3D11Query> m_disjoint[kSlots], m_begin[kSlots], m_end[kSlots];
    SlotRing m_slots;
};

// OpenGL: GL_TIMESTAMP query counters in the app's context, which has to be
// current on the calling thread. GL_TIMESTAMP also reads the GPU clock
// directly, which calibrates it against QPC.
class GlTimer {
public:
    bool Init() {
        *this = GlTimer{};
        if (!wglGetCurrentContext()) return false;
        m_genQueries = reinterpret_cast<GenQueries>(wglGetProcAddress("glGenQueries"));
        m_queryCounter = reinterpret_cast<QueryCounter>(wglGetProcAddress("glQueryCounter"));
        m_getQueryObjectiv = reinterpret_cast<GetQueryObjectiv>(wglGetProcAddress("glGetQueryObjectiv"));
        m_getQueryObjectui64v = reinterpret_cast<GetQueryObjectui64v>(wglGetProcAddress("glGetQueryObjectui64v"));
        m_getInteger64v = reinterpret_cast<GetInteger64v>(wglGetProcAddress("glGetInteger64v"));
        if (!m_genQueries || !m_queryCounter || !m_getQueryObjectiv || !m_getQueryObjectui64v) return false;
        m_genQueries(kSlots * 2, m_queries);
        m_qpcFrequency = QpcFrequency();
        m_ready = true;
        Calibrate();
        return true;
    }

    bool Active() const { return m_ready; }

    void Begin() {
        if (!m_ready) return;
        const int slot = m_slots.OpenForBegin();
        if (slot >= 0) m_queryCounter(m_queries[slot * 2], kTimestamp);
    }

    void End(uint64_t frameId) {
        if (!m_ready) return;
        const int slot = m_slots.OpenForEnd();
        if (slot < 0) return;
        m_queryCounter(m_queries[slot * 2 + 1], kTimestamp);
        m_slots.Close(slot, frameId);
    }

    void Collect(std::vector<GpuRecord>& out) {
        if (!m_ready) return;
        for (int slot = m_slots.Oldest(); slot >= 0; slot = m_slots.Oldest()) {
            int available = 0;
            m_getQueryObjectiv(m_queries[slot * 2 + 1], kResultAvailable, &available);
            if (!available) break;
            uint64_t begin = 0, end = 0;
            if (m_slots[slot].begun) m_getQueryObjectui64v(m_queries[slot * 2], kResult, &begin);
            m_getQueryObjectui64v(m_queries[slot * 2 + 1], kResult, &end);
            out.push_back(MakeRecord(timeline::GpuApi::OpenGL, m_slots[slot].frameId, begin, end, 1000000000ull, m_calibration));
            m_slots.Free(slot);
        }
        if (timeline::Qpc() - m_calibration.cpuQpc > m_qpcFrequency) Calibrate();
    }

private:
    static constexpr unsigned kTimestamp = 0x8E28, kResult = 0x8866, kResultAvailable = 0x8867;
    using GenQueries = void(APIENTRY*)(int, unsigned*);
    using QueryCounter = void(APIENTRY*)(unsigned, unsigned);
    using GetQueryObjectiv = void(APIENTRY*)(unsigned, unsigned, int*);
    using GetQueryObjectui64v = void(APIENTRY*)(unsigned, unsigned, uint64_t*);
    using GetInteger64v = void(APIENTRY*)(unsigned, int64_t*);

    void Calibrate() {
        if (!m_getInteger64v) return;
        int64_t gpu = 0;
        const uint64_t before = timeline::Qpc();
        m_getInteger64v(kTimestamp, &gpu);
        const uint64_t after = timeline::Qpc();
        if (gpu > 0) m_calibration = Calibration{true, uint64_t(gpu), before + (after - before) / 2, 1000000000ull, m_qpcFrequency};
    }

    bool m_ready = false;
    unsigned m_queries[kSlots * 2] = {};
    GenQueries m_genQueries = nullptr;
    QueryCounter m_queryCounter = nullptr;
    GetQueryObjectiv m_getQueryObjectiv = nullptr;
    GetQueryObjectui64v m_getQueryObjectui64v = nullptr;
    GetInteger64v m_getInteger64v = nullptr;
    uint64_t m_qpcFrequency = 1;
    Calibration m_calibration;
    SlotRing m_slots;
};

} // namespace perf::gpu
