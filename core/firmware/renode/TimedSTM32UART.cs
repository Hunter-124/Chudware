// STM32F4 asynchronous TTL USART. Wire stimuli and register completion use virtual time.
using System;
using System.Globalization;
using Antmicro.Migrant;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.UART;

namespace Antmicro.Renode.Peripherals.Chudware
{
    [AllowedTranslations(AllowedTranslation.WordToDoubleWord | AllowedTranslation.ByteToDoubleWord)]
    public sealed class TimedSTM32UART : IDoubleWordPeripheral, IKnownSize, IUART
    {
        public TimedSTM32UART(IMachine machine, string name, uint frequency = 16000000)
        {
            runtime = new BusRuntime(machine, name, "uart");
            var configuredClock = runtime.Config.Number("controller_clock_hz", frequency);
            if(configuredClock <= 0 || double.IsNaN(configuredClock) || double.IsInfinity(configuredClock))
            {
                throw new ArgumentException("UART controller_clock_hz must be finite and positive");
            }
            clockHz = configuredClock;
            var peer = runtime.Config.Section("peer");
            var variant = peer.Text("electrical_variant", "ttl");
            RequireTtl(variant);
            if(peer.Has("baud_hz"))
            {
                ConfigurePeer(peer.Number("baud_hz", 0), (int)peer.Number("data_bits", 8),
                    peer.Text("parity", "none"), peer.Number("stop_bits", 1), variant);
            }
            rxSignal = runtime.Signal("rx");
            txSignal = runtime.Signal("tx");
            rxElectricalTailNs = CalculateElectricalTailNs("rx");
            txElectricalTailNs = CalculateElectricalTailNs("tx");
            receiveDecoder = new FrameDecoder();
            peerDecoder = new FrameDecoder();
            receiveTickAction = ReceiveTick;
            transmitPeerTickAction = TransmitPeerTick;
            Reset();
        }

        public long Size { get { return 0x100; } }

        [DefaultInterrupt]
        public GPIO IRQ { get; } = new GPIO();
        // Same GPIO surface as STM32_UART. The curated F4 has no DMA controller.
        public GPIO DMARequest { get; } = new GPIO();

        [field: Transient]
        public event Action<byte> CharReceived;
        [field: Transient]
        public event Action<byte, ulong> TimedCharReceived;
        public ulong LastTransmitTimestampNs { get; private set; }

        public uint BaudRate { get { return (uint)Math.Min(uint.MaxValue, ControllerFormat().BaudHz); } }
        public Bits StopBits
        {
            get
            {
                switch((cr2 >> 12) & 3)
                {
                    case 1: return Bits.Half;
                    case 2: return Bits.Two;
                    case 3: return Bits.OneAndAHalf;
                    default: return Bits.One;
                }
            }
        }
        public Parity ParityBit
        {
            get { return (cr1 & PCE) == 0 ? Parity.None : ((cr1 & PS) == 0 ? Parity.Even : Parity.Odd); }
        }

        // Monitor primitives: ReceiveText "...", ReceiveByte 0x55, ConfigurePeer 9600 8 "none" 1 "ttl".
        // All calls reserve consecutive frames on the peer transmitter, never an RX FIFO.
        public void ConfigurePeer(double baudHz, int dataBits = 8, string parity = "none",
            double stopBits = 1, string electricalVariant = "ttl")
        {
            RequireTtl(electricalVariant);
            if(baudHz <= 0 || double.IsNaN(baudHz) || double.IsInfinity(baudHz))
            {
                throw new ArgumentException("UART peer baud must be finite and positive");
            }
            if(dataBits < 7 || dataBits > 9)
            {
                throw new ArgumentException("UART peer data_bits must be 7, 8, or 9");
            }
            if(stopBits != 0.5 && stopBits != 1 && stopBits != 1.5 && stopBits != 2)
            {
                throw new ArgumentException("UART peer stop_bits must be 0.5, 1, 1.5, or 2");
            }
            var parityMode = ParseParity(parity);
            peerFormat = new FrameFormat(baudHz, dataBits, parityMode, stopBits, 16, false);
            peerConfigured = true;
        }

