@echo off
setlocal
cd /d "%~dp0"
for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "TASK_VS=%%i"
if not defined TASK_VS exit /b 1
call "%TASK_VS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
if not exist build mkdir build
cl /nologo /std:c++17 /EHsc /W4 /O2 /utf-8 mesh_test.cpp /Fobuild\mesh_test.obj /Febuild\mesh_test.exe
if errorlevel 1 exit /b 1
if exist probe.cpp (
  cl /nologo /std:c++17 /EHsc /W4 /O2 /utf-8 /DUNICODE /D_UNICODE /DWIN32_LEAN_AND_MEAN /DNOMINMAX probe.cpp /Fobuild\probe.obj /Febuild\ui_panel_volume_probe.exe /link d3d11.lib dxgi.lib d3dcompiler.lib windowscodecs.lib ole32.lib
  if errorlevel 1 exit /b 1
)
