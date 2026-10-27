#pragma once
#include <condition_variable>
#include <functional>
#include <mutex>
#include <optional>
#include <thread>
#include <utility>

namespace perf {
// One in-flight callback and one replaceable pending value. Start/Stop belong
// to the runtime lifecycle owner; Submit may race Stop. No callback holds mutex_.
template<class T> class LatestValueWorker {
public:
    ~LatestValueWorker() { Stop(); }
    void Start(std::function<void(const T&)> sink) {
        Stop();
        sink_ = std::move(sink);
        { std::lock_guard<std::mutex> lock(mutex_); stopping_ = false; active_ = true; }
        try { thread_ = std::thread([this] { Run(); }); }
        catch (...) { std::lock_guard<std::mutex> lock(mutex_); active_ = false; throw; }
    }
    bool Submit(T value) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!active_ || stopping_) return false;
            pending_ = std::move(value);
        }
        wake_.notify_one();
        return true;
    }
    void Stop() {
        { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
        wake_.notify_one();
        if (thread_.joinable()) thread_.join();
        { std::lock_guard<std::mutex> lock(mutex_); active_ = false; pending_.reset(); }
    }
private:
    void Run() {
        for (;;) {
            std::optional<T> value;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                wake_.wait(lock, [this] { return stopping_ || pending_.has_value(); });
                if (!pending_) return;
                value = std::move(pending_); pending_.reset();
            }
            sink_(*value);
        }
    }
    std::mutex mutex_;
    std::condition_variable wake_;
    std::optional<T> pending_;
    std::function<void(const T&)> sink_;
    std::thread thread_;
    bool stopping_ = true, active_ = false;
};
}