        public void ReceiveText(string text)
        {
            if(text == null) throw new ArgumentNullException("text");
            foreach(var character in text)
            {
                if(character > 255) throw new ArgumentException("UART ReceiveText accepts byte-valued characters only");
            }
            foreach(var character in text) ReceiveByte(character);
        }

        public void WriteChar(byte value) { ReceiveByte(value); }

        public void ReceiveByte(int value)
        {
            var format = InputFormat();
            if(value < 0 || value >= (1 << format.DataBits))
            {
                throw new ArgumentOutOfRangeException("value", "UART value exceeds configured peer data width");
            }
            var start = Math.Max(runtime.NowNs + 1, peerAvailableNs);
            var end = AddTime(start, format.FrameNs);
            peerAvailableNs = end;
            ExtendReceiveWindow(AddTime(end, ElectricalTailNs("rx", format)));
            ScheduleFrame(rxSignal, "peer", start, value, format, false);
            runtime.Emit("rx_scheduled", "value=" + value + " start_ns=" + start + " end_ns=" + end);
        }

        // A break is a real low wire interval. FE/data=0 and optional LIN LBD follow sampling.
        public void ReceiveBreak(double durationSeconds)
        {
            if(durationSeconds <= 0 || double.IsNaN(durationSeconds) || double.IsInfinity(durationSeconds))
            {
                throw new ArgumentException("UART break duration must be finite and positive");
            }
            var format = InputFormat();
            var start = Math.Max(runtime.NowNs + 1, peerAvailableNs);
            var end = AddTime(start, durationSeconds * 1e9);
            peerAvailableNs = AddTime(end, format.BitNs);
            ExtendReceiveWindow(AddTime(peerAvailableNs, ElectricalTailNs("rx", format)));
            ScheduleLevel(rxSignal, "peer", start, 0);
            ScheduleLevel(rxSignal, "peer", end, 1);
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
                case 0x00:
                    statusRead = true;
                    return status;
                case 0x04:
                    var value = receiveData;
                    status &= ~RXNE;
                    if(statusRead)
                    {
                        status &= ~(PE | FE | NF | ORE | IDLE);
                        statusRead = false;
                    }
                    UpdateIrq();
                    runtime.Emit("register_read", "register=DR value=" + value + " outcome=consumed");
                    return value;
                case 0x08: return brr;
                case 0x0C: return cr1;
                case 0x10: return cr2;
                case 0x14: return cr3;
                case 0x18: return gtpr;
                default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
                case 0x00:
                    // SR PE/FE/NF/ORE/IDLE are SR-read then DR-read clear only.
                    // RXNE, TC, LBD and CTS are writable-zero-to-clear.
                    status &= value | ~(RXNE | TC | LBD | CTS);
                    UpdateIrq();
                    break;
                case 0x04:
                    if((cr1 & UE) != 0) RequireAsynchronous();
                    if(transmitDataFull)
                    {
                        // No hardware TX-overrun flag exists: an unpaced DR write overwrites DR.
                        runtime.Emit("tx_overwrite", "register=DR value=" + (value & 0x1FF) + " outcome=data_lost");
                    }
                    transmitData = value & 0x1FF;
                    transmitDataFull = true;
                    status &= ~(TC | TXE);
                    UpdateIrq();
                    BeginTransmit();
                    break;
                case 0x08:
                    brr = value & 0xFFFF;
                    controllerFormat = CalculateControllerFormat();
                    BeginTransmit();
                    break;
                case 0x0C:
                    cr1 = value & 0xBFFF;
                    controllerFormat = CalculateControllerFormat();
                    if((cr1 & (UE | RE)) != (UE | RE))
                    {
                        receiveDecoder.Abort();
                        idleSequence++;
                    }
                    if((cr1 & UE) == 0) AbortTransmit();
                    if((cr1 & UE) != 0) RequireAsynchronous();
                    if((cr1 & SBK) != 0 && (cr1 & (UE | TE)) == (UE | TE)) transmitBreakPending = true;
                    BeginTransmit();
                    UpdateIrq();
                    break;
                case 0x10:
                    cr2 = value & 0x7FFF;
                    controllerFormat = CalculateControllerFormat();
                    if((cr1 & UE) != 0) RequireAsynchronous();
                    break;
                case 0x14:
                    cr3 = value & 0xFFF;
                    controllerFormat = CalculateControllerFormat();
                    if((cr1 & UE) != 0) RequireAsynchronous();
                    UpdateIrq();
                    break;
                case 0x18: gtpr = value & 0xFFFF; break;
            }
        }

