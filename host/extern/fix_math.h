// Workaround for MSVC 14.51 (VS 2026) missing C99 float math function declarations.
// The <cmath> header in this toolchain expects these to exist in the global namespace,
// but the UCRT headers don't declare them.
// This header is force-included via /FI on the AI-CAD target only (C++ files).

#pragma once
#include <math.h>

#ifdef __cplusplus
extern "C" {
#endif

float __cdecl acosf(float);
float __cdecl acoshf(float);
float __cdecl asinf(float);
float __cdecl asinhf(float);
float __cdecl atanf(float);
float __cdecl atanhf(float);
float __cdecl atan2f(float, float);
float __cdecl cbrtf(float);
float __cdecl ceilf(float);
float __cdecl copysignf(float, float);
float __cdecl cosf(float);
float __cdecl coshf(float);
float __cdecl erff(float);
float __cdecl erfcf(float);
float __cdecl expf(float);
float __cdecl exp2f(float);
float __cdecl expm1f(float);
float __cdecl fabsf(float);
float __cdecl fdimf(float, float);
float __cdecl floorf(float);
float __cdecl fmaf(float, float, float);
float __cdecl fmaxf(float, float);
float __cdecl fminf(float, float);
float __cdecl fmodf(float, float);
float __cdecl frexpf(float, int*);
float __cdecl hypotf(float, float);
int __cdecl ilogbf(float);
float __cdecl ldexpf(float, int);
float __cdecl lgammaf(float);
long long __cdecl llrintf(float);
long long __cdecl llroundf(float);
float __cdecl logf(float);
float __cdecl log10f(float);
float __cdecl log1pf(float);
float __cdecl log2f(float);
float __cdecl logbf(float);
long __cdecl lrintf(float);
long __cdecl lroundf(float);
float __cdecl modff(float, float*);
float __cdecl nearbyintf(float);
float __cdecl nextafterf(float, float);
float __cdecl nexttowardf(float, long double);
float __cdecl powf(float, float);
float __cdecl remainderf(float, float);
float __cdecl remquof(float, float, int*);
float __cdecl rintf(float);
float __cdecl roundf(float);
float __cdecl scalblnf(float, long);
float __cdecl scalbnf(float, int);
float __cdecl sinf(float);
float __cdecl sinhf(float);
float __cdecl sqrtf(float);
float __cdecl tanf(float);
float __cdecl tanhf(float);
float __cdecl tgammaf(float);
float __cdecl truncf(float);

#ifdef __cplusplus
}
#endif
