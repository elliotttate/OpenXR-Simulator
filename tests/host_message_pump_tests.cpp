#include "host_message_pump.h"
#include <cstdio>
#include <cstring>
namespace {
int failures = 0, delivered = 0;
void Check(bool condition, const char* text) { if (!condition) { ++failures; std::fprintf(stderr,"FAIL: %s\n",text); } }
LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM w, LPARAM l) {
    if (message == WM_APP + 7) { ++delivered; return 0; }
    return DefWindowProcW(window,message,w,l);
}
}
int main(int argc,char** argv) {
    const bool expected = argc == 2 && std::strcmp(argv[1],"enabled") == 0;
    Check(perf::HostMessagePumpEnabled() == expected,"startup policy");
    WNDCLASSW cls{}; cls.lpfnWndProc=WindowProc; cls.hInstance=GetModuleHandleW(nullptr); cls.lpszClassName=L"SimXRHostPumpFixture";
    Check(RegisterClassW(&cls)!=0,"register isolated host window");
    HWND window=CreateWindowW(cls.lpszClassName,L"",0,0,0,0,0,HWND_MESSAGE,nullptr,cls.hInstance,nullptr);
    Check(window!=nullptr,"create isolated host window");
    Check(PostMessageW(window,WM_APP+7,0,0)!=0,"post host event");
    perf::PumpHostMessagesIfEnabled();
    Check(delivered == (expected ? 1 : 0),"runtime leaves host events queued by default");
    MSG message{};
    const bool pending=PeekMessageW(&message,window,WM_APP+7,WM_APP+7,PM_REMOVE)!=0;
    Check(pending != expected,"event remains owned by host when compatibility is off");
    if(pending) DispatchMessageW(&message);
    Check(delivered==1,"host event delivered exactly once");
    SetEnvironmentVariableA("SIMXR_PUMP_HOST_MESSAGES",expected ? "0" : "1");
    Check(perf::HostMessagePumpEnabled()==expected,"startup policy remains stable");
    DestroyWindow(window); UnregisterClassW(cls.lpszClassName,cls.hInstance);
    std::printf("host_message_pump: failures=%d\n",failures);
    return failures ? 1 : 0;
}