        public void Reset()
        {
            runtime.Reset();
            status = TXE | TC;
            brr = cr1 = cr2 = cr3 = gtpr = receiveData = transmitData = 0;
            controllerFormat = CalculateControllerFormat();
            statusRead = transmitDataFull = transmitBusy = transmitBreakPending = false;
            receiveMonitoring = transmitPeerMonitoring = false;
            receiveWindowEnd = transmitPeerWindowEnd = peerAvailableNs = 0;
            transmitSequence++;
            idleSequence++;
            receiveDecoder.Abort();
            peerDecoder.Abort();
            lastReceiveLevel = lastPeerLevel = 1;
            lowSinceNs = 0;
            lowActive = linBreakReported = false;
            LastTransmitTimestampNs = 0;
            rxSignal.Drive("peer", 1, runtime.NowNs);
            txSignal.Drive("mcu", 1, runtime.NowNs);
            IRQ.Set(false);
            DMARequest.Set(false);
        }

        private void RequireAsynchronous()
        {
            if((cr2 & (1u << 11)) != 0 || (cr3 & ((1u << 1) | (1u << 3) | (1u << 5) | (1u << 8) | (1u << 9))) != 0)
            {
                throw new NotSupportedException("TimedSTM32UART supports asynchronous full-duplex TTL only; synchronous, IrDA, smartcard, half-duplex and RTS/CTS are unsupported");
            }
        }

        private static void RequireTtl(string variant)
        {
            if(!string.Equals(variant, "ttl", StringComparison.OrdinalIgnoreCase))
            {
                throw new NotSupportedException("TimedSTM32UART electrical_variant supports ttl only (not RS-232/RS-485)");
            }
        }

        private static int ParseParity(string value)
        {
            if(string.Equals(value, "none", StringComparison.OrdinalIgnoreCase)) return 0;
            if(string.Equals(value, "even", StringComparison.OrdinalIgnoreCase)) return 1;
            if(string.Equals(value, "odd", StringComparison.OrdinalIgnoreCase)) return 2;
            throw new ArgumentException("UART parity must be none, even, or odd");
        }

        private FrameFormat ControllerFormat() { return controllerFormat; }

        private FrameFormat CalculateControllerFormat()
        {
            // OVER8: BRR[3] is ignored; divisor = 8*mantissa + fraction[2:0].
            var divisor = (cr1 & OVER8) == 0 ? brr : ((brr >> 4) * 8 + (brr & 7));
            var parity = (cr1 & PCE) == 0 ? 0 : ((cr1 & PS) == 0 ? 1 : 2);
            var dataBits = ((cr1 & M) == 0 ? 8 : 9) - (parity == 0 ? 0 : 1);
            double stop;
            switch((cr2 >> 12) & 3)
            {
                case 1: stop = 0.5; break;
                case 2: stop = 2; break;
                case 3: stop = 1.5; break;
                default: stop = 1; break;
            }
            return new FrameFormat(divisor == 0 ? 0 : clockHz / divisor, dataBits, parity, stop,
                (cr1 & OVER8) == 0 ? 16 : 8, (cr3 & (1u << 11)) != 0);
        }

        private FrameFormat InputFormat()
        {
            if(peerConfigured) return peerFormat;
            var format = ControllerFormat();
            if(format.BaudHz == 0)
            {
                throw new InvalidOperationException("ConfigurePeer baud before injecting UART input when firmware has not programmed BRR");
            }
            // Existing functional tracks may mirror BRR. Explicit electrical links must specify peer baud.
            return format;
        }

