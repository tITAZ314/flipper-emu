using System;
using System.Collections.Generic;
using System.IO;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.SPI;

namespace Antmicro.Renode.Peripherals.FlipperEmu
{
    /// <summary>
    /// microSD card in SPI mode, backed by a sector image on the host.
    /// </summary>
    /// <remarks>
    /// The command set is not "the SD spec in full": it is what the 1.4.3 firmware's
    /// own driver (<c>targets/f7/furi_hal/furi_hal_sd.c</c>) sends, measured on the
    /// wire before this model existed (docs/BRINGUP_LOG.md §20):
    ///
    /// <code>
    /// CMD0  arg 0      crc 0x95 -> R1 0x01 (idle)
    /// CMD8  arg 0x1AA          -> R7: R1 0x01, then 00 00 01 AA
    /// CMD55 / ACMD41           -> R1 0x01 until ready, then 0x00
    /// CMD58                    -> R3: R1, then OCR with CCS set (SDHC)
    /// CMD16 / CMD13            -> R1, (+ R2 status for CMD13)
    /// CMD9 / CMD10             -> R1, then 0xFE + 16 bytes + 2 CRC
    /// CMD17                    -> R1, then 0xFE + 512 bytes + 2 CRC
    /// CMD24                    -> R1, then the host writes token+data+crc and the
    ///                             card answers 0x05, busy 0x00, then 0xFF
    /// </code>
    ///
    /// Responses are produced as a byte queue because the driver *polls*: it reads
    /// until something other than 0xFF appears (<c>sd_spi_wait_for_data</c>) and up
    /// to 8 bytes for an R1 (<c>sd_spi_wait_for_data_and_read</c>).  CRCs are
    /// ignored in both directions, exactly as the driver ignores them.
    ///
    /// Only bytes sent while the bus router reports this card as selected reach
    /// here, so a command frame is always complete; <c>SetSelected</c> starts a
    /// fresh transaction and drops whatever was left of the previous one.
    ///
    /// The image is opened read/write and written through, so a run can delete
    /// <c>.int/.slideshow</c> just like the device does when the slideshow exits -
    /// which means each verification run wants a fresh image.
    /// </remarks>
    public class SdCardSpi : ISPIPeripheral, IDisposable
    {
        private const int BlockSize = 512;
        private const int CommandLength = 6;

        private const byte R1Ready = 0x00;
        private const byte R1Idle = 0x01;
        private const byte R1IllegalCommand = 0x04;
        private const byte DataToken = 0xFE;
        private const byte IdleLine = 0xFF;

        /// <summary>Data response token for an accepted block write.</summary>
        private const byte AcceptedToken = 0x05;

        /// <summary>How many command frames to spell out before switching to summaries.</summary>
        private const long LogFirstCommands = 512;

        /// <summary>Command frames between summary lines.</summary>
        private const long LogEveryCommands = 4096;

        private readonly object sync = new object();
        private readonly FileStream image;
        private readonly long sectorCount;

        private readonly byte[] command = new byte[CommandLength];
        private readonly Queue<byte> pending = new Queue<byte>();

        private int commandBytes;
        private bool ready;      // ACMD41 accepted: the card left the idle state
        private bool appCmd;     // the previous frame was CMD55
        private bool highCapacity = true;
        private uint blockSector;

        // Write payload collection. The token is tracked separately because the
        // host sends two dummy bytes first, and the two CRC bytes are reads it does
        // not look at.
        private bool writeActive;
        private bool writeTokenSeen;
        private int writeIndex;
        private int writeCrc;
        private byte[] writeBuffer;

        private long commandsSeen;
        private long sectorsRead;
        private long sectorsWritten;
        private long discardedBytes;

        /// <summary>
        /// Every sector the host has asked for at least once. The read log is what makes a
        /// failed mount diagnosable, but a mount alone fills its budget, so this set keeps
        /// later directory lookups and file reads from disappearing silently.
        /// </summary>
        private HashSet<uint> sectorsTouched = new HashSet<uint>();
        private int discardsLogged;

