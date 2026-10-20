# Embed maintained C# sources; installed lib never reads a developer checkout.
# Byte-array initializers keep exact file bytes and avoid MSVC C2026 string-literal limits.
set(_firmware_bus_dir "${CMAKE_CURRENT_SOURCE_DIR}/firmware/renode")
set(_firmware_bus_files BusRuntime TimedSTM32I2C TimedSTM32UART TimedSTM32SPI TimedSTM32CAN)
set(_firmware_bus_symbols kFirmwareBusRuntime kFirmwareTimedI2C kFirmwareTimedUART kFirmwareTimedSPI kFirmwareTimedCAN)
set(_firmware_bus_header "#pragma once\nnamespace chudware::firmware {\n")
foreach(_index RANGE 0 4)
    list(GET _firmware_bus_files ${_index} _file)
    list(GET _firmware_bus_symbols ${_index} _symbol)
    set(_path "${_firmware_bus_dir}/${_file}.cs")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_path}")
    file(READ "${_path}" _source HEX)
    string(REGEX REPLACE "(................................)" "\\1\n" _source "${_source}")
    string(REGEX REPLACE "([0-9a-f][0-9a-f])" "'\\\\x\\1'," _source "${_source}")
    string(APPEND _firmware_bus_header "inline constexpr char ${_symbol}[] = {\n${_source}'\\0'};\n")
endforeach()
string(APPEND _firmware_bus_header "}\n")
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")
# Preserve the header timestamp when source bytes are unchanged.
file(CONFIGURE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/generated/firmware_bus_sources.h"
     CONTENT "${_firmware_bus_header}" @ONLY NEWLINE_STYLE LF)
