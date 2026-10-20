# MSVC LTCG can exhaust memory when several engine-sized links run together.
# Keep compilation parallel and honor an explicitly selected caller link pool.
if(MSVC AND CMAKE_GENERATOR MATCHES "^Ninja" AND NOT DEFINED CMAKE_JOB_POOL_LINK)
  get_property(_chudware_link_pools GLOBAL PROPERTY JOB_POOLS)
  if(NOT _chudware_link_pools)
    set(_chudware_link_pools ${CMAKE_JOB_POOLS})
  endif()
  list(APPEND _chudware_link_pools chudware_msvc_lto_link=1)
  list(REMOVE_DUPLICATES _chudware_link_pools)
  set_property(GLOBAL PROPERTY JOB_POOLS "${_chudware_link_pools}")
  set(CMAKE_JOB_POOL_LINK chudware_msvc_lto_link)
endif()
