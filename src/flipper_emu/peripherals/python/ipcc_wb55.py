# IronPython 2.7 model of the STM32WB55 IPCC (inter-processor communication
# controller) -- the mailbox between this Cortex-M4 (CPU1) and the Cortex-M0+
# BLE core (CPU2).
#
# We do not run the closed-source CPU2 stack, but the firmware's start-up path
# talks to it extensively: it publishes a command by setting a channel bit in
# C1TOC2SR (0x0C) and then polls C2TOC1SR (0x1C) waiting for the core to answer.
# Unanswered, that handshake times out into a furi_check failure and a reset,
# which is what the bring-up log shows.
#
# This model makes the core answer instantly: every channel CPU1 sets in
# C1TOC2SR immediately appears set in C2TOC1SR and is cleared again when either
# side clears it.  The *payload* the firmware then reads out of the shared SRAM
# mailbox (FUS/stack version information) is not synthesised yet -- see
# docs/PERIPHERAL_COVERAGE.md.

_C1CR = 0x00
_C1MR = 0x04
_C1SCR = 0x08
_C1TOC2SR = 0x0C
_C2CR = 0x10
_C2MR = 0x14
_C2SCR = 0x18
_C2TOC1SR = 0x1C

#: Channels 1-6 are used by the firmware; the rest of the byte is reserved.
_CHANNEL_MASK = 0xFF

try:
    _ipcc_regs
except NameError:
    _ipcc_regs = {
        _C1CR: 0x00000000,
        _C1MR: 0xFFFFFFFF,
        _C1TOC2SR: 0x00000000,
        _C2CR: 0x00000000,
        _C2MR: 0xFFFFFFFF,
    }

if request.IsRead:
    if request.Offset == _C2TOC1SR:
        # "CPU2" acknowledges everything CPU1 has sent.
        request.Value = _ipcc_regs[_C1TOC2SR] & _CHANNEL_MASK
    else:
        request.Value = _ipcc_regs.get(request.Offset, 0) & 0xFFFFFFFF
else:
    offset = request.Offset
    if offset == _C1TOC2SR:
        _ipcc_regs[_C1TOC2SR] = request.Value & _CHANNEL_MASK
    elif offset == _C1SCR:
        # Writing a 1 clears the corresponding CPU1->CPU2 status bit.
        _ipcc_regs[_C1TOC2SR] = _ipcc_regs[_C1TOC2SR] & ~(request.Value & _CHANNEL_MASK)
    elif offset in (_C2TOC1SR, _C2SCR):
        pass  # CPU2-side registers, not driven by CPU1
    else:
        _ipcc_regs[offset] = request.Value & 0xFFFFFFFF
