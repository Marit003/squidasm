from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Generator, List, Optional

from netqasm.backend.messages import (
    InitNewAppMessage,
    Message,
    OpenEPRSocketMessage,
    StopAppMessage,
    SubroutineMessage,
    deserialize_host_msg,
)
from netqasm.lang.instr import flavour
from netqasm.lang.parsing import deserialize as deser_subroutine
from netqasm.lang.subroutine import Subroutine
from netsquid.components.component import Component, Port
from netsquid.nodes import Node

from pydynaa import EventExpression
from squidasm.qoala.sim.common import (
    ComponentProtocol,
    PhysicalQuantumMemory,
    PortListener,
)
from squidasm.qoala.sim.memory import ProgramMemory
from squidasm.qoala.sim.netstack import Netstack, NetstackComponent
from squidasm.qoala.sim.signals import SIGNAL_HOST_HAND_MSG, SIGNAL_PROC_HAND_MSG

if TYPE_CHECKING:
    from squidasm.qoala.sim.qnos import Qnos, QnosComponent
    from squidasm.qoala.sim.qnosprocessor import ProcessorComponent


class HandlerComponent(Component):
    """NetSquid component representing a QNodeOS handler.

    Subcomponent of a QnosComponent.

    The "QnodeOS handler" represents the combination of the following components
    within QNodeOS:
     - interface with the Host
     - scheduler

    Has communications ports with
     - the processor component of this QNodeOS
     - the Host component on this node

    This is a static container for handler-related components and ports.
    Behavior of a QNodeOS handler is modeled in the `Handler` class,
    which is a subclass of `Protocol`.
    """

    def __init__(self, node: Node) -> None:
        super().__init__(f"{node.name}_handler")
        self._node = node
        self.add_ports(["proc_out", "proc_in"])
        self.add_ports(["host_out", "host_in"])

    @property
    def processor_in_port(self) -> Port:
        return self.ports["proc_in"]

    @property
    def processor_out_port(self) -> Port:
        return self.ports["proc_out"]

    @property
    def host_in_port(self) -> Port:
        return self.ports["host_in"]

    @property
    def host_out_port(self) -> Port:
        return self.ports["host_out"]

    @property
    def node(self) -> Node:
        return self._node

    @property
    def processor_comp(self) -> ProcessorComponent:
        return self.supercomponent.processor

    @property
    def netstack_comp(self) -> NetstackComponent:
        return self.supercomponent.netstack

    @property
    def qnos_comp(self) -> QnosComponent:
        return self.supercomponent


class RunningApp:
    def __init__(self, pid: int) -> None:
        self._id = pid
        self._pending_subroutines: List[Subroutine] = []

    def add_subroutine(self, subroutine: Subroutine) -> None:
        self._pending_subroutines.append(subroutine)

    def next_subroutine(self) -> Optional[Subroutine]:
        if len(self._pending_subroutines) > 0:
            return self._pending_subroutines.pop()
        return None

    @property
    def id(self) -> int:
        return self._id


