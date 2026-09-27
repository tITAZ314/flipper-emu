"""Tests for the bus/peripheral dispatch layer.

The generated Renode platform is only as sound as the map it comes from, so
these tests assert the invariants that make the dispatch unambiguous: no
overlapping ranges, peripherals that a model can be constructed for, every
region either modelled or explicitly stubbed, and a platform file that mentions
every peripheral exactly once.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flipper_emu.fw import memmap  # noqa: E402
from flipper_emu.platform import generate  # noqa: E402


class AddressMapTests(unittest.TestCase):
    def test_no_overlapping_ranges(self) -> None:
        self.assertEqual(memmap.overlap_issues(), [])

    def test_peripheral_registration_is_constructible(self) -> None:
        """Renode needs power-of-two, base-aligned blocks for peripherals."""
        self.assertEqual(memmap.alignment_issues(memmap.PERIPHERALS), [])

    def test_every_region_has_a_kind_renode_understands(self) -> None:
        for region in memmap.MEMORY_REGIONS:
            self.assertIn(region.kind, generate.MEMORY_KINDS)
            self.assertGreater(region.size, 0)

    def test_every_peripheral_is_modelled_or_stubbed(self) -> None:
        for entry in memmap.PERIPHERALS:
            self.assertTrue(
                entry.is_modelled or entry.stub,
                "%s has neither a model nor a stub" % entry.name,
            )

    def test_flash_split_matches_the_official_package(self) -> None:
        """The application must fit below the wireless-stack reservation."""
        self.assertEqual(memmap.FLASH_APP_SIZE, 768 * 1024)
        self.assertEqual(memmap.FLASH_STACK_BASE, 0x080C0000)
        self.assertEqual(memmap.FLASH_APP_END, memmap.FLASH_STACK_BASE)

    def test_lookup_helpers(self) -> None:
        self.assertEqual(memmap.peripheral_for(0x58000000).name, "RCC")
        self.assertEqual(memmap.peripheral_for(0x40013004).name, "SPI1")
        self.assertEqual(memmap.region_for(0x08001234).name, "flash")
        self.assertIsNone(memmap.peripheral_for(0x00000000))

    def test_stub_and_model_counts_are_reported(self) -> None:
        self.assertEqual(
            len(memmap.modelled_peripherals()) + len(memmap.stubbed_peripherals()),
            len(memmap.PERIPHERALS),
        )
        self.assertGreater(len(memmap.modelled_peripherals()), 15)


class PlatformGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repl = generate.render_repl()

    def test_hsem_is_our_model_with_the_boot_chain_stand_in(self) -> None:
        """The HSEM stub used to answer every read with 0.

        `furi_hal_bt_init` verifies (without taking) that CPU1 holds the CLK48
        semaphore and furi_crashes otherwise, so the block needs a model *and* a
        stand-in for the boot loader this emulator does not run. See
        ``peripherals/cs/HsemWb55.cs``.
        """
        self.assertIn(
            "hsem: Antmicro.Renode.Peripherals.FlipperEmu.HsemWb55", self.repl
        )
        self.assertIn("numberOfSemaphores: 32", self.repl)
        self.assertIn("bootChainLockMask: 0x20", self.repl)
        # Measured: the application waits for the RNG semaphore to be *held* and
        # writes its core id afterwards, so the write releases it (`false`) and the
        # emulator must hand that semaphore back to CPU1 (no CPU2 arbitration here).
        self.assertIn("coreIdWriteClaims: false", self.repl)
        self.assertIn("arbitratedSemaphores: 0x1", self.repl)

    def test_rng_is_modelled(self) -> None:
        """The stub returned 1 for every read, so every random value was constant."""
        self.assertIn("rng: Antmicro.Renode.Peripherals.FlipperEmu.RngWb55", self.repl)
        self.assertIn("seed: 0x12345678", self.repl)

    def test_every_gpio_port_is_our_input_capable_model(self) -> None:
        """Renode's STM32_GPIOPort cannot make an input pin read high.

        That model reads every input as 0 and ignores injected levels, and the
        1.4.3 bootloader chooses DFU/recovery/normal boot from *polled* button
        levels, so the board would always show its DFU splash. See
        ``peripherals/cs/GpioWb55Port.cs``.
        """
        self.assertNotIn("GPIOPort.STM32_GPIOPort", self.repl)
        for name in ("A", "B", "C", "D", "E", "H"):
            self.assertIn(
                "gpioPort%s: Antmicro.Renode.Peripherals.FlipperEmu.GpioWb55Port" % name,
                self.repl,
            )
            self.assertIn('portName: "%s"' % name, self.repl)

    def test_gpio_pins_still_reach_exti(self) -> None:
        """Every pin still reaches its EXTI line, with GPIOB's display pins split.

        Renode refuses to use one output as a source twice, so the two pins that
        feed the panel model are named individually and the rest of that port
        keeps a range. The other five ports take the plain full range.
        """
        self.assertEqual(self.repl.count("[0-15] -> exti@[0-15]"), 5)
        for line in ("[2-15] -> exti@[2-15]", "1 -> st7567@0", "0 -> st7567@1"):
            self.assertIn(line, self.repl)

    def test_resc_injects_released_button_levels(self) -> None:
        """The board inputs the boot code polls are driven after `start`."""
        resc = generate.render_resc("platform/x.repl", "artifacts/flash.img")
        for line in (
            "gpioPortB OnGPIO 10 true",  # UP, active low
            "gpioPortB OnGPIO 11 true",  # LEFT: the bootloader's DFU entry
            "gpioPortB OnGPIO 12 true",
            "gpioPortC OnGPIO 6 true",
            "gpioPortC OnGPIO 13 true",
            "gpioPortH OnGPIO 3 false",  # OK is active high
            "gpioPortC OnGPIO 10 true",  # SD card detect
        ):
            self.assertIn(line, resc)
        # `start` resets peripheral state, so the levels have to come after it.
        self.assertLess(resc.index("\nstart\n"), resc.index("gpioPortB OnGPIO 10 true"))

    def test_flash_is_aliased_to_address_zero(self) -> None:
        """The Cortex-M model fetches its initial SP/PC from address 0."""
        self.assertIn("sysbus 0x00000000;", self.repl)

    def test_every_peripheral_appears_once(self) -> None:
        for entry in memmap.PERIPHERALS:
            name = generate._renode_name(entry)
            self.assertEqual(
                self.repl.count("\n%s: " % name) + self.repl.startswith("%s: " % name),
                1,
                "%s declared more than once" % name,
            )

    def test_exti_connections_use_index_lists(self) -> None:
        """Renode rejects `[0] -> nvic@6`; range connections need `-> nvic@[6]`.

        Attribute-style connections (`-> nvic@23`, as used by the combined
        inputs) are legitimate and must stay without brackets.
        """
        for line in self.repl.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and "-> nvic@" in stripped:
                self.assertIn("@[", stripped, stripped)

    def test_stub_scripts_do_not_assign_none(self) -> None:
        for script in generate.TRIVIAL_SCRIPTS.values():
            self.assertNotIn("else None", script)
        self.assertNotIn("else None", generate.DEFAULT_STUB_SCRIPT)

    def test_memory_regions_come_first(self) -> None:
        flash_at = self.repl.index("flash: Memory.MappedMemory")
        first_peripheral = self.repl.index("tim2: ")
        self.assertLess(flash_at, first_peripheral)

    def test_dump_format_is_json(self) -> None:
        records = json.loads(json.dumps(memmap.peripheral_records()))
        self.assertEqual(len(records), len(memmap.PERIPHERALS))
        self.assertEqual(records[0]["status"], "modelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
