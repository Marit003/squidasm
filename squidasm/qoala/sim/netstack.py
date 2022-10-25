from __future__ import annotations

from typing import Dict, Generator, List, Optional

import netsquid as ns
from netqasm.sdk.build_epr import (
    SER_CREATE_IDX_NUMBER,
    SER_CREATE_IDX_TYPE,
    SER_RESPONSE_KEEP_IDX_BELL_STATE,
    SER_RESPONSE_KEEP_IDX_GOODNESS,
    SER_RESPONSE_KEEP_LEN,
    SER_RESPONSE_MEASURE_IDX_MEASUREMENT_BASIS,
    SER_RESPONSE_MEASURE_IDX_MEASUREMENT_OUTCOME,
    SER_RESPONSE_MEASURE_LEN,
)
from netsquid.components import QuantumProcessor
from netsquid.components.instructions import INSTR_ROT_X, INSTR_ROT_Z
from netsquid.components.qprogram import QuantumProgram
from netsquid.qubits.ketstates import BellIndex
from netsquid_magic.link_layer import MagicLinkLayerProtocolWithSignaling
from qlink_interface import (
    ReqCreateAndKeep,
    ReqCreateBase,
    ReqMeasureDirectly,
    ReqReceive,
    ResCreateAndKeep,
    ResMeasureDirectly,
)
from qlink_interface.interface import ReqRemoteStatePrep

from pydynaa import EventExpression
from squidasm.qoala.runtime.environment import GlobalNodeInfo, LocalEnvironment
from squidasm.qoala.sim.common import (
    ComponentProtocol,
    NetstackBreakpointCreateRequest,
    NetstackBreakpointReceiveRequest,
    NetstackCreateRequest,
    NetstackReceiveRequest,
    PhysicalQuantumMemory,
)
from squidasm.qoala.sim.constants import PI
from squidasm.qoala.sim.egp import EgpProtocol
from squidasm.qoala.sim.eprsocket import EprSocket
from squidasm.qoala.sim.memory import ProgramMemory, QuantumMemory, SharedMemory
from squidasm.qoala.sim.message import Message
from squidasm.qoala.sim.netstackcomp import NetstackComponent
from squidasm.qoala.sim.netstackinterface import NetstackInterface
from squidasm.qoala.sim.process import IqoalaProcess
from squidasm.qoala.sim.qdevice import AllocError, QDevice
from squidasm.qoala.sim.scheduler import Scheduler
from squidasm.qoala.sim.signals import (
    SIGNAL_MEMORY_FREED,
    SIGNAL_PEER_NSTK_MSG,
    SIGNAL_PROC_NSTK_MSG,
)