        private void BeginTransmit()
        {
            if(transmitBusy || (!transmitDataFull && !transmitBreakPending) || (cr1 & (UE | TE)) != (UE | TE)) return;
            var format = ControllerFormat();
            if(format.BaudHz == 0) return;
            transmitBusy = true;
            var sequence = transmitSequence;
            // The DR/shift-register handoff is finite; writes between here and handoff still target DR.
            runtime.Schedule((ulong)Math.Max(1, Math.Ceiling(1e9 / clockHz)), () =>
            {
                if(sequence != transmitSequence) return;
                var isBreak = transmitBreakPending;
                transmitBreakPending = false;
                var value = (int)transmitData & ((1 << format.DataBits) - 1);
                if(!isBreak)
                {
                    transmitDataFull = false;
                    status |= TXE;
                }
                status &= ~TC;
                UpdateIrq();
                var start = runtime.NowNs;
                var end = AddTime(start, format.FrameNs);
                ExtendTransmitPeerWindow(AddTime(end, ElectricalTailNs("tx", format)));
                ScheduleFrame(txSignal, "mcu", start, value, format, isBreak);
                runtime.Emit("tx_start", "value=" + value + " end_ns=" + end + " baud_hz=" + Number(format.BaudHz));
                ScheduleAt(end, () =>
                {
                    if(sequence != transmitSequence) return;
                    transmitBusy = false;
                    if(!isBreak)
                    {
                        LastTransmitTimestampNs = runtime.NowNs;
                        runtime.Emit("tx_complete", "value=" + value + " outcome=transmitted");
                        TimedCharReceived?.Invoke((byte)value, runtime.NowNs);
                        CharReceived?.Invoke((byte)value);
                    }
                    cr1 &= ~SBK;
                    if(transmitDataFull || transmitBreakPending) BeginTransmit();
                    else status |= TC;
                    UpdateIrq();
                });
            });
        }

        private void AbortTransmit()
        {
            transmitSequence++;
            transmitBusy = transmitDataFull = transmitBreakPending = false;
            status |= TXE | TC;
            txSignal.Drive("mcu", 1, runtime.NowNs);
        }

        private void ScheduleFrame(SignalRuntime signal, string endpoint, ulong start, int value, FrameFormat format, bool isBreak)
        {
            ScheduleLevel(signal, endpoint, start, 0);
            if(isBreak)
            {
                ScheduleLevel(signal, endpoint, AddTime(start, format.FrameNs), 1);
                return;
            }
            var parity = 0;
            for(var bit = 0; bit < format.DataBits; bit++)
            {
                var level = (value >> bit) & 1;
                parity ^= level;
                ScheduleLevel(signal, endpoint, AddTime(start, (bit + 1) * format.BitNs), level);
            }
            var stopStart = 1 + format.DataBits;
            if(format.Parity != 0)
            {
                ScheduleLevel(signal, endpoint, AddTime(start, stopStart * format.BitNs), parity ^ (format.Parity == 2 ? 1 : 0));
                stopStart++;
            }
            ScheduleLevel(signal, endpoint, AddTime(start, stopStart * format.BitNs), 1);
        }

        private void ScheduleLevel(SignalRuntime signal, string endpoint, ulong time, int state)
        {
            var sequence = transmitSequence;
            ScheduleAt(time, () =>
            {
                // Reset cancels all runtime work; disabling UE cancels local TX only.
                if(endpoint == "mcu" && sequence != transmitSequence) return;
                signal.Drive(endpoint, state, runtime.NowNs);
            });
        }

        private void ScheduleAt(ulong time, Action action)
        {
            runtime.Schedule(time > runtime.NowNs ? time - runtime.NowNs : 0, action);
        }

        private void ExtendReceiveWindow(ulong end)
        {
            receiveWindowEnd = Math.Max(receiveWindowEnd, end);
            idleSequence++;
            if(receiveMonitoring) return;
            lastReceiveLevel = rxSignal.Sample("mcu", runtime.NowNs).Level;
            receiveTickTimeNs = runtime.NowNs + 1;
            receiveMonitoring = true;
            runtime.Schedule(1, receiveTickAction);
        }

