cmake_minimum_required(VERSION 3.20)
project(chudware_platform LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(chudware_platform STATIC dpi.cpp resources.cpp)
get_filename_component(CHUDWARE_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
target_include_directories(chudware_platform PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${CHUDWARE_REPO_ROOT})

if(WIN32)
  target_sources(chudware_platform PRIVATE dialogs_win32.cpp window_win32.cpp paths_win32.cpp)
  target_link_libraries(chudware_platform PUBLIC dwmapi)
else()
  target_sources(chudware_platform PRIVATE dialogs_linux.cpp window_linux.cpp paths_linux.cpp)
endif()

# Parent (ui/imgui) must call target_link_libraries(chudware_platform PUBLIC glfw) and pass STB includes.
if(TARGET glfw)
  target_link_libraries(chudware_platform PUBLIC glfw)
endif()
if(DEFINED CHUDWARE_STB_INCLUDE_DIR)
  target_include_directories(chudware_platform PRIVATE ${CHUDWARE_STB_INCLUDE_DIR})
endif()
