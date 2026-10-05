using System;
using System.Collections.Generic;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Chudware
{
    // STM32F4 legacy SPI master. DR and the shift register each hold one frame.
    // Wire voltages and propagation belong exclusively to the shared SignalRuntime.
    public sealed class TimedSTM32SPI : IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral, IKnownSize, IGPIOReceiver
    {
        public TimedSTM32SPI(IMachine machine, string name, uint frequency = 0)
        {
            runtime = new BusRuntime(machine, name, "spi");
            clock = runtime.Config.Number("controller_clock_hz", frequency);
            sck = runtime.Signal("sck");
            mosi = runtime.Signal("mosi");
            miso = runtime.Signal("miso");
            cs = runtime.Signal("cs");
            IRQ = new GPIO();
            DMAReceive = new GPIO();
            DMATransmit = new GPIO();
            NSS = new GPIO();
            var peer = runtime.Config.Section("peer");
            ConfigurePeer((int)peer.Number("cpol", 0), (int)peer.Number("cpha", 0),
                peer.Flag("lsb_first", false), (int)peer.Number("data_bits", 8),
                peer.Number("clock_to_output_s", 0), peer.Number("setup_s", 0), peer.Number("hold_s", 0));
            readMask = (int)peer.Number("read_mask", 0x80);
            addressMask = (int)peer.Number("address_mask", 0x7F);
            autoIncrement = peer.Flag("auto_increment", true);
            SetResponse(peer.Text("response_hex", ""));
            SetRegisters(peer.Text("register_hex", ""));
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public GPIO DMAReceive { get; private set; }
        public GPIO DMATransmit { get; private set; }
        public GPIO NSS { get; private set; }
        public long Size { get { return 0x400; } }

        // Input 0 is the external peer's active-low chip select; input 1 is
        // controller hardware NSS. Software-managed NSS does not assert peer CS.
        public void OnGPIO(int number, bool value)
        {
            if(number == 0) SetChipSelect(value);
            else if(number == 1) SetNSS(value);
        }

        public void SetChipSelect(bool level)
        {
            cs.Drive("mcu", level ? 1 : 0, runtime.NowNs);
            ObservePeer();
            StartObservation();
            runtime.Emit("cs", "value=" + (level ? "1" : "0"));
        }

        public void SetNSS(bool level)
        {
            nssHigh = level;
            CheckModeFault();
        }

        public void ConfigurePeer(int cpol, int cpha, bool lsbFirst, int dataBits,
            double clockToOutputSeconds, double setupSeconds, double holdSeconds)
        {
            if((cpol != 0 && cpol != 1) || (cpha != 0 && cpha != 1) || (dataBits != 8 && dataBits != 16))
                throw new ArgumentException("SPI peer requires CPOL/CPHA 0 or 1 and 8 or 16 data bits.");
            peerCpol = cpol;
            peerCpha = cpha;
            peerLsbFirst = lsbFirst;
            peerBits = dataBits;
            outputDelay = Nanoseconds(clockToOutputSeconds);
            setup = Nanoseconds(setupSeconds);
            hold = Nanoseconds(holdSeconds);
        }

        // Response is a transaction-local byte stream, restarted at each CS.
        // Register mode, if configured, uses command bit read_mask and address_mask.
        public void SetResponse(string responseHex)
        {
            var text = (responseHex ?? "").Replace(" ", "").Replace(":", "").Replace(",", "");
            if((text.Length & 1) != 0) throw new ArgumentException("SPI response must contain whole hex bytes.");
            var next = new byte[text.Length / 2];
            for(var i = 0; i < next.Length; i++) next[i] = byte.Parse(text.Substring(i * 2, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            response = next;
        }

        // Atomic address:value map replacement; all numbers are hexadecimal.
        public void SetRegisters(string registerHex)
        {
            var next = new Dictionary<int, ushort>();
            foreach(var entry in (registerHex ?? "").Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var parts = entry.Split(':');
                if(parts.Length != 2) throw new ArgumentException("SPI register map must be addr:value comma-separated hex.");
                var address = int.Parse(parts[0].Trim(), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                var value = ushort.Parse(parts[1].Trim(), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                if(address < 0 || address > 0xFFFF) throw new ArgumentOutOfRangeException("registerHex");
                next[address] = value;
            }
            registers = next;
        }

        public byte ReadByte(long offset) { return (byte)ReadAccess(offset, 8); }
        public ushort ReadWord(long offset) { return (ushort)ReadAccess(offset, 16); }
        public uint ReadDoubleWord(long offset) { return ReadAccess(offset, 32); }
        public void WriteByte(long offset, byte value) { WriteAccess(offset, value, 8); }
        public void WriteWord(long offset, ushort value) { WriteAccess(offset, value, 16); }
        public void WriteDoubleWord(long offset, uint value) { WriteAccess(offset, value, 32); }

        public void Reset()
        {
            runtime.Reset();
            transferGeneration++;
            peerGeneration++;
            observationGeneration++;
            cr1 = cr2 = rxData = txData = 0;
            crcPolynomial = 7;
            rxCrc = txCrc = 0;
            i2sConfiguration = 0;
            i2sPrescaler = 2;
            txFull = busy = rxFull = overrun = modeFault = crcError = crcFrame = false;
            overrunReadData = modeFaultReadStatus = rxDmaPending = txDmaPending = false;
            observing = selected = rejected = false;
            pendingPeerWrites.Clear();
            nssHigh = true;
            previousClock = 0;
            sck.Drive("mcu", 0, runtime.NowNs);
            mosi.Drive("mcu", 0, runtime.NowNs);
            cs.Drive("mcu", 1, runtime.NowNs);
            miso.Drive("peer", -1, runtime.NowNs);
            NSS.Set(true);
            UpdateInterrupts();
        }

        private uint ReadAccess(long offset, int width)
        {
            if(offset < 0 || (offset & 3) + width / 8 > 4) return 0;
            var aligned = offset & ~3L;
            uint value;
            switch(aligned)
            {
                case 0x00: value = cr1; break;
                case 0x04: value = cr2; break;
                case 0x08:
                    value = Status;
                    if(overrun && overrunReadData) { overrun = false; overrunReadData = false; }
                    if(modeFault) modeFaultReadStatus = true;
                    UpdateInterrupts();
                    break;
                case 0x0C:
                    value = rxData;
                    rxFull = false;
                    if(overrun) overrunReadData = true;
                    UpdateInterrupts();
                    break;
                case 0x10: value = crcPolynomial; break;
                case 0x14: value = rxCrc; break;
                case 0x18: value = txCrc; break;
                case 0x1C: value = i2sConfiguration; break;
                case 0x20: value = i2sPrescaler; break;
                default: value = 0; break;
            }
            return value >> ((int)(offset & 3) * 8);
        }

        private void WriteAccess(long offset, uint value, int width)
        {
            if(offset < 0 || (offset & 3) + width / 8 > 4) return;
            var shift = (int)(offset & 3) * 8;
            var mask = width == 32 ? uint.MaxValue : ((1U << width) - 1) << shift;
            value <<= shift;
            switch(offset & ~3L)
            {
                case 0x00:
                    var old = cr1;
                    cr1 = ((cr1 & ~mask) | (value & mask)) & 0xFFFF;
                    if(modeFault && modeFaultReadStatus) { modeFault = false; modeFaultReadStatus = false; }
                    if((old & CrcEnable) == 0 && (cr1 & CrcEnable) != 0) rxCrc = txCrc = 0;
                    if(!busy) sck.Drive("mcu", Polarity, runtime.NowNs);
                    if((cr2 & 4) != 0 && (cr1 & Master) != 0)
                    {
                        NSS.Set(!Enabled);
                        SetChipSelect(!Enabled);
                    }
                    CheckModeFault();
                    if(Enabled && (cr1 & Master) == 0) runtime.Emit("unsupported", "cause=slave_controller_mode");
                    if((old & Enable) != 0 && !Enabled && busy) AbortTransfer("disabled");
                    TryStart();
                    break;
                case 0x04:
                    cr2 = ((cr2 & ~mask) | (value & mask)) & 0xFF;
                    if((cr2 & 4) != 0 && (cr1 & Master) != 0) { NSS.Set(!Enabled); SetChipSelect(!Enabled); }
                    CheckModeFault();
                    break;
                case 0x08:
                    if((mask & 16) != 0 && (value & 16) == 0) crcError = false;
                    break;
                case 0x0C:
                    // Writing while TXE=0 cannot create another software queue.
                    if(!txFull) { txData = (ushort)value; txFull = true; TryStart(); }
                    break;
                case 0x10: crcPolynomial = (ushort)(((uint)crcPolynomial & ~mask) | (value & mask)); break;
                case 0x1C:
                    i2sConfiguration = ((i2sConfiguration & ~mask) | (value & mask)) & 0xFFF;
                    if((i2sConfiguration & 0x800) != 0) runtime.Emit("unsupported", "cause=i2s_mode");
                    break;
                case 0x20: i2sPrescaler = ((i2sPrescaler & ~mask) | (value & mask)) & 0x3FF; break;
            }
            UpdateInterrupts();
        }

        private void TryStart()
        {
            if(busy || !Enabled || (cr1 & Master) == 0 || modeFault || (i2sConfiguration & 0x800) != 0) return;
            if((cr1 & 0x8000) != 0) { runtime.Emit("unsupported", "cause=one_wire_signal_mapping"); return; }
            var receiveOnly = (cr1 & 0x400) != 0;
            if(!txFull && !receiveOnly) return;
            if(clock <= 0) { runtime.Emit("unsupported", "cause=controller_clock_missing"); return; }
            busy = true;
            var token = ++transferGeneration;
            runtime.Schedule((ulong)Math.Max(1, Math.Ceiling(1e9 / clock)), delegate
            {
                if(token != transferGeneration || !Enabled) return;
                shiftControl = cr1;
                shiftBits = (shiftControl & 0x800) != 0 ? 16 : 8;
                shiftTx = receiveOnly ? (ushort)0 : txData;
                shiftRx = 0;
                txFull = false;
                edgeIndex = 0;
                halfPeriod = Math.Max(1UL, (ulong)Math.Ceiling(1e9 * (1 << (int)((shiftControl >> 3) & 7)) / clock));
                sck.Drive("mcu", ShiftPolarity, runtime.NowNs);
                if((shiftControl & 1) == 0) DriveMosi(0);
                StartObservation();
                runtime.Emit("transfer_start", "value=" + shiftTx.ToString("X4") + " bits=" + shiftBits + " half_period_ns=" + halfPeriod);
                UpdateInterrupts();
                ScheduleEdge(token);
            });
            UpdateInterrupts();
        }

        private void ScheduleEdge(int token)
        {
            runtime.Schedule(halfPeriod, delegate
            {
                if(token != transferGeneration) return;
                var leading = (edgeIndex & 1) == 0;
                var bit = edgeIndex / 2;
                sck.Drive("mcu", leading ? 1 - ShiftPolarity : ShiftPolarity, runtime.NowNs);
                if((shiftControl & 1) != 0 && leading) DriveMosi(bit);
                if((shiftControl & 1) == 0 && !leading && bit + 1 < shiftBits) DriveMosi(bit + 1);
                ObservePeer();
                StartObservation();
                var sampleEdge = (shiftControl & 1) == 0 ? leading : !leading;
                if(sampleEdge)
                {
                    var sample = miso.Sample("mcu", runtime.NowNs);
                    var receivedBit = sample.Level == 0 ? 0 : 1;
                    if(sample.Level < 0) runtime.Emit("sample_indeterminate", "signal=miso bit=" + bit + " cause=" + sample.Cause);
                    var position = (shiftControl & 0x80) != 0 ? bit : shiftBits - 1 - bit;
                    shiftRx |= (ushort)(receivedBit << position);
                    if(bit + 1 == shiftBits) LatchReceive();
                }
                if(token != transferGeneration) return;
                edgeIndex++;
                if(edgeIndex < shiftBits * 2) ScheduleEdge(token);
                else CompleteTransfer();
            });
        }

        private void DriveMosi(int bit)
        {
            var position = (shiftControl & 0x80) != 0 ? bit : shiftBits - 1 - bit;
            var outputEnabled = (shiftControl & 0x400) == 0;
            mosi.Drive("mcu", outputEnabled ? (shiftTx >> position) & 1 : -1, runtime.NowNs);
        }

        private void LatchReceive()
        {
            if(crcFrame) return;
            if(rxFull) { overrun = true; overrunReadData = false; }
            else { rxData = shiftRx; rxFull = true; }
            runtime.Emit("receive_ready", "register=DR value=" + shiftRx.ToString("X4") +
                " outcome=" + (overrun ? "overrun" : "received"));
            UpdateInterrupts();
        }

        private void CompleteTransfer()
        {
            if((shiftControl & CrcEnable) != 0)
            {
                if(crcFrame)
                {
                    if(shiftRx != rxCrc) crcError = true;
                    crcFrame = false;
                }
                else
                {
                    txCrc = UpdateCrc(txCrc, shiftTx);
                    rxCrc = UpdateCrc(rxCrc, shiftRx);
                }
            }
            runtime.Emit("transfer_complete", "value=" + shiftRx.ToString("X4") + " outcome=" + (overrun ? "overrun" : "received"));
            busy = false;
            if((cr1 & (CrcEnable | CrcNext)) == (CrcEnable | CrcNext))
            {
                cr1 &= ~CrcNext;
                txData = txCrc;
                txFull = true;
                crcFrame = true;
            }
            UpdateInterrupts();
            TryStart();
        }

        private ushort UpdateCrc(ushort crc, ushort word)
        {
            var top = 1 << (shiftBits - 1);
            var mask = shiftBits == 16 ? 0xFFFF : 0xFF;
            for(var i = 0; i < shiftBits; i++)
            {
                var position = (shiftControl & 0x80) != 0 ? i : shiftBits - 1 - i;
                var feedback = ((crc & top) != 0 ? 1 : 0) ^ ((word >> position) & 1);
                crc = (ushort)((crc << 1) & mask);
                if(feedback != 0) crc ^= (ushort)(crcPolynomial & mask);
            }
            return crc;
        }

        private void AbortTransfer(string cause)
        {
            transferGeneration++;
            busy = false;
            crcFrame = false;
            sck.Drive("mcu", Polarity, runtime.NowNs);
            runtime.Emit("transfer_abort", "cause=" + cause);
            UpdateInterrupts();
        }

        private void CheckModeFault()
        {
            if(!Enabled || (cr1 & Master) == 0) return;
            var inputLow = (cr1 & 0x200) != 0 ? (cr1 & 0x100) == 0 : (cr2 & 4) == 0 && !nssHigh;
            if(!inputLow) return;
            modeFault = true;
            modeFaultReadStatus = false;
            cr1 &= ~(Enable | Master);
            if(busy) AbortTransfer("nss_mode_fault");
            NSS.Set(true);
            runtime.Emit("mode_fault", "register=SR cause=nss_low");
            UpdateInterrupts();
        }

        private void StartObservation()
        {
            var horizon = Math.Max(cs.SettlingTimeNs, sck.SettlingTimeNs);
            observationUntil = runtime.NowNs + horizon;
            if(observing || horizon == 0) return;
            observing = true;
            ObserveAgain(observationGeneration);
        }

        private ulong ObservationInterval
        {
            get
            {
                var networkStep = sck.PollIntervalNs;
                if(cs.PollIntervalNs != 0) networkStep = networkStep == 0 ? cs.PollIntervalNs : Math.Min(networkStep, cs.PollIntervalNs);
                if(networkStep == 0) networkStep = IdleObservationInterval;
                return busy ? Math.Max(1UL, Math.Min(networkStep, halfPeriod / 16)) : Math.Max(1UL, networkStep);
            }
        }

        private void ObserveAgain(int token)
        {
            // Observe actual propagated edges, not just the master's drive commands.
            // Quantization is emitted with each completed peer word. Stop polling a
            // settled idle selection; subsequent GPIO/clock drives restart observation.
            runtime.Schedule(ObservationInterval, delegate
            {
                if(token != observationGeneration) return;
                ObservePeer();
                if(busy || runtime.NowNs < observationUntil) ObserveAgain(token);
                else observing = false;
            });
        }

        private void ObservePeer()
        {
            var now = runtime.NowNs;
            var csLevel = cs.Sample("peer", now).Level;
            var clockLevel = sck.Sample("peer", now).Level;
            if(csLevel == 0 && !selected)
            {
                selected = true;
                rejected = false;
                pendingPeerWrites.Clear();
                selectedAt = now;
                peerGeneration++;
                peerRx = 0;
                peerSampleCount = peerOutputCount = responseIndex = 0;
                hasCommand = false;
                lastSampleAt = 0;
                previousClock = clockLevel >= 0 ? clockLevel : peerCpol;
                peerTx = NextPeerWord();
                if(peerCpha == 0) DrivePeerBit(0);
                runtime.Emit("peer_select", "outcome=selected");
            }
            else if(csLevel == 1 && selected)
            {
                var holdViolation = lastSampleAt != 0 && now - lastSampleAt < hold;
                var recognized = !rejected && peerSampleCount == 0 && !holdViolation;
                if(recognized)
                {
                    foreach(var write in pendingPeerWrites)
                    {
                        registers[write.Key] = write.Value;
                        runtime.Emit("peer_register_write", "register=" + write.Key.ToString("X") +
                            " value=" + write.Value.ToString("X4"));
                    }
                }
                pendingPeerWrites.Clear();
                runtime.Emit("peer_transaction", "outcome=" + (recognized ? "recognized" : "rejected") +
                    (holdViolation ? " cause=cs_hold" : ""));
                selected = false;
                peerGeneration++;
                miso.Drive("peer", -1, now);
            }
            if(clockLevel < 0 || !selected || clockLevel == previousClock) return;
            previousClock = clockLevel;
            if(rejected) return;
            if(now - selectedAt < setup)
            {
                RejectPeer("cause=cs_setup");
                return;
            }
            var leading = clockLevel != peerCpol;
            var sampling = peerCpha == 0 ? leading : !leading;
            if(sampling)
            {
                var sample = mosi.Sample("peer", now);
                if(sample.Level < 0)
                {
                    runtime.Emit("sample_indeterminate", "signal=mosi bit=" + peerSampleCount +
                        " cause=" + sample.Cause);
                    RejectPeer("signal=mosi cause=" + sample.Cause);
                    return;
                }
                var position = peerLsbFirst ? peerSampleCount : peerBits - 1 - peerSampleCount;
                peerRx |= (ushort)(sample.Level << position);
                lastSampleAt = now;
                peerSampleCount++;
                if(peerSampleCount == peerBits)
                {
                    ProcessPeerWord(peerRx);
                    peerRx = 0;
                    peerSampleCount = 0;
                    peerOutputCount = 0;
                    peerTx = NextPeerWord();
                }
            }
            else
            {
                // CPHA=0's first bit was already present before the first edge.
                if(peerCpha == 0 && peerOutputCount == 0 && peerSampleCount != 0) peerOutputCount = 1;
                DrivePeerBit(peerOutputCount);
                peerOutputCount++;
            }
        }

        private void RejectPeer(string fields)
        {
            rejected = true;
            peerGeneration++;
            pendingPeerWrites.Clear();
            miso.Drive("peer", -1, runtime.NowNs);
            runtime.Emit("peer_transaction", "outcome=rejected " + fields);
        }

        private void DrivePeerBit(int bit)
        {
            if(bit >= peerBits) return;
            var position = peerLsbFirst ? bit : peerBits - 1 - bit;
            var level = (peerTx >> position) & 1;
            var token = peerGeneration;
            runtime.Schedule(outputDelay, delegate
            {
                if(token == peerGeneration && selected && !rejected) miso.Drive("peer", level, runtime.NowNs);
            });
        }

        private ushort NextPeerWord()
        {
            if(registers.Count != 0)
            {
                ushort value;
                return hasCommand && commandRead && registers.TryGetValue(registerAddress, out value) ? value : (ushort)0;
            }
            if(responseIndex >= response.Length) return (ushort)(peerBits == 16 ? 0xFFFF : 0xFF);
            var first = response[responseIndex++];
            if(peerBits == 8) return first;
            var second = responseIndex < response.Length ? response[responseIndex++] : 0xFF;
            return (ushort)((first << 8) | second);
        }

        private void ProcessPeerWord(ushort word)
        {
            runtime.Emit("peer_word", "value=" + word.ToString("X4") + " sample_quantization_ns=" +
                (sck.SettlingTimeNs == 0 && cs.SettlingTimeNs == 0 ? 0UL : ObservationInterval));
            if(registers.Count == 0) return;
            if(!hasCommand)
            {
                hasCommand = true;
                commandRead = (word & readMask) != 0;
                registerAddress = word & addressMask;
                return;
            }
            var address = registerAddress;
            // Commit only after the entire CS transaction is electrically valid.
            // A later indeterminate or incomplete word must not leave earlier writes.
            if(!commandRead) pendingPeerWrites[address] = word;
            if(autoIncrement) registerAddress = (registerAddress + 1) & addressMask;
        }

        private void UpdateInterrupts()
        {
            IRQ.Set((rxFull && (cr2 & 0x40) != 0) || (!txFull && (cr2 & 0x80) != 0)
                || ((overrun || modeFault || crcError) && (cr2 & 0x20) != 0));
            // Renode STM32DMA reacts to request assertion; pulse on each available
            // beat rather than holding a request across successive DR refills.
            var rxRequest = rxFull && (cr2 & 1) != 0;
            var txRequest = Enabled && !txFull && (cr2 & 2) != 0;
            var pulseRx = rxRequest && !rxDmaPending;
            var pulseTx = txRequest && !txDmaPending;
            rxDmaPending = rxRequest;
            txDmaPending = txRequest;
            // Requests can synchronously re-enter DR through a wired DMA engine.
            if(pulseRx) DMAReceive.Blink();
            if(pulseTx && Enabled && !txFull && (cr2 & 2) != 0) DMATransmit.Blink();
        }

        private uint Status { get { return (rxFull ? 1U : 0) | (!txFull ? 2U : 0) | (crcError ? 16U : 0)
            | (modeFault ? 32U : 0) | (overrun ? 64U : 0) | (busy ? 128U : 0); } }
        private bool Enabled { get { return (cr1 & Enable) != 0; } }
        private int Polarity { get { return (int)((cr1 >> 1) & 1); } }
        private int ShiftPolarity { get { return (int)((shiftControl >> 1) & 1); } }
        private static ulong Nanoseconds(double seconds)
        {
            if(double.IsNaN(seconds) || double.IsInfinity(seconds) || seconds < 0 || seconds > ulong.MaxValue / 1e9)
                throw new ArgumentOutOfRangeException("seconds");
            return (ulong)Math.Ceiling(seconds * 1e9);
        }

        private const uint Master = 4, Enable = 0x40, CrcNext = 0x1000, CrcEnable = 0x2000;
        private const ulong IdleObservationInterval = 100;
        private readonly BusRuntime runtime;
        private readonly SignalRuntime sck, mosi, miso, cs;
        private readonly double clock;
        private Dictionary<int, ushort> registers = new Dictionary<int, ushort>();
        private readonly Dictionary<int, ushort> pendingPeerWrites = new Dictionary<int, ushort>();
        private byte[] response = new byte[0];
        private uint cr1, cr2, shiftControl, i2sConfiguration, i2sPrescaler;
        private ushort rxData, txData, shiftTx, shiftRx, crcPolynomial, rxCrc, txCrc, peerRx, peerTx;
        private bool txFull, busy, rxFull, overrun, modeFault, crcError, crcFrame;
        private bool overrunReadData, modeFaultReadStatus, rxDmaPending, txDmaPending;
        private bool nssHigh, observing, selected, rejected, peerLsbFirst, hasCommand, commandRead, autoIncrement;
        private int transferGeneration, peerGeneration, observationGeneration, shiftBits, edgeIndex;
        private int peerCpol, peerCpha, peerBits, peerSampleCount, peerOutputCount, previousClock, responseIndex, readMask, addressMask, registerAddress;
        private ulong halfPeriod = 1, outputDelay, setup, hold, selectedAt, lastSampleAt, observationUntil;
    }
}
