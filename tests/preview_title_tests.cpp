#include "preview_title.h"
#include <atomic>
#include <iostream>
#include <thread>

namespace {
preview_title::Mailbox titles;
constexpr UINT kBlock=WM_APP+0x352;
HANDLE ready,blocked,releaseBlock,applied;
std::atomic<HWND> window{nullptr};
std::atomic<unsigned> applyCount{0};
LRESULT CALLBACK WindowProc(HWND hwnd,UINT message,WPARAM wParam,LPARAM lParam) {
    if (message==kBlock) {
        SetEvent(blocked);
        WaitForSingleObject(releaseBlock,5000);
        return 0;
    }
    if (message==preview_title::kApplyMessage) {
        if (titles.Apply(hwnd)) {++applyCount;SetEvent(applied);}
        return 0;
    }
    if (message==WM_CLOSE) {DestroyWindow(hwnd);return 0;}
    if (message==WM_DESTROY) {titles.Forget(hwnd);PostQuitMessage(0);return 0;}
    return DefWindowProcW(hwnd,message,wParam,lParam);
}
}

int main() {
    ready=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    blocked=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    releaseBlock=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    applied=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    std::thread uiThread([] {
        WNDCLASSW type{};type.lpfnWndProc=WindowProc;
        type.hInstance=GetModuleHandleW(nullptr);type.lpszClassName=L"SimXRTitleRegression";
        RegisterClassW(&type);
        HWND hwnd=CreateWindowExW(0,type.lpszClassName,L"initial",0,0,0,100,100,nullptr,nullptr,type.hInstance,nullptr);
        window=hwnd;SetEvent(ready);
        MSG message;
        while (hwnd && GetMessageW(&message,nullptr,0,0)>0) {
            TranslateMessage(&message);DispatchMessageW(&message);
        }
        UnregisterClassW(type.lpszClassName,type.hInstance);
    });
    bool ok=WaitForSingleObject(ready,3000)==WAIT_OBJECT_0 && window.load()!=nullptr;
    HWND hwnd=window.load();
    if (ok) {
        PostMessageW(hwnd,kBlock,0,0);
        ok=WaitForSingleObject(blocked,3000)==WAIT_OBJECT_0;
    }
    // A real synchronous WM_SETTEXT is a negative control: the window thread
    // is blocked, while caption publication must continue and coalesce.
    HANDLE synchronousDone=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    HANDLE publicationDone=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    std::thread synchronous;
    std::thread producer;
    if (ok) {
        synchronous=std::thread([&] {SetWindowTextW(hwnd,L"synchronous control");SetEvent(synchronousDone);});
        producer=std::thread([&] {
            for (unsigned i=0;i<1000;++i) titles.Publish(hwnd,L"frame "+std::to_wstring(i));
            titles.Publish(hwnd,L"latest completed frame");
            SetEvent(publicationDone);
        });
        const bool nonblocking=WaitForSingleObject(publicationDone,1000)==WAIT_OBJECT_0;
        const bool controlBlocked=WaitForSingleObject(synchronousDone,0)==WAIT_TIMEOUT;
        ok=nonblocking && controlBlocked;
        if (!ok) std::cerr<<"Caption publishing waited for the blocked UI thread\n";
    }
    SetEvent(releaseBlock);
    if (producer.joinable()) producer.join();
    if (synchronous.joinable()) synchronous.join();
    if (ok) {
        ok=WaitForSingleObject(applied,3000)==WAIT_OBJECT_0;
        wchar_t text[128]{};GetWindowTextW(hwnd,text,128);
        ok=ok && std::wstring(text)==L"latest completed frame" && applyCount.load()==1;
        if (!ok) std::cerr<<"Latest caption did not replace the obsolete backlog\n";
        ResetEvent(applied);
        titles.Forget(hwnd);
        titles.Publish(hwnd,L"new session caption");
        ok=ok && WaitForSingleObject(applied,3000)==WAIT_OBJECT_0;
        GetWindowTextW(hwnd,text,128);
        ok=ok && std::wstring(text)==L"new session caption";
    }
    if (hwnd) PostMessageW(hwnd,WM_CLOSE,0,0);
    uiThread.join();
    for (HANDLE event:{ready,blocked,releaseBlock,applied,synchronousDone,publicationDone})CloseHandle(event);
    if (ok) std::cout<<"Blocked-window negative control, nonblocking coalescing and new-session caption passed\n";
    return ok ? 0 : 1;
}
