#include <windows.h>
#include <algorithm>
#include <array>
#include <cstdio>
#include <vector>
int main() {
 const int keys[]={VK_SHIFT,'W','S','A','D','Q','E','M',VK_NUMPAD8,VK_NUMPAD2,VK_NUMPAD4,VK_NUMPAD6,VK_ADD,VK_SUBTRACT,VK_NUMPAD7,VK_NUMPAD9,VK_NUMPAD1,VK_NUMPAD3,VK_NUMPAD5,VK_SPACE,'F',VK_TAB,'R','T',VK_RETURN,VK_LCONTROL,VK_LMENU,'G','V','B',VK_UP,VK_DOWN,VK_LEFT,VK_RIGHT,'I','K','J','L'};
 std::array<std::vector<double>,38> samples;
 LARGE_INTEGER freq{};QueryPerformanceFrequency(&freq);
 for(int run=0;run<300;++run){
  for(size_t k=0;k<38;++k){LARGE_INTEGER a{},b{};QueryPerformanceCounter(&a);volatile SHORT value=GetAsyncKeyState(keys[k]);(void)value;QueryPerformanceCounter(&b);samples[k].push_back((b.QuadPart-a.QuadPart)*1000.0/freq.QuadPart);}
  Sleep(8);
 }
 for(size_t k=0;k<38;++k){auto& v=samples[k];std::sort(v.begin(),v.end());printf("vk=%d median_ms=%.6f p99_ms=%.6f\n",keys[k],v[v.size()/2],v[v.size()*99/100]);}
}
