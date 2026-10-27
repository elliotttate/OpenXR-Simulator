#pragma once
#include <windows.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>

namespace preview_lifecycle {

// The owning UI thread publishes creation completion even when creation fails or
// stop was requested first. Reset is permitted only before start/after join.
class State {
public:
    void Reset() {
        std::lock_guard<std::mutex> lock(mutex_);
        creationComplete_ = false;
        stopRequested_.store(false);
        ownedWindow_.store(nullptr);
        ownerThreadId_.store(0);
    }
    void OnThreadStarted() {
        MSG message{};
        PeekMessageW(&message, nullptr, WM_USER, WM_USER, PM_NOREMOVE);
        ownerThreadId_.store(GetCurrentThreadId());
    }
    void SetOwnedWindow(HWND window) { ownedWindow_.store(window); }
    void PublishCreationComplete() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            creationComplete_ = true;
        }
        ready_.notify_all();
    }
    bool WaitForCreation(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        return ready_.wait_for(lock, timeout, [this] { return creationComplete_; });
    }
    bool StopRequested() const { return stopRequested_.load(); }
    DWORD RequestClose() {
        stopRequested_.store(true);
        {
            std::unique_lock<std::mutex> lock(mutex_);
            ready_.wait(lock, [this] { return creationComplete_; });
        }
        const HWND window = ownedWindow_.load();
        if (!window) return ERROR_SUCCESS;
        DWORD process = 0;
        if (GetWindowThreadProcessId(window, &process) != ownerThreadId_.load() ||
            process != GetCurrentProcessId()) return ERROR_INVALID_WINDOW_HANDLE;
        if (PostMessageW(window, WM_CLOSE, 0, 0)) return ERROR_SUCCESS;
        const DWORD error = GetLastError();
        return error ? error : ERROR_GEN_FAILURE;
    }
    bool OnWindowDestroyed(HWND window) {
        if (!window || ownedWindow_.load() != window ||
            ownerThreadId_.load() != GetCurrentThreadId()) return false;
        stopRequested_.store(true);
        ownedWindow_.store(nullptr);
        // This is our dedicated UI thread, never the host's message queue.
        PostQuitMessage(0);
        return true;
    }
    DWORD PumpUntilStopped() {
        MSG message{};
        while (!StopRequested()) {
            const BOOL result = GetMessageW(&message, nullptr, 0, 0);
            if (result == 0) return ERROR_SUCCESS;
            if (result == -1) {
                const DWORD error = GetLastError();
                return error ? error : ERROR_GEN_FAILURE;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        return ERROR_SUCCESS;
    }
private:
    std::atomic<bool> stopRequested_{false};
    std::atomic<HWND> ownedWindow_{nullptr};
    std::atomic<DWORD> ownerThreadId_{0};
    std::mutex mutex_;
    std::condition_variable ready_;
    bool creationComplete_ = false;
};
}
