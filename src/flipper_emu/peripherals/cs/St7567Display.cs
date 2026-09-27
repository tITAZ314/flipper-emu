using System;
using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// ST7567 128x64 monochrome LCD panel as an SPI slave.
    /// </summary>
    /// <remarks>
    /// The panel is write-only from the firmware's point of view: u8g2 drives it
    /// through <c>u8x8_hw_spi_stm32</c> → <c>furi_hal_spi_bus_tx</c>, so the model
    /// only has to capture the byte stream and the A0 (command/data) line.
    /// Interpretation lives in the host-side decoder
    /// (<c>flipper_emu.frontend.st7567</c>), which keeps the emulator-side model
    /// trivial and lets the framing logic be unit-tested without an emulator.
    ///
    /// Measured stream this is built against: the ST7567 init sequence
    /// (<c>E2 A2 A0 C8 40 25 81 20 2F A4 AF</c>), then column/page addressing
    /// (<c>0x10</c>/<c>0xB0-0xBF</c>) followed by 132 bytes of pixel data per page.
    ///
    /// Wire <c>A0</c> to input 0 and <c>RESET</c> to input 1 if the board file
    /// connects them; without them the decoder infers command/data framing from
    /// the ST7567 command set, and the tags below record which situation applies.
    /// </remarks>
    public class St7567Display : ISPIPeripheral, IGPIOReceiver, IDisposable
    {
        // Record format: 7-byte header, then 2-byte records: [tag][payload].
        //  0x01 data byte, A0 high (pixel data)
        //  0x02 data byte, A0 low  (command)
        //  0x03 A0 pin changed
        //  0x04 RESET pin changed
        //  0x05 byte captured before A0 was known (treated as command-ish)
        private const byte DataWithA0High = 0x01;
        private const byte DataWithA0Low = 0x02;
        private const byte A0Changed = 0x03;
        private const byte ResetChanged = 0x04;
        private const byte DataUnknownA0 = 0x05;

        private static readonly byte[] Header = { 0x46, 0x5A, 0x44, 0x50, 0x49, 0x31, 0x0A }; // "FZDPI1\n"

        private readonly object sync = new object();
        private readonly string outputPath;

        private FileStream output;
        private int bytesWritten;
        private int capturedBytes;
        private bool a0High = true;
        private bool a0Known;
        private bool resetLineHigh = true;
        private bool firstOpen = true;

        public St7567Display(IMachine machine, string outputPath = "artifacts/display-stream.bin")
        {
            this.outputPath = outputPath;
            this.InfoLog("ST7567 panel attached; recording the SPI stream to {0}", outputPath);
        }

        /// <summary>SPI bytes captured so far (for sanity checks in logs).</summary>
        public int CapturedBytes
        {
            get { lock(sync) { return capturedBytes; } }
        }

        public void Reset()
        {
            lock(sync)
            {
                resetLineHigh = false;
                WriteRecord(ResetChanged, 1); // asserted
                resetLineHigh = true;
                WriteRecord(ResetChanged, 0); // released
            }
        }

        /// <summary>Input 0 is A0/DI (command/data), input 1 is /RESET (active low).</summary>
        public void OnGPIO(int number, bool value)
        {
            lock(sync)
            {
                if(number == 0)
                {
                    // The firmware rewrites the A0 line for every byte it sends, so
                    // only edges are worth recording: otherwise the stream is ~99%
                    // redundant pin events and a host-side decoder cannot keep up.
                    // Data bytes are tagged from a0High either way (see Transmit).
                    if(a0Known && a0High == value)
                    {
                        return;
                    }
                    a0High = value;
                    a0Known = true;
                    WriteRecord(A0Changed, (byte)(value ? 1 : 0));
                }
                else if(number == 1)
                {
                    if(resetLineHigh == value)
                    {
                        return;
                    }
                    resetLineHigh = value;
                    // /RESET idles high; while it is low the panel is held in reset,
                    // so payload 1 (asserted) is the edge the decoder clears on.
                    WriteRecord(ResetChanged, (byte)(value ? 0 : 1));
                }
            }
        }

        public byte Transmit(byte data)
        {
            lock(sync)
            {
                if(capturedBytes < 8)
                {
                    this.InfoLog("ST7567 byte #{0} = 0x{1:X2} (a0 high: {2})",
                                 capturedBytes, data, a0High);
                }
                capturedBytes++;
                if(!a0Known)
                {
                    WriteRecord(DataUnknownA0, data);
                }
                else
                {
                    WriteRecord(a0High ? DataWithA0High : DataWithA0Low, data);
                }
            }
            // The panel is write-only in this design; a real ST7567 would return
            // its status register for a read command, which the firmware never uses.
            return 0;
        }

        public void FinishTransmission()
        {
            // furi_hal_spi runs one transaction per byte, so this is where the
            // stream becomes visible to the host-side decoder.
            lock(sync)
            {
                Flush();
            }
        }

        public void Dispose()
        {
            lock(sync)
            {
                if(output != null)
                {
                    output.Flush();
                    output.Dispose();
                    output = null;
                }
            }
        }

        private void WriteRecord(byte tag, byte payload)
        {
            EnsureOutput();
            output.WriteByte(tag);
            output.WriteByte(payload);
            bytesWritten += 2;
            if(bytesWritten >= 512)
            {
                Flush();
            }
        }

        private void EnsureOutput()
        {
            if(output != null)
            {
                return;
            }

            var directory = Path.GetDirectoryName(outputPath);
            if(!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
            {
                Directory.CreateDirectory(directory);
            }

            // Truncate on the first open, append if the stream was closed and
            // reopened (so a dispose mid-run cannot lose captured bytes).
            var mode = firstOpen ? FileMode.Create : FileMode.Append;
            output = new FileStream(outputPath, mode, FileAccess.Write, FileShare.Read);
            if(firstOpen)
            {
                output.Write(Header, 0, Header.Length);
                firstOpen = false;
            }
        }

        private void Flush()
        {
            if(output == null)
            {
                return;
            }

            output.Flush();
            bytesWritten = 0;
        }

    }
}