        /// <summary>CMD24 frames seen, used to arm the one-shot write probe.</summary>
        private int writeAttempts;

        /// <summary>Byte exchanges logged since the first CMD24's response.</summary>
        private int postCmd24Exchanges;

        /// <summary>Commands still to name after the first accepted write.</summary>
        private int logNextCommands;

        /// <summary>Byte exchanges still to name after the first accepted write.</summary>
        private int postWriteExchanges;

        public SdCardSpi(string imagePath)
        {
            if(string.IsNullOrEmpty(imagePath) || !File.Exists(imagePath))
            {
                this.InfoLog(
                    "SD card: no image at {0} - the card will not answer (MISO stays high)",
                    string.IsNullOrEmpty(imagePath) ? "(unset)" : imagePath);
                return;
            }

            image = new FileStream(imagePath, FileMode.Open, FileAccess.ReadWrite, FileShare.Read);
            sectorCount = image.Length / BlockSize;
            this.InfoLog(
                "SD card: image {0}, {1} sectors ({2} MiB), SDHC",
                imagePath,
                sectorCount,
                (image.Length + (1024 * 1024 - 1)) / (1024 * 1024));
        }

        /// <summary>True when an image is attached, i.e. when the card answers at all.</summary>
        public bool CardPresent
        {
            get { return image != null; }
        }

        /// <summary>
        /// True while the post-write response probe window is open. The bus router logs
        /// what it hands back to the host at the same moment: comparing the two shows
        /// whether a byte the card produced actually reached the host, which is the
        /// difference between a card-side bug and an SPI-side one.
        /// </summary>
        public bool ProbingPostWrite
        {
            get
            {
                lock(sync)
                {
                    return postWriteExchanges > 0;
                }
            }
        }

        public long SectorsWritten
        {
            get { lock(sync) { return sectorsWritten; } }
        }

        /// <summary>Called by the bus router whenever PC12 changes (active low).</summary>
        public void SetSelected(bool selected)
        {
            // Chip select changes deliberately carry no state here. The driver toggles
            // chip select around every single-byte transfer (measured: an assert and a
            // release per poll byte), so dropping queued bytes or half-finished framing
            // on a select destroyed CMD24's R1 before the driver could read it - and
            // with it every write, which is what left the storage service unable to
            // save anything. Both are reset where they belong instead: when a new
            // command frame starts, or in Reset().
            _ = selected;
        }

        public byte Transmit(byte data)
        {
            lock(sync)
            {
                if(image == null)
                {
                    return IdleLine;
                }

                if(writeActive && pending.Count == 0)
                {
                    // The write phase only starts once CMD24's R1 has actually been read
                    // off the bus. The driver polls for that R1 byte by byte, and every
                    // one of those polls is a byte this model has to answer with the
                    // queued R1 - swallowing them as "write payload" made the driver see
                    // 0xFF eight times, give up, and re-initialise the whole card, which
                    // is why no write ever reached the image.
                    //
                    // While the write is waiting for its data token, a command start
                    // byte (01xxxxxx) means the host abandoned it: let the command
                    // assembler have that byte instead of swallowing it as payload.
                    if(writeTokenSeen || (data & 0xC0) != 0x40)
                    {
                        var writeResponse = WriteByte(data);
                        ProbeExchange(data, writeResponse);
                        return writeResponse;
                    }

                    writeActive = false;
                }

                if(commandBytes == 0)
                {
                    if((data & 0xC0) != 0x40)
                    {
                        // Not a command start byte. The driver clocks 0xFF while it polls
                        // for a response and toggles chip select around every byte, so
                        // ignoring anything that is not a command start is what keeps
                        // framing intact - and what lets a queued response survive until
                        // the read that wants it. A real card does the same: bytes seen
                        // before a command start are ignored.
                        var ignored = NextResponse();
                        ProbeExchange(data, ignored);
                        return ignored;
                    }

                    // A new command frame starts here, so whatever the previous command
                    // left unread can be dropped now - and named, because bytes still
                    // queued at this point are a payload the driver did not take off the
                    // bus (its block reads go through furi_hal_spi_bus_trx_dma).
                    if(pending.Count > 0)
                    {
                        if(discardsLogged < LogFirstDiscards)
                        {
                            discardsLogged++;
                            this.WarningLog(
                                "SD card: {0} queued byte(s) discarded - the host did not read them (last command's payload?)",
                                pending.Count);
                        }
                        discardedBytes += pending.Count;
                        pending.Clear();
                    }
                }

                if(commandBytes < CommandLength)
                {
                    command[commandBytes++] = data;
                    if(commandBytes == CommandLength)
                    {
                        HandleCommand();
                        // The frame is done: chip select no longer resets this, so the
                        // next command start byte begins a fresh frame from here.
                        commandBytes = 0;
                    }
                    var response = NextResponse();
                    ProbeExchange(data, response);
                    return response;
                }

                var idleResponse = NextResponse();
                ProbeExchange(data, idleResponse);
                return idleResponse;
            }
        }

