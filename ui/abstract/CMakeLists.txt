cmake_minimum_required(VERSION 3.20)
project(aicad_ui_abstract LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(aicad_ui_abstract STATIC
    command_registry.cpp
    panel_spec.cpp
    ask_form.cpp
    piping_view.cpp       # T-U7 piping doc → net canvas adapter
    hydraulic_view.cpp    # T-U7 hydraulic doc → net canvas adapter
    structural_view.cpp   # T-U7 structural doc → net canvas adapter
)
target_include_directories(aicad_ui_abstract PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)

add_executable(test_command_registry test/test_command_registry.cpp)
target_link_libraries(test_command_registry PRIVATE aicad_ui_abstract)
add_test(NAME ui_abstract_command_registry COMMAND test_command_registry)

# ask_form is transport-neutral (no ImGui dependency), so its test lives with the
# other ui/imgui/test/*.cpp sources but builds here where aicad_ui_abstract already
# links -- keeps this a fast, lock-free target independent of the ui/imgui GUI build.
add_executable(test_ask_form ${CMAKE_CURRENT_SOURCE_DIR}/../imgui/test/test_ask_form.cpp)
target_link_libraries(test_ask_form PRIVATE aicad_ui_abstract)
add_test(NAME ui_abstract_ask_form COMMAND test_ask_form)