class Handler(ComponentProtocol):
    """NetSquid protocol representing a QNodeOS handler."""

    def __init__(
        self, comp: HandlerComponent, qnos: Qnos, qdevice_type: Optional[str] = "nv"
    ) -> None:
        """Processor handler constructor. Typically created indirectly through
        constructing a `Qnos` instance.

        :param comp: NetSquid component representing the handler
        :param qnos: `Qnos` protocol that owns this protocol
        """
        super().__init__(name=f"{comp.name}_protocol", comp=comp)
        self._comp = comp
        self._qnos = qnos

        self.add_listener(
            "host",
            PortListener(self._comp.ports["host_in"], SIGNAL_HOST_HAND_MSG),
        )
        self.add_listener(
            "processor",
            PortListener(self._comp.ports["proc_in"], SIGNAL_PROC_HAND_MSG),
        )

        # Number of applications that were handled so far. Used as a unique ID for
        # the next application.
        self._app_counter = 0

        # Currently active (running or waiting) applications.
        self._applications: Dict[int, RunningApp] = {}

        # Whether the quantum memory for applications should be reset when the
        # application finishes.
        self._should_clear_memory: bool = True

        # Set the expected flavour such that Host messages are deserialized correctly.
        if qdevice_type == "nv":
            self._flavour: Optional[flavour.Flavour] = flavour.NVFlavour()
        elif qdevice_type == "generic":
            self._flavour: Optional[flavour.Flavour] = flavour.VanillaFlavour()
        else:
            raise ValueError

    @property
    def program_memories(self) -> Dict[int, ProgramMemory]:
        return self._qnos.program_memories

    @property
    def physical_memory(self) -> PhysicalQuantumMemory:
        return self._qnos.physical_memory

    @property
    def should_clear_memory(self) -> bool:
        return self._should_clear_memory

    @should_clear_memory.setter
    def should_clear_memory(self, value: bool) -> None:
        self._should_clear_memory = value

    @property
    def flavour(self) -> Optional[flavour.Flavour]:
        return self._flavour

    @flavour.setter
    def flavour(self, flavour: Optional[flavour.Flavour]) -> None:
        self._flavour = flavour

    def _send_host_msg(self, msg: Any) -> None:
        self._comp.host_out_port.tx_output(msg)

    def _receive_host_msg(self) -> Generator[EventExpression, None, str]:
        return (yield from self._receive_msg("host", SIGNAL_HOST_HAND_MSG))

    def _send_processor_msg(self, msg: str) -> None:
        self._comp.processor_out_port.tx_output(msg)

    def _receive_processor_msg(self) -> Generator[EventExpression, None, str]:
        return (yield from self._receive_msg("processor", SIGNAL_PROC_HAND_MSG))

    @property
    def qnos(self) -> Qnos:
        return self._qnos

    @property
    def netstack(self) -> Netstack:
        return self.qnos.netstack

    def _next_app(self) -> Optional[RunningApp]:
        for app in self._applications.values():
            return app
        return None

    def init_new_app(self, pid: int) -> int:
        self._app_counter += 1
        self.program_memories[pid] = ProgramMemory(
            pid, self.physical_memory.qubit_count
        )
        self._applications[pid] = RunningApp(pid)
        self._logger.debug(f"registered app with ID {pid}")
        return pid

    def open_epr_socket(self, pid: int, socket_id: int, remote_id: int) -> None:
        self._logger.debug(f"Opening EPR socket ({socket_id}, {remote_id})")
        self.netstack.open_epr_socket(pid, socket_id, remote_id)

    def add_subroutine(self, pid: int, subroutine: Subroutine) -> None:
        self._applications[pid].add_subroutine(subroutine)

    def _deserialize_subroutine(self, msg: SubroutineMessage) -> Subroutine:
        # return deser_subroutine(msg.subroutine, flavour=flavour.NVFlavour())
        return deser_subroutine(msg.subroutine, flavour=self._flavour)

    def clear_application(self, pid: int) -> None:
        for virt_id, phys_id in self.program_memories[pid].qubit_mapping.items():
            self.program_memories[pid].unmap_virt_id(virt_id)
            if phys_id is not None:
                self.physical_memory.free(phys_id)
        self.program_memories.pop(pid)

    def stop_application(self, pid: int) -> None:
        self._logger.debug(f"stopping application with ID {pid}")
        if self.should_clear_memory:
            self._logger.debug(f"clearing qubits for application with ID {pid}")
            self.clear_application(pid)
            self._applications.pop(pid)
        else:
            self._logger.info(f"NOT clearing qubits for application with ID {pid}")

    def assign_processor(
        self, pid: int, subroutine: Subroutine
    ) -> Generator[EventExpression, None, ProgramMemory]:
        """Tell the processor to execute a subroutine and wait for it to finish.

        :param pid: ID of the application this subroutine is for
        :param subroutine: the subroutine to execute
        """
        self._send_processor_msg(subroutine)
        result = yield from self._receive_processor_msg()
        assert result == "subroutine done"
        self._logger.debug(f"result: {result}")
        prog_mem = self.program_memories[pid]
        return prog_mem

    def msg_from_host(self, msg: Message) -> None:
        """Handle a deserialized message from the Host."""
        if isinstance(msg, InitNewAppMessage):
            pid = self.init_new_app(msg.max_qubits)
            self._send_host_msg(pid)
        elif isinstance(msg, OpenEPRSocketMessage):
            self.open_epr_socket(msg.pid, msg.epr_socket_id, msg.remote_node_id)
        elif isinstance(msg, SubroutineMessage):
            subroutine = self._deserialize_subroutine(msg)
            self.add_subroutine(subroutine.pid, subroutine)
        elif isinstance(msg, StopAppMessage):
            self.stop_application(msg.pid)

    def run(self) -> Generator[EventExpression, None, None]:
        """Run this protocol. Automatically called by NetSquid during simulation."""

        # Loop forever acting on messages from the Host.
        while True:
            # Wait for a new message from the Host.
            raw_host_msg = yield from self._receive_host_msg()
            self._logger.debug(f"received new msg from host: {raw_host_msg}")
            msg = deserialize_host_msg(raw_host_msg)

            # Handle the message. This updates the handler's state and may e.g.
            # add a pending subroutine for an application.
            self.msg_from_host(msg)

            # Get the next application that needs work.
            app = self._next_app()
            if app is not None:
                # Flush all pending subroutines for this app.
                while True:
                    subrt = app.next_subroutine()
                    if subrt is None:
                        break
                    prog_mem = yield from self.assign_processor(app.id, subrt)
                    self._send_host_msg(prog_mem)
