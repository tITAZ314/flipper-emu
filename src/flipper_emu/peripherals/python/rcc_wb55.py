# IronPython 2.7 model of the STM32WB55 RCC (reset and clock control).
#
# Why this exists: Renode's STM32L0_RCC model has the L0 register map, but the
# firmware does read-modify-write on the WB55 clock-enable registers (for example
# RCC_APB1ENR1 at 0x58) and verifies what it wrote.  With the L0 layout those
# accesses land on unimplemented offsets and read back as zero, so the firmware's
# own consistency check calls furi_check -> SYSRESETREQ.
#
# Three RCC behaviours matter here, all of them found by instrumenting the reset
# site rather than guessed (docs/BRINGUP_LOG.md section 8).  furi_hal_bus_enable():
#   1. asserts the peripheral is DISABLED in the enable register before enabling
#      it, so enable bits must round-trip;
#   2. asserts a reset is ASSERTED in a reset register before de-asserting it, so
#      the RSTR registers are read/write and hold their value until software
#      clears them (they are not self-clearing pulses);
#   3. resets the chip itself (NVIC_SystemReset) and expects a real reset
#      afterwards, so the register file must return to its reset values when the
#      machine resets - otherwise clock state leaks from one boot attempt into the
#      next and check (1) then fails forever.
#
# Register offsets come from RM0434 / the CMSIS device header (RCC_TypeDef).

# (enable bit, ready bit) pairs in RCC_CR; "ready" follows "enable" on read.
_CR_READY_PAIRS = ((8, 10), (16, 17), (24, 25), (26, 27))

_RCC_CR = 0x00
_RCC_ICSCR = 0x04
_RCC_CFGR = 0x08
_RCC_PLLCFGR = 0x0C
_RCC_CICR = 0x20
_RCC_AHB1RSTR = 0x28
_RCC_AHB2RSTR = 0x2C
_RCC_AHB3RSTR = 0x30
_RCC_APB1RSTR1 = 0x38
_RCC_APB1RSTR2 = 0x3C
_RCC_APB2RSTR = 0x40
_RCC_APB3RSTR = 0x44
_RCC_AHB1ENR = 0x48
_RCC_AHB2ENR = 0x4C
_RCC_AHB3ENR = 0x50
_RCC_APB1ENR1 = 0x58
_RCC_APB1ENR2 = 0x5C
_RCC_APB2ENR = 0x60
_RCC_CCIPR = 0x88
_RCC_BDCR = 0x90
_RCC_CSR = 0x94
_RCC_CRRCR = 0x98

#: Reset registers: read/write, and the firmware asserts on their values.  Their
#: *side effect* (resetting the target peripheral) is not modelled yet; that needs
#: a bit-to-peripheral map, which the CMSIS header provides
#: (RCC_APB1RSTR1_* bit definitions).
_RCC_RESET_REGISTERS = (
    _RCC_AHB1RSTR,
    _RCC_AHB2RSTR,
    _RCC_AHB3RSTR,
    _RCC_APB1RSTR1,
    _RCC_APB1RSTR2,
    _RCC_APB2RSTR,
    _RCC_APB3RSTR,
)


def _rcc_reset_values():
    """Register contents right after a system reset, as the firmware expects."""
    return {
        _RCC_CR: 0x00000100,  # HSI16 on; the ready bit comes from the read path
        _RCC_ICSCR: 0x00000000,
        _RCC_CFGR: 0x00000000,
        _RCC_CSR: 0x0C000000,  # PINRSTF | BORRSTF, as after a real reset
    }


def _rcc_read(offset):
    if offset == _RCC_CR:
        value = _rcc_regs.get(_RCC_CR, 0)
        for enable, ready in _CR_READY_PAIRS:
            if value & (1 << enable):
                value |= 1 << ready
            else:
                value &= ~(1 << ready)
        _rcc_regs[_RCC_CR] = value
        return value
    if offset == _RCC_CFGR:
        value = _rcc_regs.get(_RCC_CFGR, 0)
        # The clock switch completes instantly: SWS (bits 2-3) mirrors SW (0-1).
        value = (value & ~0x0C) | ((value & 0x03) << 2)
        _rcc_regs[_RCC_CFGR] = value
        return value
    if offset == _RCC_BDCR:
        value = _rcc_regs.get(_RCC_BDCR, 0)
        # 32.768 kHz oscillator: "ready" follows "enable" or "bypass" instantly.
        if value & 0x03:  # LSEON (bit 0) or LSEBYP (bit 1)
            value |= 1 << 1  # LSERDY
        else:
            value &= ~(1 << 1)
        _rcc_regs[_RCC_BDCR] = value
        return value
    if offset == _RCC_CSR:
        value = _rcc_regs.get(_RCC_CSR, 0)
        # LSION (bit 0) drives LSIRDY (bit 1): the firmware polls this while
        # bringing the RTC clock up, and resets the chip if it never comes up.
        if value & 0x01:
            value |= 1 << 1
        else:
            value &= ~(1 << 1)
        _rcc_regs[_RCC_CSR] = value
        return value
    if offset == _RCC_CRRCR:
        value = _rcc_regs.get(_RCC_CRRCR, 0)
        if value & 0x01:  # HSI48ON -> HSI48RDY
            value |= 1 << 1
        else:
            value &= ~(1 << 1)
        _rcc_regs[_RCC_CRRCR] = value
        return value
    return _rcc_regs.get(offset, 0)


try:
    _rcc_regs
except NameError:
    # First execution of this file: start from the reset state.
    _rcc_regs = _rcc_reset_values()

if request.IsInit:
    # Peripheral registration and every machine reset (including the firmware's
    # own NVIC_SystemReset) arrive here: start over from the reset values so no
    # clock or reset state survives into the next boot attempt.
    _rcc_regs.clear()
    _rcc_regs.update(_rcc_reset_values())
elif request.IsRead:
    request.Value = _rcc_read(request.Offset)
else:
    _rcc_offset = request.Offset
    if _rcc_offset == _RCC_CSR:
        # CSR mixes control and flag bits: LSION (bit 0) is plain read/write while
        # the reset flags (bits 24-31, RMVF first) are write-1-to-clear.  Clearing
        # everything that was written would also drop LSION, which the firmware
        # sets to start the LSI.
        _rcc_written = request.Value & 0xFFFFFFFF
        _rcc_current = _rcc_regs.get(_RCC_CSR, 0)
        _rcc_regs[_RCC_CSR] = (
            (_rcc_current & ~(_rcc_written & 0xFF000000) & ~0x01) | (_rcc_written & 0x01)
        )
    else:
        # Enable registers, reset registers and clock configuration are plain
        # read/write.  Reset bits deliberately persist until software clears them.
        _rcc_regs[_rcc_offset] = request.Value & 0xFFFFFFFF

