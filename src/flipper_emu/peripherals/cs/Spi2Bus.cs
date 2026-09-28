using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// SPI2 as the firmware actually uses it: two devices behind chip selects.
    /// </summary>
    /// <remarks>
    /// Why a router instead of two Renode SPI connections: on f18/WB55 both the
    /// ST7567 panel and the microSD card hang off the *same* bus
    /// (<c>furi_hal_spi_bus_handle_display</c>, <c>..._sd_slow</c>,
    /// <c>..._sd_fast</c> all have <c>.bus = &amp;furi_hal_spi_bus_d</c>), and a
    /// Renode SPI connection delivers every transfer to its single attached
    /// peripheral. Measured with the card-detect pin low: 128 000 CMD0 frames in
    /// a 16 s run landed in the panel model's recorder, which is what polluted the
    /// display stream (docs/BRINGUP_LOG.md §20). Real hardware splits them by chip
    /// select, and so does this model:
    ///
    /// <code>
    /// PC12 gpio_sdcard_cs    low -> the card sees the byte
    /// PC11 gpio_display_cs   low -> the panel sees the byte
    /// neither                -> the byte goes nowhere (MISO idles high)
    /// </code>
    ///
    /// The "neither selected" case matters: the SD driver sends 80 dummy clocks
    /// and one purge byte after every command with its own CS high. On silicon the
    /// panel ignores those because *its* CS is high too; with the panel as a blind
    /// listener they would be recorded as display traffic.
    ///
    /// GPIO inputs are the firmware's own pins (from its 1.4.3 resource table):
    /// 0 = PB1 <c>gpio_display_di</c> (A0/DC), 1 = PB0 <c>gpio_display_rst_n</c>,
    /// 2 = PC12 <c>gpio_sdcard_cs</c>, 3 = PC11 <c>gpio_display_cs</c>.
    /// </remarks>
    public class Spi2Bus : ISPIPeripheral, IGPIOReceiver, IDisposable
    {
        private const int InputPanelA0 = 0;
        private const int InputPanelReset = 1;
        private const int InputCardCs = 2;
        private const int InputPanelCs = 3;

        /// <summary>Bytes between traffic summaries - enough to see the split, few enough to read.</summary>
        private const long LogEveryBytes = 65536;

        private readonly object sync = new object();
        private readonly St7567Display panel;
        private readonly SdCardSpi card;

        // Chip selects idle high (a real bus has them pulled up), so nothing is
        // selected until the firmware drives a line low. That also keeps the
        // pre-init bytes going to the panel exactly as they did before the card
        // existed.
        private bool cardSelected;
        private bool panelSelected;

        private long cardBytes;
        private long panelBytes;
        private long unselectedBytes;
        private long nextReport;

        public Spi2Bus(
            IMachine machine,
            string panelOutputPath = "artifacts/display-stream.bin",
            string cardImagePath = "")
        {
            panel = new St7567Display(machine, panelOutputPath);
            card = new SdCardSpi(cardImagePath);
            nextReport = LogEveryBytes;
            this.InfoLog(
                "SPI2 router up: panel recorder {0}, card image {1}",
                panelOutputPath,
                string.IsNullOrEmpty(cardImagePath) ? "(none - card will not answer)" : cardImagePath);
        }

        /// <summary>SPI bytes routed to the card (for sanity checks in logs).</summary>
        public long CardBytes
        {
            get { lock(sync) { return cardBytes; } }
        }

        public byte Transmit(byte data)
        {
            lock(sync)
            {
                // The card wins a tie: it is only ever selected while the panel is
                // not (one bus handle at a time), and its conversation is the one
                // that must not be polluted.
                if(cardSelected)
                {
                    cardBytes++;
                    var cardAnswer = card.Transmit(data);
                    if(card.ProbingPostWrite)
                    {
                        // Logged next to the card's own probe lines: if the card produced a
                        // byte that the router hands back here and the driver still does not
                        // see it, the loss is inside the SPI/DMA plumbing, not the card.
                        this.InfoLog(
                            "SPI2 router: host sent 0x{0:X2}, card produced 0x{1:X2}",
                            data,
                            cardAnswer);
                    }
                    Report();
                    return cardAnswer;
                }

                if(panelSelected)
                {
                    panelBytes++;
                    var panelAnswer = panel.Transmit(data);
                    Report();
                    return panelAnswer;
                }

                unselectedBytes++;
                Report();
                return 0xFF;
            }
        }

        public void FinishTransmission()
        {
            lock(sync)
            {
                if(cardSelected)
                {
                    card.FinishTransmission();
                }
                else if(panelSelected)
                {
                    panel.FinishTransmission();
                }
            }
        }

        public void OnGPIO(int number, bool value)
        {
            lock(sync)
            {
                switch(number)
                {
                    case InputCardCs:
                        cardSelected = !value;
                        card.SetSelected(!value);
                        break;
                    case InputPanelCs:
                        panelSelected = !value;
                        break;
                    default:
                        panel.OnGPIO(number, value);
                        break;
                }
            }
        }

        public void Reset()
        {
            lock(sync)
            {
                cardSelected = false;
                panelSelected = false;
                panel.Reset();
                card.Reset();
            }
        }

        public void Dispose()
        {
            lock(sync)
            {
                Report(force: true, final: true);
                panel.Dispose();
                card.Dispose();
            }
        }

        /// <summary>Periodic traffic split, so a run log shows who got the bytes.</summary>
        private void Report(bool force = false, bool final = false)
        {
            var total = cardBytes + panelBytes + unselectedBytes;
            if(!force && total < nextReport)
            {
                return;
            }

            nextReport = total + LogEveryBytes;
            this.InfoLog(
                "SPI2 {0}: card {1} B, panel {2} B, unselected {3} B (CS: card {4}, panel {5})",
                final ? "final totals" : "totals",
                cardBytes,
                panelBytes,
                unselectedBytes,
                cardSelected ? "low" : "high",
                panelSelected ? "low" : "high");
        }
    }
}
