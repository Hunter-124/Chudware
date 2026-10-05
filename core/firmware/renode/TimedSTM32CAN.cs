// bxCAN register layout, filter encodings and IRQ numbering follow Renode STMCAN
// (renode-infrastructure add012af003a0f620d3da52828262676f374d121).
// Copyright (c) 2010-2025 Antmicro; Copyright (c) 2011-2015 Realtime Embedded.
// Derived register conventions are MIT licensed:
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Chudware
{
    // Classical CAN 2.0 only. One scalar V(CAN_H)-V(CAN_L) network: common mode,
    // asymmetry, EMC, crosstalk and nonlinear transceiver protection are excluded.
    // The bus does not use CANHub: ACK and completion are sampled wire events.
    public sealed class TimedSTM32CAN : IDoubleWordPeripheral, IKnownSize, INumberedGPIOOutput
    {
        public TimedSTM32CAN(IMachine machine, string name, uint frequency = 16000000, TimedSTM32CAN master = null)
        {
            if(master != null && master.master != null)
            {
                throw new ArgumentException("CAN filter master must be CAN1");
            }
            this.master = master;
            runtime = new BusRuntime(machine, name, "can");
            clock = runtime.Config.Number("controller_clock_hz", frequency);
            if(clock <= 0) throw new ArgumentException("CAN controller clock must be positive");
            filters = master == null ? new Filters() : master.filters;
            var outputs = new Dictionary<int, IGPIO>();
            for(var i = 0; i < 4; ++i) outputs[i] = new GPIO();
            Connections = new ReadOnlyDictionary<int, IGPIO>(outputs);
            medium = new Medium(this, runtime.Signal("can_diff"));
            controller = new Node(medium, "mcu", this);
            medium.Nodes.Add(controller);
            Reset();
            var peer = runtime.Config.Section("peer");
            if(peer.Has("bitrate_hz"))
            {
                ConfigurePeer(peer.Text("endpoint", "peer"), (uint)peer.Number("bitrate_hz", 0),
                    peer.Number("sample_point", 0.875), (int)peer.Number("sjw_tq", 1), peer.Flag("ack_enabled", true));
            }
            foreach(var p in peer.Entries("nodes"))
            {
                ConfigurePeer(p.Text("endpoint", "peer"), (uint)p.Number("bitrate_hz", 0),
                    p.Number("sample_point", 0.875), (int)p.Number("sjw_tq", 1), p.Flag("ack_enabled", true));
            }
            runtime.Emit("model", "outcome=differential_only clock_hz=" + F(clock)
                + " limits=common_mode_asymmetry_EMC_crosstalk_nonlinearity_dispersion_excluded");
        }

        public long Size { get { return 0x400; } }
        public IReadOnlyDictionary<int, IGPIO> Connections { get; private set; }
        public GPIO TXInterrupt { get { return (GPIO)Connections[0]; } }
        public GPIO RX0Interrupt { get { return (GPIO)Connections[1]; } }
        public GPIO RX1Interrupt { get { return (GPIO)Connections[2]; } }
        public GPIO SCEInterrupt { get { return (GPIO)Connections[3]; } }

        // A peer is a real independently clocked CAN receiver/transmitter. Merely
        // creating a controller never creates an implicit ACK source.
        public void ConfigurePeer(string endpoint, uint bitrate, double samplePoint, int sjwTq, bool ackEnabled)
        {
            if(String.IsNullOrEmpty(endpoint) || endpoint == "mcu" || bitrate == 0 || samplePoint <= 0 || samplePoint >= 1)
                throw new ArgumentException("Invalid CAN peer timing/endpoint");
            if(medium.Active) throw new InvalidOperationException("Configure CAN peers while bus is idle");
            Node node;
            if(!peers.TryGetValue(endpoint, out node))
            {
                node = new Node(medium, endpoint, null);
                peers.Add(endpoint, node);
                medium.Nodes.Add(node);
            }
            // Explicit peer clock_hz is used when supplied. Otherwise a peer's
            // bitrate and sample point define its oscillator without calibration.
            var pc = runtime.Config.Section("peer");
            foreach(var p in pc.Entries("nodes")) if(p.Text("endpoint", "peer") == endpoint) pc = p;
            var peerClock = pc.Number("clock_hz", bitrate * 16.0);
            var prescaler = Math.Max(1, (int)Math.Round(peerClock / (bitrate * 16.0)));
            var total = Math.Max(4, (int)Math.Round(peerClock / (prescaler * (double)bitrate)));
            var sample = Math.Max(2, Math.Min(total - 1, (int)Math.Round(total * samplePoint)));
            if(sjwTq < 1 || sjwTq > 4 || sjwTq > total - sample)
                throw new ArgumentException("CAN SJW must be 1..4 and not exceed phase segment 2");
            node.Timing = new Timing(1e9 * prescaler / peerClock, total, sample, sjwTq);
            node.AckEnabled = ackEnabled;
            medium.Release(node);
            runtime.Emit("peer", "endpoint=" + endpoint + " bitrate_hz=" + F(1e9 / node.Timing.BitNs)
                + " sample_point=" + F((double)sample / total) + " ack_enabled=" + (ackEnabled ? "1" : "0"));
        }

        // External test equipment/fault driver is a declared physical endpoint,
        // not an implicit protocol peer or a termination-failure boolean.
        public void DriveExternal(string endpoint, int state)
        {
            if(endpoint == "mcu" || peers.ContainsKey(endpoint)) throw new ArgumentException("Use a dedicated external driver endpoint");
            var declared = false;
            foreach(var e in runtime.Config.Section("electrical").Entries("signals"))
                if(e.Text("name", "") == "can_diff")
                    foreach(var p in e.Entries("endpoints"))
                        if(p.Text("id", "") == endpoint && p.Text("role", "") == "external_driver") declared = true;
            if(!declared) throw new ArgumentException("External CAN driver must be declared with role=external_driver");
            medium.Line.Drive(endpoint, state, runtime.NowNs);
            runtime.Emit("external_drive", "endpoint=" + endpoint + " value=" + state);
        }

        public void SubmitFrame(string endpoint, uint id, bool extended, bool remote, string payloadHex)
        {
            Node peer;
            if(!peers.TryGetValue(endpoint, out peer)) throw new ArgumentException("Configure CAN peer before submitting a frame");
            if(id > (extended ? 0x1FFFFFFFu : 0x7FFu)) throw new ArgumentException("CAN identifier exceeds selected width");
            var text = (payloadHex ?? "").Replace(" ", "").Replace(":", "").Replace("-", "");
            if((text.Length & 1) != 0 || text.Length > 16) throw new ArgumentException("Classical CAN payload must contain 0..8 bytes");
            var data = new byte[text.Length / 2];
            for(var i = 0; i < data.Length; ++i) data[i] = Byte.Parse(text.Substring(2 * i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            peer.Pending.Enqueue(new Frame(id, extended, remote, data, data.Length));
            medium.Request();
        }

        public void Reset()
        {
            runtime.Reset();
            medium.Reset();
            mcr = 0x00010002; msr = 0x00000C02; ier = 0; btr = 0x01230000;
            tec = rec = lec = 0; busOff = false; recoveryGeneration++;
            sequence = 0;
            for(var i = 0; i < 3; ++i) mailboxes[i] = new Mailbox();
            for(var i = 0; i < 2; ++i) { fifo[i].Clear(); fifoFlags[i] = 0; }
            if(master == null) filters.Reset();
            controller.Configure(ControllerTiming());
            controller.Tec = controller.Rec = 0;
            foreach(var peer in peers.Values) { peer.Pending.Clear(); peer.Tec = peer.Rec = 0; }
            UpdateInterrupts();
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset >= 0x180 && offset < 0x1B0)
            {
                var m = mailboxes[(int)((offset - 0x180) / 16)];
                switch((offset - 0x180) % 16)
                {
                    case 0: return m.Tir;
                    case 4: return m.Dtr;
                    case 8: return m.Low;
                    case 12: return m.High;
                }
            }
            if(offset >= 0x1B0 && offset < 0x1D0)
            {
                var f = (int)((offset - 0x1B0) / 16);
                if(fifo[f].Count == 0) return 0;
                var r = fifo[f][0];
                switch((offset - 0x1B0) % 16)
                {
                    case 0: return r.Frame.Rir;
                    case 4: return (uint)r.Frame.Dlc | ((uint)r.Match << 8) | ((uint)r.Timestamp << 16);
                    case 8: return r.Frame.Word(0);
                    case 12: return r.Frame.Word(4);
                }
            }
            if(offset >= 0x240 && offset < 0x320 && (offset & 3) == 0)
                return filters.Words[(int)((offset - 0x240) / 4)];
            switch(offset)
            {
                case 0: return mcr;
                case 4: return msr;
                case 8: return TransmitStatus();
                case 12: return ReceiveStatus(0);
                case 16: return ReceiveStatus(1);
                case 20: return ier;
                case 24: return ErrorStatus();
                case 28: return btr;
                case 0x200: return filters.Fmr;
                case 0x204: return filters.Mode;
                case 0x20C: return filters.Scale;
                case 0x214: return filters.Assignment;
                case 0x21C: return filters.Active;
                default: return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            runtime.Emit("register", "register=" + offset.ToString("X", CultureInfo.InvariantCulture) + " value=" + value.ToString("X", CultureInfo.InvariantCulture));
            if(offset >= 0x180 && offset < 0x1B0)
            {
                var index = (int)((offset - 0x180) / 16);
                var m = mailboxes[index];
                if(m.Pending) return;
                switch((offset - 0x180) % 16)
                {
                    case 0:
                        m.Tir = value & 0xFFFFFFFEu;
                        if((value & 1) != 0)
                        {
                            m.Tir |= 1; m.Pending = true; m.Order = sequence++; m.Status = 0;
                            medium.Request();
                        }
                        break;
                    case 4: m.Dtr = value & 0xFFFF010Fu; break;
                    case 8: m.Low = value; break;
                    case 12: m.High = value; break;
                }
                UpdateInterrupts(); return;
            }
            if(offset >= 0x200)
            {
                if(master != null) return; // CAN2 filters are written through CAN1.
                if(offset >= 0x240 && offset < 0x320 && (offset & 3) == 0)
                {
                    var word = (int)((offset - 0x240) / 4);
                    if((filters.Fmr & 1) != 0 || (filters.Active & (1u << (word / 2))) == 0) filters.Words[word] = value;
                    return;
                }
                switch(offset)
                {
                    case 0x200: filters.Fmr = (filters.Fmr & ~0x3F01u) | (value & 0x3F01); break;
                    case 0x204: if((filters.Fmr & 1) != 0) filters.Mode = value & 0x0FFFFFFF; break;
                    case 0x20C: if((filters.Fmr & 1) != 0) filters.Scale = value & 0x0FFFFFFF; break;
                    case 0x214: if((filters.Fmr & 1) != 0) filters.Assignment = value & 0x0FFFFFFF; break;
                    case 0x21C: filters.Active = value & 0x0FFFFFFF; break;
                }
                return;
            }
            switch(offset)
            {
                case 0:
                    if((value & (1u << 15)) != 0) { Reset(); return; }
                    var old = mcr;
                    mcr = value & 0x000100FFu;
                    var entering = (mcr & 1) != 0;
                    if(!entering) msr &= ~1u;
                    else if(!medium.Active) msr |= 1;
                    if((mcr & 2) != 0 && !medium.Active) { msr |= 2; if((old & 2) == 0) msr |= 16; }
                    else msr &= ~2u;
                    if((old & 1) != 0 && !entering)
                    {
                        controller.Configure(ControllerTiming());
                        if(busOff) BeginRecovery();
                    }
                    if(busOff && (mcr & 0x40) != 0) BeginRecovery();
                    medium.Request(); break;
                case 4: msr &= ~(value & 0x1C); break;
                case 8:
                    for(var i = 0; i < 3; ++i)
                    {
                        var m = mailboxes[i];
                        if((value & (1u << (8 * i))) != 0) m.Status = 0;
                        if((value & (1u << (7 + 8 * i))) != 0 && m.Pending)
                        {
                            m.Abort = true;
                            if(controller.Tx == null || controller.Mailbox != i) Complete(i, 0, "aborted");
                        }
                    }
                    break;
                case 12: WriteFifo(0, value); break;
                case 16: WriteFifo(1, value); break;
                case 20: ier = value & 0x00038F7Fu; break;
                case 24: lec = (int)((value >> 4) & 7); break;
                case 28: if((msr & 1) != 0) { btr = value & 0xC37F03FFu; controller.Configure(ControllerTiming()); } break;
            }
            UpdateInterrupts();
        }

        private bool Running { get { return (msr & 3) == 0 && !busOff; } }
        private bool Silent { get { return (btr & 0x80000000u) != 0; } }
        private bool Loopback { get { return (btr & 0x40000000u) != 0; } }
        private Timing ControllerTiming()
        {
            var ts1 = (int)((btr >> 16) & 15) + 1;
            var ts2 = (int)((btr >> 20) & 7) + 1;
            var sjw = (int)((btr >> 24) & 3) + 1;
            return new Timing(1e9 * ((btr & 1023) + 1) / clock, 1 + ts1 + ts2, 1 + ts1, sjw);
        }
        private void WriteFifo(int f, uint value)
        {
            fifoFlags[f] &= ~(value & 0x18);
            if((value & 0x20) != 0 && fifo[f].Count > 0) fifo[f].RemoveAt(0);
        }
        private uint ReceiveStatus(int f) { return (uint)fifo[f].Count | fifoFlags[f]; }
        private uint TransmitStatus()
        {
            uint result = 0; var code = -1; var lowest = -1;
            for(var i = 0; i < 3; ++i)
            {
                var m = mailboxes[i]; result |= m.Status << (8 * i);
                if(!m.Pending) { result |= 1u << (26 + i); if(code < 0) code = i; }
                else if(lowest < 0 || Before(mailboxes[lowest], m)) lowest = i;
            }
            if(lowest >= 0) result |= 1u << (29 + lowest);
            // CODE is next free mailbox, or lowest-priority pending when full.
            if(code < 0)
            {
                code = 0;
                for(var i = 1; i < 3; ++i) if(Before(mailboxes[code], mailboxes[i])) code = i;
            }
            return result | ((uint)code << 24);
        }
        private uint ErrorStatus()
        {
            return (uint)((tec >= 96 || rec >= 96 ? 1 : 0) | (tec >= 128 || rec >= 128 ? 2 : 0)
                | (busOff ? 4 : 0) | (lec << 4)) | ((uint)Math.Min(255, tec) << 16) | ((uint)Math.Min(255, rec) << 24);
        }
        private void UpdateInterrupts()
        {
            var ts = TransmitStatus();
            TXInterrupt.Set((ier & 1) != 0 && (ts & 0x00010101u) != 0);
            for(var f = 0; f < 2; ++f)
            {
                var bits = (ier >> (f == 0 ? 1 : 4)) & 7;
                Connections[f + 1].Set(((bits & 1) != 0 && fifo[f].Count > 0)
                    || ((bits & 2) != 0 && (fifoFlags[f] & 8) != 0) || ((bits & 4) != 0 && (fifoFlags[f] & 16) != 0));
            }
            SCEInterrupt.Set(((ier & 0x8000) != 0 && (msr & 4) != 0)
                || ((ier & 0x10000) != 0 && (msr & 8) != 0) || ((ier & 0x20000) != 0 && (msr & 16) != 0));
        }
        private bool Before(Mailbox a, Mailbox b)
        {
            if((mcr & 4) != 0) return a.Order < b.Order;
            var ax = (a.Tir & 4) != 0; var bx = (b.Tir & 4) != 0;
            var ai = ax ? (a.Tir >> 3) & 0x1FFFFFFF : a.Tir >> 21;
            var bi = bx ? (b.Tir >> 3) & 0x1FFFFFFF : b.Tir >> 21;
            var abase = ax ? ai >> 18 : ai; var bbase = bx ? bi >> 18 : bi;
            if(abase != bbase) return abase < bbase;
            if(ax != bx) return !ax;
            if(ai != bi) return ai < bi;
            return (a.Tir & 2) < (b.Tir & 2);
        }
        private void StartController()
        {
            if(!Running || (Silent && !Loopback) || controller.ReadyNs > runtime.NowNs) return;
            var index = -1;
            for(var i = 0; i < 3; ++i) if(mailboxes[i].Pending && (index < 0 || Before(mailboxes[i], mailboxes[index]))) index = i;
            if(index < 0) return;
            controller.Mailbox = index;
            controller.SetTransmit(mailboxes[index].GetFrame());
            msr |= 0x100;
            runtime.Emit("tx_frame", "mailbox=" + index + " bits=" + controller.Tx.Bits.Count
                + " bitrate_hz=" + F(1e9 / controller.Timing.BitNs)
                + " sample_point=" + F((double)controller.Timing.Sample / controller.Timing.Total)
                + " sjw_tq=" + controller.Timing.Sjw);
        }
        private void Complete(int index, uint status, string outcome)
        {
            var m = mailboxes[index]; m.Status = status | 1; m.Pending = false; m.Tir &= ~1u; m.Abort = false;
            msr &= ~0x100u;
            runtime.Emit("tx_complete", "mailbox=" + index + " outcome=" + outcome + " tec=" + tec + " rec=" + rec);
            UpdateInterrupts();
        }
        private void ArbitrationLost(Node node)
        {
            if(node.Owner != null)
            {
                var m = mailboxes[node.Mailbox]; m.Status |= 4;
                runtime.Emit("arbitration", "mailbox=" + node.Mailbox + " outcome=lost tec=" + tec);
                if((mcr & 0x10) != 0 || m.Abort) Complete(node.Mailbox, 4, "arbitration_lost");
            }
            node.Tx = null; medium.Release(node);
        }
        private void TransmitSuccess(Node node)
        {
            if(node.Owner != null)
            {
                if(tec > 0) --tec;
                controller.Tec = tec; lec = 0;
                Complete(node.Mailbox, 2, Loopback ? "internal_loopback" : "acknowledged");
            }
            else { if(node.Tec > 0) --node.Tec; node.Pending.Dequeue(); }
            if(node.Tec >= 128 || node.Rec >= 128)
                node.ReadyNs = runtime.NowNs + (ulong)Math.Ceiling(11 * node.Timing.BitNs);
            node.Tx = null;
        }
        private void Receive(Frame frame)
        {
            if(!Running) return;
            if(rec > 127) rec = 120; else if(rec > 0) --rec;
            controller.Rec = rec; lec = 0;
            var match = -1; var bankChosen = -1; var rankChosen = -1; var fChosen = 0;
            var boundary = (int)((filters.Fmr >> 8) & 63);
            var shortId = (ushort)(((frame.Rir >> 16) & 0xFFE0) | ((frame.Rir & 2) << 3)
                | ((frame.Rir & 4) << 1) | ((frame.Rir >> 18) & 7));
            var numbers = new int[2];
            for(var bank = 0; bank < 28; ++bank)
            {
                var bit = 1u << bank; var wide = (filters.Scale & bit) != 0; var list = (filters.Mode & bit) != 0;
                var f = (filters.Assignment & bit) != 0 ? 1 : 0;
                var count = wide ? (list ? 2 : 1) : (list ? 4 : 2);
                var first = numbers[f]; numbers[f] += count;
                if((filters.Fmr & 1) != 0 || (filters.Active & bit) == 0 || (master == null ? bank >= boundary : bank < boundary)) continue;
                var rank = (wide ? 2 : 0) + (list ? 1 : 0);
                for(var i = 0; i < count; ++i)
                {
                    bool matches;
                    if(wide)
                    {
                        var id = filters.Words[2 * bank + (list ? i : 0)];
                        var mask = list ? 0xFFFFFFFFu : filters.Words[2 * bank + 1];
                        matches = (frame.Rir & mask) == (id & mask);
                    }
                    else
                    {
                        var w = filters.Words[2 * bank + (list ? i / 2 : i)];
                        var id = list ? (ushort)(w >> (16 * (i % 2))) : (ushort)w;
                        var mask = list ? (ushort)0xFFFF : (ushort)(w >> 16);
                        matches = (shortId & mask) == (id & mask);
                    }
                    if(matches && (rank > rankChosen || (rank == rankChosen && (bankChosen < 0 || bank < bankChosen))))
                    { rankChosen = rank; bankChosen = bank; match = first + i; fChosen = f; }
                }
            }
            if(match < 0) { UpdateInterrupts(); return; }
            var entry = new Received(frame, match, (ushort)(runtime.NowNs / (ulong)Math.Max(1, ControllerTiming().BitNs)));
            if(fifo[fChosen].Count == 3)
            {
                fifoFlags[fChosen] |= 16;
                if((mcr & 8) == 0) fifo[fChosen][2] = entry;
            }
            else fifo[fChosen].Add(entry);
            if(fifo[fChosen].Count == 3) fifoFlags[fChosen] |= 8;
            runtime.Emit("rx_frame", "id=" + frame.Id + " fifo=" + fChosen + " dlc=" + frame.Dlc + " outcome=accepted");
            UpdateInterrupts();
        }
        private void RecordError(Node node, int code, string cause, bool transmitting, bool passiveAckDominant = false)
        {
            // ISO 11898 error-passive ACK exception: an isolated passive node
            // does not become bus-off from absent ACK alone.
            var wasPassive = node.Tec >= 128 || node.Rec >= 128;
            if(transmitting)
            {
                if(!(code == 3 && wasPassive && !passiveAckDominant)) node.Tec += 8;
            }
            else node.Rec = Math.Min(255, node.Rec + 1);
            if(node.Owner == null) return;
            var oldFlags = ErrorStatus() & 7;
            tec = node.Tec; rec = node.Rec; lec = code;
            if(tec > 255)
            {
                busOff = true;
                runtime.Emit("bus_off", "outcome=entered tec=" + tec);
                if((mcr & 0x40) != 0) BeginRecovery();
            }
            var entered = (ErrorStatus() & 7) & ~oldFlags;
            if(((ier & 0x800) != 0 && code > 0 && code < 7)
                || ((entered & 1) != 0 && (ier & 0x100) != 0)
                || ((entered & 2) != 0 && (ier & 0x200) != 0)
                || ((entered & 4) != 0 && (ier & 0x400) != 0)) msr |= 4;
            runtime.Emit("error", "cause=" + cause + " lec=" + code + " tec=" + tec + " rec=" + rec);
            UpdateInterrupts();
        }
        private void BeginRecovery()
        {
            var generation = ++recoveryGeneration;
            var consecutive = 0; var occurrences = 0;
            var bit = (ulong)Math.Max(1, ControllerTiming().BitNs);
            Action observe = null;
            observe = () =>
            {
                if(generation != recoveryGeneration || !busOff) return;
                if((mcr & 1) != 0 || ((mcr & 0x40) == 0 && (msr & 3) != 0)) return;
                if(medium.Line.Sample("mcu", runtime.NowNs).Level == 1) ++consecutive; else consecutive = 0;
                if(consecutive >= 11) { consecutive = 0; ++occurrences; }
                if(occurrences >= 128)
                {
                    busOff = false; tec = rec = lec = 0; controller.Tec = controller.Rec = 0;
                    runtime.Emit("bus_off", "outcome=recovered recessive_sequences=128");
                    UpdateInterrupts(); medium.Request(); return;
                }
                runtime.Schedule(bit, observe);
            };
            runtime.Schedule(bit, observe);
        }
        private static string F(double value) { return value.ToString("R", CultureInfo.InvariantCulture); }

        private sealed class Medium
        {
            public Medium(TimedSTM32CAN owner, SignalRuntime line) { Owner = owner; Line = line; }
            public readonly TimedSTM32CAN Owner;
            public readonly SignalRuntime Line;
            public readonly List<Node> Nodes = new List<Node>();
            public bool Active;
            private bool scheduled, resting, synchronized;
            private int generation;
            private int endCount, flagsPending;
            private bool errored;
            public void Reset()
            {
                ++generation; Active = scheduled = resting = synchronized = errored = false;
                flagsPending = 0;
                foreach(var n in Nodes) { n.Tx = null; n.Decoder = null; n.ReadyNs = 0; Release(n); }
            }
            public void Release(Node node) { Drive(node, 1); }
            public void Drive(Node node, int bit)
            {
                if(node.Owner != null && Owner.Loopback) return;
                var state = bit == 0 ? 0 : -1;
                if(node.DrivenState == state) return;
                node.DrivenState = state;
                Line.Drive(node.Endpoint, state, Owner.runtime.NowNs);
            }
            public int Sample(Node node, bool detailed = false)
            {
                if(node.Owner != null && Owner.Loopback)
                {
                    if(node.Tx == null) return 1;
                    return node.BitIndex == node.Tx.AckIndex ? 0 : node.Tx.Bits[Math.Min(node.BitIndex, node.Tx.Bits.Count - 1)] & 1;
                }
                var sample = Line.Sample(node.Endpoint, Owner.runtime.NowNs);
                if(detailed)
                    Owner.runtime.Emit("sample", "signal=can_diff endpoint=" + node.Endpoint + " voltage_v=" + F(sample.Voltage)
                        + " bit=" + sample.Level + " cause=" + sample.Cause);
                return sample.Level;
            }
            public void Request()
            {
                if(Active || scheduled || resting) return;
                var pending = false;
                foreach(var box in Owner.mailboxes) if(box.Pending && Owner.Running && (!Owner.Silent || Owner.Loopback)) pending = true;
                if(!Owner.Loopback)
                    foreach(var node in Nodes) if(node.Owner == null && node.Pending.Count > 0 && node.Tec < 256) pending = true;
                if(!pending) return;
                scheduled = true;
                var current = generation;
                var idleBits = 0;
                Action start = null;
                start = () =>
                {
                    if(current != generation) return;
                    // Initial synchronization needs 11 sampled recessive bits;
                    // subsequent SOFs follow the measured error/intermission gap.
                    if(!Owner.Loopback && Line.Sample("mcu", Owner.runtime.NowNs).Level != 1)
                    { idleBits = 0; synchronized = false; }
                    else ++idleBits;
                    if(idleBits < (synchronized ? 1 : 11))
                    {
                        Owner.runtime.Schedule((ulong)Math.Max(1, Owner.controller.Timing.BitNs), start);
                        return;
                    }
                    synchronized = true;
                    scheduled = false;
                    Owner.StartController();
                    foreach(var n in Nodes)
                        if(!Owner.Loopback && n.Owner == null && n.Pending.Count > 0 && n.Tec < 256 && n.ReadyNs <= Owner.runtime.NowNs)
                            n.SetTransmit(n.Pending.Peek());
                    var any = false;
                    foreach(var n in Nodes) if((n.Owner != null || !Owner.Loopback) && n.Tx != null) any = true;
                    if(!any)
                    {
                        var earliest = UInt64.MaxValue;
                        foreach(var n in Nodes)
                            if((n.Owner != null || !Owner.Loopback) && n.ReadyNs > Owner.runtime.NowNs) earliest = Math.Min(earliest, n.ReadyNs);
                        if(earliest != UInt64.MaxValue) Owner.runtime.Schedule(earliest - Owner.runtime.NowNs, Request);
                        return;
                    }
                    Active = true; endCount = flagsPending = 0; errored = false;
                    var epoch = ++generation;
                    foreach(var n in Nodes)
                    {
                        if(n.Owner != null && !Owner.Running)
                        {
                            if((Owner.mcr & 2) != 0)
                            {
                                Owner.msr |= 8;
                                if((Owner.mcr & 0x20) != 0) { Owner.mcr &= ~2u; Owner.msr &= ~2u; }
                                Owner.UpdateInterrupts();
                            }
                            if(!Owner.Running) continue;
                        }
                        if(Owner.Loopback && n.Owner == null) continue;
                        n.Begin(epoch);
                    }
                };
                Owner.runtime.Schedule(1, start);
            }
            public void Finished(Node node, Frame frame)
            {
                if(node.Done) return;
                node.Done = true;
                var transmitting = node.Tx != null;
                if(transmitting)
                {
                    Owner.TransmitSuccess(node);
                    if(node.Owner != null && Owner.Loopback) Owner.Receive(frame);
                }
                else if(node.Owner != null) Owner.Receive(frame);
                Release(node);
                ++endCount;
                TryFinish(node);
            }
            public void Error(Node node, int code, string cause)
            {
                if(!Active || node.Invalid) return;
                node.Invalid = node.Done = true; ++endCount; errored = true;
                var transmitting = node.Tx != null;
                var wasPassive = node.Tec >= 128 || node.Rec >= 128;
                Owner.RecordError(node, code, cause, transmitting);
                if(transmitting && (node.Tec >= 128 || node.Rec >= 128))
                    node.ReadyNs = Owner.runtime.NowNs + (ulong)Math.Ceiling(25 * node.Timing.BitNs);
                if(transmitting && node.Owner != null)
                {
                    var box = Owner.mailboxes[node.Mailbox]; box.Status |= 8;
                    if((Owner.mcr & 0x10) != 0 || box.Abort)
                        Owner.Complete(node.Mailbox, 8, "transmit_error");
                }
                node.Tx = null;
                var silent = node.Owner != null && Owner.Silent;
                if(!wasPassive && !silent) Drive(node, 0); else Release(node);
                var epoch = generation;
                ++flagsPending;
                if(transmitting && code == 3 && wasPassive)
                {
                    var remaining = 6;
                    Action sampleFlag = null;
                    sampleFlag = () =>
                    {
                        if(epoch != generation || !Active) return;
                        if(Line.Sample(node.Endpoint, Owner.runtime.NowNs).Level == 0)
                        {
                            Owner.RecordError(node, 3, "dominant_during_passive_ack_flag", true, true);
                            return;
                        }
                        if(--remaining > 0)
                            Owner.runtime.Schedule((ulong)Math.Max(1, node.Timing.BitNs), sampleFlag);
                    };
                    Owner.runtime.Schedule((ulong)Math.Max(1, node.Timing.BitNs), sampleFlag);
                }
                // Other transmitters/receivers observe this physical error flag
                // using their own clocks and thresholds, not forced TEC updates.
                Owner.runtime.Schedule((ulong)Math.Max(1, 6 * node.Timing.BitNs), () =>
                {
                    if(epoch != generation) return;
                    Release(node); --flagsPending; TryFinish(node);
                });
            }
            private void TryFinish(Node node)
            {
                if(!Active || flagsPending != 0) return;
                var activeNodes = 0; var transmitters = 0;
                foreach(var n in Nodes)
                {
                    if(n.Epoch == generation) ++activeNodes;
                    if(n.Tx != null) ++transmitters;
                }
                if(endCount >= activeNodes || (errored && transmitters == 0))
                    FinishIdle((errored ? 11 : 3) * node.Timing.BitNs);
            }
            private void FinishIdle(double delay)
            {
                Active = false; resting = true;
                var epoch = ++generation;
                foreach(var n in Nodes) Release(n);
                if((Owner.mcr & 1) != 0) Owner.msr |= 1;
                if((Owner.mcr & 2) != 0 && (Owner.msr & 2) == 0) Owner.msr |= 0x12;
                Owner.UpdateInterrupts();
                Owner.runtime.Schedule((ulong)Math.Max(1, delay),
                    () => { if(epoch == generation) { resting = false; Request(); } });
            }
            public bool Current(int epoch) { return Active && epoch == generation; }
        }

        private sealed class Node
        {
            public Node(Medium medium, string endpoint, TimedSTM32CAN owner) { this.medium = medium; Endpoint = endpoint; Owner = owner; }
            public readonly string Endpoint;
            public readonly TimedSTM32CAN Owner;
            public readonly Queue<Frame> Pending = new Queue<Frame>();
            public Timing Timing;
            public WireFrame Tx;
            public Decoder Decoder;
            public bool AckEnabled = true, Done, Invalid;
            public int Tec, Rec, Mailbox, BitIndex, Epoch, DrivenState = Int32.MinValue;
            public ulong ReadyNs;
            public readonly Medium medium;
            private int phase, previousLevel;
            private bool started, ackDriving;
            private double nextNs;
            public void Configure(Timing timing) { Timing = timing; }
            public void SetTransmit(Frame frame) { Tx = new WireFrame(frame); }
            public void Begin(int epoch)
            {
                Epoch = epoch; BitIndex = phase = 0; previousLevel = 1;
                Done = Invalid = ackDriving = false; started = Tx != null;
                Decoder = new Decoder(this);
                nextNs = medium.Owner.runtime.NowNs;
                Tick();
            }
            private void Tick()
            {
                if(!medium.Current(Epoch) || Done) return;
                var now = medium.Owner.runtime.NowNs;
                if(started && phase == 0)
                {
                    if(Tx != null) medium.Drive(this, Tx.Bits[Math.Min(BitIndex, Tx.Bits.Count - 1)] & 1);
                    else
                    {
                        if(ackDriving) { medium.Release(this); ackDriving = false; }
                        if(Decoder.AckDue && !Invalid && AckEnabled && !(Owner != null && Owner.Silent))
                        { medium.Drive(this, 0); ackDriving = true; Decoder.AckDue = false; }
                    }
                }
                var level = medium.Sample(this, started && phase == Timing.Sample);
                if(!started)
                {
                    if(level == 0) { started = true; phase = 0; }
                }
                else if(Tx == null && level == 0 && previousLevel == 1 && phase != 0)
                {
                    // Receiver resynchronizes to actual sampled recessive->dominant
                    // edges. Bound phase correction by programmed SJW time quanta.
                    if(phase <= Timing.Sample) phase -= Math.Min(phase, Timing.Sjw);
                    else phase += Math.Min(Timing.Total - phase, Timing.Sjw);
                }
                previousLevel = level;
                if(started && phase == Timing.Sample)
                {
                    if(level < 0) { medium.Error(this, 4, "indeterminate_differential_sample"); return; }
                    if(Owner != null)
                    {
                        if(level != 0) Owner.msr |= 0xC00; else Owner.msr &= ~0xC00u;
                    }
                    if(Tx != null)
                    {
                        var symbol = Tx.Bits[Math.Min(BitIndex, Tx.Bits.Count - 1)];
                        var expected = symbol & 1;
                        if(BitIndex == Tx.AckIndex)
                        {
                            if(level != 0) { medium.Error(this, 3, "missing_ack"); return; }
                        }
                        else if(expected != level)
                        {
                            if((symbol & 2) != 0 && expected == 1 && level == 0)
                                medium.Owner.ArbitrationLost(this);
                            else { medium.Error(this, expected == 0 ? 5 : 4, "bit_monitoring"); return; }
                        }
                    }
                    if(!Invalid) Decoder.Accept(level);
                    if(!medium.Current(Epoch) || Done) return;
                }
                if(started)
                {
                    ++phase;
                    if(phase >= Timing.Total) { phase = 0; ++BitIndex; }
                }
                nextNs += Timing.TqNs;
                var delay = (ulong)Math.Max(1, Math.Round(nextNs - now));
                medium.Owner.runtime.Schedule(delay, Tick);
            }
        }

        // All receivers de-stuff and parse their own sampled wire stream; frame
        // delivery is not copied from the transmitted payload or preflight flags.
        private sealed class Decoder
        {
            public Decoder(Node node) { this.node = node; }
            public bool AckDue;
            private readonly Node node;
            private readonly List<int> raw = new List<int>(104);
            private int last = -1, run, expectedRaw = -1, crc, crcBits, crcWire;
            private bool stuffed = true, expectStuff, payloadDone;
            private int tail, eof;
            private Frame decoded;
            public void Accept(int bit)
            {
                if(stuffed)
                {
                    if(expectStuff)
                    {
                        if(bit == last) { Error(1, "stuff_error"); return; }
                        last = bit; run = 1; expectStuff = false;
                        if(payloadDone && crcBits == 15) stuffed = false;
                        return;
                    }
                    run = bit == last ? run + 1 : 1; last = bit;
                    if(run == 5) expectStuff = true;
                    if(!payloadDone)
                    {
                        raw.Add(bit); crc = CrcStep(crc, bit);
                        if(raw.Count == 1 && bit != 0) { Error(2, "invalid_sof"); return; }
                        if(raw.Count == 14)
                        {
                            var extended = raw[13] == 1;
                            expectedRaw = extended ? -1 : 19;
                        }
                        if(raw.Count == 19 && raw[13] == 0)
                        {
                            if(raw[14] != 0) { Error(2, "reserved_bit"); return; }
                            var dlc = Bits(raw, 15, 4);
                            expectedRaw = 19 + (raw[12] == 0 ? 8 * Math.Min(8, dlc) : 0);
                        }
                        if(raw.Count == 39 && raw[13] == 1)
                        {
                            if(raw[12] != 1) { Error(2, "invalid_srr"); return; }
                            if(raw[33] != 0 || raw[34] != 0) { Error(2, "reserved_bit"); return; }
                            var dlc = Bits(raw, 35, 4);
                            expectedRaw = 39 + (raw[32] == 0 ? 8 * Math.Min(8, dlc) : 0);
                        }
                        if(raw.Count == expectedRaw) payloadDone = true;
                    }
                    else
                    {
                        crcWire = (crcWire << 1) | bit; ++crcBits;
                        if(crcBits == 15)
                        {
                            if(crcWire != crc) { Error(6, "crc_error"); return; }
                            decoded = Decode();
                            if(!expectStuff) stuffed = false;
                        }
                    }
                    return;
                }
                // Unstuffed CRC delimiter, ACK slot, ACK delimiter, 7 EOF bits.
                if(tail == 0)
                {
                    if(bit != 1) { Error(2, "crc_delimiter"); return; }
                    if(node.Tx == null) AckDue = true;
                }
                else if(tail == 1) { /* ACK is checked by the actual transmitter. */ }
                else
                {
                    if(bit != 1) { Error(2, tail == 2 ? "ack_delimiter" : "eof_form_error"); return; }
                    if(tail >= 3 && ++eof == 7) node.medium.Finished(node, decoded);
                }
                ++tail;
            }
            private Frame Decode()
            {
                var ext = raw[13] == 1;
                var id = (uint)Bits(raw, 1, 11);
                if(ext) id = (id << 18) | (uint)Bits(raw, 14, 18);
                var remote = raw[ext ? 32 : 12] != 0;
                var dlc = Bits(raw, ext ? 35 : 15, 4);
                var count = remote ? 0 : Math.Min(8, dlc);
                var data = new byte[count];
                for(var i = 0; i < count; ++i) data[i] = (byte)Bits(raw, (ext ? 39 : 19) + 8 * i, 8);
                return new Frame(id, ext, remote, data, dlc);
            }
            private void Error(int code, string cause) { node.medium.Error(node, code, cause); }
        }

        private sealed class WireFrame
        {
            public WireFrame(Frame frame)
            {
                var raw = new List<byte>(118);
                raw.Add(0);
                Append(raw, frame.Extended ? frame.Id >> 18 : frame.Id, 11, true);
                if(frame.Extended)
                {
                    raw.Add(3); raw.Add(3); // SRR and IDE recessive, arbitration
                    Append(raw, frame.Id & 0x3FFFF, 18, true);
                    raw.Add((byte)(frame.Remote ? 3 : 2)); raw.Add(0); raw.Add(0);
                }
                else { raw.Add((byte)(frame.Remote ? 3 : 2)); raw.Add(2); raw.Add(0); }
                Append(raw, (uint)frame.Dlc, 4, false);
                if(!frame.Remote) foreach(var b in frame.Data) Append(raw, b, 8, false);
                var crc = 0; foreach(var b in raw) crc = CrcStep(crc, b & 1);
                Append(raw, (uint)crc, 15, false);
                var last = -1; var run = 0;
                for(var i = 0; i < raw.Count; ++i)
                {
                    var b = raw[i] & 1; Bits.Add(raw[i]);
                    run = b == last ? run + 1 : 1; last = b;
                    if(run == 5) { Bits.Add((byte)((1 - b) | (raw[i] & 2))); last = 1 - b; run = 1; }
                }
                Bits.Add(1); // CRC delimiter
                AckIndex = Bits.Count;
                Bits.Add(1); // ACK slot, transmitter releases
                Bits.Add(1); // ACK delimiter
                for(var i = 0; i < 7; ++i) Bits.Add(1);
            }
            // Bit 0 is the wire level; bit 1 marks the arbitration field.
            public readonly List<byte> Bits = new List<byte>(160);
            public readonly int AckIndex;
            private static void Append(List<byte> bits, uint value, int count, bool arbitration)
            {
                var marker = arbitration ? 2 : 0;
                for(var i = count - 1; i >= 0; --i) bits.Add((byte)(((value >> i) & 1) | (uint)marker));
            }
        }
        private static int CrcStep(int crc, int bit)
        {
            var feedback = ((crc >> 14) & 1) ^ bit;
            crc = (crc << 1) & 0x7FFF;
            if(feedback != 0) crc ^= 0x4599;
            return crc;
        }
        private static int Bits(List<int> bits, int start, int count)
        {
            var value = 0;
            for(var i = 0; i < count; ++i) value = (value << 1) | bits[start + i];
            return value;
        }
        private sealed class Frame
        {
            public Frame(uint id, bool extended, bool remote, byte[] data, int dlc)
            { Id = id; Extended = extended; Remote = remote; Data = data; Dlc = dlc; }
            public readonly uint Id;
            public readonly bool Extended, Remote;
            public readonly byte[] Data;
            public readonly int Dlc;
            public uint Rir { get { return (Extended ? (Id << 3) | 4u : Id << 21) | (Remote ? 2u : 0u); } }
            public uint Word(int start)
            {
                uint word = 0;
                for(var i = 0; i < 4 && start + i < Data.Length; ++i) word |= (uint)Data[start + i] << (8 * i);
                return word;
            }
        }
        private sealed class Mailbox
        {
            public uint Tir, Dtr, Low, High, Status;
            public ulong Order;
            public bool Pending, Abort;
            public Frame GetFrame()
            {
                var ext = (Tir & 4) != 0; var remote = (Tir & 2) != 0; var dlc = (int)(Dtr & 15);
                var data = new byte[remote ? 0 : Math.Min(8, dlc)];
                for(var i = 0; i < data.Length; ++i) data[i] = (byte)((i < 4 ? Low : High) >> (8 * (i % 4)));
                return new Frame(ext ? (Tir >> 3) & 0x1FFFFFFF : Tir >> 21, ext, remote, data, dlc);
            }
        }
        private sealed class Filters
        {
            public uint Fmr, Mode, Scale, Assignment, Active;
            public readonly uint[] Words = new uint[56];
            public void Reset() { Fmr = 0x2A1C0E01; Mode = Scale = Assignment = Active = 0; Array.Clear(Words, 0, Words.Length); }
        }
        private sealed class Received
        {
            public Received(Frame frame, int match, ushort timestamp) { Frame = frame; Match = match; Timestamp = timestamp; }
            public readonly Frame Frame;
            public readonly int Match;
            public readonly ushort Timestamp;
        }
        private sealed class Timing
        {
            public Timing(double tqNs, int total, int sample, int sjw) { TqNs = tqNs; Total = total; Sample = sample; Sjw = sjw; }
            public readonly double TqNs;
            public readonly int Total, Sample, Sjw;
            public double BitNs { get { return TqNs * Total; } }
        }
        private readonly BusRuntime runtime;
        private readonly double clock;
        private readonly TimedSTM32CAN master;
        private readonly Filters filters;
        private readonly Medium medium;
        private readonly Node controller;
        private readonly Dictionary<string, Node> peers = new Dictionary<string, Node>();
        private readonly Mailbox[] mailboxes = new Mailbox[3];
        private readonly List<Received>[] fifo = { new List<Received>(3), new List<Received>(3) };
        private readonly uint[] fifoFlags = new uint[2];
        private uint mcr, msr, ier, btr;
        private int tec, rec, lec, recoveryGeneration;
        private bool busOff;
        private ulong sequence;
    }
}