        /// <summary>
        /// Names the byte exchanges that follow the first CMD24, once. The write path is
        /// invisible otherwise: everything after the command's response is data, so a
        /// driver that deasserts chip select (or gives up) leaves no trace in the log.
        /// </summary>
        private void ProbeExchange(byte received, byte response)
        {
            if(postWriteExchanges > 0)
            {
                postWriteExchanges--;
                this.InfoLog(
                    "SD card: post-write probe {0}: host sent 0x{1:X2}, card answered 0x{2:X2}",
                    24 - postWriteExchanges,
                    received,
                    response);
            }

            if(writeAttempts != 1 || postCmd24Exchanges >= 16)
            {
                return;
            }

            postCmd24Exchanges++;
            this.InfoLog(
                "SD card: write probe {0}: host sent 0x{1:X2}, card answered 0x{2:X2}",
                postCmd24Exchanges,
                received,
                response);
        }

        /// <summary>The firmware runs one transaction per byte; nothing to close here.</summary>
        public void FinishTransmission()
        {
        }

        public void Reset()
        {
            lock(sync)
            {
                commandBytes = 0;
                ready = false;
                appCmd = false;
                highCapacity = true;
                writeActive = false;
                writeTokenSeen = false;
                pending.Clear();
            }
        }

        public void Dispose()
        {
            lock(sync)
            {
                if(commandsSeen > 0)
                {
                    this.InfoLog(
                        "SD card: {0} command frames handled, {1} sectors read, {2} sectors written",
                        commandsSeen,
                        sectorsRead,
                        sectorsWritten);
                }
                if(image != null)
                {
                    image.Flush();
                    image.Dispose();
                }
                if(discardedBytes > 0)
                {
                    this.WarningLog(
                        "SD card: {0} bytes were never read by the host in total", discardedBytes);
                }
            }
        }

        /// <summary>How many sector writes to name in the log before going quiet.</summary>
        private const int LogFirstWrites = 16;

        private byte NextResponse()
        {
            return pending.Count > 0 ? pending.Dequeue() : IdleLine;
        }

        /// <summary>One NCR byte then the R1 - the driver reads until non-0xFF.</summary>
        private void EnqueueResponse(byte r1)
        {
            pending.Enqueue(IdleLine);
            pending.Enqueue(r1);
        }

        private void EnqueueRegister(byte[] data)
        {
            pending.Enqueue(DataToken);
            foreach(var value in data)
            {
                pending.Enqueue(value);
            }
            // CRC: the driver purges two bytes and never looks at them.
            pending.Enqueue(IdleLine);
            pending.Enqueue(IdleLine);
        }

        private void EnqueueBlock(uint sector)
        {
            pending.Enqueue(DataToken);
            var buffer = ReadSector(sector);
            for(var index = 0; index < BlockSize; index++)
            {
                pending.Enqueue(buffer[index]);
            }
            pending.Enqueue(IdleLine);
            pending.Enqueue(IdleLine);
        }

