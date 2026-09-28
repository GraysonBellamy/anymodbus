"""``MockServer`` — several :class:`MockSlave` s answering on one line.

A real RS-485 line carries many slaves; each ignores requests for the others.
:class:`MockServer` owns the stream, reads each request frame once, and routes
it by address, so simulated slaves can share a line without competing for
bytes. :meth:`MockSlave.serve` is a one-slave :class:`MockServer`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
import anyio.abc

from anymodbus._mock.slave import MockSlave, ServerException
from anymodbus._types import ExceptionCode, Framing
from anymodbus.exceptions import ChecksumError, ConfigurationError, FrameError, ProtocolError
from anymodbus.framing import get_framer
from anymodbus.pdu import encode_exception_response

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LOGGER = logging.getLogger("anymodbus.mock")

_BROADCAST_ADDRESS = 0

# t1.5 used to frame requests whose function code has no length table. The
# in-process pairs used in tests deliver bytes at once, so a short gap is
# enough; pass a longer one when serving a real, slow line.
_DEFAULT_INTER_CHAR_IDLE = 0.002

# The stream ended or failed: the client closed its end, or the port went away.
# On Windows a peer close can surface as a plain ``anyserial.SerialError``, an
# ``OSError``.
_STREAM_ENDED = (
    anyio.EndOfStream,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    OSError,
)


class MockServer:
    """Serve several :class:`MockSlave` s on one stream, routing requests by address.

    Each request frame is read once. A request whose checksum fails, or that
    cannot be read, is logged and dropped, like on a real line. A request for
    an address no slave has gets no reply. A broadcast (address 0) is applied
    by every slave and answered by none. Otherwise the addressed slave's
    :meth:`MockSlave.handle` produces the reply, which
    :meth:`MockSlave.send_response` sends with that slave's
    :class:`FaultPlan` applied.

    Requests are handled one at a time: while a slave's reply is delayed by
    its :class:`FaultPlan`, the next request waits, as it would on a
    half-duplex line.

    Args:
        *slaves: The slaves to serve. Each needs a distinct address and the
            server's framing.
        framing: The wire framing, :attr:`Framing.RTU` (default) or ``ASCII``.
        inter_char_idle: The t1.5 gap, in seconds, that ends an RTU request
            whose function code has no known length.
        on_request: Optional callback, called with ``(address, request_pdu)``
            for every request whose checksum is valid, whichever slave it is
            for, before it is answered. Useful for recording what went out on
            the line and when.
    """

    def __init__(
        self,
        *slaves: MockSlave,
        framing: Framing = Framing.RTU,
        inter_char_idle: float = _DEFAULT_INTER_CHAR_IDLE,
        on_request: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self._framing = framing
        self._inter_char_idle = inter_char_idle
        self._on_request = on_request
        self._slaves: dict[int, MockSlave] = {}
        for slave in slaves:
            self.add(slave)

    @property
    def framing(self) -> Framing:
        """The wire framing this server reads and writes."""
        return self._framing

    @property
    def slaves(self) -> Mapping[int, MockSlave]:
        """The slaves served, by address."""
        return dict(self._slaves)

    def add(self, slave: MockSlave) -> None:
        """Serve ``slave`` too.

        Raises:
            ConfigurationError: Another slave already has its address, or its
                framing differs from the server's.
        """
        if slave.address in self._slaves:
            msg = f"a slave with address {slave.address} is already served"
            raise ConfigurationError(msg)
        if slave.framing is not self._framing:
            msg = (
                f"slave {slave.address} uses {slave.framing.value} framing; "
                f"the server uses {self._framing.value}"
            )
            raise ConfigurationError(msg)
        self._slaves[slave.address] = slave

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        """Answer requests on ``stream`` until it closes or the task is cancelled."""
        framer = get_framer(self._framing)
        while True:
            try:
                address, request_pdu = await framer.read_request_adu(
                    stream, inter_char_idle=self._inter_char_idle
                )
            except ChecksumError:
                _LOGGER.warning("MockServer: checksum mismatch on request, dropping")
                continue
            except FrameError:
                _LOGGER.warning("MockServer: unreadable request frame, dropping")
                continue
            except _STREAM_ENDED:
                return
            try:
                await self._answer(stream, address, request_pdu)
            except _STREAM_ENDED:
                return

    async def _answer(self, stream: anyio.abc.ByteStream, address: int, pdu: bytes) -> None:
        if self._on_request is not None:
            self._on_request(address, pdu)
        if address == _BROADCAST_ADDRESS:
            # *serial §2.1*: every slave applies a broadcast; none replies.
            for each in self._slaves.values():
                _response_pdu(each, pdu)
            return
        slave = self._slaves.get(address)
        if slave is None:
            return
        response_pdu = _response_pdu(slave, pdu)
        if response_pdu is not None:
            await slave.send_response(stream, response_pdu)


def _response_pdu(slave: MockSlave, pdu: bytes) -> bytes | None:
    """Ask ``slave`` to handle ``pdu``, turning a refusal into an exception response.

    Returns ``None`` for a request with function code 0 (invalid per
    *app §4.1*), which has no exception response to give.
    """
    fc = pdu[0] & 0x7F
    try:
        return slave.handle(pdu)
    except ServerException as exc:
        code = int(exc.code)
    except ProtocolError:
        # A malformed request, or a quantity outside the spec range.
        code = ExceptionCode.ILLEGAL_DATA_VALUE
    if fc == 0:
        _LOGGER.warning("MockServer: request with function code 0, not answering")
        return None
    return encode_exception_response(fc, code)


__all__ = ["MockServer"]
