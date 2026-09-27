# IronPython 2.7 model of the STM32WB55 RTC and its backup registers, placed at the
# address the firmware actually uses: base 0x40002000 with BKPxR starting at +0x50.
#
# Why this exists (docs/BRINGUP_LOG.md section 14): the 1.4.3 boot code reads the
# RTC boot mode and the RTC flags out of BKP1R (bits 19:16 hold the boot mode), like
# this - decoded from the release ELF, where every `furi_hal_rtc_*` accessor does
# `LDR r3, =0x40002000` followed by `LDR/STR [r3, #0x54]`:
#
#     furi_hal_rtc_get_boot_mode:  LDR r3,=0x40002000 ; LDR r0,[r3,#0x54] ; UBFX #16,#4
#     furi_hal_rtc_is_flag_set:    LDR r3,=0x40002000 ; LDR r3,[r3,#0x54] ; TST
#     furi_hal_rtc_set_boot_mode:  LDR r2,=0x40002000 ; BFI r3,r0,#16,#4 ; STR [r2,#0x54]
#
# That window was not mapped, so Renode answered every read with all ones.  All ones
# means "every flag set", the boot code's DFU flag test succeeds, and the firmware
# shows the "Update & Recovery Mode / DFU Started" splash instead of starting itself.
# With this model the window behaves like a real, freshly powered chip: registers
# round-trip what software writes, backup registers read back as zero (boot mode
# "normal", no flags) until software sets them, and the RTC status bits that no
# software write produces are reported ready.
#
# Known simplification: the backup registers are reset on a machine reset, whereas
# real ones are battery-backed and survive it.  Nothing in the boot path depends on
# that; persisting them (for e.g. the DFU flag the app sets before rebooting) is a
# refinement to make if a feature needs it.

_TR = 0x00
_DR = 0x04
_CR = 0x08
_ISR = 0x0C
_PRER = 0x10
_WUTR = 0x14
_ALRMAR = 0x18
_ALRMBR = 0x1C
_WPR = 0x24
_SSR = 0x28
_SHIFTR = 0x2C
_TSTR = 0x30
_TSDR = 0x34
_TSSSR = 0x38
_CALR = 0x3C
_ALRMASSR = 0x40
_ALRMBSSR = 0x44

#: First backup register (BKP0R).  BKP1R - the boot mode / flags register the boot
#: code cares about - is therefore at 0x54.
_BKP0R = 0x50
_BKP_COUNT = 32

#: ISR bits a running RTC always reports: alarm A/B write-ok, wakeup timer write-ok,
#: shift operation pending-free, initialisation done, shadow registers valid, and
#: calendar initialised.  The firmware polls these before it trusts the calendar.
_ISR_READY = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6)

try:
    _rtc_regs
except NameError:
    _rtc_regs = {}


def _rtc_reset_values():
    """Register contents right after reset: 2020-01-01 00:00:00, RTC running."""
    values = {
        _TR: 0x00000000,          # 00:00:00, no BCD fields set
        _DR: 0x00202101,          # year 20, weekday 4, month 1, day 1
        _CR: 0x00000000,
        _ISR: _ISR_READY,
        _PRER: 0x007F00FF,        # async 127 / sync 255, the usual reset values
        _WUTR: 0x0000FFFF,
        _ALRMAR: 0x00000000,
        _ALRMBR: 0x00000000,
        _WPR: 0x000000FF,         # write protection off, as after reset
        _SSR: 0x00000000,
        _SHIFTR: 0x00000000,
    }
    for index in range(_BKP_COUNT):
        values[_BKP0R + 4 * index] = 0x00000000
    return values


if request.IsInit:
    # Registration and every machine reset start from the reset values again.
    _rtc_regs.clear()
    _rtc_regs.update(_rtc_reset_values())
elif request.IsRead:
    if _BKP0R <= request.Offset < _BKP0R + 4 * _BKP_COUNT:
        # Backup registers are plain storage: whatever software wrote, or zero.
        request.Value = _rtc_regs.get(request.Offset, 0)
    else:
        # Anything the model does not know about reads as zero - never all ones.
        request.Value = _rtc_regs.get(request.Offset, 0)
else:
    if request.Offset == _ISR:
        # The RTC clears a status flag when software writes zero to that bit; the
        # "ready" bits stay set because the hardware keeps producing them.
        _rtc_regs[_ISR] = _ISR_READY | (_rtc_regs.get(_ISR, 0) & (request.Value & 0xFFFFFFFF))
    else:
        _rtc_regs[request.Offset] = request.Value & 0xFFFFFFFF