        private void LogCommand(byte cmd, uint arg, byte r1)
        {
            if(logNextCommands > 0)
            {
                logNextCommands--;
                this.InfoLog(
                    "SD card: after first write - cmd 0x{0:X2} arg 0x{1:X8} -> R1 0x{2:X2}",
                    cmd,
                    arg,
                    r1);
            }

            if(commandsSeen <= LogFirstCommands)
            {
                this.InfoLog(
                    "SD cmd 0x{0:X2} arg 0x{1:X8} -> R1 0x{2:X2}{3}",
                    cmd,
                    arg,
                    r1,
                    ready ? " ready" : " idle");
            }
            else if(commandsSeen % LogEveryCommands == 0)
            {
                this.InfoLog(
                    "SD card: {0} commands, {1} sectors read, {2} written (last cmd 0x{3:X2} arg 0x{4:X8})",
                    commandsSeen,
                    sectorsRead,
                    sectorsWritten,
                    cmd,
                    arg);
            }
        }

        private void HandleCommand()
        {
            commandsSeen++;
            var cmd = (byte)(command[0] & 0x3F);
            var arg = ((uint)command[1] << 24) | ((uint)command[2] << 16) |
                      ((uint)command[3] << 8) | command[4];
            var r1 = ready ? R1Ready : R1Idle;

            if(appCmd)
            {
                // Only ACMD41 matters to this driver. Accepting it is what ends the
                // init loop in furi_hal_sd.c.
                appCmd = false;
                if(cmd == 41)
                {
                    var wasReady = ready;
                    ready = true;
                    r1 = R1Ready;
                    EnqueueResponse(r1);
                    if(!wasReady)
                    {
                        this.InfoLog("SD card: ACMD41 accepted (arg 0x{0:X8}) - card ready", arg);
                    }
                }
                else
                {
                    r1 = R1IllegalCommand;
                    EnqueueResponse(r1);
                }
                LogCommand(cmd, arg, r1);
                return;
            }

            switch(cmd)
            {
                case 0: // GO_IDLE_STATE
                    ready = false;
                    r1 = R1Idle;
                    EnqueueResponse(r1);
                    break;

                case 1: // SEND_OP_COND (not sent in SPI mode by this driver)
                    EnqueueResponse(r1);
                    break;

                case 8: // SEND_IF_COND -> R7: R1 + 4 echoed bytes
                    r1 = R1Idle;
                    pending.Enqueue(IdleLine);
                    pending.Enqueue(r1);
                    pending.Enqueue(0x00);
                    pending.Enqueue(0x00);
                    pending.Enqueue((byte)((arg >> 8) & 0xFF));
                    pending.Enqueue((byte)(arg & 0xFF));
                    break;

                case 9: // SEND_CSD
                    EnqueueResponse(R1Ready);
                    EnqueueRegister(BuildCsd());
                    break;

                case 10: // SEND_CID
                    EnqueueResponse(R1Ready);
                    EnqueueRegister(BuildCid());
                    break;

                case 13: // SEND_STATUS -> R2 (R1 + status; the driver wants both 0)
                    EnqueueResponse(R1Ready);
                    pending.Enqueue(0x00);
                    break;

                case 16: // SET_BLOCKLEN
                    EnqueueResponse(R1Ready);
                    break;

                case 17: // READ_SINGLE_BLOCK
                    r1 = R1Ready;
                    blockSector = highCapacity ? arg : arg / BlockSize;
                    EnqueueResponse(r1);
                    EnqueueBlock(blockSector);
                    break;

                case 24: // WRITE_SINGLE_BLOCK
                    r1 = R1Ready;
                    blockSector = highCapacity ? arg : arg / BlockSize;
                    EnqueueResponse(r1);
                    writeActive = true;
                    writeTokenSeen = false;
                    writeIndex = 0;
                    writeCrc = 0;
                    writeAttempts++;
                    postCmd24Exchanges = 0;
                    if(writeBuffer == null)
                    {
                        // The driver retries a failed write, so the payload buffer is
                        // allocated once and reused instead of on every CMD24.
                        writeBuffer = new byte[BlockSize];
                    }
                    break;

                case 55: // APP_CMD
                    appCmd = true;
                    EnqueueResponse(r1);
                    break;

                case 58: // READ_OCR -> R3: R1 + OCR; bit 6 of the first byte is CCS
                    EnqueueResponse(r1);
                    pending.Enqueue(0xC0);
                    pending.Enqueue(0xFF);
                    pending.Enqueue(0x80);
                    pending.Enqueue(0x00);
                    break;

                // The driver loops CMD17/CMD24 per block, so the multi-block forms
                // are deliberately not implemented - saying "illegal command" is
                // better than answering wrongly.
                case 18:
                case 23:
                case 25:
                default:
                    r1 = R1IllegalCommand;
                    EnqueueResponse(r1);
                    break;
            }

            LogCommand(cmd, arg, r1);
        }

