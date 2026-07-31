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

# OpenXR Simulator Runtime DLL
add_library(openxr_simulator SHARED
    src/runtime.cpp
    src/json.h
    src/mcp_integration.h
    src/ui_enhancements.h
)

# Link libraries
target_link_libraries(openxr_simulator
    d3d11
    d3d12
    dxgi
    d3dcompiler
    dwmapi
    uxtheme
)

# Windows-specific settings
if(WIN32)
    target_compile_definitions(openxr_simulator PRIVATE
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        _CRT_SECURE_NO_WARNINGS
    )
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
