using System;
using System.Collections.Generic;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Chudware
{
    // STM32F4 master-mode register controller. Bytes are shifted on the shared
    // electrical signals; neither a monitor update nor a DR access completes a byte.
    [AllowedTranslations(AllowedTranslation.WordToDoubleWord)]
    public sealed class TimedSTM32I2C : IDoubleWordPeripheral, IBytePeripheral, IKnownSize
    {
        public TimedSTM32I2C(IMachine machine, string name, uint frequency = 16000000)
        {
            runtime = new BusRuntime(machine, name, "i2c");
            clockHz = runtime.Config.Number("controller_clock_hz", frequency);
            scl = runtime.Signal("scl");
            sda = runtime.Signal("sda");
            EventInterrupt = new GPIO();
            ErrorInterrupt = new GPIO();
            Reset();
        }

        public GPIO EventInterrupt { get; private set; }
        public GPIO ErrorInterrupt { get; private set; }
        public long Size { get { return 0x400; } }

        // A device must be declared before a stream arrives. Generic maps are
        // useful for firmware with a documented, non-sensor register protocol.
        public void SetDevice(int address, string model)
        {
            if(address < 0 || address > 127) throw new ArgumentOutOfRangeException("address");
            var device = new RegisterDevice(model);
            lock(deviceLock) devices[address] = device;
        }

        // The monitor accepts e.g. "75:68,47:01,48:00". Parse the entire update
        // first: malformed input cannot leave a partially updated sample map.
        public void SetRegisters(int address, string registerHex)
        {
            var updates = new Dictionary<int, byte>();
            foreach(var item in registerHex.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var pair = item.Split(':');
                if(pair.Length != 2) throw new ArgumentException("Expected comma-separated hex register:value pairs");
                int reg = int.Parse(pair[0], NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                int value = int.Parse(pair[1], NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                if(reg < 0 || reg > 255 || value < 0 || value > 255) throw new ArgumentOutOfRangeException("registerHex");
                updates[reg] = (byte)value;
            }
            lock(deviceLock)
            {
                RegisterDevice device;
                if(!devices.TryGetValue(address, out device)) throw new ArgumentException("Declare the device with SetDevice before updating registers");
                device.Update(updates);
            }
            runtime.Emit("register_update", "address=" + address + " count=" + updates.Count);
        }

        public void Reset()
        {
            runtime.Reset();
            cr1 = cr2 = oar1 = oar2 = sr1 = sr2 = ccr = fltr = 0;
            trise = 2;
            state = State.Idle;
            busy = startPending = stopPending = txQueued = rxFull = rxShiftFull = addressRead = false;
            receiveEnded = false;
            selected = null;
            Release();
            UpdateInterrupts();
        }

        public byte ReadByte(long offset) { return (byte)ReadDoubleWord(offset); }
        public void WriteByte(long offset, byte value) { WriteDoubleWord(offset, value); }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case 0x00: return cr1;
            case 0x04: return cr2;
            case 0x08: return oar1;
            case 0x0C: return oar2;
            case 0x10: return ReadData();
            case 0x14: addressRead = (sr1 & ADDR) != 0; return sr1;
            case 0x18:
                uint value = sr2;
                if(addressRead && (sr1 & ADDR) != 0)
                {
                    addressRead = false;
                    sr1 &= ~ADDR;
                    if(state == State.Read)
                    {
                        receiveBudget = (cr1 & ACK) != 0 ? -1 : ((cr1 & POS) != 0 ? 2 : 1);
                        BeginReceive();
                    }
                    else if(state == State.Write) { sr1 |= TXE; TryTransmit(); }
                    UpdateInterrupts();
                }
                return value;
            case 0x1C: return ccr;
            case 0x20: return trise;
            case 0x24: return fltr;
            default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case 0x00:
                if((value & SWRST) != 0) { Reset(); cr1 = SWRST; return; }
                uint old = cr1;
                cr1 = value & 0x7FFFu;
                if((value & PE) == 0 && (old & PE) != 0)
                {
                    runtime.Reset();
                    state = State.Idle; sr1 = sr2 = 0;
                    busy = startPending = stopPending = txQueued = rxFull = rxShiftFull = false;
                    addressRead = receiveEnded = false; selected = null;
                    cr1 &= ~(START | STOP);
                    Release(); UpdateInterrupts();
                    return;
                }
                if((old & ACK) != 0 && (cr1 & ACK) == 0 && state == State.Read &&
                    (sr1 & ADDR) == 0 && !receiveEnded && receiveBudget < 0)
                {
                    // POS shifts NACK to the next byte, including when ACK is
                    // cleared just after ADDR releases the first receive byte.
                    // Never restart a finite budget on subsequent CR1 writes.
                    receiveBudget = (cr1 & POS) != 0 ? 2 : 1;
                }
                if((value & PE) != 0)
                {
                    if((value & START) != 0) { startPending = true; TryControl(); }
                    if((value & STOP) != 0) { stopPending = true; startPending = false; TryControl(); }
                }
                break;
            case 0x04: cr2 = value & 0x1FFF; break;
            case 0x08: oar1 = value; break;
            case 0x0C: oar2 = value; break;
            case 0x10: WriteData((byte)value); break;
            case 0x14: sr1 &= value | ~ERRORS; break; // error flags are write-zero-to-clear
            case 0x1C: ccr = value & 0xFFFF; break;
            case 0x20: trise = value & 0x3F; break;
            case 0x24: fltr = value & 0x1F; break;
            }
            UpdateInterrupts();
        }

        private ulong LowNs
        {
            get
            {
                uint divider = Math.Max(1u, ccr & 0xFFF);
                uint factor = (ccr & 0x8000) == 0 ? 1u : ((ccr & 0x4000) == 0 ? 2u : 16u);
                return Math.Max(1UL, (ulong)Math.Ceiling(divider * (double)factor * 1e9 / clockHz));
            }
        }
        private ulong HighNs
        {
            get
            {
                uint divider = Math.Max(1u, ccr & 0xFFF);
                uint factor = (ccr & 0xC000) == 0xC000 ? 9u : 1u;
                return Math.Max(1UL, (ulong)Math.Ceiling(divider * (double)factor * 1e9 / clockHz));
            }
        }
        private ulong PeerDelay(string key)
        {
            return (ulong)Math.Max(0, Math.Ceiling(runtime.Config.Section("peer").Number(key, 0) * 1e9));
        }
        private void Drive(SignalRuntime signal, string endpoint, int level) { signal.Drive(endpoint, level, runtime.NowNs); }
        private void Release()
        {
            ++peerResponsePhase;
            Drive(scl, "mcu", -1); Drive(scl, "peer", -1);
            Drive(sda, "mcu", -1); Drive(sda, "peer", -1);
        }
        private void After(ulong ns, Action action) { runtime.Schedule(ns, action); }
        private void AfterPeerResponse(int level)
        {
            ulong phase = peerResponsePhase;
            After(PeerDelay("response_delay_s"), () =>
            {
                if(phase == peerResponsePhase) Drive(sda, "peer", level);
            });
        }
        private void UpdateInterrupts()
        {
            EventInterrupt.Set((cr2 & (1u << 9)) != 0 && ((sr1 & (SB | ADDR | BTF)) != 0 || ((cr2 & (1u << 10)) != 0 && (sr1 & (TXE | RXNE)) != 0)));
            ErrorInterrupt.Set((cr2 & (1u << 8)) != 0 && (sr1 & ERRORS) != 0);
        }

        // A released clock is not necessarily high. Keep the operation pending
        // while RC settling or slave stretching holds SCL below VIH. Polling is
        // virtual-time scheduling; firmware continues running and may reset/STOP.
        private void WaitHigh(SignalRuntime signal, string endpoint, Action ready)
        {
            WaitHighPending(signal, endpoint, ready, runtime.NowNs, false);
        }
        private void WaitHighPending(SignalRuntime signal, string endpoint, Action ready, ulong since, bool reported)
        {
            var sample = signal.Sample(endpoint, runtime.NowNs);
            if(sample.Level == 1) { ready(); return; }
            if(!reported && runtime.NowNs - since >= LowNs + HighNs)
            {
                runtime.Emit("pending", "signal=" + (ReferenceEquals(signal, scl) ? "scl" : "sda") + " cause=" + sample.Cause + " voltage_v=" + sample.Voltage.ToString("R", CultureInfo.InvariantCulture));
                reported = true;
            }
            bool alreadyReported = reported;
            After(Math.Max(100UL, HighNs / 8), () => WaitHighPending(signal, endpoint, ready, since, alreadyReported));
        }
        private void RaiseClock(Action ready)
        {
            Drive(scl, "mcu", -1);
            WaitHigh(scl, "mcu", ready);
        }
        private void RaiseTransferClock(Action ready)
        {
            ulong stretch = PeerDelay("clock_stretch_s");
            if(stretch != 0)
            {
                Drive(scl, "peer", 0);
                Drive(scl, "mcu", -1);
                After(stretch, () => { Drive(scl, "peer", -1); WaitHigh(scl, "mcu", ready); });
            }
            else RaiseClock(ready);
        }
        private void TryControl()
        {
            if(busy || (cr1 & PE) == 0) return;
            if(state == State.Write && txQueued) { TryTransmit(); return; }
            if(stopPending) { BeginStop(); return; }
            if(!startPending) return;
            busy = true;
            ++peerResponsePhase;
            bool repeated = (sr2 & 2) != 0;
            sr2 |= 3; // MSL and BUSY remain true until a real STOP
            sr2 &= ~4u;
            sr1 &= ~(SB | ADDR | TXE | BTF);
            Drive(scl, "mcu", 0);
            Drive(sda, "peer", -1);
            Drive(sda, "mcu", -1);
            After(LowNs, () => RaiseClock(() => WaitHigh(sda, "mcu", () =>
            {
                Drive(sda, "mcu", 0);
                After(HighNs, () =>
                {
                    Drive(scl, "mcu", 0);
                    busy = false; startPending = false; cr1 &= ~START;
                    state = State.Address; sr1 |= SB;
                    runtime.Emit(repeated ? "repeated_start" : "start", "outcome=ready");
                    UpdateInterrupts();
                    TryControl();
                });
            })));
        }
        private void BeginStop()
        {
            busy = true;
            ++peerResponsePhase;
            Drive(scl, "mcu", 0); Drive(sda, "peer", -1); Drive(sda, "mcu", 0);
            After(LowNs, () => RaiseClock(() => After(HighNs / 2, () =>
            {
                Drive(sda, "mcu", -1);
                WaitHigh(sda, "mcu", () =>
                {
                    busy = false; stopPending = false; cr1 &= ~STOP;
                    state = State.Idle; sr2 = 0;
                    sr1 &= ~(SB | ADDR | TXE | BTF);
                    txQueued = false; selected = null;
                    runtime.Emit("stop", "outcome=complete");
                    UpdateInterrupts();
                    TryControl();
                });
            })));
        }

        private void WriteData(byte value)
        {
            if((cr1 & PE) == 0) return;
            if(state == State.Address && !busy && (sr1 & SB) != 0)
            {
                sr1 &= ~SB;
                busy = true;
                ShiftByte(value, false, true, (sampled, ack) =>
                {
                    busy = false;
                    if(!ack) { sr1 |= AF; state = State.Fault; }
                    else
                    {
                        state = (sampled & 1) != 0 ? State.Read : State.Write;
                        sr1 |= ADDR;
                        if(state == State.Write) { sr2 |= 4; if(selected != null) selected.BeginWrite(); }
                        else
                        {
                            sr2 &= ~4u; receiveEnded = false;
                            lock(deviceLock) snapshot = selected != null ? selected.Snapshot() : null;
                        }
                    }
                    runtime.Emit("address", "value=" + (sampled >> 1) + " read=" + (sampled & 1) + " outcome=" + (ack ? "ack" : "nack"));
                    UpdateInterrupts(); TryControl();
                });
                return;
            }
            if(state != State.Write || txQueued) return;
            txData = value; txQueued = true;
            sr1 &= ~(TXE | BTF);
            TryTransmit();
        }
        private void TryTransmit()
        {
            if(busy || !txQueued || state != State.Write || (sr1 & ADDR) != 0) return;
            byte value = txData;
            txQueued = false; busy = true; sr1 |= TXE; sr1 &= ~BTF;
            UpdateInterrupts();
            ShiftByte(value, false, false, (sampled, ack) =>
            {
                busy = false;
                if(!ack) { sr1 |= AF; state = State.Fault; }
                else if(selected != null) selected.Write(sampled);
                if(!txQueued && ack) sr1 |= TXE | BTF;
                runtime.Emit("write", "value=" + sampled + " outcome=" + (ack ? "ack" : "nack"));
                UpdateInterrupts(); TryControl(); TryTransmit();
            });
        }
        private uint ReadData()
        {
            byte value = rxData;
            if(rxFull)
            {
                rxFull = rxShiftFull;
                if(rxShiftFull) rxData = rxShift;
                rxShiftFull = false;
                sr1 &= ~(RXNE | BTF);
                if(rxFull) sr1 |= RXNE;
                // Reading DR releases controller clock stretching when both
                // finite receive registers were full.
                if(!busy) { TryControl(); if(!stopPending) BeginReceive(); }
                UpdateInterrupts();
            }
            return value;
        }
        private void BeginReceive()
        {
            if(busy || rxShiftFull || state != State.Read || receiveEnded || (sr1 & ADDR) != 0 || startPending) return;
            busy = true;
            byte value = selected != null ? snapshot[selected.Pointer] : (byte)0xFF;
            int register = selected != null ? selected.Pointer : -1;
            ShiftByte(value, true, false, (sampled, ack) =>
            {
                busy = false;
                if(selected != null) selected.Advance();
                if(rxFull) { rxShift = sampled; rxShiftFull = true; sr1 |= BTF; }
                else { rxData = sampled; rxFull = true; sr1 |= RXNE; }
                if(receiveBudget > 0) --receiveBudget;
                receiveEnded = !ack;
                runtime.Emit("read", "register=" + register + " value=" + sampled + " outcome=" + (ack ? "ack" : "nack"));
                UpdateInterrupts();
                TryControl();
                if(!stopPending && !startPending) BeginReceive();
            });
        }

        private void ShiftByte(byte value, bool fromPeer, bool address, Action<byte, bool> complete)
        {
            byte received = 0;
            Action<int> bit = null;
            bit = index =>
            {
                ++peerResponsePhase;
                Drive(scl, "mcu", 0);
                Drive(sda, fromPeer ? "mcu" : "peer", -1);
                int level = (value & (0x80 >> index)) == 0 ? 0 : -1;
                // Peer response delay changes SDA setup relative to SCL, not
                // just a timestamp on an already completed register read.
                if(fromPeer)
                {
                    AfterPeerResponse(level);
                }
                else Drive(sda, "mcu", level);
                After(LowNs, () => RaiseTransferClock(() => After(HighNs / 2, () =>
                {
                    var sample = sda.Sample(fromPeer ? "mcu" : "peer", runtime.NowNs);
                    if(sample.Level < 0) { WireError("sda_indeterminate"); return; }
                    received = (byte)((received << 1) | sample.Level);
                    // A master releasing SDA but observing dominant low has
                    // lost arbitration. This can also be caused by bus loading.
                    if(!fromPeer && level == -1 && sda.Sample("mcu", runtime.NowNs).Level == 0)
                    {
                        ++peerResponsePhase;
                        sr1 |= ARLO; sr2 &= ~1u; busy = false; state = State.Fault;
                        Drive(scl, "mcu", -1); Drive(sda, "mcu", -1);
                        runtime.Emit("error", "cause=arbitration_lost"); UpdateInterrupts(); return;
                    }
                    After(HighNs - HighNs / 2, () =>
                    {
                        // Responses stay live through stretching and sampling,
                        // but must not drive SDA after this bit's falling clock.
                        ++peerResponsePhase;
                        if(index < 7) bit(index + 1);
                        else AckBit(received, fromPeer, address, complete);
                    });
                })));
            };
            bit(0);
        }
        private void AckBit(byte received, bool fromPeer, bool address, Action<byte, bool> complete)
        {
            ++peerResponsePhase;
            Drive(scl, "mcu", 0);
            bool acknowledge = fromPeer ? (receiveBudget > 1 || (receiveBudget < 0 && (cr1 & ACK) != 0 && !stopPending)) : selected != null;
            if(address)
            {
                lock(deviceLock) devices.TryGetValue(received >> 1, out selected);
                acknowledge = selected != null;
            }
            string sender = fromPeer ? "mcu" : "peer";
            string receiver = fromPeer ? "peer" : "mcu";
            Drive(sda, receiver, -1);
            if(fromPeer) Drive(sda, sender, acknowledge ? 0 : -1);
            else
            {
                Drive(sda, sender, -1);
                AfterPeerResponse(acknowledge ? 0 : -1);
            }
            After(LowNs, () => RaiseTransferClock(() => After(HighNs / 2, () =>
            {
                var sample = sda.Sample(receiver, runtime.NowNs);
                if(sample.Level < 0) { WireError("ack_indeterminate"); return; }
                bool ack = sample.Level == 0;
                After(HighNs - HighNs / 2, () =>
                {
                    ++peerResponsePhase;
                    Drive(scl, "mcu", 0); Drive(sda, sender, -1);
                    complete(received, ack);
                });
            })));
        }
        private void WireError(string cause)
        {
            ++peerResponsePhase;
            busy = false; state = State.Fault; sr1 |= BERR;
            runtime.Emit("error", "cause=" + cause);
            Drive(scl, "mcu", 0); Drive(sda, "mcu", -1); Drive(sda, "peer", -1);
            UpdateInterrupts(); TryControl();
        }

        private sealed class RegisterDevice
        {
            public RegisterDevice(string model)
            {
                Model = model.ToLowerInvariant();
                if(Model != "mpu6050" && Model != "bmp280" && Model != "lis3mdl" && Model != "tmp103" && Model != "registers") throw new ArgumentException("Unsupported I2C register device: " + model);
                Initialize();
            }
            public string Model { get; private set; }
            public int Pointer { get; private set; }
            public void Update(Dictionary<int, byte> updates)
            {
                foreach(var update in updates)
                {
                    // Streams carry identity/calibration/data, never silently
                    // reset configuration that the running firmware wrote.
                    if(Model == "registers" || !Writable(update.Key)) registers[update.Key] = update.Value;
                }
                if(Model == "tmp103") UpdateTemperatureFlags();
            }
            public void BeginWrite() { pointerPending = true; }
            public void Write(byte value)
            {
                if(pointerPending)
                {
                    increment = Model != "lis3mdl" || (value & 0x80) != 0;
                    Pointer = Model == "lis3mdl" ? value & 0x7F : value;
                    pointerPending = false;
                    return;
                }
                if(Writable(Pointer))
                {
                    if((Model == "mpu6050" && Pointer == 0x6B && (value & 0x80) != 0) ||
                       (Model == "bmp280" && Pointer == 0xE0 && value == 0xB6) ||
                       (Model == "lis3mdl" && Pointer == 0x21 && (value & 0x04) != 0)) Initialize();
                    else
                    {
                        registers[Pointer] = Model == "tmp103" && Pointer == 1
                            ? (byte)((value & 0xE7) | (registers[1] & 0x18)) : value;
                        if(Model == "tmp103") UpdateTemperatureFlags();
                    }
                }
                Advance();
            }
            public void Advance()
            {
                if(increment) Pointer = (Pointer + 1) & (Model == "lis3mdl" ? 0x3F : 0xFF);
            }
            public byte[] Snapshot()
            {
                var result = (byte[])registers.Clone();
                if(Model == "mpu6050")
                {
                    bool awake = (registers[0x6B] & 0x40) == 0;
                    if(awake)
                    {
                        Scale(result, 0x3B, 3, 1 << ((registers[0x1C] >> 3) & 3), false);
                        Scale(result, 0x43, 3, 1 << ((registers[0x1B] >> 3) & 3), false);
                        Array.Copy(result, 0x3B, latched, 0x3B, 14);
                    }
                    else Array.Copy(latched, 0x3B, result, 0x3B, 14);
                    result[0x3A] = awake ? (byte)1 : (byte)0;
                }
                else if(Model == "lis3mdl")
                {
                    bool active = (registers[0x22] & 3) < 2;
                    if(active)
                    {
                        Scale(result, 0x28, 3, 6842.0 / lisSensitivities[(registers[0x21] >> 5) & 3], true);
                        Array.Copy(result, 0x28, latched, 0x28, 6);
                    }
                    else Array.Copy(latched, 0x28, result, 0x28, 6);
                    result[0x27] = active ? (byte)0x0F : (byte)0;
                }
                else if(Model == "bmp280")
                {
                    if((registers[0xF4] & 3) != 0) Array.Copy(result, 0xF7, latched, 0xF7, 6);
                    else Array.Copy(latched, 0xF7, result, 0xF7, 6);
                    result[0xF3] = 0;
                }
                return result;
            }
            private static void Scale(byte[] data, int offset, int count, double divisor, bool littleEndian)
            {
                if(divisor == 1) return;
                for(int i = 0; i < count; ++i)
                {
                    int p = offset + 2 * i;
                    short raw = (short)(littleEndian ? data[p] | (data[p + 1] << 8) : (data[p] << 8) | data[p + 1]);
                    int adjusted = (int)Math.Round(raw / divisor);
                    data[p + (littleEndian ? 0 : 1)] = (byte)adjusted;
                    data[p + (littleEndian ? 1 : 0)] = (byte)(adjusted >> 8);
                }
            }
            private void UpdateTemperatureFlags()
            {
                byte flags = 0;
                if((sbyte)registers[0] < (sbyte)registers[2]) flags |= 8;
                if((sbyte)registers[0] > (sbyte)registers[3]) flags |= 16;
                if((registers[1] & 4) != 0) flags |= (byte)(registers[1] & 0x18);
                registers[1] = (byte)((registers[1] & 0xE7) | flags);
            }
            private bool Writable(int reg)
            {
                if(Model == "mpu6050") return reg >= 0x19 && reg <= 0x38 || reg >= 0x63 && reg <= 0x74;
                if(Model == "bmp280") return reg == 0xE0 || reg == 0xF4 || reg == 0xF5;
                if(Model == "lis3mdl") return reg >= 0x20 && reg <= 0x24 || reg == 0x30 || reg >= 0x32 && reg <= 0x33;
                if(Model == "tmp103") return reg >= 1 && reg <= 3;
                return true;
            }
            private void Initialize()
            {
                // Preserve supplied immutable trimming/sample stimulus on reset;
                // restore the writable power-on configuration independently.
                for(int i = 0; i < 256; ++i) if(Writable(i)) registers[i] = 0;
                Array.Clear(latched, 0, latched.Length);
                if(Model == "mpu6050") { registers[0x75] = 0x68; registers[0x6B] = 0x40; }
                if(Model == "lis3mdl") { registers[0x0F] = 0x3D; registers[0x20] = 0x10; registers[0x22] = 3; }
                if(Model == "tmp103") { registers[1] = 2; registers[2] = 0xF6; registers[3] = 60; }
                if(Model == "bmp280")
                {
                    registers[0xD0] = 0x58;
                    int[] trim = {27504,26435,-1000,36477,-10685,3024,2855,140,-7,15500,-14600,6000};
                    for(int i = 0; i < trim.Length; ++i) { registers[0x88 + 2 * i] = (byte)trim[i]; registers[0x89 + 2 * i] = (byte)(trim[i] >> 8); }
                    // Bosch disabled-conversion sentinel, not a fabricated sample.
                    latched[0xF7] = latched[0xFA] = 0x80;
                }
                Pointer = 0; increment = true; pointerPending = false;
            }
            private static readonly int[] lisSensitivities = { 6842, 3421, 2281, 1711 };
            private readonly byte[] registers = new byte[256];
            private readonly byte[] latched = new byte[256];
            private bool increment = true, pointerPending;
        }

        private enum State { Idle, Address, Write, Read, Fault }
        private const uint PE = 1, START = 1u << 8, STOP = 1u << 9, ACK = 1u << 10, POS = 1u << 11, SWRST = 1u << 15;
        private const uint SB = 1, ADDR = 2, BTF = 4, RXNE = 1u << 6, TXE = 1u << 7;
        private const uint BERR = 1u << 8, ARLO = 1u << 9, AF = 1u << 10, ERRORS = 0xDF00;
        private readonly BusRuntime runtime;
        private readonly SignalRuntime scl, sda;
        private readonly double clockHz;
        private readonly object deviceLock = new object();
        private readonly Dictionary<int, RegisterDevice> devices = new Dictionary<int, RegisterDevice>();
        private uint cr1, cr2, oar1, oar2, sr1, sr2, ccr, trise, fltr;
        private ulong peerResponsePhase;
        private State state;
        private bool busy, startPending, stopPending, txQueued, rxFull, rxShiftFull, addressRead, receiveEnded;
        private int receiveBudget;
        private byte txData, rxData, rxShift;
        private byte[] snapshot;
        private RegisterDevice selected;
    }
}
