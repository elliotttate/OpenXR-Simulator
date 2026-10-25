cmake_minimum_required(VERSION 3.16)
project(OpenXR-Simulator VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Output directories
set(SIMXR_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/bin" CACHE PATH "Runtime and test output directory")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${SIMXR_OUTPUT_DIRECTORY})
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${SIMXR_OUTPUT_DIRECTORY})
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${SIMXR_OUTPUT_DIRECTORY})

# For multi-config generators (Visual Studio)
foreach(OUTPUTCONFIG ${CMAKE_CONFIGURATION_TYPES})
    string(TOUPPER ${OUTPUTCONFIG} OUTPUTCONFIG)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${SIMXR_OUTPUT_DIRECTORY})
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${SIMXR_OUTPUT_DIRECTORY})
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_${OUTPUTCONFIG} ${SIMXR_OUTPUT_DIRECTORY})
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
    add_executable(xr_ui_probe probe/xr_probe.cpp)
    target_compile_definitions(xr_ui_probe PRIVATE XR_PROBE_DIRECT_RUNTIME)
    target_link_libraries(xr_ui_probe PRIVATE d3d12 dxgi d3dcompiler)
    add_dependencies(xr_ui_probe openxr_simulator)
    add_executable(xr_ui_desktop probe/xr_ui_desktop.cpp)
    target_link_libraries(xr_ui_desktop PRIVATE d3d11 dxgi opengl32)
    add_dependencies(xr_ui_desktop openxr_simulator)
    add_executable(xr_ui_probe_vk probe/xr_probe_vk.cpp)
    target_compile_definitions(xr_ui_probe_vk PRIVATE XR_PROBE_DIRECT_RUNTIME)
    add_dependencies(xr_ui_probe_vk openxr_simulator)
    add_executable(projection_timing_tests tests/projection_timing_tests.cpp)
    target_include_directories(projection_timing_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    add_test(NAME projection_timing COMMAND projection_timing_tests)

    add_executable(ui_resolution_tests tests/ui_resolution_tests.cpp)
    target_include_directories(ui_resolution_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    if(MSVC)
        target_compile_options(ui_resolution_tests PRIVATE /utf-8)
    endif()
    add_test(NAME ui_resolution COMMAND ui_resolution_tests)

    add_executable(ui_pointer_lifecycle_tests tests/ui_pointer_lifecycle_tests.cpp)
    target_include_directories(ui_pointer_lifecycle_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    target_compile_definitions(ui_pointer_lifecycle_tests PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    add_test(NAME ui_pointer_lifecycle COMMAND ui_pointer_lifecycle_tests)

    add_executable(ui_surface_classification_tests tests/ui_surface_classification_tests.cpp)
    target_include_directories(ui_surface_classification_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    target_compile_definitions(ui_surface_classification_tests PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    add_test(NAME ui_surface_classification COMMAND ui_surface_classification_tests)

    option(SIMXR_ENABLE_UI_AUTOMATION_TESTS "Run real MCP screenshot/hover/click tests (requires Python dependencies)" OFF)
    if(SIMXR_ENABLE_UI_AUTOMATION_TESTS)
        find_package(Python3 REQUIRED COMPONENTS Interpreter)
        add_test(NAME ui_targeting COMMAND ${Python3_EXECUTABLE} -m unittest discover -s ${CMAKE_SOURCE_DIR}/tests -p test_ui_targeting.py)
        function(simxr_ui_test name target backend eye hand layout)
            set(extra_args)
            if(name STREQUAL "ui_e2e_d3d12")
                list(APPEND extra_args --negative-tests)
            endif()
            add_test(NAME ${name} COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
                --probe $<TARGET_FILE:${target}> --backend ${backend} --eye ${eye} --hand ${hand} --layout ${layout}
                --evidence ${CMAKE_BINARY_DIR}/ui-evidence ${extra_args})
            set_tests_properties(${name} PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        endfunction()
        simxr_ui_test(ui_e2e_d3d12 xr_ui_probe d3d12 left right side_by_side)
        simxr_ui_test(ui_e2e_over_under xr_ui_probe d3d12 right left over_under)
        simxr_ui_test(ui_e2e_stereo xr_ui_probe d3d12 both right side_by_side)
        simxr_ui_test(ui_e2e_d3d11 xr_ui_desktop d3d11 right left side_by_side)
        simxr_ui_test(ui_e2e_opengl xr_ui_desktop opengl left right side_by_side)
        simxr_ui_test(ui_e2e_vulkan xr_ui_probe_vk vulkan left right side_by_side)
        add_test(NAME ui_e2e_world_eye COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
            --probe $<TARGET_FILE:xr_ui_probe> --ui-mode world --origin-mode eye --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
        add_test(NAME ui_e2e_world_surface COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
            --probe $<TARGET_FILE:xr_ui_probe> --ui-mode world --origin-mode hand --hand left --eye right --layout over_under
            --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
        set_tests_properties(ui_e2e_world_eye ui_e2e_world_surface PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        foreach(mode quad world_eye world_surface)
            set(offset_args --app-ray-offset)
            if(mode MATCHES "^world")
                list(APPEND offset_args --ui-mode world)
                if(mode STREQUAL "world_surface")
                    list(APPEND offset_args --origin-mode hand)
                endif()
            endif()
            add_test(NAME ui_e2e_offset_${mode} COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
                --probe $<TARGET_FILE:xr_ui_probe> ${offset_args} --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
            set_tests_properties(ui_e2e_offset_${mode} PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        endforeach()
        add_test(NAME ui_e2e_capture_publication COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
            --probe $<TARGET_FILE:xr_ui_desktop> --backend d3d11 --lock-legacy-status --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
        set_tests_properties(ui_e2e_capture_publication PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        add_test(NAME ui_e2e_hold COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
            --probe $<TARGET_FILE:xr_ui_probe> --hold-tests --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
        set_tests_properties(ui_e2e_hold PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        foreach(hand left right)
            add_test(NAME ui_e2e_secondary_${hand} COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/ui_automation_e2e.py
                --probe $<TARGET_FILE:xr_ui_probe> --hand ${hand} --activation-button secondary --evidence ${CMAKE_BINARY_DIR}/ui-evidence)
            set_tests_properties(ui_e2e_secondary_${hand} PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
        endforeach()
    endif()
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
        \"library_path\": \"${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.dll\"
    }
}")

file(MAKE_DIRECTORY "${SIMXR_OUTPUT_DIRECTORY}")
file(WRITE ${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.json ${RUNTIME_MANIFEST_CONTENT})

# Installation
install(TARGETS openxr_simulator DESTINATION bin)
install(FILES ${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.json DESTINATION bin)