        /// <summary>How many sector reads to name in the log before going quiet.</summary>
        private const int LogFirstReads = 64;

        /// <summary>How many distinct sectors to name in the log (a mount uses most of them).</summary>
        private const int LogDistinctSectors = 192;

        /// <summary>How many discard events to name before going quiet.</summary>
        private const int LogFirstDiscards = 6;

        private byte[] ReadSector(uint sector)
        {
            var buffer = new byte[BlockSize];
            if(sector >= sectorCount)
            {
                this.WarningLog("SD card: read past the image (sector {0} of {1})", sector, sectorCount);
                return buffer;
            }

            // Naming the first reads is what makes a failed mount diagnosable: the
            // sequence (boot sector, FAT, root directory, data clusters) says how
            // far FatFS got before it gave up. The mount alone spends that budget,
            // so every sector that is touched for the first time is named as well -
            // that is what makes a later directory lookup or file open (i.e. reads
            // past the FAT) visible instead of silent.
            var firstTouch = sectorsTouched.Add(sector);
            if(sectorsRead < LogFirstReads || (firstTouch && sectorsTouched.Count <= LogDistinctSectors))
            {
                this.InfoLog("SD card: read sector {0} (offset 0x{1:X})", sector, (long)sector * BlockSize);
            }

            image.Seek((long)sector * BlockSize, SeekOrigin.Begin);
            var read = 0;
            while(read < BlockSize)
            {
                var got = image.Read(buffer, read, BlockSize - read);
                if(got <= 0)
                {
                    break;
                }
                read += got;
            }
            sectorsRead++;
            return buffer;
        }

        /// <summary>Absorbs the write payload the host clocks out after CMD24.</summary>
        private byte WriteByte(byte data)
        {
            if(!writeTokenSeen)
            {
                // Two dummy bytes separate CMD24's R1 from the token.
                if(data == DataToken)
                {
                    writeTokenSeen = true;
                    // Naming the token separates "CMD24 answered but the host's data
                    // never arrived" from "the host never issued CMD24 at all" - the
                    // 512 payload bytes come from a DMA transfer, which is where a
                    // stalled write is most likely to be stuck.
                    this.InfoLog(
                        "SD card: write token seen - expecting {0} bytes for sector {1}",
                        BlockSize,
                        blockSector);
                }
                return AcceptedToken;
            }

            if(writeIndex < BlockSize)
            {
                writeBuffer[writeIndex++] = data;
                return AcceptedToken;
            }

            writeCrc++;
            if(writeCrc < 2)
            {
                return AcceptedToken;
            }

            CommitWrite();
            writeActive = false;
            writeTokenSeen = false;
            // Data response: accepted (0x05), busy (0x00), then idle so the driver's
            // wait-for-0xFF after re-selecting the card terminates.
            //
            // The response is deliberately stretched over several reads, and the byte that
            // supplies the final CRC character answers 0x05 as well. Two probes in this
            // file measured why: the card produces 0x05 for the read after that CRC byte and
            // the bus router hands it straight to the SPI, yet the driver's token read still
            // sampled something else - the SPI controller inside the emulator does not
            // surface every received byte to the CPU in this "DMA burst, then single byte
            // reads" pattern. A real card may put anything on MISO while the host is
            // clocking data; the driver samples it only for the response token and the busy
            // byte and discards every other byte, so repeating 0x05 and then 0x00 across a
            // few reads is indistinguishable from real behaviour - and it makes the write
            // succeed whether or not the CPU sees each byte one transfer late.
            pending.Enqueue(0x05);
            pending.Enqueue(0x05);
            pending.Enqueue(0x05);
            pending.Enqueue(0x00);
            pending.Enqueue(0x00);
            pending.Enqueue(IdleLine);
            pending.Enqueue(IdleLine);
            pending.Enqueue(IdleLine);
            return 0x05;
        }

