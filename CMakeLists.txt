cmake_minimum_required(VERSION 3.16)
project(OpenXR-Simulator VERSION 1.7.1 LANGUAGES CXX)

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
    src/frame_timeline.h
    src/gpu_frame_timer.h
    src/projection_timing.h
    src/preview_focus.h
    src/command_epochs.h
    src/ui_enhancements.h
)
if(CMAKE_SIZEOF_VOID_P EQUAL 4)
    target_sources(openxr_simulator PRIVATE openxr_simulator_x86.def)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(runtime_command_worker_tests tests/runtime_command_worker_tests.cpp src/flicker_detector.cpp)
    target_compile_definitions(runtime_command_worker_tests PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    add_test(NAME runtime_command_worker COMMAND runtime_command_worker_tests)
    add_executable(xr_ui_probe probe/xr_probe.cpp)
    target_compile_definitions(xr_ui_probe PRIVATE XR_PROBE_DIRECT_RUNTIME)
    target_link_libraries(xr_ui_probe PRIVATE d3d12 dxgi d3dcompiler)
    add_dependencies(xr_ui_probe openxr_simulator)
    add_executable(xr_ui_desktop probe/xr_ui_desktop.cpp)
    target_link_libraries(xr_ui_desktop PRIVATE d3d11 dxgi opengl32)
    add_dependencies(xr_ui_desktop openxr_simulator)
    add_executable(xr_layer_order probe/xr_layer_order.cpp)
    target_link_libraries(xr_layer_order PRIVATE d3d11 d3d12 dxgi)
    add_dependencies(xr_layer_order openxr_simulator)
    add_executable(xr_ui_probe_vk probe/xr_probe_vk.cpp)
    target_compile_definitions(xr_ui_probe_vk PRIVATE XR_PROBE_DIRECT_RUNTIME)
    add_dependencies(xr_ui_probe_vk openxr_simulator)
    add_executable(projection_timing_tests tests/projection_timing_tests.cpp)
    target_include_directories(projection_timing_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    add_test(NAME projection_timing COMMAND projection_timing_tests)

    add_executable(command_epochs_tests tests/command_epochs_tests.cpp)
    target_include_directories(command_epochs_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    add_test(NAME command_epochs COMMAND command_epochs_tests)

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
        foreach(backend d3d11 d3d12)
            foreach(mode projection stereo_quads)
                set(layer_args)
                if(mode STREQUAL "stereo_quads")
                    list(APPEND layer_args --stereo-quads)
                endif()
                add_test(NAME composition_order_${backend}_${mode}
                    COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/composition_layer_order_e2e.py
                        --probe $<TARGET_FILE:xr_layer_order> --runtime $<TARGET_FILE:openxr_simulator>
                        --backend ${backend} ${layer_args} --evidence ${CMAKE_BINARY_DIR}/layer-evidence)
                set_tests_properties(composition_order_${backend}_${mode} PROPERTIES RUN_SERIAL TRUE TIMEOUT 60)
            endforeach()
        endforeach()
    endif()
    add_executable(action_state_tests tests/action_state_tests.cpp)
    target_include_directories(action_state_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME action_state COMMAND action_state_tests)

    add_executable(pose_math_tests tests/pose_math_tests.cpp)
    target_include_directories(pose_math_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME pose_math COMMAND pose_math_tests)

    add_executable(swapchain_state_tests tests/swapchain_state_tests.cpp)
    target_include_directories(swapchain_state_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME swapchain_state COMMAND swapchain_state_tests)

    add_executable(frame_state_tests tests/frame_state_tests.cpp)
    target_include_directories(frame_state_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME frame_state COMMAND frame_state_tests)
    add_executable(preview_focus_tests tests/preview_focus_tests.cpp)
    target_include_directories(preview_focus_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME preview_focus COMMAND preview_focus_tests)

    add_executable(preview_title_tests tests/preview_title_tests.cpp)
    target_include_directories(preview_title_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    target_link_libraries(preview_title_tests PRIVATE user32)
    add_test(NAME preview_title COMMAND preview_title_tests)


    add_executable(composition_validation_tests tests/composition_validation_tests.cpp)
    target_include_directories(composition_validation_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME composition_validation COMMAND composition_validation_tests)

    add_executable(composition_render_tests tests/composition_render_tests.cpp)
    target_include_directories(composition_render_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME composition_render COMMAND composition_render_tests)

    add_executable(api_validation_tests tests/api_validation_tests.cpp)
    target_include_directories(api_validation_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME api_validation COMMAND api_validation_tests)
    add_executable(ui_input_polling_tests tests/ui_input_polling_tests.cpp)
    target_include_directories(ui_input_polling_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    if(MSVC)
        target_compile_options(ui_input_polling_tests PRIVATE /utf-8)
    endif()
    add_test(NAME ui_input_polling COMMAND ui_input_polling_tests)
    add_executable(frame_timeline_tests tests/frame_timeline_tests.cpp)
    target_include_directories(frame_timeline_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    add_test(NAME frame_timeline COMMAND frame_timeline_tests)
    set_tests_properties(frame_timeline PROPERTIES TIMEOUT 60)
    add_executable(gpu_frame_timer_tests tests/gpu_frame_timer_tests.cpp)
    target_include_directories(gpu_frame_timer_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    target_link_libraries(gpu_frame_timer_tests PRIVATE d3d12 d3d11 dxgi)
    add_test(NAME gpu_frame_timer COMMAND gpu_frame_timer_tests)
    set_tests_properties(gpu_frame_timer PROPERTIES TIMEOUT 60)
    add_executable(ui_gpu_timing_tests tests/ui_gpu_timing_tests.cpp)
    target_include_directories(ui_gpu_timing_tests PRIVATE ${CMAKE_SOURCE_DIR}/src)
    if(MSVC)
        target_compile_options(ui_gpu_timing_tests PRIVATE /utf-8)
    endif()
    add_test(NAME ui_gpu_timing COMMAND ${CMAKE_COMMAND} -E env --unset=OPENXR_SIM_GPU_TIMING $<TARGET_FILE:ui_gpu_timing_tests>)
    add_executable(frame_analysis_tests tests/frame_analysis_tests.cpp)
    target_include_directories(frame_analysis_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/tools/frametime_viewer)
    add_test(NAME frame_analysis COMMAND frame_analysis_tests)
    add_executable(capture_tests tests/capture_tests.cpp)
    target_include_directories(capture_tests PRIVATE ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/tools/frametime_viewer)
    target_compile_definitions(capture_tests PRIVATE _CRT_SECURE_NO_WARNINGS)
    add_test(NAME capture COMMAND capture_tests)
endif()

# Frame-time viewer: a separate process that reads a game's frame timeline
# (src/frame_timeline.h) out of shared memory. Fetches Dear ImGui and ImPlot.
option(SIMXR_BUILD_FRAMETIME_VIEWER "Build openxr_sim_frametime.exe" ON)
if(SIMXR_BUILD_FRAMETIME_VIEWER)
    include(FetchContent)
    FetchContent_Declare(imgui GIT_REPOSITORY https://github.com/ocornut/imgui.git GIT_TAG v1.92.9b GIT_SHALLOW TRUE)
    FetchContent_Declare(implot GIT_REPOSITORY https://github.com/epezent/implot.git GIT_TAG v1.0 GIT_SHALLOW TRUE)
    FetchContent_MakeAvailable(imgui implot)
    add_executable(openxr_sim_frametime WIN32
        tools/frametime_viewer/main.cpp
        tools/frametime_viewer/viewer_app.cpp
        tools/frametime_viewer/viewer_app.h
        tools/frametime_viewer/ui_kit.cpp
        tools/frametime_viewer/ui_kit.h
        tools/frametime_viewer/frame_analysis.h
        tools/frametime_viewer/timeline_client.h
        tools/frametime_viewer/capture_file.h
        tools/frametime_viewer/capture_report.h
        tools/frametime_viewer/capture_session.h
        tools/frametime_viewer/headless.cpp
        tools/frametime_viewer/headless.h
        src/json.h
        src/frame_timeline.h
        ${imgui_SOURCE_DIR}/imgui.cpp
        ${imgui_SOURCE_DIR}/imgui_draw.cpp
        ${imgui_SOURCE_DIR}/imgui_tables.cpp
        ${imgui_SOURCE_DIR}/imgui_widgets.cpp
        ${imgui_SOURCE_DIR}/backends/imgui_impl_win32.cpp
        ${imgui_SOURCE_DIR}/backends/imgui_impl_dx11.cpp
        ${implot_SOURCE_DIR}/implot.cpp
        ${implot_SOURCE_DIR}/implot_items.cpp
    )
    # BEFORE: the Vulkan include dir can be a vcpkg tree carrying an older ImGui/ImPlot.
    target_include_directories(openxr_sim_frametime BEFORE PRIVATE
        ${CMAKE_SOURCE_DIR}/src ${CMAKE_SOURCE_DIR}/tools/frametime_viewer
        ${imgui_SOURCE_DIR} ${imgui_SOURCE_DIR}/backends ${implot_SOURCE_DIR})
    target_compile_definitions(openxr_sim_frametime PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE _CRT_SECURE_NO_WARNINGS)
    target_link_libraries(openxr_sim_frametime PRIVATE d3d11 dxgi d3dcompiler dwmapi)
    if(MSVC)
        target_compile_options(openxr_sim_frametime PRIVATE /utf-8)
        # Static CRT, like the runtime DLL: the viewer ships beside it in the release
        # package and must start on a machine without the Visual C++ redistributable.
        set_property(TARGET openxr_sim_frametime PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
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
        # Statically link the C/C++ runtime (/MT) so the DLL does not depend on the
        # process's MSVCP140.dll. Godot loads an older MSVCP140 (14.15) into the
        # process; if our DLL binds to that older copy, std::string/locale layout
        # mismatches crash during static init (access violation in MSVCP140 0x18e44)
        # before any runtime entry point runs. /MT embeds the CRT we built with.
        set_property(TARGET openxr_simulator PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
        target_compile_options(openxr_simulator PRIVATE
            $<$<CONFIG:Debug>:/MTd>
            $<$<CONFIG:Release>:/MT>
            $<$<CONFIG:RelWithDebInfo>:/MT>)
        target_link_options(openxr_simulator PRIVATE
            $<$<CONFIG:Debug>:/NODEFAULTLIB:MSVCRTD.lib>
            $<$<CONFIG:Release>:/NODEFAULTLIB:MSVCRT.lib>
            $<$<CONFIG:RelWithDebInfo>:/NODEFAULTLIB:MSVCRT.lib>)
    endif()
endif()

# Stamped into the frame timeline's session info, so a capture names the build
# it came from. Taken at configure time.
execute_process(COMMAND git rev-parse --short=10 HEAD WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    OUTPUT_VARIABLE SIMXR_BUILD_ID OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
execute_process(COMMAND git status --porcelain --untracked-files=no WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    OUTPUT_VARIABLE SIMXR_TREE_CHANGES OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT SIMXR_BUILD_ID)
    set(SIMXR_BUILD_ID unknown)
endif()
if(SIMXR_TREE_CHANGES)
    string(APPEND SIMXR_BUILD_ID "-dirty")
endif()
target_compile_definitions(openxr_simulator PRIVATE SIMXR_BUILD_ID="${SIMXR_BUILD_ID}")

# Generate OpenXR runtime manifest
set(RUNTIME_MANIFEST_CONTENT "{
    \"file_format_version\": \"1.0.0\",
    \"runtime\": {
        \"name\": \"OpenXR Simulator\",
        \"library_path\": \"${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.dll\"
    }
}")

file(MAKE_DIRECTORY "${SIMXR_OUTPUT_DIRECTORY}")
file(WRITE ${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.json ${RUNTIME_MANIFEST_CONTENT})

# Installation
install(TARGETS openxr_simulator DESTINATION bin)
install(FILES ${SIMXR_OUTPUT_DIRECTORY}/openxr_simulator.json DESTINATION bin)