        private void ReceiveTick()
        {
            var now = runtime.NowNs;
            var format = receiveDecoder.Active ? receiveDecoder.Format : ControllerFormat();
            var enabled = (cr1 & (UE | RE)) == (UE | RE) && format.BaudHz != 0;
            if(format.BaudHz == 0) format = InputFormat();
            var sample = rxSignal.Sample("mcu", now);
            if(enabled)
            {
                if(!receiveDecoder.Active && lastReceiveLevel == 1 && sample.Level == 0)
                {
                    RequireAsynchronous();
                    receiveDecoder.Start(format);
                    idleSequence++;
                    status &= ~IDLE;
                    runtime.Emit("rx_start", "baud_hz=" + Number(format.BaudHz));
                }
                else if(receiveDecoder.Active && receiveDecoder.Step(sample))
                {
                    CompleteReceive(receiveDecoder);
                }
                TrackBreak(sample.Level, now, format);
            }
            if(sample.Level >= 0) lastReceiveLevel = sample.Level;
            if(now < receiveWindowEnd || receiveDecoder.Active)
            {
                receiveTickTimeNs += format.TickPeriodNs;
                ScheduleAt((ulong)Math.Ceiling(receiveTickTimeNs), receiveTickAction);
            }
            else
            {
                receiveMonitoring = false;
            }
        }

        private void CompleteReceive(FrameDecoder decoder)
        {
            var error = decoder.Errors;
            var value = decoder.Value;
            if((status & RXNE) != 0)
            {
                status |= ORE;
                runtime.Emit("rx_overrun", "value=" + value + " outcome=dropped cause=undrained_DR");
            }
            else
            {
                receiveData = (uint)value;
                status |= RXNE;
                if((cr3 & (1u << 6)) != 0) DMARequest.Blink();
            }
            status |= error;
            runtime.Emit("rx_complete", "value=" + value + " errors=" + error +
                " outcome=" + decoder.Outcome + " cause=" + decoder.ElectricalCause);
            decoder.Abort();
            UpdateIrq();
            var sequence = ++idleSequence;
            var idleFormat = ControllerFormat();
            if(idleFormat.BaudHz != 0)
            {
                runtime.Schedule((ulong)Math.Ceiling(idleFormat.FrameNs), () =>
                {
                    if(sequence != idleSequence || (cr1 & (UE | RE)) != (UE | RE) || receiveDecoder.Active || lastReceiveLevel != 1) return;
                    status |= IDLE;
                    UpdateIrq();
                });
            }
        }

        private void TrackBreak(int level, ulong now, FrameFormat format)
        {
            if(level == 0)
            {
                if(!lowActive)
                {
                    lowActive = true;
                    lowSinceNs = now;
                    linBreakReported = false;
                }
                var lin = (cr2 & (1u << 14)) != 0;
                var bits = lin ? ((cr2 & (1u << 5)) != 0 ? 11 : 10) :
                    2 + format.DataBits + (format.Parity == 0 ? 0 : 1);
                if(!linBreakReported && now - lowSinceNs >= bits * format.BitNs)
                {
                    linBreakReported = true;
                    if(lin) status |= LBD;
                    runtime.Emit("break", "value=0 cause=continuous_low");
                    UpdateIrq();
                }
            }
            else if(level == 1)
            {
                lowActive = false;
            }
        }

        private void ExtendTransmitPeerWindow(ulong end)
        {
            transmitPeerWindowEnd = Math.Max(transmitPeerWindowEnd, end);
            if(transmitPeerMonitoring) return;
            lastPeerLevel = txSignal.Sample("peer", runtime.NowNs).Level;
            transmitPeerTickTimeNs = runtime.NowNs + 1;
            transmitPeerMonitoring = true;
            runtime.Schedule(1, transmitPeerTickAction);
        }

        private void TransmitPeerTick()
        {
            var format = peerDecoder.Active ? peerDecoder.Format : (peerConfigured ? peerFormat : ControllerFormat());
            if(format.BaudHz == 0)
            {
                transmitPeerMonitoring = false;
                return;
            }
            var sample = txSignal.Sample("peer", runtime.NowNs);
            if(!peerDecoder.Active && lastPeerLevel == 1 && sample.Level == 0) peerDecoder.Start(format);
            else if(peerDecoder.Active && peerDecoder.Step(sample))
            {
                runtime.Emit("tx_peer_receive", "value=" + peerDecoder.Value + " errors=" + peerDecoder.Errors +
                    " outcome=" + peerDecoder.Outcome + " cause=" + peerDecoder.ElectricalCause);
                peerDecoder.Abort();
            }
            if(sample.Level >= 0) lastPeerLevel = sample.Level;
            if(runtime.NowNs < transmitPeerWindowEnd || peerDecoder.Active)
            {
                transmitPeerTickTimeNs += format.TickPeriodNs;
                ScheduleAt((ulong)Math.Ceiling(transmitPeerTickTimeNs), transmitPeerTickAction);
            }
            else transmitPeerMonitoring = false;
        }

