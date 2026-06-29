cmake_minimum_required(VERSION 3.20)
project(aicad_ui_abstract LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(aicad_ui_abstract STATIC
    command_registry.cpp
    panel_spec.cpp
)
target_include_directories(aicad_ui_abstract PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)

add_executable(test_command_registry test/test_command_registry.cpp)
target_link_libraries(test_command_registry PRIVATE aicad_ui_abstract)
add_test(NAME ui_abstract_command_registry COMMAND test_command_registry)
