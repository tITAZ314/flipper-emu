"""The NVIC IRQ numbers in the generated platform must match the firmware.

The vector table is the authority: the handler named at slot ``16 + n`` of the
firmware's vector table owns IRQ ``n``.  Deriving the numbers this way is what
caught the off-by-one that kept the firmware from waking out of tickless idle -
``LPTIM1`` was wired to ``nvic@46``, which is ``HSEM``'s vector, so the wake-up
landed on a masked NVIC input and the CPU stayed in WFI.

The derivation needs the flash image (``flipper_emu load``) and a release ELF;
when either is missing the derivation test skips, while the literal pin below
always runs.
"""

from __future__ import annotations

import glob
import os
import struct
import unittest

from flipper_emu.console.symbols import ElfSymbolTable
from flipper_emu.platform import generate

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_FLASH = os.path.join(_REPO, "artifacts", "flash.img")

#: Handler name -> the generator's peripheral keys that own that vector.
_HANDLERS = {
    "TIM1_UP_TIM16_IRQHandler": ("TIM1", "TIM16"),
    "TIM1_TRG_COM_TIM17_IRQHandler": ("TIM17",),
    "TIM2_IRQHandler": ("TIM2",),
    "USART1_IRQHandler": ("USART1",),
    "LPUART1_IRQHandler": ("LPUART1",),
    "RTC_Alarm_IRQHandler": ("RTC",),
    "LPTIM1_IRQHandler": ("LPTIM1",),
    "LPTIM2_IRQHandler": ("LPTIM2",),
}


def _release_elf() -> str:
    found = sorted(glob.glob(os.path.join(_REPO, "firmware", "*.elf")))
    return found[0] if found else ""


class IrqNumberTests(unittest.TestCase):
    def test_expected_numbers_are_pinned(self) -> None:
        """The numbers this project measured, so a silent edit cannot slip in."""
        expected = {
            "TIM1": 25,
            "TIM2": 28,
            "TIM16": 25,
            "TIM17": 26,
            "USART1": 36,
            "LPUART1": 37,
            "LPTIM1": 47,
            "LPTIM2": 48,
            "RTC": 41,
        }
        actual = {
            name: irq
            for name, connections in generate.PERIPHERAL_NVIC_IRQS.items()
            for _signal, irq in connections[:1]
        }
        self.assertEqual(expected, actual)

    def test_numbers_are_within_the_cortex_m_irq_range(self) -> None:
        for name, connections in generate.PERIPHERAL_NVIC_IRQS.items():
            for _signal, irq in connections:
                self.assertTrue(0 <= irq < 80, "%s -> %d" % (name, irq))

    def test_dma_channels_feed_the_spi2_block_irqs(self) -> None:
        """DMA2 channel 6 (IRQ 60) is what the microSD block read waits on.

        ``targets/f7/furi_hal/furi_hal_spi.c`` (1.4.3) defines ``SPI_DMA = DMA2`` with
        ``SPI_DMA_RX_CHANNEL = LL_DMA_CHANNEL_6`` (``SPI_DMA_RX_IRQ =
        FuriHalInterruptIdDma2Ch6``) and ``SPI_DMA_TX_CHANNEL = LL_DMA_CHANNEL_7``, and
        ``furi_hal_spi_bus_trx_dma()`` returns only after ``spi_dma_isr`` - which runs on
        that RX completion - releases its semaphore.  Without those two connections the
        firmware logs "DMA timeout" and dies in ``furi_check()`` at
        ``sd_device_read+0xCA``, so this pins them (docs/ISSUES_AND_LOGS.md, P21).
        """
        self.assertEqual((11, 12, 13, 14, 15, 16, 17), generate.DMA_CHANNEL_IRQS["DMA1"])
        self.assertEqual((55, 56, 57, 58, 59, 60, 61), generate.DMA_CHANNEL_IRQS["DMA2"])

        lines = generate.render_repl().splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("dma2:"))
        block = []
        for line in lines[start + 1:]:
            if line and not line.startswith(" "):
                break
            block.append(line.strip())
        self.assertIn("5 -> nvic@60", block)
        self.assertIn("6 -> nvic@61", block)

    def test_tim1_wires_its_single_supported_vector(self) -> None:
        """TIM1 has UP/TRG_COM/CC; one model output drives one NVIC input."""
        self.assertEqual((("IRQ", 25),), generate.PERIPHERAL_NVIC_IRQS["TIM1"])

    @unittest.skipUnless(
        os.path.exists(_FLASH) and _release_elf(), "needs artifacts/flash.img and a release ELF"
    )
    def test_matches_the_firmwares_vector_table(self) -> None:
        elf = ElfSymbolTable.read(_release_elf())
        with open(_FLASH, "rb") as handle:
            image = handle.read()
        derived = {}
        for slot in range(16, 80):
            address = struct.unpack_from("<I", image, slot * 4)[0]
            resolution = elf.resolve(address)
            if resolution is None:
                continue
            name = resolution.symbol.name
            for key in _HANDLERS.get(name, ()):
                derived[key] = slot - 16
        self.assertTrue(derived, "no known handlers found in the vector table")
        for key, irq in derived.items():
            self.assertEqual(
                irq,
                generate.PERIPHERAL_NVIC_IRQS[key][0][1],
                "%s: vector table says IRQ %d" % (key, irq),
            )


if __name__ == "__main__":
    unittest.main()
