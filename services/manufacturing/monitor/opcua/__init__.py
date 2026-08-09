"""Typed read-only OPC UA support.

Scope: UA-TCP HEL/ACK, SecurityPolicy None, anonymous sessions, Browse,
BrowseNext, Read, chunked-response reassembly, and orderly closure.  Outgoing
requests are single-chunk and refused when they exceed the negotiated buffer.
Subscriptions, discovery, encryption, certificates, methods, node management,
and historical services are outside the implemented surface.  Importing this
package creates no network activity.
"""

from .client import UaClientError, UaProtocolError, UaServiceError, UaTcpClient
from .machine_tools import (
    MachineToolRows,
    MachineToolShape,
    NodeReading,
    SUPPORTED_MACHINE_TOOL_SHAPES,
    map_machine_tools,
)

__all__ = (
    "MachineToolRows",
    "MachineToolShape",
    "NodeReading",
    "SUPPORTED_MACHINE_TOOL_SHAPES",
    "UaClientError",
    "UaProtocolError",
    "UaServiceError",
    "UaTcpClient",
    "map_machine_tools",
)
