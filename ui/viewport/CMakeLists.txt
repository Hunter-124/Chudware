cmake_minimum_required(VERSION 3.20)
project(aicad_ui_viewport LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(AICAD_GLAD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../extern/glad")

add_library(aicad_ui_viewport STATIC
  renderer.cpp
  mesh.cpp
  camera_anim.cpp
  viewport_impl.cpp
  render/aicm_loader.cpp
  ../../core/util/aicm_handoff.cpp
  render/id_pass.cpp
  render/overlays.cpp
)

target_include_directories(aicad_ui_viewport PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/../abstract
  ${CMAKE_CURRENT_SOURCE_DIR}/../../core
)
if(UNIX AND NOT APPLE)
  target_link_libraries(aicad_ui_viewport PUBLIC rt)
endif()

if(TARGET glad)
  target_link_libraries(aicad_ui_viewport PUBLIC glad)
  target_include_directories(aicad_ui_viewport PUBLIC ${AICAD_GLAD_DIR}/include)
elseif(EXISTS "${AICAD_GLAD_DIR}/src/gl.c")
  target_sources(aicad_ui_viewport PRIVATE ${AICAD_GLAD_DIR}/src/gl.c)
  target_include_directories(aicad_ui_viewport PUBLIC ${AICAD_GLAD_DIR}/include)
else()
  message(FATAL_ERROR "glad not found — build from ui/imgui (owns glad) or vendor host/extern/glad")
endif()

find_package(OpenGL REQUIRED)
target_link_libraries(aicad_ui_viewport PUBLIC OpenGL::GL)

if(MSVC)
  target_compile_options(aicad_ui_viewport PRIVATE /utf-8)
endif()
