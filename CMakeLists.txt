cmake_minimum_required(VERSION 3.16)
project(OpenXR-Simulator VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Output directories
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR}/bin)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR}/bin)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR}/bin)

# For multi-config generators (Visual Studio)
foreach(OUTPUTCONFIG ${CMAKE_CONFIGURATION_TYPES})
    string(TOUPPER ${OUTPUTCONFIG} OUTPUTCONFIG)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${CMAKE_SOURCE_DIR}/bin)
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${CMAKE_SOURCE_DIR}/bin)
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${CMAKE_SOURCE_DIR}/bin)
endforeach()

# Include directories
include_directories(${CMAKE_SOURCE_DIR}/include)
include_directories(${CMAKE_SOURCE_DIR}/src)

# Vulkan headers only -- the runtime resolves every entry point through the
# app-supplied vkGetInstanceProcAddr (or a lazily loaded vulkan-1.dll), so it must
# keep loading on machines with no Vulkan at all.
find_path(SIMXR_VULKAN_INCLUDE_DIR vulkan/vulkan.h
    HINTS $ENV{VULKAN_SDK}/Include $ENV{VULKAN_SDK}/include
    PATHS ${CMAKE_SOURCE_DIR}/../BotW-BetterVR/cmake-build-Release/vcpkg_installed/x64-windows-static/include
          ${CMAKE_SOURCE_DIR}/../BotW-BetterVR/build/vcpkg_installed/x64-windows-static/include)
if(NOT SIMXR_VULKAN_INCLUDE_DIR)
    message(FATAL_ERROR "vulkan/vulkan.h not found. Install the Vulkan SDK or set VULKAN_SDK.")
endif()
include_directories(${SIMXR_VULKAN_INCLUDE_DIR})

# OpenXR Simulator Runtime DLL
add_library(openxr_simulator SHARED
    src/runtime.cpp
    src/flicker_detector.cpp
    src/flicker_detector.h
    src/json.h
    src/mcp_integration.h
    src/projection_timing.h
    src/ui_enhancements.h
)

include(CTest)
if(BUILD_TESTING)
    add_executable(projection_timing_tests tests/projection_timing_tests.cpp)
    target_include_directories(projection_timing_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    add_test(NAME projection_timing COMMAND projection_timing_tests)

    add_executable(ui_resolution_tests tests/ui_resolution_tests.cpp)
    target_include_directories(ui_resolution_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    if(MSVC)
        target_compile_options(ui_resolution_tests PRIVATE /utf-8)
    endif()
    add_test(NAME ui_resolution COMMAND ui_resolution_tests)

    add_executable(ui_input_polling_tests tests/ui_input_polling_tests.cpp)
    target_include_directories(ui_input_polling_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    if(MSVC)
        target_compile_options(ui_input_polling_tests PRIVATE /utf-8)
    endif()
    add_test(NAME ui_input_polling COMMAND ui_input_polling_tests)
endif()

# Link libraries
target_link_libraries(openxr_simulator
    d3d11
    d3d12
    dxgi
    d3dcompiler
    dwmapi
    msimg32
    uxtheme
)

# Windows-specific settings
if(WIN32)
    target_compile_definitions(openxr_simulator PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        _CRT_SECURE_NO_WARNINGS
        UNICODE
        _UNICODE
    )
    if(MSVC)
        # Without this MSVC decodes the UTF-8 sources as the system ANSI codepage.
        target_compile_options(openxr_simulator PRIVATE /utf-8)
    endif()
endif()

# Generate OpenXR runtime manifest
set(RUNTIME_MANIFEST_CONTENT "{
    \"file_format_version\": \"1.0.0\",
    \"runtime\": {
        \"library_path\": \"${CMAKE_SOURCE_DIR}/bin/openxr_simulator.dll\"
    }
}")

file(WRITE ${CMAKE_SOURCE_DIR}/bin/openxr_simulator.json ${RUNTIME_MANIFEST_CONTENT})

# Installation
install(TARGETS openxr_simulator DESTINATION bin)
install(FILES ${CMAKE_SOURCE_DIR}/bin/openxr_simulator.json DESTINATION bin)
