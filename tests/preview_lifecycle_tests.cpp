#include "preview_lifecycle.h"
#include <iostream>

namespace {
constexpr wchar_t kClass[] = L"SimXRHiddenLifecycleFixture";
constexpr UINT kNestedLoop = WM_APP + 1;
constexpr UINT kNativeMenu = WM_APP + 2;
struct Context {
    preview_lifecycle::State state;
    HANDLE createAllowed = CreateEventW(nullptr, TRUE, TRUE, nullptr);
    HANDLE exited = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE nested = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE consumedQuit = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE menuEntered = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HWND window = nullptr;
    DWORD threadId = 0;
    bool failCreation = false;
    std::atomic<bool> creationAttempted{false};
    std::atomic<DWORD> error{ERROR_SUCCESS};
};
int checks = 0, failures = 0;
bool Check(bool pass, const char* name) {
    ++checks;
    if (!pass) { ++failures; std::cerr << "FAIL: " << name << '\n'; }
    return pass;
}
bool Wait(HANDLE event) { return WaitForSingleObject(event, 2000) == WAIT_OBJECT_0; }
LRESULT CALLBACK WindowProcedure(HWND window, UINT message, WPARAM w, LPARAM l) {
    Context* context = reinterpret_cast<Context*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        context = static_cast<Context*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(context));
    }
    if (!context) return DefWindowProcW(window, message, w, l);
    if (message == WM_CLOSE) { DestroyWindow(window); return 0; }
    if (message == WM_DESTROY) { context->state.OnWindowDestroyed(window); return 0; }
    if (message == WM_ENTERMENULOOP) SetEvent(context->menuEntered);
    if (message == kNativeMenu) {
        HMENU menu = CreatePopupMenu();
        if (!menu || !AppendMenuW(menu, MF_STRING, 1, L"Lifecycle regression")) {
            context->error.store(GetLastError());
            if (menu) DestroyMenu(menu);
            return 0;
        }
        // Exercise Windows' actual modal menu pump as well as the synthetic
        // swallowed-Quit negative control. No user input is required.
        TrackPopupMenu(menu, TPM_RETURNCMD, 100, 100, 0, window, nullptr);
        DestroyMenu(menu);
        return 0;
    }
    if (message == kNestedLoop) {
        SetEvent(context->nested);
        // A real nested GetMessage/Dispatch loop deliberately consumes thread Quit.
        // It does not model a particular menu/overlay implementation.
        MSG msg{};
        while (!context->state.StopRequested()) {
            const BOOL result = GetMessageW(&msg, nullptr, 0, 0);
            if (result == 0) { SetEvent(context->consumedQuit); continue; }
            if (result == -1) { context->error.store(GetLastError()); break; }
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        return 0;
    }
    return DefWindowProcW(window, message, w, l);
}
DWORD WINAPI Worker(void* data) {
    Context& c = *static_cast<Context*>(data);
    c.state.OnThreadStarted();
    WaitForSingleObject(c.createAllowed, INFINITE);
    if (c.state.StopRequested()) {
        c.state.PublishCreationComplete(); SetEvent(c.exited); return 0;
    }
    c.creationAttempted.store(true);
    c.window = CreateWindowExW(0, c.failCreation ? L"SimXRMissingFixtureClass" : kClass,
        L"Hidden lifecycle fixture", WS_OVERLAPPED, 0, 0, 10, 10,
        nullptr, nullptr, GetModuleHandleW(nullptr), &c);
    if (!c.window) {
        if (!c.failCreation) c.error.store(GetLastError());
        c.state.PublishCreationComplete(); SetEvent(c.exited); return 0;
    }
    c.state.SetOwnedWindow(c.window);
    c.state.PublishCreationComplete();
    c.error.store(c.state.PumpUntilStopped());
    if (IsWindow(c.window)) DestroyWindow(c.window);
    SetEvent(c.exited);
    return 0;
}
HANDLE Start(Context& c) { return CreateThread(nullptr, 0, Worker, &c, 0, &c.threadId); }
bool Finish(Context& c, HANDLE thread) {
    if (!Check(Wait(c.exited), "UI thread exits within bound")) return false;
    if (!Check(Wait(thread), "native thread completion")) return false;
    Check(c.error.load() == ERROR_SUCCESS, "message-loop API success");
    Check(!c.window || !IsWindow(c.window), "window destroyed before thread completion");
    CloseHandle(thread);
    CloseHandle(c.createAllowed); CloseHandle(c.exited);
    CloseHandle(c.nested); CloseHandle(c.consumedQuit); CloseHandle(c.menuEntered);
    return true;
}
DWORD WINAPI Requester(void* data) {
    auto& c = *static_cast<Context*>(data);
    c.error.store(c.state.RequestClose());
    return 0;
}
}
int main() {
    WNDCLASSW wc{};
    wc.lpfnWndProc = WindowProcedure; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = kClass;
    if (!Check(RegisterClassW(&wc) != 0, "register fixture class")) return 1;
    // Heap contexts intentionally survive any failed bounded wait; the fixture then
    // returns failure without destroying synchronization objects used by a live worker.
    for (int repeat = 0; repeat < 12; ++repeat) {
        auto* c = new Context;
        HANDLE thread = Start(*c);
        if (!Check(thread != nullptr, "start hidden UI thread") ||
            !Check(c->state.WaitForCreation(std::chrono::milliseconds(2000)), "creation readiness")) return 1;
        if (repeat == 0) {
            Check(!c->state.OnWindowDestroyed(c->window), "foreign caller cannot quit preview queue");
            HWND host = CreateWindowExW(0, kClass, L"Hidden host fixture", WS_OVERLAPPED,
                0, 0, 10, 10, nullptr, nullptr, GetModuleHandleW(nullptr), c);
            Check(host != nullptr, "create unrelated host window");
            DestroyWindow(host);
            Check(!c->state.StopRequested(), "host destruction does not stop preview");
            MSG msg{};
            Check(!PeekMessageW(&msg, nullptr, WM_QUIT, WM_QUIT, PM_REMOVE), "host queue receives no Quit");
            Check(PostMessageW(c->window, WM_CLOSE, 0, 0) != 0, "user Close posts to owned window");
        } else {
            Check(c->state.RequestClose() == ERROR_SUCCESS, "instance teardown posts window Close");
        }
        if (!Finish(*c, thread)) return 1;
        Check(c->state.StopRequested(), "destruction retains explicit stop state");
        c->state.Reset();
        Check(!c->state.StopRequested(), "joined lifecycle can reset");
        Check(!c->state.WaitForCreation(std::chrono::milliseconds(0)), "reset clears ready state");
        delete c;
    }
    {
        auto* c = new Context;
        HANDLE thread = Start(*c);
        if (!Check(thread != nullptr, "start nested-loop worker") ||
            !Check(c->state.WaitForCreation(std::chrono::milliseconds(2000)), "nested worker ready")) return 1;
        Check(PostMessageW(c->window, kNestedLoop, 0, 0) != 0, "enter hidden nested loop");
        if (!Check(Wait(c->nested), "nested loop active")) return 1;
        Check(PostThreadMessageW(c->threadId, WM_QUIT, 0, 0) != 0, "legacy thread-only Quit sent");
        if (!Check(Wait(c->consumedQuit), "nested loop consumes legacy Quit")) return 1;
        Check(WaitForSingleObject(c->exited, 25) == WAIT_TIMEOUT, "negative control: thread-only signal did not stop UI");
        Check(c->state.RequestClose() == ERROR_SUCCESS, "window Close stops nested loop");
        if (!Finish(*c, thread)) return 1;
        delete c;
    }
    {
        auto* c = new Context;
        HANDLE thread = Start(*c);
        if (!Check(thread != nullptr, "start native-menu worker") ||
            !Check(c->state.WaitForCreation(std::chrono::milliseconds(2000)), "native-menu worker ready")) return 1;
        Check(PostMessageW(c->window, kNativeMenu, 0, 0) != 0, "enter Windows modal menu");
        if (!Check(Wait(c->menuEntered), "Windows menu loop is active")) return 1;
        Check(c->state.RequestClose() == ERROR_SUCCESS, "window Close stops Windows menu loop");
        if (!Finish(*c, thread)) return 1;
        delete c;
    }
    {
        auto* c = new Context;
        c->failCreation = true;
        HANDLE thread = Start(*c);
        if (!Check(thread != nullptr, "start failed-creation worker")) return 1;
        if (!Check(c->state.WaitForCreation(std::chrono::milliseconds(2000)), "failed creation publishes readiness")) return 1;
        Check(c->creationAttempted.load() && !c->window, "missing class actually fails window creation");
        Check(c->state.RequestClose() == ERROR_SUCCESS, "failed creation releases stop waiter");
        if (!Finish(*c, thread)) return 1;
        delete c;
    }
    {
        auto* c = new Context;
        ResetEvent(c->createAllowed);
        HANDLE thread = Start(*c);
        HANDLE requester = CreateThread(nullptr, 0, Requester, c, 0, nullptr);
        if (!Check(thread && requester, "start delayed-creation stop race")) return 1;
        const ULONGLONG deadline = GetTickCount64() + 2000;
        while (!c->state.StopRequested() && GetTickCount64() < deadline) Sleep(1);
        Check(c->state.StopRequested(), "stop visible before readiness");
        Check(WaitForSingleObject(requester, 0) == WAIT_TIMEOUT, "stop waits for actual readiness");
        SetEvent(c->createAllowed);
        if (!Check(Wait(requester), "pre-ready stop requester completes") || !Finish(*c, thread)) return 1;
        Check(c->window == nullptr, "pre-ready stop creates no window");
        Check(!c->creationAttempted.load(), "pre-ready stop bypasses creation attempt");
        CloseHandle(requester); delete c;
    }
    Check(UnregisterClassW(kClass, GetModuleHandleW(nullptr)) != 0, "all fixture windows retired");
    std::cout << "preview lifecycle: " << checks << " checks, " << failures << " failures\n";
    return failures ? 1 : 0;
}
