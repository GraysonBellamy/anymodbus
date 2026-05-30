"""The framing strategy seam: :class:`Framer` + shared response interpretation.

A :class:`Framer` captures the two operations that are *genuinely*
framing-specific — encode a PDU into a wire ADU, and read **one raw frame**
back. Everything that is framing-*agnostic* about a response — the function-code
semantics (exception-bit split, FC mismatch, the illegal ``fc == 0``) — lives in
the single shared :func:`interpret_response_pdu`, which :class:`anymodbus.Bus`
calls for every framing. This keeps RTU / ASCII / (future) TCP from ever
diverging on response interpretation.

See the 0.2 implementation plan §3 / decision D1 for the rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, assert_never

from anymodbus._types import Framing
from anymodbus.exceptions import (
    ProtocolError,
    UnexpectedResponseError,
    code_to_exception,
)

if TYPE_CHECKING:
    import anyio.abc

    from anymodbus._types import FunctionCode

# *app §4.1*: "Function code '0' is not valid." Shared by the RTU framer (where
# a zero FC is an unframeable-length guard) and :func:`interpret_response_pdu`
# (where, for delimiter-framed ASCII, any byte can be framed so the check must
# live at interpretation). Single constant so both raise the identical message.
_FC_ZERO_MSG = "slave returned function code 0 (invalid per app §4.1)"

# High bit set on the FC byte marks an exception response (*app §7*).
_EXCEPTION_BIT = 0x80


class Framer(Protocol):
    """Strategy that turns PDUs into wire ADUs and reads ONE raw frame back.

    Framers are stateless; :func:`get_framer` returns a shared module singleton
    per framing.
    """

    def encode_adu(self, *, slave_address: int, pdu: bytes) -> bytes:
        """Wrap a PDU into a full transmission ADU (RTU: +CRC; ASCII: ``:``..LRC..CRLF)."""
        ...

    async def read_adu(
        self,
        stream: anyio.abc.ByteStream,
        *,
        expected_slave_address: int,
        inter_char_idle: float,
    ) -> tuple[int, bytes]:
        """Read ONE frame addressed to ``expected_slave_address``; return ``(slave, pdu)``.

        The trailing checksum is verified and stray frames (addressed to other
        slaves) are skipped before returning. The returned ``pdu`` is the
        function-code byte plus body and MAY carry the exception bit
        (``fc & 0x80``) — interpreting that is :func:`interpret_response_pdu`'s
        job, not the framer's.

        The caller (``Bus._one_txn``) wraps this in
        ``anyio.fail_after(request_timeout)``; the framer does NOT enforce its
        own deadline. ``inter_char_idle`` is RTU rx-timing (stray drain +
        unknown-FC gap fallback); delimiter-/length-prefixed framings ignore it.

        Raises (framing-level only): :class:`FrameError` (truncated /
        un-de-hexable / no terminator), :class:`CRCError` / :class:`LRCError`
        (checksum failed on a frame addressed to us), :class:`ProtocolError`
        (``fc == 0``, unframeable), :class:`ModbusUnsupportedFunctionError`
        (RTU: response FC has no length table).
        """
        ...


def get_framer(framing: Framing) -> Framer:
    """Return the shared singleton :class:`Framer` for ``framing``.

    Imports the concrete framer module lazily (and only the one requested) to
    avoid an import cycle with :class:`anymodbus.Bus` and to keep RTU-only
    sessions from importing the ASCII framer.
    """
    # Lazy imports break the bus/framer import cycle and keep RTU-only sessions
    # from importing the ASCII framer.
    if framing is Framing.RTU:
        from anymodbus.framer import RTU_FRAMER  # noqa: PLC0415

        return RTU_FRAMER
    if framing is Framing.ASCII:
        from anymodbus.framer_ascii import ASCII_FRAMER  # noqa: PLC0415

        return ASCII_FRAMER
    assert_never(framing)  # exhaustive: Framing has only RTU / ASCII


def interpret_response_pdu(
    *,
    slave_address: int,
    pdu: bytes,
    expected_function_code: FunctionCode,
) -> tuple[int, bytes]:
    """Validate a checksum-verified response PDU, framing-agnostically.

    Returns ``(slave_address, pdu)`` for a normal, matching response; otherwise
    raises the matching domain error. This is the single source of truth for
    function-code semantics, so RTU / ASCII / (future) TCP can never diverge.

    Args:
        slave_address: The slave address the framer read off the wire.
        pdu: Function-code byte + body, checksum already stripped/verified by
            the framer. May carry the exception bit.
        expected_function_code: The FC we sent, used to disambiguate exception
            responses and surface mismatches.

    Raises:
        ProtocolError: Function code 0 (invalid per *app §4.1*).
        UnexpectedResponseError: Echoed FC did not match the request.
        ModbusExceptionResponse: Slave returned an exception (FC | 0x80); the
            concrete subclass depends on the exception code.
    """
    fc = pdu[0]
    expected_fc = int(expected_function_code)
    if fc == 0:
        raise ProtocolError(_FC_ZERO_MSG)
    if fc & _EXCEPTION_BIT:
        base_fc = fc & 0x7F
        if base_fc != expected_fc:
            msg = f"exception response echoes fc {base_fc:#04x}, expected {expected_fc:#04x}"
            raise UnexpectedResponseError(msg)
        raise code_to_exception(function_code=base_fc, exception_code=pdu[1])
    if fc != expected_fc:
        msg = f"slave returned fc {fc:#04x}, expected {expected_fc:#04x}"
        raise UnexpectedResponseError(msg)
    return slave_address, pdu


__all__ = ["Framer", "get_framer", "interpret_response_pdu"]