        private ulong ElectricalTailNs(string signalName, FrameFormat format)
        {
            return (ulong)Math.Ceiling(Math.Max(format.FrameNs * 2, signalName == "rx" ? rxElectricalTailNs : txElectricalTailNs));
        }

        private double CalculateElectricalTailNs(string signalName)
        {
            // Bounds only the edge-observation window, not a second electrical solver.
            // All voltages, thresholds and waveform evolution remain in SignalRuntime.
            double delay = 0, capacitance = 0, resistance = 0;
            foreach(var signal in runtime.Config.Section("electrical").Entries("signals"))
            {
                if(signal.Text("name", "") != signalName) continue;
                capacitance += signal.Number("wire_capacitance_f", 0);
                resistance += signal.Number("series_r_ohm", 0);
                foreach(var endpoint in signal.Entries("endpoints"))
                {
                    capacitance += endpoint.Number("input_capacitance_f", 0);
                    resistance = Math.Max(resistance, Math.Max(endpoint.Number("r_low_ohm", 0), endpoint.Number("r_high_ohm", 0)));
                    delay += endpoint.Number("tx_delay_s", 0) + endpoint.Number("rx_delay_s", 0) +
                        Math.Max(endpoint.Number("rise_s", 0), endpoint.Number("fall_s", 0));
                }
                foreach(var segment in signal.Entries("segments")) delay += 8 * segment.Number("propagation_s", 0);
                foreach(var bias in signal.Entries("bias")) resistance = Math.Max(resistance, bias.Number("resistance_ohm", 0));
            }
            return (delay + 20 * resistance * capacitance) * 1e9;
        }

        private void UpdateIrq()
        {
            IRQ.Set((cr1 & UE) != 0 && (
                ((cr1 & (1u << 4)) != 0 && (status & IDLE) != 0) ||
                ((cr1 & (1u << 5)) != 0 && (status & (RXNE | ORE)) != 0) ||
                ((cr1 & (1u << 6)) != 0 && (status & TC) != 0) ||
                ((cr1 & (1u << 7)) != 0 && (status & TXE) != 0) ||
                ((cr1 & (1u << 8)) != 0 && (status & PE) != 0) ||
                ((cr3 & 1) != 0 && (status & (FE | NF | ORE)) != 0) ||
                ((cr2 & (1u << 6)) != 0 && (status & LBD) != 0)));
        }

        private static ulong AddTime(ulong start, double delay)
        {
            return checked(start + (ulong)Math.Ceiling(delay));
        }
        private static string Number(double value) { return value.ToString("R", CultureInfo.InvariantCulture); }

        private struct FrameFormat
        {
            public FrameFormat(double baudHz, int dataBits, int parity, double stopBits, int oversampling, bool oneBit)
            {
                BaudHz = baudHz;
                DataBits = dataBits;
                Parity = parity;
                Stop = stopBits;
                Oversampling = oversampling;
                OneBit = oneBit;
                BitNs = baudHz == 0 ? 0 : 1e9 / baudHz;
                FrameNs = (1 + dataBits + (parity == 0 ? 0 : 1) + stopBits) * BitNs;
                TickPeriodNs = Math.Max(1, BitNs / oversampling);
            }
            public readonly double BitNs, FrameNs, TickPeriodNs;
            public readonly double BaudHz;
            public readonly int DataBits, Parity, Oversampling;
            public readonly double Stop;
            public readonly bool OneBit;
        }

