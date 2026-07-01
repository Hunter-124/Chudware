cmake_minimum_required(VERSION 3.20)
project(aicad_platform LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(aicad_platform STATIC dpi.cpp resources.cpp)
get_filename_component(AICAD_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
target_include_directories(aicad_platform PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${AICAD_REPO_ROOT})

if(WIN32)
  target_sources(aicad_platform PRIVATE dialogs_win32.cpp window_win32.cpp paths_win32.cpp)
  target_link_libraries(aicad_platform PUBLIC dwmapi)
else()
  target_sources(aicad_platform PRIVATE dialogs_linux.cpp window_linux.cpp paths_linux.cpp)
endif()

# Parent (ui/imgui) must call target_link_libraries(aicad_platform PUBLIC glfw) and pass STB includes.
if(TARGET glfw)
  target_link_libraries(aicad_platform PUBLIC glfw)
endif()
if(DEFINED AICAD_STB_INCLUDE_DIR)
  target_include_directories(aicad_platform PRIVATE ${AICAD_STB_INCLUDE_DIR})
endif()
