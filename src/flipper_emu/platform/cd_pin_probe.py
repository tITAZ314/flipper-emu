# -*- coding: utf-8 -*-
"""Sample the card-detect pin the way the firmware reads it.

The storage stack only talks to a card when ``furi_hal_sd_is_present()`` is
true, and that function is one line:

    bool furi_hal_sd_is_present(void) { return !furi_hal_gpio_read(&gpio_sdcard_cd); }

``gpio_sdcard_cd`` is PC10 (``fh_furi_hal_resources.h``: ``SD_CD_GPIO_Port
GPIOC``, ``SD_CD_Pin LL_GPIO_PIN_10``), so everything hinges on what a read of
GPIOC IDR bit 10 returns.  The board file injects that pin high ("no card", the
pin is active low), yet recent runs boot with ``[I][StorageExt] card detected``
and then retry mounting ten times - so either the injection is losing a race
against the firmware's first read, or the firmware itself drives the pin low
(``fh_furi_hal_resources.c`` does ``furi_hal_gpio_write(&gpio_sdcard_cd, 0)``
right after configuring it as an input, and the SD power-reset path reconfigures
it as an output open-drain).

This probe answers which, by sampling GPIOC's registers over the first seconds of
emulation instead of guessing from the UART:

* ``IDR``   (0x10) - what the firmware's read returns; bit 10 low means "card
  inserted" to the driver.
* ``MODER`` (0x00) - 0b00 input, 0b01 output; tells whether the firmware took the
  pin over.
* ``OTYPER``(0x04) - open-drain is what ``furi_hal_sd_present_pin_set_low()`` sets.
* ``PUPDR`` (0x0C) - pull-up is what ``furi_hal_sd_presence_init()`` sets.
* ``ODR``   (0x14) - bit 10 / bit 12 show what the firmware is driving, and PC12
  is ``SD_CS``, the chip select the card model will have to watch.

Output: artifacts/cd-pin-probe.log
"""

# Read the peripheral window, not core space: core-space reads from this context
# returned the wrong window earlier in the project (documented as P20).
from System.Threading import Thread, ThreadStart
import time

OUT = r"C:\Users\gtttr\auraesp32\flipper-emu\artifacts\cd-pin-probe.log"

GPIOC = 0x48000800
REGISTERS = (
    ("MODER", 0x00),
    ("OTYPER", 0x04),
    ("PUPDR", 0x0C),
    ("IDR", 0x10),
    ("ODR", 0x14),
)

#: Seconds into the run to sample.  0.3 s is after the firmware's early resource
#: init (which writes the CD pin), 1.5 s and 2.5 s bracket the first mount
#: retry, which is what the power-reset path reconfigures the pin for.
SAMPLES = (0.3, 0.8, 1.5, 2.5, 5.0, 10.0)

#: To present a card, drive the pin from the *monitor*, not from here:
#:
#:     py -3.9 -m flipper_emu run --seconds 16 --renode-command "gpioPortC OnGPIO 10 false"
#:
#: (an injection sent this way lands after the board file's ``OnGPIO 10 true``, so it wins).
#: A probe cannot do it: Renode 1.17's IronPython exposes neither
#: ``self.Machine.GetPeripheral`` (measured: "'Machine' object has no attribute
#: 'GetPeripheral'") nor a usable ``self.TryFindPeripheralByName("gpioPortC")`` (measured: it
#: returns a name **string**, not the peripheral object, so ``.OnGPIO()`` fails with
#: "'str' object has no attribute 'OnGPIO'").  This probe therefore *measures* the pin - which
#: is what makes a card/no-card A/B trustworthy - and the injection stays on the command line.


def log(message):
    handle = open(OUT, "a")
    handle.write(str(message) + "\n")
    handle.close()
    print(str(message))


log("CD_PIN_PROBE_LOADED")


def describe(label, value):
    bits = "".join(
        "%s=%d " % (name, (value >> pin) & 1)
        for name, pin in (("PC10/CD", 10), ("PC11", 11), ("PC12/CS", 12))
    )
    return "%s 0x%08X  %s" % (label, value, bits)


def sample():
    bus = self.Machine.SystemBus
    previous = None
    for target in SAMPLES:
        time.sleep(max(0.0, target - (previous or 0.0)))
        previous = target
        try:
            values = {}
            for name, offset in REGISTERS:
                values[name] = bus.ReadDoubleWord(GPIOC + offset)
            log("t+%4.1fs  %s" % (target, describe("IDR", values["IDR"])))
            log("          %s" % describe("MODER", values["MODER"]))
            log("          %s" % describe("ODR", values["ODR"]))
            log("          %s  PUPDR=%08X OTYPER=%08X"
                % ("", values["PUPDR"], values["OTYPER"]))
        except Exception as exc:
            log("t+%4.1fs  FAILED %s" % (target, exc))
            return


Thread(ThreadStart(sample)).Start()