        // A single hardware shift register, sampled at 7/8/9 or 3/4/5 (ONEBIT uses center only).
        // No injected FE/PE/NF: these are consequences of start/data/parity/stop samples.
        // Electrical uncertainty is evidence, independent of ONEBIT's suppression of hardware NF.
        private sealed class FrameDecoder
        {
            public bool Active { get; private set; }
            public FrameFormat Format { get; private set; }
            public int Value { get; private set; }
            public uint Errors { get; private set; }
            public bool ElectricallyIndeterminate { get; private set; }
            public string ElectricalCause { get; private set; }
            public string Outcome { get { return ElectricallyIndeterminate ? "unresolved" : Errors == 0 ? "sampled" : "receive_error"; } }
            public void Start(FrameFormat format)
            {
                Format = format;
                Active = true;
                Value = ones = phase = ticks = votes = samples = 0;
                Errors = 0;
                ElectricallyIndeterminate = false;
                ElectricalCause = "resolved";
                previousSample = 0;
                different = false;
                center = format.Oversampling / 2;
            }
            public void Abort() { Active = false; }
            public bool Step(SignalSample sample)
            {
                ticks++;
                var spread = Format.OneBit ? 0 : 1;
                if(ticks < center - spread || ticks > center + spread) return false;
                var level = sample.Level;
                var resolved = level < 0 ? previousSample : level;
                if(level < 0)
                {
                    if(!ElectricallyIndeterminate) ElectricalCause = sample.Cause;
                    ElectricallyIndeterminate = true;
                    if(!Format.OneBit) Errors |= NF;
                }
                if(samples != 0 && resolved != previousSample) different = true;
                previousSample = resolved;
                votes += resolved;
                samples++;
                if(ticks != center + spread) return false;
                if(different) Errors |= NF;
                var bit = votes * 2 >= samples ? 1 : 0;
                votes = samples = 0;
                different = false;
                if(phase == 0)
                {
                    if(bit != 0)
                    {
                        Active = false; // A pulse shorter than a valid start bit is rejected, not FE.
                        return false;
                    }
                }
                else if(phase <= Format.DataBits)
                {
                    Value |= bit << (phase - 1);
                    ones ^= bit;
                }
                else if(Format.Parity != 0 && phase == Format.DataBits + 1)
                {
                    if(bit != (ones ^ (Format.Parity == 2 ? 1 : 0))) Errors |= PE;
                }
                else
                {
                    if(bit == 0) Errors |= FE;
                    Active = false;
                    return true;
                }
                phase++;
                var stopPhase = 1 + Format.DataBits + (Format.Parity == 0 ? 0 : 1);
                // RM0090: reception samples the first stop bit; extra stop time is not data.
                center = phase == stopPhase
                    ? stopPhase * Format.Oversampling + (int)(Format.Oversampling * (Format.Stop < 1 ? 0.25 : 0.5))
                    : (int)((phase + 0.5) * Format.Oversampling);
                return false;
            }
            private int phase, ticks, center, votes, samples, ones, previousSample;
            private bool different;
        }

        private readonly BusRuntime runtime;
        private readonly SignalRuntime rxSignal, txSignal;
        private readonly double clockHz;
        private readonly double rxElectricalTailNs, txElectricalTailNs;
        private readonly FrameDecoder receiveDecoder, peerDecoder;
        private readonly Action receiveTickAction, transmitPeerTickAction;
        private FrameFormat peerFormat, controllerFormat;
        private bool peerConfigured, statusRead, transmitDataFull, transmitBusy, transmitBreakPending;
        private bool receiveMonitoring, transmitPeerMonitoring, lowActive, linBreakReported;
        private uint status, brr, cr1, cr2, cr3, gtpr, receiveData, transmitData;
        private ulong peerAvailableNs, receiveWindowEnd, transmitPeerWindowEnd, lowSinceNs;
        private double receiveTickTimeNs, transmitPeerTickTimeNs;
        private int lastReceiveLevel, lastPeerLevel, transmitSequence, idleSequence;
        private const uint PE = 1u << 0, FE = 1u << 1, NF = 1u << 2, ORE = 1u << 3,
            IDLE = 1u << 4, RXNE = 1u << 5, TC = 1u << 6, TXE = 1u << 7, LBD = 1u << 8, CTS = 1u << 9;
        private const uint SBK = 1u << 0, RE = 1u << 2, TE = 1u << 3, PS = 1u << 9,
            PCE = 1u << 10, M = 1u << 12, UE = 1u << 13, OVER8 = 1u << 15;
    }
}