        private void CommitWrite()
        {
            if(blockSector >= sectorCount)
            {
                this.WarningLog(
                    "SD card: write past the image (sector {0} of {1})", blockSector, sectorCount);
                return;
            }

            image.Seek((long)blockSector * BlockSize, SeekOrigin.Begin);
            image.Write(writeBuffer, 0, BlockSize);
            image.Flush();
            sectorsWritten++;
            if(sectorsWritten == 1)
            {
                // The first accepted write is the interesting one: whatever the driver
                // sends next says whether it believed the write (another CMD24 for the
                // FAT or the payload) or gave up on it (a card re-initialisation).
                logNextCommands = 40;
                postWriteExchanges = 24;
            }
            if(sectorsWritten <= LogFirstWrites)
            {
                this.InfoLog(
                    "SD card: wrote sector {0} (offset 0x{1:X})",
                    blockSector,
                    (long)blockSector * BlockSize);
            }
        }

        /// <summary>CSD version 2 (SDHC): capacity = (DeviceSize + 1) * 512 KiB.</summary>
        private byte[] BuildCsd()
        {
            var deviceSize = (uint)Math.Max(0, (sectorCount / 1024) - 1);
            var csd = new byte[16];
            csd[0] = 0x40; // CSD_STRUCTURE = 1 (v2)
            csd[1] = 0x0E; // TAAC
            csd[2] = 0x00; // NSAC
            csd[3] = 0x32; // TRAN_SPEED (25 MHz)
            csd[4] = 0x5B; // CCC
            csd[5] = 0x59; // READ_BL_LEN = 9 -> 512 byte blocks
            csd[6] = 0x00;
            csd[7] = (byte)((deviceSize >> 16) & 0x3F);
            csd[8] = (byte)((deviceSize >> 8) & 0xFF);
            csd[9] = (byte)(deviceSize & 0xFF);
            csd[10] = 0x7F;
            csd[11] = 0x80;
            csd[12] = 0x0A;
            csd[13] = 0x40;
            csd[14] = 0x00;
            csd[15] = 0x01; // CRC field zero, "always 1" bit set
            return csd;
        }

        private byte[] BuildCid()
        {
            var cid = new byte[16];
            cid[0] = 0x03; // manufacturer: SanDisk
            cid[1] = (byte)'S';
            cid[2] = (byte)'D';
            cid[3] = (byte)'F';
            cid[4] = (byte)'L';
            cid[5] = (byte)'P';
            cid[6] = (byte)'R';
            cid[7] = (byte)'0';
            cid[8] = 0x10; // revision 1.0
            cid[9] = 0x00;
            cid[10] = 0x00;
            cid[11] = 0x00;
            cid[12] = 0x01; // serial number
            cid[13] = 0x01; // manufacturing year 2025: high nibble == 1
            cid[14] = 0x91; // low nibble of the year == 9, month == 1
            cid[15] = 0x01; // CRC field zero, "always 1" bit set
            return cid;
        }
    }
}
