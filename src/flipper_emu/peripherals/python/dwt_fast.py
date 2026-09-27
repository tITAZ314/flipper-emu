# IronPython 2.7 model of the Cortex-M4 DWT cycle counter.
#
# furi_hal_cortex_delay_us() busy-waits on DWT->CYCCNT (offset 0x04), and the
# battery-gauge probe asks for 4-second delays.  On real silicon CYCCNT ticks
# with the core; emulating that honestly would spin hundreds of millions of loop
# iterations before boot could continue.  The firmware only ever compares
# CYCCNT *deltas*, so advancing it by one large step per read keeps every guard
# delay logically instantaneous while leaving virtual time and every other
# peripheral untouched.
#
# The script body is re-executed on every access (that is Renode's Python
# peripheral contract), while the module scope persists -- hence the guarded
# initialisation below.

_DWT_STEP = 1 << 26  # covers a 1-second delay per read at 64 MHz

try:
    _dwt_cyccnt
except NameError:
    _dwt_cyccnt = 0

if request.IsRead:
    if request.Offset == 0x04:  # CYCCNT
        _dwt_cyccnt = (_dwt_cyccnt + _DWT_STEP) & 0xFFFFFFFF
        request.Value = _dwt_cyccnt
    elif request.Offset == 0x00:  # CTRL: cycle counter present and enabled
        request.Value = 0x40000001
    elif request.Offset == 0x08:  # CPI overhead, harmless
        request.Value = 0
    else:
        request.Value = 0
else:
    if request.Offset == 0x04:
        _dwt_cyccnt = request.Value & 0xFFFFFFFF