class Netstack(ComponentProtocol):
    """NetSquid protocol representing the QNodeOS network stack."""

    def __init__(
        self,
        comp: NetstackComponent,
        local_env: LocalEnvironment,
        scheduler: Scheduler,
        qdevice: QDevice,
    ) -> None:
        """Network stack protocol constructor. Typically created indirectly through
        constructing a `Qnos` instance.

        :param comp: NetSquid component representing the network stack
        :param qnos: `Qnos` protocol that owns this protocol
        """
        super().__init__(name=f"{comp.name}_protocol", comp=comp)

        # References to objects.
        self._comp = comp
        self._scheduler = scheduler
        self._local_env = local_env

        # Values are references to objects created elsewhere
        self._processes: Dict[int, IqoalaProcess] = {}  # program ID -> process

        # Owned objects.
        self._interface = NetstackInterface(comp, local_env, qdevice)
        self._egps: Dict[int, EgpProtocol] = {}

    def assign_ll_protocol(
        self, remote_id: int, prot: MagicLinkLayerProtocolWithSignaling
    ) -> None:
        """Set the magic link layer protocol that this network stack uses to produce
        entangled pairs with the remote node.

        :param prot: link layer protocol instance
        """
        self._egps[remote_id] = EgpProtocol(self._comp.node, prot)

    def remote_id_to_peer_name(self, remote_id: int) -> str:
        node_info = self._local_env.get_global_env().get_nodes()[remote_id]
        # TODO figure out why mypy does not like this
        return node_info.name  # type: ignore

    def open_epr_socket(self, pid: int, socket_id: int, remote_node_id: int) -> None:
        """Create a new EPR socket with the specified remote node.

        :param pid: ID of the application that creates this EPR socket
        :param socket_id: ID of the socket
        :param remote_node_id: ID of the remote node
        """
        if pid not in self._epr_sockets:
            self._epr_sockets[pid] = []
        self._epr_sockets[pid].append(EprSocket(socket_id, remote_node_id))

    def _send_processor_msg(self, msg: Message) -> None:
        """Send a message to the processor."""
        self._comp.processor_out_port.tx_output(msg)

    def _receive_processor_msg(self) -> Generator[EventExpression, None, Message]:
        """Receive a message from the processor. Block until there is at least one
        message."""
        return (yield from self._receive_msg("processor", SIGNAL_PROC_NSTK_MSG))

    def _send_peer_msg(self, peer: str, msg: Message) -> None:
        """Send a message to the network stack of the other node.

        NOTE: for now we assume there is only one other node, which is 'the' peer."""
        self._comp.peer_out_port(peer).tx_output(msg)

    def _receive_peer_msg(self, peer: str) -> Generator[EventExpression, None, Message]:
        """Receive a message from the network stack of the other node. Block until
        there is at least one message.

        NOTE: for now we assume there is only one other node, which is 'the' peer."""
        return (
            yield from self._receive_msg(
                f"peer_{peer}", f"{SIGNAL_PEER_NSTK_MSG}_{peer}"
            )
        )

    def get_shared_mem(self, pid: int) -> SharedMemory:
        prog_mem = self._interface.memmgr.get_program_memory(pid)
        return prog_mem.shared_mem

    def get_quantum_mem(self, pid: int) -> QuantumMemory:
        prog_mem = self._interface.memmgr.get_program_memory(pid)
        return prog_mem.quantum_mem

    def start(self) -> None:
        """Start this protocol. The NetSquid simulator will call and yield on the
        `run` method. Also start the underlying EGP protocol."""
        super().start()
        for egp in self._egps.values():
            egp.start()

    def stop(self) -> None:
        """Stop this protocol. The NetSquid simulator will stop calling `run`.
        Also stop the underlying EGP protocol."""
        for egp in self._egps.values():
            egp.stop()
        super().stop()

    def _read_request_args_array(self, pid: int, array_addr: int) -> List[int]:
        shared_mem = self.get_shared_mem(pid)
        # TODO figure out why mypy does not like this
        return shared_mem.get_array(array_addr)  # type: ignore

    def _construct_request(self, remote_id: int, args: List[int]) -> ReqCreateBase:
        """Construct a link layer request from application request info.

        :param remote_id: ID of remote node
        :param args: NetQASM array elements from the arguments array specified by the
            application
        :return: link layer request object
        """
        typ = args[SER_CREATE_IDX_TYPE]
        assert typ is not None
        num_pairs = args[SER_CREATE_IDX_NUMBER]
        assert num_pairs is not None

        # TODO
        MINIMUM_FIDELITY = 0.99

        if typ == 0:
            request = ReqCreateAndKeep(
                remote_node_id=remote_id,
                number=num_pairs,
                minimum_fidelity=MINIMUM_FIDELITY,
            )
        elif typ == 1:
            request = ReqMeasureDirectly(
                remote_node_id=remote_id,
                number=num_pairs,
                minimum_fidelity=MINIMUM_FIDELITY,
            )
        elif typ == 2:
            request = ReqRemoteStatePrep(
                remote_node_id=remote_id,
                number=num_pairs,
                minimum_fidelity=MINIMUM_FIDELITY,
            )
        else:
            raise ValueError(f"Unsupported create type {typ}")
        return request

    @property
    def physical_memory(self) -> PhysicalQuantumMemory:
        return self._qnos.physical_memory

    @property
    def qdevice(self) -> QuantumProcessor:
        return self._comp.node.qdevice

    def find_epr_socket(
        self, pid: int, sck_id: int, rem_id: int
    ) -> Optional[EprSocket]:
        """Get a specific EPR socket or None if it does not exist.

        :param pid: app ID
        :param sck_id: EPR socket ID
        :param rem_id: remote node ID
        :return: the corresponding EPR socket or None if it does not exist
        """
        if pid not in self._epr_sockets:
            return None
        for sck in self._epr_sockets[pid]:
            if sck.socket_id == sck_id and sck.remote_id == rem_id:
                return sck
        return None

    def handle_create_ck_request(
        self, req: NetstackCreateRequest, request: ReqCreateAndKeep
    ) -> Generator[EventExpression, None, None]:
        """Handle a Create and Keep request as the initiator/creator, until all
        pairs have been created.

        This method uses the EGP protocol to create and measure EPR pairs with
        the remote node. It will fully complete the request before returning. If
        the pair created by the EGP protocol is another Bell state than Phi+,
        local gates are applied to do a correction, such that the final
        delivered pair is always Phi+.

        The method can however yield (i.e. give control back to the simulator
        scheduler) in the following cases: - no communication qubit is
        available; this method will resume when a
          SIGNAL_MEMORY_FREED is given (currently only the processor can do
          this)
        - when waiting for the EGP protocol to produce the next pair; this
          method resumes when the pair is delivered
        - a Bell correction gate is applied

        This method does not return anything. This method has the side effect
        that NetQASM array value are written to.

        :param req: application request info (app ID and NetQASM array IDs)
        :param request: link layer request object
        """
        num_pairs = request.number

        shared_mem = self.get_shared_mem(req.pid)
        quantum_mem = self.get_quantum_mem(req.pid)
        qubit_ids = shared_mem.get_array(req.qubit_array_addr)

        self._logger.info(f"putting CK request to EGP for {num_pairs} pairs")
        self._logger.info(f"qubit IDs specified by application: {qubit_ids}")
        self._logger.info(f"splitting request into {num_pairs} 1-pair requests")
        request.number = 1

        start_time = ns.sim_time()

        for pair_index in range(num_pairs):
            self._logger.info(f"trying to allocate comm qubit for pair {pair_index}")
            while True:
                try:
                    phys_id = self.physical_memory.allocate_comm()
                    break
                except AllocError:
                    self._logger.info("no comm qubit available, waiting...")

                    # Wait for a signal indicating the communication qubit might be free
                    # again.
                    yield self.await_signal(
                        sender=self._qnos.processor, signal_label=SIGNAL_MEMORY_FREED
                    )
                    self._logger.info(
                        "a 'free' happened, trying again to allocate comm qubit..."
                    )

            # Put the request to the EGP.
            self._logger.info(f"putting CK request for pair {pair_index}")
            self._egps[req.remote_node_id].put(request)

            # Wait for a signal from the EGP.
            self._logger.info(f"waiting for result for pair {pair_index}")
            yield self.await_signal(
                sender=self._egps[req.remote_node_id],
                signal_label=ResCreateAndKeep.__name__,
            )
            # Get the EGP's result.
            result: ResCreateAndKeep = self._egps[req.remote_node_id].get_signal_result(
                ResCreateAndKeep.__name__, receiver=self
            )
            self._logger.info(f"got result for pair {pair_index}: {result}")

            # Bell state corrections. Resulting state is always Phi+ (i.e. B00).
            if result.bell_state == BellIndex.B00:
                pass
            elif result.bell_state == BellIndex.B01:
                prog = QuantumProgram()
                prog.apply(INSTR_ROT_X, qubit_indices=[0], angle=PI)
                yield self.qdevice.execute_program(prog)
            elif result.bell_state == BellIndex.B10:
                prog = QuantumProgram()
                prog.apply(INSTR_ROT_Z, qubit_indices=[0], angle=PI)
                yield self.qdevice.execute_program(prog)
            elif result.bell_state == BellIndex.B11:
                prog = QuantumProgram()
                prog.apply(INSTR_ROT_X, qubit_indices=[0], angle=PI)
                prog.apply(INSTR_ROT_Z, qubit_indices=[0], angle=PI)
                yield self.qdevice.execute_program(prog)

            virt_id = shared_mem.get_array_value(req.qubit_array_addr, pair_index)
            quantum_mem.map_virt_id(virt_id, phys_id)
            self._logger.info(
                f"mapping virtual qubit {virt_id} to physical qubit {phys_id}"
            )

            gen_duration_ns_float = ns.sim_time() - start_time
            gen_duration_us_int = int(gen_duration_ns_float / 1000)
            self._logger.info(f"gen duration (us): {gen_duration_us_int}")

            # Length of response array slice for a single pair.
            slice_len = SER_RESPONSE_KEEP_LEN

            # Populate results array.
            for i in range(slice_len):
                # Write -1 to unused array elements.
                value = -1

                # Write corresponding result value to the other array elements.
                if i == SER_RESPONSE_KEEP_IDX_GOODNESS:
                    value = gen_duration_us_int
                if i == SER_RESPONSE_KEEP_IDX_BELL_STATE:
                    value = result.bell_state

                # Calculate array element location.
                arr_index = slice_len * pair_index + i

                shared_mem.set_array_value(req.result_array_addr, arr_index, value)
            self._logger.debug(
                f"wrote to @{req.result_array_addr}[{slice_len * pair_index}:"
                f"{slice_len * pair_index + slice_len}] for app ID {req.pid}"
            )
            self._send_processor_msg(Message(content="wrote to array"))

    def handle_create_md_request(
        self, req: NetstackCreateRequest, request: ReqMeasureDirectly
    ) -> Generator[EventExpression, None, None]:
        """Handle a Create and Measure request as the initiator/creator, until all
        pairs have been created and measured.

        This method uses the EGP protocol to create EPR pairs with the remote node.
        It will fully complete the request before returning.

        No Bell state corrections are done. This means that application code should
        use the result information to check, for each pair, the generated Bell state
        and possibly post-process the measurement outcomes.

        The method can yield (i.e. give control back to the simulator scheduler) in
        the following cases:
        - no communication qubit is available; this method will resume when a
          SIGNAL_MEMORY_FREED is given (currently only the processor can do this)
        - when waiting for the EGP protocol to produce the next pair; this method
          resumes when the pair is delivered

        This method does not return anything.
        This method has the side effect that NetQASM array value are written to.

        :param req: application request info (app ID and NetQASM array IDs)
        :param request: link layer request object
        """

        # Put the reqeust to the EGP.
        self._egps[req.remote_node_id].put(request)

        results: List[ResMeasureDirectly] = []

        # Wait for all pairs to be created. For each pair, the EGP sends a separate
        # signal that is awaited here. Only after the last pair, we write the results
        # to the array. This is done since the whole request (i.e. all pairs) is
        # expected to finish in a short time anyway. However, writing results for a
        # pair as soon as they are done may be implemented in the future.
        for _ in range(request.number):
            phys_id = self.physical_memory.allocate_comm()

            yield self.await_signal(
                sender=self._egps[req.remote_node_id],
                signal_label=ResMeasureDirectly.__name__,
            )
            result: ResMeasureDirectly = self._egps[
                req.remote_node_id
            ].get_signal_result(ResMeasureDirectly.__name__, receiver=self)
            self._logger.debug(f"bell index: {result.bell_state}")
            results.append(result)
            self.physical_memory.free(phys_id)

        shared_mem = self.get_shared_mem(req.pid)

        # Length of response array slice for a single pair.
        slice_len = SER_RESPONSE_MEASURE_LEN

        # Populate results array.
        for pair_index in range(request.number):
            result = results[pair_index]

            for i in range(slice_len):
                # Write -1 to unused array elements.
                value = -1

                # Write corresponding result value to the other array elements.
                if i == SER_RESPONSE_MEASURE_IDX_MEASUREMENT_OUTCOME:
                    value = result.measurement_outcome
                elif i == SER_RESPONSE_MEASURE_IDX_MEASUREMENT_BASIS:
                    value = result.measurement_basis.value
                elif i == SER_RESPONSE_KEEP_IDX_BELL_STATE:
                    value = result.bell_state.value

                # Calculate array element location.
                arr_index = slice_len * pair_index + i

                shared_mem.set_array_value(req.result_array_addr, arr_index, value)

        self._send_processor_msg(Message(content="wrote to array"))

    def handle_create_request(
        self, req: NetstackCreateRequest
    ) -> Generator[EventExpression, None, None]:
        """Issue a request to create entanglement with a remote node.

        :param req: request info
        """

        # EPR socket should exist.
        epr_socket = self.find_epr_socket(
            req.pid, req.epr_socket_id, req.remote_node_id
        )
        assert epr_socket is not None

        # Read request parameters from the corresponding NetQASM array.
        args = self._read_request_args_array(req.pid, req.arg_array_addr)

        # Create the link layer request object.
        request = self._construct_request(req.remote_node_id, args)

        # Send it to the receiver node and wait for an acknowledgement.
        peer = self.remote_id_to_peer_name(epr_socket.remote_id)
        self._send_peer_msg(peer, Message(content=request))
        peer_msg = yield from self._receive_peer_msg(peer)
        self._logger.debug(f"received peer msg: {peer_msg}")

        # Handle the request.
        if isinstance(request, ReqCreateAndKeep):
            yield from self.handle_create_ck_request(req, request)
        elif isinstance(request, ReqMeasureDirectly):
            yield from self.handle_create_md_request(req, request)

    def handle_receive_ck_request(
        self, req: NetstackReceiveRequest, request: ReqCreateAndKeep
    ) -> Generator[EventExpression, None, None]:
        """Handle a Create and Keep request as the receiver, until all pairs have
        been created.

        This method uses the EGP protocol to create EPR pairs with the remote
        node. It will fully complete the request before returning.

        If the pair created by the EGP protocol is another Bell state than Phi+,
        it is assumed that the *other* node applies local gates such that the
        final delivered pair is always Phi+.

        The method can yield (i.e. give control back to the simulator scheduler)
        in the following cases: - no communication qubit is available; this
        method will resume when a
          SIGNAL_MEMORY_FREED is given (currently only the processor can do
          this)
        - when waiting for the EGP protocol to produce the next pair; this
          method resumes when the pair is delivered

        This method does not return anything. This method has the side effect
        that NetQASM array value are written to.

        :param req: application request info (app ID and NetQASM array IDs)
        :param request: link layer request object
        """
        assert isinstance(request, ReqCreateAndKeep)

        num_pairs = request.number

        self._logger.info(f"putting CK request to EGP for {num_pairs} pairs")
        self._logger.info(f"splitting request into {num_pairs} 1-pair requests")

        start_time = ns.sim_time()

        for pair_index in range(num_pairs):
            self._logger.info(f"trying to allocate comm qubit for pair {pair_index}")
            while True:
                try:
                    phys_id = self.physical_memory.allocate_comm()
                    break
                except AllocError:
                    self._logger.info("no comm qubit available, waiting...")

                    # Wait for a signal indicating the communication qubit might be free
                    # again.
                    yield self.await_signal(
                        sender=self._qnos.processor, signal_label=SIGNAL_MEMORY_FREED
                    )
                    self._logger.info(
                        "a 'free' happened, trying again to allocate comm qubit..."
                    )

            # Put the request to the EGP.
            self._logger.info(f"putting CK request for pair {pair_index}")
            self._egps[req.remote_node_id].put(
                ReqReceive(remote_node_id=req.remote_node_id)
            )
            self._logger.info(f"waiting for result for pair {pair_index}")

            # Wait for a signal from the EGP.
            yield self.await_signal(
                sender=self._egps[req.remote_node_id],
                signal_label=ResCreateAndKeep.__name__,
            )
            # Get the EGP's result.
            result: ResCreateAndKeep = self._egps[req.remote_node_id].get_signal_result(
                ResCreateAndKeep.__name__, receiver=self
            )
            self._logger.info(f"got result for pair {pair_index}: {result}")

            shared_mem = self.get_shared_mem(req.pid)
            quantum_mem = self.get_quantum_mem(req.pid)
            virt_id = shared_mem.get_array_value(req.qubit_array_addr, pair_index)
            quantum_mem.map_virt_id(virt_id, phys_id)
            self._logger.info(
                f"mapping virtual qubit {virt_id} to physical qubit {phys_id}"
            )

            gen_duration_ns_float = ns.sim_time() - start_time
            gen_duration_us_int = int(gen_duration_ns_float / 1000)
            self._logger.info(f"gen duration (us): {gen_duration_us_int}")

            # Length of response array slice for a single pair.
            slice_len = SER_RESPONSE_KEEP_LEN

            for i in range(slice_len):
                # Write -1 to unused array elements.
                value = -1

                # Write corresponding result value to the other array elements.
                if i == SER_RESPONSE_KEEP_IDX_GOODNESS:
                    value = gen_duration_us_int
                if i == SER_RESPONSE_KEEP_IDX_BELL_STATE:
                    value = result.bell_state.value

                # Calculate array element location.
                arr_index = slice_len * pair_index + i

                shared_mem.set_array_value(req.result_array_addr, arr_index, value)
            self._logger.debug(
                f"wrote to @{req.result_array_addr}[{slice_len * pair_index}:"
                f"{slice_len * pair_index + slice_len}] for app ID {req.pid}"
            )
            self._send_processor_msg(Message(content="wrote to array"))

    def handle_receive_md_request(
        self, req: NetstackReceiveRequest, request: ReqMeasureDirectly
    ) -> Generator[EventExpression, None, None]:
        """Handle a Create and Measure request as the receiver, until all
        pairs have been created and measured.

        This method uses the EGP protocol to create EPR pairs with the remote node.
        It will fully complete the request before returning.

        No Bell state corrections are done. This means that application code should
        use the result information to check, for each pair, the generated Bell state
        and possibly post-process the measurement outcomes.

        The method can yield (i.e. give control back to the simulator scheduler)
        in the following cases: - no communication qubit is available; this
        method will resume when a
          SIGNAL_MEMORY_FREED is given (currently only the processor can do
          this)
        - when waiting for the EGP protocol to produce the next pair; this
          method resumes when the pair is delivered

        This method does not return anything. This method has the side effect
        that NetQASM array value are written to.

        :param req: application request info (app ID and NetQASM array IDs)
        :param request: link layer request object
        """
        assert isinstance(request, ReqMeasureDirectly)

        self._egps[req.remote_node_id].put(
            ReqReceive(remote_node_id=req.remote_node_id)
        )

        results: List[ResMeasureDirectly] = []

        for _ in range(request.number):
            phys_id = self._interface.qdevice.allocate_comm()

            yield self.await_signal(
                sender=self._egps[req.remote_node_id],
                signal_label=ResMeasureDirectly.__name__,
            )
            result: ResMeasureDirectly = self._egps[
                req.remote_node_id
            ].get_signal_result(ResMeasureDirectly.__name__, receiver=self)
            results.append(result)

            self.physical_memory.free(phys_id)

        shared_mem = self.get_shared_mem(req.pid)

        # Length of response array slice for a single pair.
        slice_len = SER_RESPONSE_MEASURE_LEN

        # Populate results array.
        for pair_index in range(request.number):
            result = results[pair_index]

            for i in range(slice_len):
                # Write -1 to unused array elements.
                value = -1

                # Write corresponding result value to the other array elements.
                if i == SER_RESPONSE_MEASURE_IDX_MEASUREMENT_OUTCOME:
                    value = result.measurement_outcome
                elif i == SER_RESPONSE_MEASURE_IDX_MEASUREMENT_BASIS:
                    value = result.measurement_basis.value
                elif i == SER_RESPONSE_KEEP_IDX_BELL_STATE:
                    value = result.bell_state.value

                # Calculate array element location.
                arr_index = slice_len * pair_index + i

                shared_mem.set_array_value(req.result_array_addr, arr_index, value)

            self._send_processor_msg(Message(content="wrote to array"))

    def handle_receive_request(
        self, req: NetstackReceiveRequest
    ) -> Generator[EventExpression, None, None]:
        """Issue a request to receive entanglement from a remote node.

        :param req: request info
        """

        # EPR socket should exist.
        epr_socket = self.find_epr_socket(
            req.pid, req.epr_socket_id, req.remote_node_id
        )
        assert epr_socket is not None

        # Wait for the network stack in the remote node to get the corresponding
        # 'create' request from its local application and send it to us.
        # NOTE: we do not check if the request from the other node matches our own
        # request. Also, we simply block until synchronizing with the other node,
        # and then fully handle the request. There is no support for queueing
        # and/or interleaving multiple different requests.
        peer = self.remote_id_to_peer_name(epr_socket.remote_id)
        msg = yield from self._receive_peer_msg(peer)
        create_request = msg.content
        self._logger.debug(f"received {create_request} from peer")

        # Acknowledge to the remote node that we received the request and we will
        # start handling it.
        self._logger.debug("sending 'ready' to peer")
        self._send_peer_msg(peer, Message(content="ready"))

        # Handle the request, based on the type that we now know because of the
        # other node.
        if isinstance(create_request, ReqCreateAndKeep):
            yield from self.handle_receive_ck_request(req, create_request)
        elif isinstance(create_request, ReqMeasureDirectly):
            yield from self.handle_receive_md_request(req, create_request)

    def handle_breakpoint_create_request(
        self,
    ) -> Generator[EventExpression, None, None]:
        # Synchronize with the remote node.

        self._logger.warning("USING EPR SOCKET (0, 0) FOR BREAKPOINT!!!!")
        peer = self.remote_id_to_peer_name(self._epr_sockets[0][0].remote_id)

        self._send_peer_msg(peer, Message(content="breakpoint start"))
        response = yield from self._receive_peer_msg(peer)
        assert response.content == "breakpoint start"

        # Remote node is now ready. Notify the processor.
        self._send_processor_msg(Message(content="breakpoint ready"))

        # Wait for the processor to finish handling the breakpoint.
        processor_msg = yield from self._receive_processor_msg()
        assert processor_msg.content == "breakpoint end"

        # Tell the remote node that the breakpoint has finished.
        self._send_peer_msg(peer, Message(content="breakpoint end"))

        # Wait for the remote node to have finsihed as well.
        response = yield from self._receive_peer_msg(peer)
        assert response.content == "breakpoint end"

        # Notify the processor that we are done.
        self._send_processor_msg(Message(content="breakpoint finished"))

    def handle_breakpoint_receive_request(
        self,
    ) -> Generator[EventExpression, None, None]:
        # Synchronize with the remote node.

        self._logger.warning("USING EPR SOCKET (0, 0) FOR BREAKPOINT!!!!")
        peer = self.remote_id_to_peer_name(self._epr_sockets[0][0].remote_id)

        msg = yield from self._receive_peer_msg(peer)
        assert msg.content == "breakpoint start"
        self._send_peer_msg(peer, Message(content="breakpoint start"))

        # Notify the processor we are ready to handle the breakpoint.
        self._send_processor_msg(Message(content="breakpoint ready"))

        # Wait for the processor to finish handling the breakpoint.
        processor_msg = yield from self._receive_processor_msg()
        assert processor_msg.content == "breakpoint end"

        # Wait for the remote node to finish and tell it we are finished as well.
        peer_msg = yield from self._receive_peer_msg(peer)
        assert peer_msg.content == "breakpoint end"
        self._send_peer_msg(peer, Message(content="breakpoint end"))

        # Notify the processor that we are done.
        self._send_processor_msg(Message(content="breakpoint finished"))

    def run(self) -> Generator[EventExpression, None, None]:
        # Loop forever acting on messages from the processor.
        while True:
            # Wait for a new message.
            msg = yield from self._receive_processor_msg()
            self._logger.debug(f"received new msg from processor: {msg}")
            request = msg.content

            # Handle it.
            if isinstance(request, NetstackCreateRequest):
                yield from self.handle_create_request(msg)
                self._logger.debug("create request done")
            elif isinstance(request, NetstackReceiveRequest):
                yield from self.handle_receive_request(msg)
                self._logger.debug("receive request done")
            elif isinstance(request, NetstackBreakpointCreateRequest):
                yield from self.handle_breakpoint_create_request()
                self._logger.debug("breakpoint create request done")
            elif isinstance(request, NetstackBreakpointReceiveRequest):
                yield from self.handle_breakpoint_receive_request()
                self._logger.debug("breakpoint receive request done")

    def add_process(self, process: IqoalaProcess) -> None:
        self._processes[process.prog_instance.pid] = process
