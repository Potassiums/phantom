# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# Phantom: AutoRemesher's Qt-free core, built as a library for the Auto Remesh operator.

# Avoid noisy warnings from third-party code.
if((CMAKE_C_COMPILER_ID STREQUAL "GNU") OR (CMAKE_C_COMPILER_ID MATCHES "Clang"))
  add_cxx_flag(
    "-Wno-unused-result"
  )
  remove_cxx_flag(
    "-Wmissing-declarations"
  )
elseif(MSVC)
  add_cxx_flag("/W0")
endif()

if(WIN32)
  add_definitions(-D_USE_MATH_DEFINES -DNOMINMAX)
endif()

set(INC
  .
  include
  thirdparty/isotropicremesher
  thirdparty/meshoptimizer/src
)

set(INC_SYS
)

set(SRC
  autoremesher_capi.cc
  autoremesher_capi.hh

  src/AutoRemesher/autoremesher.cpp
  src/AutoRemesher/constrainedleastsquares.cpp
  src/AutoRemesher/framefield.cpp
  src/AutoRemesher/isotropicremesher.cpp
  src/AutoRemesher/meshseparator.cpp
  src/AutoRemesher/mixedintegerleastsquares.cpp
  src/AutoRemesher/parameterizer.cpp
  src/AutoRemesher/positionkey.cpp
  src/AutoRemesher/quadextractor.cpp
  src/AutoRemesher/quadparameterizer.cpp
  src/AutoRemesher/singularitysimplifier.cpp
  src/AutoRemesher/surfacemesh.cpp

  thirdparty/isotropicremesher/axisalignedboundingboxtree.cpp
  thirdparty/isotropicremesher/isotropichalfedgemesh.cpp
  thirdparty/isotropicremesher/isotropicremesher.cpp

  thirdparty/meshoptimizer/src/indexgenerator.cpp
  thirdparty/meshoptimizer/src/simplifier.cpp
)

set(LIB
  PRIVATE bf::dependencies::eigen
  PRIVATE bf::dependencies::optional::tbb
)

blender_add_lib(extern_autoremesher "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")
