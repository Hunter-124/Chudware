// Unified GL header for host TUs.
// Desktop: glad GL 3.3 core. EMSCRIPTEN/CHUDWARE_GLES3: system GLES3 (WebGL2).
#pragma once

#if defined(__EMSCRIPTEN__) || defined(CHUDWARE_GLES3)
#  include <GLES3/gl3.h>
// Desktop-only tokens used behind #ifndef CHUDWARE_GLES3 elsewhere.
#  ifndef GL_PROGRAM_POINT_SIZE
#    define GL_PROGRAM_POINT_SIZE 0x8642
#  endif
#  ifndef GL_POINT_SPRITE
#    define GL_POINT_SPRITE 0x8861
#  endif
// GLES has glDepthRangef only (no double glDepthRange).
#  ifndef glDepthRange
#    define glDepthRange(n, f) glDepthRangef((GLfloat)(n), (GLfloat)(f))
#  endif
#else
#  include <glad/gl.h>
#endif
