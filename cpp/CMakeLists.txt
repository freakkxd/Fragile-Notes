cmake_minimum_required(VERSION 3.20)
project(FragileNotes VERSION 0.3.6 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
find_package(OpenSSL REQUIRED)
pkg_check_modules(YAML_CPP yaml-cpp)

add_library(fragile_core STATIC
    src/core/vault.cpp
    src/core/crdt.cpp
    src/core/tasks.cpp
    src/core/srs.cpp
    src/core/workspaces.cpp
    src/core/crypto.cpp
    src/core/dataview.cpp
    src/core/templates.cpp
    src/core/media.cpp
    src/services/fts.cpp
    src/services/vault_service.cpp
)

target_include_directories(fragile_core PUBLIC include)

target_link_libraries(fragile_core PUBLIC
    OpenSSL::Crypto
    OpenSSL::SSL
    $<$<BOOL:${YAML_CPP_FOUND}>:yaml-cpp>
)

if(YAML_CPP_FOUND)
    target_include_directories(fragile_core PUBLIC ${YAML_CPP_INCLUDE_DIRS})
endif()

target_compile_options(fragile_core PRIVATE -Wall -Wextra -Wpedantic -O2)

add_executable(fragile_tests tests/test_core.cpp)
target_link_libraries(fragile_tests PRIVATE fragile_core)

enable_testing()
add_test(NAME core_tests COMMAND fragile_tests)

add_library(fragile_bridge SHARED src/bridge/py_bridge.cpp)
target_link_libraries(fragile_bridge PRIVATE fragile_core)
set_target_properties(fragile_bridge PROPERTIES OUTPUT_NAME _fragile_cpp PREFIX "")
