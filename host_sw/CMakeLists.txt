# PROSE-APEX Host Software Stack — Build System
#
# Supports two build modes:
#   1. Simulation (default): PROSE_SIM=ON, no CUDA/CXL hardware needed.
#   2. Production: PROSE_SIM=OFF, requires CUDA Toolkit and CXL-capable system.
#
# Usage:
#   mkdir build && cd build
#   cmake .. -DPROSE_SIM=ON    # Simulation mode
#   cmake .. -DPROSE_SIM=OFF   # Production mode (requires CUDA)
#   make -j$(nproc)
#
# Author: Anonymous

cmake_minimum_required(VERSION 3.18)
project(prose_apex_host LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 17)

# Build mode: simulation vs production.
option(PROSE_SIM "Build in simulation mode (no hardware required)" ON)

# Compiler flags.
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wall -Wextra -O2")
set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} -O2 --expt-relaxed-constexpr")

if(PROSE_SIM)
  add_definitions(-DPROSE_SIM)
  message(STATUS "PROSE-APEX: Building in SIMULATION mode")
else()
  message(STATUS "PROSE-APEX: Building in PRODUCTION mode (CUDA required)")
  find_package(CUDAToolkit REQUIRED)
endif()

# Source files.
set(PROSE_HOST_SOURCES
    prose_allocator.cpp
    prose_runtime.cpp
)

set(PROSE_CUDA_SOURCES
    prose_gpu_bridge.cu
    prose_feedback.cu
)

# Library: prose_host (C++ components).
add_library(prose_host STATIC ${PROSE_HOST_SOURCES})
target_include_directories(prose_host PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

if(NOT PROSE_SIM)
  target_link_libraries(prose_host PUBLIC CUDA::cudart)
endif()

# Library: prose_gpu (CUDA kernels).
if(NOT PROSE_SIM)
  add_library(prose_gpu STATIC ${PROSE_CUDA_SOURCES})
  target_include_directories(prose_gpu PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  set_target_properties(prose_gpu PROPERTIES
      CUDA_SEPARABLE_COMPILATION ON
      CUDA_ARCHITECTURES "80;86;89;90")
  target_link_libraries(prose_gpu PUBLIC CUDA::cudart)
endif()

# Executable: integration test.
add_executable(prose_apex_test main.cpp)
target_link_libraries(prose_apex_test PRIVATE prose_host)

if(NOT PROSE_SIM)
  target_link_libraries(prose_apex_test PRIVATE prose_gpu)
endif()

# Executable: single-host Mode B (endpoint-gated pull) end-to-end benchmark.
#
# This runs the Mode B protocol against a REAL commodity CXL Type-3 device
# (devdax / CXL NUMA node) with no custom endpoint silicon, and measures RPE and
# promotion latency on real CXL.mem traffic. It has no CUDA or hardware-only
# dependency: on a host without a CXL device it self-labels as EMULATED and
# still runs the protocol. Build it in both modes.
add_executable(bench_modeb_e2e bench_modeb_e2e.cpp)
target_include_directories(bench_modeb_e2e PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# Install targets.
install(TARGETS prose_apex_test DESTINATION bin)
install(TARGETS prose_host DESTINATION lib)
install(FILES
    prose_types.h
    prose_allocator.h
    prose_runtime.h
    DESTINATION include/prose)
