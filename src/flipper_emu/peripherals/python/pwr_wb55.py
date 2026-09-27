# IronPython 2.7 model of the STM32WB55 PWR (power control).
#
# The firmware touches this block very early, for two reasons found by tracing
# what it reads (docs/BRINGUP_LOG.md section 9):
#
#   1. PWR_CR1.DBP: write access to the backup domain (RCC_BDCR, RTC) stays locked
#      until software sets it.  The firmware sets it before starting the RTC
#      clock; a stub that always returned zero made the emulated chip look like it
#      never unlocked the backup domain.
#   2. PWR_CR4.C2BOOT: request to boot the CPU2 (BLE) core.  We do not run that
#      core - the firmware's IPCC polling is answered by the ipcc_wb55 stub - but
#      the bit is remembered so the request/acknowledge sequence looks sane.
#
# The register file round-trips what software writes, which is what the firmware
# verifies.  Hardware status bits that no software write produces (regulator
# ready, wakeup flags) are not synthesised yet.

_PWR_CR1 = 0x00
_PWR_CR2 = 0x04
_PWR_CR3 = 0x08
_PWR_CR4 = 0x0C
_PWR_SR1 = 0x10
_PWR_SR2 = 0x14
_PWR_SCR = 0x18
_PWR_PUCRA = 0x20
_PWR_PDCRA = 0x24
_PWR_PUCRB = 0x28
_PWR_PDCRB = 0x2C
_PWR_PUCRC = 0x30
_PWR_PDCRC = 0x34
_PWR_PUCRD = 0x38
_PWR_PDCRD = 0x3C
_PWR_PUCRE = 0x40
_PWR_PDCRE = 0x44
_PWR_PUCRH = 0x48
_PWR_PDCRH = 0x4C
_PWR_C2CR1 = 0x50
_PWR_C2CR2 = 0x54
_PWR_C2CR3 = 0x58

#: PWR_CR4.C2BOOT - the CPU2 boot request the firmware issues before talking to
#: the radio stack over IPCC.
_C2BOOT = 1 << 15

#: Status flags that are write-1-to-clear (PWR_SCR).
_FLAG_REGISTERS = (_PWR_SCR,)

try:
    _pwr_regs
except NameError:
    _pwr_regs = {}


def _pwr_reset_values():
    """Register contents right after a system reset."""
    return {
        _PWR_CR1: 0x00000200,  # VOS range 1, as the firmware expects on WB55
        _PWR_CR2: 0x00000000,
        _PWR_CR3: 0x00000000,
        _PWR_CR4: 0x00000000,
    }


if request.IsInit:
    # Registration and every machine reset (including the firmware's own
    # NVIC_SystemReset) start from the reset values again.
    _pwr_regs.clear()
    _pwr_regs.update(_pwr_reset_values())
elif request.IsRead:
    _pwr_value = _pwr_regs.get(request.Offset, 0)
    if _pwr_value & _C2BOOT:
        # CPU2 is "booted" as far as the register interface is concerned; the
        # radio stack handshake itself is the IPCC stub's job.
        pass
    request.Value = _pwr_value
else:
    if request.Offset in _FLAG_REGISTERS:
        # Write one to clear the corresponding status flag.
        _pwr_regs[request.Offset] = _pwr_regs.get(request.Offset, 0) & ~(request.Value & 0xFFFFFFFF)
    else:
        _pwr_regs[request.Offset] = request.Value & 0xFFFFFFFF
