import logging
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional, Set, Tuple, Union

from netqasm.lang.encoding import RegisterName
from netsquid.components.component import Component, Port
from netsquid.protocols import Protocol

from pydynaa import EventExpression
from squidasm.qoala.sim.logging import LogManager


class PortListener(Protocol):
    def __init__(self, port: Port, signal_label: str) -> None:
        self._buffer: List[bytes] = []
        self._port: Port = port
        self._signal_label = signal_label
        self.add_signal(signal_label)

    @property
    def buffer(self) -> List[bytes]:
        return self._buffer

    def run(self) -> Generator[EventExpression, None, None]:
        while True:
            # Wait for an event saying that there is new input.
            yield self.await_port_input(self._port)

            counter = 0
            # Read all inputs and count them.
            while True:
                input = self._port.rx_input()
                if input is None:
                    break
                self._buffer += input.items
                counter += 1
            # If there are n inputs, there have been n events, but we yielded only
            # on one of them so far. "Flush" these n-1 additional events:
            while counter > 1:
                yield self.await_port_input(self._port)
                counter -= 1

            # Only after having yielded on all current events, we can schedule a
            # notification event, so that its reactor can handle all inputs at once.
            self.send_signal(self._signal_label)


class ComponentProtocol(Protocol):
    def __init__(self, name: str, comp: Component) -> None:
        super().__init__(name)
        self._listeners: Dict[str, PortListener] = {}
        self._logger: logging.Logger = LogManager.get_stack_logger(
            f"{self.__class__.__name__}({comp.name})"
        )

    def add_listener(self, name, listener: PortListener) -> None:
        self._listeners[name] = listener

    def _receive_msg(
        self, listener_name: str, wake_up_signal: str
    ) -> Generator[EventExpression, None, str]:
        listener = self._listeners[listener_name]
        if len(listener.buffer) == 0:
            yield self.await_signal(sender=listener, signal_label=wake_up_signal)
        return listener.buffer.pop(0)

    def start(self) -> None:
        super().start()
        for listener in self._listeners.values():
            listener.start()

    def stop(self) -> None:
        for listener in self._listeners.values():
            listener.stop()
        super().stop()


@dataclass
class NetstackCreateRequest:
    pid: int
    remote_node_id: int
    epr_socket_id: int
    qubit_array_addr: int
    arg_array_addr: int
    result_array_addr: int


@dataclass
class NetstackReceiveRequest:
    pid: int
    remote_node_id: int
    epr_socket_id: int
    qubit_array_addr: int
    result_array_addr: int


@dataclass
class NetstackBreakpointCreateRequest:
    pid: int


@dataclass
class NetstackBreakpointReceiveRequest:
    pid: int


class AllocError(Exception):
    pass


class PhysicalQuantumMemory:
    def __init__(self, qubit_count: int) -> None:
        self._qubit_count = qubit_count
        self._allocated_ids: Set[int] = set()
        self._comm_qubit_ids: Set[int] = {i for i in range(qubit_count)}

    @property
    def qubit_count(self) -> int:
        return self._qubit_count

    @property
    def comm_qubit_count(self) -> int:
        return len(self._comm_qubit_ids)

    def allocate(self) -> int:
        """Allocate a qubit (communcation or memory)."""
        for i in range(self._qubit_count):
            if i not in self._allocated_ids:
                self._allocated_ids.add(i)
                return i
        raise AllocError("No more qubits available")

    def allocate_comm(self) -> int:
        """Allocate a communication qubit."""
        for i in range(self._qubit_count):
            if i not in self._allocated_ids and i in self._comm_qubit_ids:
                self._allocated_ids.add(i)
                return i
        raise AllocError("No more comm qubits available")

    def allocate_mem(self) -> int:
        """Allocate a memory qubit."""
        for i in range(self._qubit_count):
            if i not in self._allocated_ids and i not in self._comm_qubit_ids:
                self._allocated_ids.add(i)
                return i
        raise AllocError("No more mem qubits available")

    def free(self, id: int) -> None:
        self._allocated_ids.remove(id)

    def is_allocated(self, id: int) -> bool:
        return id in self._allocated_ids

    def clear(self) -> None:
        self._allocated_ids = {}


class NVPhysicalQuantumMemory(PhysicalQuantumMemory):
    def __init__(self, qubit_count: int) -> None:
        super().__init__(qubit_count)
        self._comm_qubit_ids: Set[int] = {0}
