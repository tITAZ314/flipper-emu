using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// STM32WB55 DMA controller (DMA1/DMA2), channel-based, with the per-channel
    /// interrupt lines the firmware depends on.
    /// </summary>
    /// <remarks>
    /// Why this replaced Renode's <c>DMA.STM32G0DMA</c>: the microSD block read goes
    /// through <c>furi_hal_spi_bus_trx_dma()</c>
    /// (<c>targets/f7/furi_hal/furi_hal_spi.c</c>), which programs DMA2 channel 6 for RX
    /// and channel 7 for TX, enables them, and then waits on a semaphore that only
    /// <c>spi_dma_isr</c> releases - and that ISR runs on the channel 6
    /// transfer-complete interrupt.  With the stock model the firmware did reach the
    /// card (ACMD41 accepted, 0xFE token read) and the block bytes did move, but no
    /// completion interrupt ever arrived, so every read ended in "DMA timeout" and
    /// `furi_check()` -> "[CRASH][StorageSrv] furi_check failed" at
    /// <c>sd_device_read+0xCA</c>, i.e. a boot loop (docs/ISSUES_AND_LOGS.md, P21).
    ///
    /// Register map (RM0434, channel-based DMA): <c>ISR</c> 0x00, <c>IFCR</c> 0x04, then
    /// each channel owns four registers followed by four reserved bytes - a 0x14 stride,
    /// i.e. <c>CCR</c>, <c>CNDTR</c>, <c>CPAR</c>, <c>CMAR</c> at 0x08, 0x0C, 0x10, 0x14
    /// for channel 1.  Reading a channel at the 0x10 stride lands in a reserved hole,
    /// which is how the first probe written during this work was misled.
    ///
    /// Behaviour:
    ///
    /// * a channel starts when <c>CCR.EN</c> rises with a non-zero count
    ///   (<c>CNDTR == 0</c> means 65536, as on silicon);
    /// * a peripheral-to-memory channel is advanced <em>in lockstep</em> with an
    ///   already-enabled memory-to-peripheral channel, because one SPI clock moves one
    ///   byte in each direction.  The firmware enables RX before TX, so without the
    ///   lockstep the RX channel would drain bytes the peripheral never produced;
    /// * a channel raises its interrupt only while its interrupt-enable bits say so,
    ///   matching what the firmware relies on: in the TRX path it enables the RX
    ///   interrupt only, while the TX channel's vector holds no handler at all, so
    ///   raising that one would crash the firmware inside
    ///   <c>furi_hal_interrupt_call()</c>;
    /// * writing a bit to <c>IFCR</c> clears the matching flag, and each interrupt line
    ///   is a level derived from flags and enables, so the firmware's
    ///   <c>LL_DMA_ClearFlag_TC6()</c> deasserts it.
    /// </remarks>
    public class DmaWb55 : BasicDoubleWordPeripheral, INumberedGPIOOutput, IKnownSize
    {
        /// <summary>ISR: interrupt status.</summary>
        private const long InterruptStatusRegister = 0x00;

        /// <summary>IFCR: interrupt flag clear, write 1 to clear.</summary>
        private const long InterruptFlagClearRegister = 0x04;

        /// <summary>First channel's register block.</summary>
        private const long ChannelBase = 0x08;

        /// <summary>Four registers plus four reserved bytes per channel (RM0434).</summary>
        private const long ChannelStride = 0x14;

        // CCR bits, as written by the firmware's LL_DMA_Init.
        private const uint EnableBit = 1u << 0;
        private const uint TransferCompleteInterruptBit = 1u << 1;
        private const uint HalfTransferInterruptBit = 1u << 2;
        private const uint TransferErrorInterruptBit = 1u << 3;
        private const uint DirectionBit = 1u << 4; // 1: memory -> peripheral
        private const uint CircularBit = 1u << 5;
        private const uint PeripheralIncrementBit = 1u << 6;
        private const uint MemoryIncrementBit = 1u << 7;

        private const uint InterruptEnableMask =
            TransferCompleteInterruptBit | HalfTransferInterruptBit | TransferErrorInterruptBit;

        // ISR flags: GIF/TCIF/HTIF/TEIF per channel, four bits each, in channel order.
        private const uint GlobalFlagBit = 0x1u;
        private const uint TransferCompleteFlagBit = 0x2u;

        /// <summary>One DMA channel's register block and progress.</summary>
        private class Channel
        {
            public uint Control;
            public uint Count;
            public uint PeripheralAddress;
            public uint MemoryAddress;
            public uint Flags;

            public int PeripheralBytes
            {
                get { return 1 << (int)((Control >> 8) & 0x3u); }
            }

            public int MemoryBytes
            {
                get { return 1 << (int)((Control >> 10) & 0x3u); }
            }
        }
        private readonly IMachine machine;
        private readonly Channel[] channels;
        private readonly Dictionary<int, IGPIO> connections = new Dictionary<int, IGPIO>();

        private long transfers;
        private long wordsMoved;

        public DmaWb55(IMachine machine, int channels = 7) : base(machine)
        {
            this.machine = machine;
            this.channels = new Channel[channels];
            for(var index = 0; index < channels; index++)
            {
                this.channels[index] = new Channel();
                connections[index] = new GPIO();
            }
        }

        public long Size => 0x400;

        public IReadOnlyDictionary<int, IGPIO> Connections
        {
            get { return connections; }
        }

        /// <summary>Completed channel transfers (for the bring-up log and probes).</summary>
        public long Transfers
        {
            get { return transfers; }
        }

        /// <summary>Words moved across all channels.</summary>
        public long WordsMoved
        {
            get { return wordsMoved; }
        }

        public override void Reset()
        {
            foreach(var channel in channels)
            {
                channel.Control = 0;
                channel.Count = 0;
                channel.PeripheralAddress = 0;
                channel.MemoryAddress = 0;
                channel.Flags = 0;
            }
            transfers = 0;
            wordsMoved = 0;
            for(var index = 0; index < channels.Length; index++)
            {
                UpdateInterrupt(index);
            }
        }
        public override uint ReadDoubleWord(long offset)
        {
            if(offset == InterruptStatusRegister)
            {
                return BuildStatus();
            }

            int index;
            int register;
            if(!TryDecodeChannel(offset, out index, out register))
            {
                this.NoisyLog("DMA: read of reserved offset 0x{0:X}", offset);
                return 0;
            }

            var channel = channels[index];
            switch(register)
            {
                case 0x00:
                    return channel.Control;
                case 0x04:
                    return channel.Count;
                case 0x08:
                    return channel.PeripheralAddress;
                case 0x0C:
                    return channel.MemoryAddress;
                default:
                    this.NoisyLog("DMA: read of reserved channel offset 0x{0:X}", offset);
                    return 0;
            }
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            if(offset == InterruptFlagClearRegister)
            {
                ClearFlags(value);
                return;
            }

            if(offset == InterruptStatusRegister)
            {
                this.NoisyLog("DMA: write of 0x{0:X8} to read-only ISR ignored", value);
                return;
            }

            int index;
            int register;
            if(!TryDecodeChannel(offset, out index, out register))
            {
                this.NoisyLog("DMA: write of 0x{0:X8} to reserved offset 0x{1:X} ignored", value, offset);
                return;
            }

            var channel = channels[index];
            switch(register)
            {
                case 0x00:
                    WriteControl(index, value);
                    return;
                case 0x04:
                    channel.Count = value & 0xFFFF;
                    return;
                case 0x08:
                    channel.PeripheralAddress = value;
                    return;
                case 0x0C:
                    channel.MemoryAddress = value;
                    return;
                default:
                    this.NoisyLog("DMA: write of 0x{0:X8} to reserved channel offset 0x{1:X} ignored",
                                  value, offset);
                    return;
            }
        }

        /// <summary>Splits an address inside this peripheral into channel and register.</summary>
        private bool TryDecodeChannel(long offset, out int index, out int register)
        {
            index = 0;
            register = 0;
            if(offset < ChannelBase || offset >= ChannelBase + ChannelStride * channels.Length)
            {
                return false;
            }
            var relative = offset - ChannelBase;
            index = (int)(relative / ChannelStride);
            register = (int)(relative % ChannelStride);
            return true;
        }

        private void WriteControl(int index, uint value)
        {
            var channel = channels[index];
            var wasEnabled = (channel.Control & EnableBit) != 0;
            channel.Control = value;
            UpdateInterrupt(index);

            if((value & EnableBit) != 0 && !wasEnabled)
            {
                OnChannelEnabled(index);
            }
        }
        private void OnChannelEnabled(int index)
        {
            var channel = channels[index];
            var count = channel.Count == 0 ? 0x10000u : channel.Count;
            var peripheral = channel.PeripheralAddress;
            var memory = channel.MemoryAddress;
            var transmit = (channel.Control & DirectionBit) != 0;

            if(transmit)
            {
                // Memory to peripheral: this channel clocks the bus, so it also advances
                // every enabled receive channel, one word per word, the way one SPI
                // clock moves one byte in each direction.
                MoveTogether(index);
                this.InfoLog("DMA ch{0} memory 0x{1:X8} -> peripheral 0x{2:X8}, {3} word(s)",
                             index + 1, memory, peripheral, count);
                return;
            }

            // Receive channel.  On hardware it advances one word per clock of its
            // peripheral, and in this firmware the clocks come from the paired
            // memory-to-peripheral channel: the TRX path of furi_hal_spi_bus_trx_dma()
            // enables RX and TX a couple of instructions apart, RX first.  So the words
            // are moved by MoveTogether() as it clocks each byte - draining them here
            // instead handed FatFS 512 stale bytes and the mount failed with
            // "no filesystem" (measured before this comment was true).
            this.InfoLog("DMA ch{0} waiting for its clock source: peripheral 0x{1:X8} -> memory 0x{2:X8}, {3} word(s)",
                         index + 1, peripheral, memory, count);
        }

        /// <summary>Runs a memory-to-peripheral channel and any receive channel with it.</summary>
        private void MoveTogether(int index)
        {
            var transmit = channels[index];
            var count = transmit.Count == 0 ? 0x10000u : transmit.Count;
            var circular = (transmit.Control & CircularBit) != 0;

            for(var word = 0u; word < count; word++)
            {
                WritePeripheral(transmit, LoadMemory(transmit));
                wordsMoved++;

                foreach(var receiver in ReceiveChannels())
                {
                    var receive = channels[receiver];
                    var remaining = receive.Count == 0 ? 0x10000u : receive.Count;
                    if(remaining == 0)
                    {
                        continue;
                    }
                    StoreMemory(receive, ReadPeripheral(receive));
                    receive.Count = remaining - 1;
                    wordsMoved++;
                    if(receive.Count == 0 && (receive.Control & CircularBit) == 0)
                    {
                        CompleteTransfer(receiver);
                    }
                }

                transmit.Count = count - word - 1;
                if(transmit.Count == 0)
                {
                    if(circular)
                    {
                        // One period done: reload and keep running.
                        transmit.Count = count;
                        transmit.Flags |= TransferCompleteFlagBit | GlobalFlagBit;
                        UpdateInterrupt(index);
                    }
                    else
                    {
                        CompleteTransfer(index);
                    }
                }
            }
        }

        private IEnumerable<int> ReceiveChannels()
        {
            for(var index = 0; index < channels.Length; index++)
            {
                var channel = channels[index];
                if((channel.Control & EnableBit) != 0 && (channel.Control & DirectionBit) == 0)
                {
                    yield return index;
                }
            }
        }

        private bool HasEnabledTransmitChannel()
        {
            foreach(var channel in channels)
            {
                if((channel.Control & EnableBit) != 0 && (channel.Control & DirectionBit) != 0)
                {
                    return true;
                }
            }
            return false;
        }

        private void CompleteTransfer(int index)
        {
            var channel = channels[index];
            channel.Count = 0;
            channel.Flags |= TransferCompleteFlagBit | GlobalFlagBit;
            if((channel.Control & CircularBit) == 0)
            {
                channel.Control &= ~EnableBit;
            }
            transfers++;
            UpdateInterrupt(index);
        }
        private uint LoadMemory(Channel channel)
        {
            var address = channel.MemoryAddress;
            var value = ReadBus(address, channel.MemoryBytes);
            if((channel.Control & MemoryIncrementBit) != 0)
            {
                channel.MemoryAddress = address + (uint)channel.MemoryBytes;
            }
            return value;
        }

        private void StoreMemory(Channel channel, uint value)
        {
            var address = channel.MemoryAddress;
            WriteBus(address, value, channel.MemoryBytes);
            if((channel.Control & MemoryIncrementBit) != 0)
            {
                channel.MemoryAddress = address + (uint)channel.MemoryBytes;
            }
        }

        private uint ReadPeripheral(Channel channel)
        {
            var address = channel.PeripheralAddress;
            var value = ReadBus(address, channel.PeripheralBytes);
            if((channel.Control & PeripheralIncrementBit) != 0)
            {
                channel.PeripheralAddress = address + (uint)channel.PeripheralBytes;
            }
            return value;
        }

        private void WritePeripheral(Channel channel, uint value)
        {
            var address = channel.PeripheralAddress;
            WriteBus(address, value, channel.PeripheralBytes);
            if((channel.Control & PeripheralIncrementBit) != 0)
            {
                channel.PeripheralAddress = address + (uint)channel.PeripheralBytes;
            }
        }

        private uint ReadBus(uint address, int bytes)
        {
            switch(bytes)
            {
                case 1:
                    return machine.SystemBus.ReadByte(address);
                case 2:
                    return machine.SystemBus.ReadWord(address);
                default:
                    return machine.SystemBus.ReadDoubleWord(address);
            }
        }

        private void WriteBus(uint address, uint value, int bytes)
        {
            switch(bytes)
            {
                case 1:
                    machine.SystemBus.WriteByte(address, (byte)value);
                    return;
                case 2:
                    machine.SystemBus.WriteWord(address, (ushort)value);
                    return;
                default:
                    machine.SystemBus.WriteDoubleWord(address, value);
                    return;
            }
        }

        private uint BuildStatus()
        {
            var status = 0u;
            for(var index = 0; index < channels.Length; index++)
            {
                status |= channels[index].Flags << (4 * index);
            }
            return status;
        }

        private void ClearFlags(uint value)
        {
            for(var index = 0; index < channels.Length; index++)
            {
                var clear = (value >> (4 * index)) & 0xFu;
                if(clear == 0)
                {
                    continue;
                }
                channels[index].Flags &= ~clear;
                UpdateInterrupt(index);
            }
        }

        /// <summary>
        /// Level interrupt: asserted while an enabled flag is pending.  TCIF/HTIF/TEIF sit
        /// at the same bit positions as TCIE/HTIE/TEIE (bits 1..3), so no shift is needed;
        /// GIF (bit 0) has no enable of its own and never raises the line alone.
        /// </summary>
        private void UpdateInterrupt(int index)
        {
            var channel = channels[index];
            var pending = channel.Flags & channel.Control & InterruptEnableMask;
            connections[index].Set(pending != 0);
        }

        /// <summary>Human-readable state; callable from the monitor as evidence.</summary>
        public string DumpState()
        {
            var text = string.Format("DMA channels={0} transfers={1} words={2} ISR=0x{3:X8}",
                                     channels.Length, transfers, wordsMoved, BuildStatus());
            for(var index = 0; index < channels.Length; index++)
            {
                var channel = channels[index];
                if(channel.Control == 0 && channel.Count == 0)
                {
                    continue;
                }
                text += string.Format(" | ch{0} CCR=0x{1:X8} CNDTR={2} CPAR=0x{3:X8} CMAR=0x{4:X8}",
                                      index + 1, channel.Control, channel.Count,
                                      channel.PeripheralAddress, channel.MemoryAddress);
            }
            return text;
        }
    }
}
