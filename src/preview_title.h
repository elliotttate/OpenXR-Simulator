#pragma once
#include <windows.h>
#include <mutex>
#include <string>

namespace preview_title {
constexpr UINT kApplyMessage=WM_APP+0x351;

// A caption is diagnostic state, not a render dependency. One queued UI message
// carries the latest value; a busy window thread cannot block xrEndFrame.
class Mailbox {
public:
    bool Publish(HWND window,const std::wstring& title) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (window_!=window) {
            window_=window; pending_=false; latest_.clear(); applied_.clear();
        }
        if (!window) return false;
        if (!pending_ && title==applied_) return true;
        latest_=title;
        if (pending_) return true;
        pending_=PostMessageW(window,kApplyMessage,0,0)!=FALSE;
        return pending_;
    }

    bool Apply(HWND window) {
        std::wstring title;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (window_!=window || !pending_) return false;
            title=latest_; applied_=title; pending_=false;
        }
        // Only the window's own message handler calls this. In particular, do
        // not hold the mailbox lock across WM_SETTEXT/accessibility callbacks.
        SetWindowTextW(window,title.c_str());
        return true;
    }

    void Forget(HWND window) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (window_!=window) return;
        window_=nullptr; pending_=false; latest_.clear(); applied_.clear();
    }

private:
    std::mutex mutex_;
    HWND window_=nullptr;
    bool pending_=false;
    std::wstring latest_,applied_;
};
} // namespace preview_title
