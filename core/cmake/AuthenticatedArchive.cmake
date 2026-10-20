# Managed SDKs are reconstructed from authenticated bytes on every configure.
# An extracted directory or a locally writable completion marker is not provenance.
include_guard(GLOBAL)

function(chudware_authenticated_archive url archive sha256 destination)
  if(NOT EXISTS "${archive}")
    get_filename_component(_parent "${archive}" DIRECTORY)
    file(MAKE_DIRECTORY "${_parent}")
    file(DOWNLOAD "${url}" "${archive}" TLS_VERIFY ON
         EXPECTED_HASH "SHA256=${sha256}" SHOW_PROGRESS STATUS _status)
    list(GET _status 0 _code)
    if(NOT _code EQUAL 0)
      file(REMOVE "${archive}")
      message(FATAL_ERROR "Authenticated archive download failed: ${_status}\n${url}")
    endif()
  endif()
  file(SHA256 "${archive}" _actual)
  if(NOT _actual STREQUAL sha256)
    message(FATAL_ERROR "Cached archive SHA-256 mismatch (refusing extraction): ${archive}")
  endif()
  # The caller supplies a dedicated managed directory, never a developer source root.
  file(REMOVE_RECURSE "${destination}")
  file(MAKE_DIRECTORY "${destination}")
  file(ARCHIVE_EXTRACT INPUT "${archive}" DESTINATION "${destination}")
endfunction()
