# Segmented test suites (scripts/test_suites.json).
#
# chudware_apply_test_suites(<tree>) runs after every add_test() in the calling
# directory. For each suite listed under "<tree>" in the manifest it:
#   - labels the suite's registered tests (`ctest -L '^<suite>$'`), and
#   - adds a `tests-<suite>` target that builds only those tests' executables.
# Manifest entries whose test is not registered in this configuration (OCCT off,
# other platform, developer-only) are skipped. A registered test in no suite, a
# test in two suites, an unknown suite name, or a test with no build target is a
# configure error, so a new test cannot silently drop out of every suite.
#
# The default build target is a test's own executable: `test_<name>`, else `<name>`.
# Tests driven by a script or another binary list their targets in "<tree>_targets".
function(chudware_apply_test_suites tree)
  set(_manifest "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../scripts/test_suites.json")
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_manifest}")
  file(READ "${_manifest}" _json)

  string(JSON _known GET "${_json}" suites)
  string(JSON _tree GET "${_json}" "${tree}")
  string(JSON _overrides ERROR_VARIABLE _no_overrides GET "${_json}" "${tree}_targets")
  get_property(_registered DIRECTORY PROPERTY TESTS)

  # ctest refuses to start outside a container or CI runner (scripts/testbox_guard.py):
  # host runs collide with the developer's live app. Use scripts/testbox.py.
  find_program(CHUDWARE_GUARD_PYTHON NAMES python3 python REQUIRED)
  get_filename_component(_guard "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../scripts/testbox_guard.py"
                         ABSOLUTE)
  file(WRITE "${CMAKE_BINARY_DIR}/CTestCustom.cmake"
       "set(CTEST_CUSTOM_PRE_TEST \"\\\"${CHUDWARE_GUARD_PYTHON}\\\" \\\"${_guard}\\\"\")\n")

  # Every declared suite gets a target in every tree, so `tests-<suite>` builds
  # (possibly nothing) in any configured tree. A combined root build configures
  # core and host into one project; both trees then add to the same target.
  string(JSON _known_count LENGTH "${_known}")
  math(EXPR _known_last "${_known_count} - 1")
  foreach(_i RANGE ${_known_last})
    string(JSON _suite MEMBER "${_known}" ${_i})
    if(NOT TARGET tests-${_suite})
      add_custom_target(tests-${_suite} COMMENT "Building the ${_suite} test suite")
    endif()
  endforeach()

  set(_assigned "")
  string(JSON _suite_count LENGTH "${_tree}")
  if(_suite_count EQUAL 0)
    message(FATAL_ERROR "${_manifest}: \"${tree}\" lists no suites")
  endif()
  math(EXPR _suite_last "${_suite_count} - 1")
  foreach(_i RANGE ${_suite_last})
    string(JSON _suite MEMBER "${_tree}" ${_i})
    string(JSON _description ERROR_VARIABLE _unknown GET "${_known}" "${_suite}")
    if(_unknown)
      message(FATAL_ERROR "${_manifest}: \"${tree}\" uses suite \"${_suite}\", "
                          "which is not declared under \"suites\"")
    endif()
    string(JSON _tests GET "${_tree}" "${_suite}")
    string(JSON _test_count LENGTH "${_tests}")
    if(_test_count EQUAL 0)
      continue()
    endif()
    math(EXPR _test_last "${_test_count} - 1")
    foreach(_j RANGE ${_test_last})
      string(JSON _test GET "${_tests}" ${_j})
      if(_test IN_LIST _assigned)
        message(FATAL_ERROR "${_manifest}: test \"${_test}\" is listed in more than one "
                            "\"${tree}\" suite")
      endif()
      list(APPEND _assigned "${_test}")
      if(NOT _test IN_LIST _registered)
        continue()
      endif()
      set_property(TEST "${_test}" APPEND PROPERTY LABELS "${_suite}")

      set(_targets "")
      if(NOT _no_overrides)
        string(JSON _listed ERROR_VARIABLE _not_listed GET "${_overrides}" "${_test}")
        if(NOT _not_listed)
          string(JSON _target_count LENGTH "${_listed}")
          math(EXPR _target_last "${_target_count} - 1")
          foreach(_k RANGE ${_target_last})
            string(JSON _target GET "${_listed}" ${_k})
            list(APPEND _targets "${_target}")
          endforeach()
        endif()
      endif()
      if(NOT _targets)
        if(TARGET "test_${_test}")
          set(_targets "test_${_test}")
        elseif(TARGET "${_test}")
          set(_targets "${_test}")
        endif()
      endif()
      if(NOT _targets)
        message(FATAL_ERROR "${_manifest}: no build target for test \"${_test}\"; "
                            "list it under \"${tree}_targets\"")
      endif()
      foreach(_target IN LISTS _targets)
        if(TARGET "${_target}")
          add_dependencies(tests-${_suite} "${_target}")
        endif()
      endforeach()
    endforeach()
  endforeach()

  set(_unassigned "")
  foreach(_test IN LISTS _registered)
    if(NOT _test IN_LIST _assigned)
      list(APPEND _unassigned "${_test}")
    endif()
  endforeach()
  if(_unassigned)
    list(JOIN _unassigned ", " _unassigned)
    message(FATAL_ERROR "${_manifest}: tests registered in ${CMAKE_CURRENT_SOURCE_DIR} "
                        "belong to no \"${tree}\" suite: ${_unassigned}")
  endif()
endfunction()
