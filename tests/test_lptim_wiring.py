"""The LPTIM wiring: our model, at the rate the firmware's idle timer assumes.

`furi_hal_idle_timer.h`/`furi_hal_os.c` drive LPTIM1 one-shot through its *compare*
match with interrupts masked, then poll `ISR.CMPM`/`ISR.ARRM` and clear the pending
IRQ. Renode's stock `Timers.STM32L0_LpTimer` fires on its own limit instead (and says
so in the log), which delivered an interrupt the firmware has no handler for. These
tests pin the platform to our model, at 32768 Hz, with the reset line it observes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.fw import memmap  # noqa: E402
from flipper_emu.platform import generate  # noqa: E402


class LptimWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repl = Path(generate.write_platform(cls.tmp.name, None)["repl"]).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_map_uses_our_model_for_both_timers(self) -> None:
        for name in ("LPTIM1", "LPTIM2"):
            entry = next(entry for entry in memmap.PERIPHERALS if entry.name == name)
            self.assertEqual(memmap.LPTIM_MODEL, entry.model)

    def test_platform_instantiates_our_model(self) -> None:
        self.assertIn("lptim1: Antmicro.Renode.Peripherals.FlipperEmu.LptimWb55", self.repl)
        self.assertIn("lptim2: Antmicro.Renode.Peripherals.FlipperEmu.LptimWb55", self.repl)
        self.assertNotIn("Timers.STM32L0_LpTimer", self.repl)

    def test_platform_uses_the_firmwares_idle_timer_clock(self) -> None:
        """FURI_HAL_IDLE_TIMER_CLK_HZ = 32768 (LSE), not an APB rate."""
        block = self.repl.split("lptim1: ", 1)[1].split("\n", 8)
        self.assertIn("frequency: 32768", "\n".join(block))
        block2 = self.repl.split("lptim2: ", 1)[1].split("\n", 8)
        self.assertIn("frequency: 32768", "\n".join(block2))

    def test_each_timer_is_told_which_reset_line_it_owns(self) -> None:
        self.assertIn("index: 1", self.repl.split("lptim1: ", 1)[1].split("lptim2: ", 1)[0])
        self.assertIn("index: 2", self.repl.split("lptim2: ", 1)[1].split("\n\n", 1)[0])

    def test_irq_numbers_match_the_vector_table(self) -> None:
        """LPTIM1 = 47, LPTIM2 = 48 (slot 16 + n owns IRQ n)."""
        self.assertEqual((("IRQ", 47),), generate.PERIPHERAL_NVIC_IRQS["LPTIM1"])
        self.assertEqual((("IRQ", 48),), generate.PERIPHERAL_NVIC_IRQS["LPTIM2"])
        self.assertIn("IRQ -> nvic@47", self.repl)
        self.assertIn("IRQ -> nvic@48", self.repl)

    def test_reset_line_addresses_are_the_ones_the_model_watches(self) -> None:
        """APB1RSTR1 (0x58000038) bit 31 / APB1RSTR2 (0x5800003C) bit 5."""
        source = (ROOT / "src" / "flipper_emu" / "peripherals" / "cs" / "LptimWb55.cs").read_text(
            encoding="utf-8"
        )
        self.assertIn("Apb1ResetRegister1 = 0x58000038", source)
        self.assertIn("Apb1ResetRegister2 = 0x5800003C", source)
        self.assertIn("1UL << 31", source)
        self.assertIn("1UL << 5", source)


if __name__ == "__main__":
    unittest.main()
