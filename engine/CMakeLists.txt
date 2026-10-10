cmake_minimum_required(VERSION 3.24)
project(flowxer-engine VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

option(FXENG_BUILD_ENGINE "Build flowxer-engine (needs CUDA, MXL and FFmpeg)" ON)
option(FXENG_BUILD_TESTS "Build the unit tests (no GPU needed)" ON)

find_package(Threads REQUIRED)
find_package(nlohmann_json 3 REQUIRED)

# Pure logic (no CUDA, no MXL): config, ME state, mosaic map, TAI arithmetic, v210 reference.
add_library(fxeng-core STATIC
    src/core/config.cpp
    src/core/mixer.cpp
    src/core/mosaic_map.cpp
    src/core/tai.cpp
    src/core/v210.cpp
)
target_include_directories(fxeng-core PUBLIC src)
target_link_libraries(fxeng-core PUBLIC Threads::Threads nlohmann_json::nlohmann_json)
target_compile_options(fxeng-core PRIVATE -Wall -Wextra)

if(FXENG_BUILD_TESTS)
    enable_testing()
    add_executable(fxeng-tests tests/test_main.cpp tests/test_core.cpp)
    target_link_libraries(fxeng-tests PRIVATE fxeng-core)
    target_compile_options(fxeng-tests PRIVATE -Wall -Wextra)
    add_test(NAME fxeng-tests COMMAND fxeng-tests)
endif()

if(FXENG_BUILD_ENGINE)
    # sm_75 (Turing), sm_86 (A16, RTX A4000), sm_89 (L4).
    set(CMAKE_CUDA_ARCHITECTURES 75 86 89)
    enable_language(CUDA)
    find_package(CUDAToolkit REQUIRED)
    find_package(mxl CONFIG REQUIRED)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(AV REQUIRED IMPORTED_TARGET libavcodec libavformat libavutil)

    set(FXENG_PAGE ${CMAKE_CURRENT_BINARY_DIR}/generated/mosaic_page.hpp)
    add_custom_command(
        OUTPUT ${FXENG_PAGE}
        COMMAND ${CMAKE_COMMAND} -DINPUT=${CMAKE_CURRENT_SOURCE_DIR}/web/mosaic.html -DOUTPUT=${FXENG_PAGE} -DNAME=kMosaicPage
                -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/EmbedText.cmake
        DEPENDS web/mosaic.html cmake/EmbedText.cmake
        COMMENT "Embedding the mosaic test page"
    )

    add_executable(flowxer-engine
        src/main.cpp
        src/engine/api.cpp
        src/engine/encoder.cpp
        src/engine/engine.cpp
        src/engine/gpu.cu
        src/engine/hostmem.cpp
        src/engine/http.cpp
        src/engine/mediamtx.cpp
        src/engine/stats.cpp
        ${FXENG_PAGE}
    )
    set_target_properties(flowxer-engine PROPERTIES CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED ON CUDA_RUNTIME_LIBRARY Static)
    target_include_directories(flowxer-engine PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
    target_compile_definitions(flowxer-engine PRIVATE FXENG_VERSION="${PROJECT_VERSION}-p1")
    target_compile_options(flowxer-engine PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-Wall -Wextra>)
    # libcuda and libnvidia-ml come from the NVIDIA container toolkit at run time (NVML is dlopened).
    target_link_libraries(flowxer-engine PRIVATE fxeng-core mxl::mxl CUDA::cudart_static PkgConfig::AV ${CMAKE_DL_LIBS})
    install(TARGETS flowxer-engine RUNTIME DESTINATION bin)
endif()
