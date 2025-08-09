cmake_minimum_required(VERSION 3.20)
project(OpenXR-Simulator VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_WINDOWS_EXPORT_ALL_SYMBOLS ON)

# OpenXR Simulator Runtime library
add_library(openxr_simulator SHARED 
    src/runtime.cpp
)

# Include directories - we'll need to copy OpenXR headers
target_include_directories(openxr_simulator PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

# Link Windows libraries
target_link_libraries(openxr_simulator PRIVATE 
    dxgi 
    d3d11
    d3dcompiler
)

# Set output properties
set_target_properties(openxr_simulator PROPERTIES 
    OUTPUT_NAME "openxr_simulator"
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
)

# Installation rules
install(TARGETS openxr_simulator
    RUNTIME DESTINATION bin
)

install(FILES 
    ${CMAKE_CURRENT_SOURCE_DIR}/scripts/register-runtime.ps1
    ${CMAKE_CURRENT_SOURCE_DIR}/scripts/unregister-runtime.ps1
    DESTINATION scripts
)

install(FILES 
    ${CMAKE_CURRENT_SOURCE_DIR}/README.md
    ${CMAKE_CURRENT_SOURCE_DIR}/LICENSE
    DESTINATION .
)