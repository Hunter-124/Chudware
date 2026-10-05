using System;
using System.Collections.Generic;
using System.Globalization;
using Antmicro.Renode.Core;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Chudware
{
    public sealed class BusConfig
    {
        public BusConfig(Dictionary<string, object> values) { this.values = values ?? new Dictionary<string, object>(); }
        public bool Has(string key) { return values.ContainsKey(key); }
        public double Number(string key, double fallback) { object v; return values.TryGetValue(key, out v) && v != null ? Convert.ToDouble(v, CultureInfo.InvariantCulture) : fallback; }
        public string Text(string key, string fallback) { object v; return values.TryGetValue(key, out v) && v is string ? (string)v : fallback; }
        public bool Flag(string key, bool fallback) { object v; return values.TryGetValue(key, out v) && v is bool ? (bool)v : fallback; }
        public BusConfig Section(string key) { object v; return new BusConfig(values.TryGetValue(key, out v) ? v as Dictionary<string, object> : null); }
        public IEnumerable<BusConfig> Entries(string key) { object v; if(values.TryGetValue(key, out v) && v is object[]) foreach(var entry in (object[])v) yield return new BusConfig(entry as Dictionary<string, object>); }
        private readonly Dictionary<string, object> values;
    }

    public struct SignalSample
    {
        public double Voltage;
        public int Level;
        public string Cause;
    }

    public sealed class BusRuntime
    {
        public BusRuntime(IMachine machine, string name, string protocol)
        {
            this.machine = machine; this.name = name; this.protocol = protocol;
            Config = BusConfiguration.For(name);
        }
        public BusConfig Config { get; private set; }
        // Use observed execution time, not ScheduleAction's callback argument:
        // Renode 1.16.1 passes the time at which the action was scheduled.
        public ulong NowNs { get { return machine.ElapsedVirtualTime.TimeElapsed.Ticks; } }
        public void Schedule(ulong delayNs, Action action)
        {
            var captured = generation;
            machine.ScheduleAction(TimeInterval.FromNanoseconds(delayNs), _ => { if(captured == generation) action(); });
        }
        public void Reset() { generation++; }
        public SignalRuntime Signal(string signalName)
        {
            SignalRuntime signal;
            if(!signals.TryGetValue(signalName, out signal))
            {
                BusConfig definition = null;
                foreach(var entry in Config.Section("electrical").Entries("signals")) if(entry.Text("name", "") == signalName) definition = entry;
                signal = new SignalRuntime(definition, signalName == "can_diff"); signals.Add(signalName, signal);
                Emit("medium", "signal=" + signalName + " model=" + (definition == null ? "functional_ideal" : definition.Text("model", "unknown")));
            }
            return signal;
        }
        public void Emit(string eventName, string fields)
        {
            // Bound detailed samples separately; transaction evidence is bounded natively too.
            if(eventName == "sample" && sampleCount++ >= 4096) { if(sampleCount == 4097) Emit("trace_truncated", "cause=sample_limit"); return; }
            if(eventCount++ >= 65536) { if(eventCount == 65537) Console.WriteLine("CHUDWARE_BUS_EVT t_ns=" + NowNs + " bus=" + name + " protocol=" + protocol + " event=trace_truncated cause=event_limit"); return; }
            Console.WriteLine("CHUDWARE_BUS_EVT t_ns=" + NowNs.ToString(CultureInfo.InvariantCulture) + " bus=" + name + " protocol=" + protocol + " event=" + eventName + " " + fields);
        }
        private readonly IMachine machine;
        private readonly string name, protocol;
        private readonly Dictionary<string, SignalRuntime> signals = new Dictionary<string, SignalRuntime>();
        private ulong generation;
        private int sampleCount, eventCount;
    }

    // Reduced linear MNA: capacitor backward-Euler companion C/dt and Branin
    // incident e=v_far+Z0*i_far at t-TD, exactly the stamps in sim/spice.cpp.
    // No gmin or implicit termination. A singular floating resistive network is unknown.
    public sealed class SignalRuntime
    {
        private struct Transition { public double Time; public int State; public Transition(double time,int state) { Time=time;State=state; } }
        private sealed class Endpoint { public BusConfig Config; public int Node, State = -1, CommandedState = -1; public double Start, Target, Changed, Tau; public readonly List<Wave> History = new List<Wave>(); public readonly Queue<Transition> Pending = new Queue<Transition>(); }
        private sealed class Resistor { public int A, B; public double G, Source; }
        private struct Wave { public double Time, Value; public bool Known; public Wave(double t, double v, bool known = true) { Time = t; Value = v; Known = known; } }
        private sealed class Line { public int A, B; public double Z, Delay; public readonly List<Wave> AHistory = new List<Wave>(), BHistory = new List<Wave>(); }
        public SignalRuntime(BusConfig definition, bool differential)
        {
            this.definition = definition; this.differential = differential;
            if(definition == null) return;
            foreach(var e in definition.Entries("endpoints")) {
                var ep = new Endpoint { Config = e, Node = Node(e.Text("at", e.Text("id", ""))) };
                endpoints.Add(e.Text("id", ""), ep);
            }
            foreach(var segment in definition.Entries("segments")) {
                var line = new Line { A = Node(segment.Text("from", "")), B = Node(segment.Text("to", "")), Z = segment.Number("z0_ohm", 0), Delay = segment.Number("propagation_s", 0) };
                lines.Add(line); step = Math.Min(step, line.Delay / 8); horizon = Math.Max(horizon, line.Delay);
            }
            if(lines.Count == 0) {
                var locations = new List<int>(nodes.Values);
                double resistance = definition.Number("series_r_ohm", 0);
                if(resistance == 0) { foreach(var ep in endpoints.Values) ep.Node = 0; foreach(var key in new List<string>(nodes.Keys)) nodes[key] = 0; nodes["__wire"] = 0; }
                else { int common = Node("__wire"); foreach(int location in locations) resistors.Add(new Resistor { A = location, B = common, G = 2 / resistance }); }
            }
            foreach(var bias in definition.Entries("bias")) resistors.Add(new Resistor { A = Node(bias.Text("at", "")), B = -1, G = 1 / bias.Number("resistance_ohm", 0), Source = bias.Text("to", "ground") == "ground" ? 0 : bias.Number("voltage_v", 0) });
            foreach(var term in definition.Entries("terminations")) resistors.Add(new Resistor { A = Node(term.Text("at", "")), B = term.Has("other_at") ? Node(term.Text("other_at", "")) : -1, G = 1 / term.Number("resistance_ohm", 0), Source = term.Number("voltage_v", 0) });
            int nodeCount=0; foreach(int index in nodes.Values) nodeCount=Math.Max(nodeCount,index+1);
            voltage = new double[nodeCount]; capacitance = new double[nodeCount];
            foreach(var ep in endpoints.Values) {
                capacitance[ep.Node] += ep.Config.Number("input_capacitance_f", 0);
                horizon = Math.Max(horizon, ep.Config.Number("rx_delay_s", 0) + ep.Config.Number("tx_delay_s", 0));
                double edge = Math.Min(ep.Config.Number("rise_s", 0), ep.Config.Number("fall_s", 0)); if(edge > 0) step = Math.Min(step, edge / 16);
            }
            if(lines.Count == 0) capacitance[Node("__wire")] += definition.Number("wire_capacitance_f", 0);
            for(int i=0;i<capacitance.Length;i++) if(capacitance[i]>0) {
                double g=0; foreach(var r in resistors) if(r.A==i || r.B==i) g+=r.G;
                foreach(var ep in endpoints.Values) if(ep.Node==i) g+=1/Math.Min(ep.Config.Number("r_low_ohm",1),ep.Config.Number("r_high_ohm",1));
                if(g>0) step=Math.Min(step,capacitance[i]/g/16);
            }
            step = Math.Max(1e-10, step); PollIntervalNs = Math.Max(1UL, (ulong)Math.Ceiling(step*1e9));
            SettlingTimeNs = (ulong)Math.Ceiling((horizon*4+step*1024)*1e9);
            matrix = new double[voltage.Length, voltage.Length]; rhs = new double[voltage.Length];
            Reset(0);
        }
        public ulong PollIntervalNs { get; private set; }
        public ulong SettlingTimeNs { get; private set; }
        public void Reset(ulong tNs) {
            time = tNs * 1e-9; unknown = false; stableSteps = 0;
            ideal.Clear(); if(definition == null) return;
            Array.Clear(voltage, 0, voltage.Length);
            foreach(var ep in endpoints.Values) { ep.State=ep.CommandedState=-1; ep.Start=ep.Target=0; ep.Changed=time; ep.Tau=0; ep.Pending.Clear(); ep.History.Clear(); ep.History.Add(new Wave(time,0)); }
            // Quiescent start is explicit: stored energy starts at zero, biases charge it.
            foreach(var line in lines) { line.AHistory.Clear(); line.BHistory.Clear(); line.AHistory.Add(new Wave(time,0)); line.BHistory.Add(new Wave(time,0)); }
        }
        public void Drive(string endpoint, int state, ulong tNs) {
            if(state < -1 || state > 1) throw new ArgumentException("Drive state must be -1/0/1");
            if(definition == null) { ideal[endpoint]=state; return; }
            Endpoint ep; if(!endpoints.TryGetValue(endpoint,out ep)) throw new ArgumentException("Undeclared electrical endpoint: "+endpoint);
            Advance(tNs*1e-9);
            if(ep.CommandedState==state) return;
            ep.CommandedState=state;
            ep.Pending.Enqueue(new Transition(time+ep.Config.Number("tx_delay_s",0),state));
            ApplyTransitions();
        }
        public SignalSample Sample(string endpoint, ulong tNs) {
            if(definition == null) {
                int state = 1;
                foreach(var value in ideal.Values) if(value==0) state=0;
                return new SignalSample { Voltage=double.NaN, Level=state, Cause="functional_ideal" };
            }
            Endpoint ep; if(!endpoints.TryGetValue(endpoint,out ep)) throw new ArgumentException("Undeclared electrical endpoint: "+endpoint);
            double requested=tNs*1e-9;
            // A CPU/MMIO observer and a timer observer need not query in timestamp
            // order. Reading the past must not advance or rewind the circuit, and
            // receiver delay is relative to the requested time, not the frontier.
            if(requested>time) Advance(requested);
            bool known;
            double v=Incident(ep.History,requested-ep.Config.Number("rx_delay_s",0),out known);
            int level = !known ? -1 : differential ? (v>=ep.Config.Number("dominant_min_v",0) ? 0 : v<=ep.Config.Number("recessive_max_v",0) ? 1 : -1) : (v<=ep.Config.Number("vil_v",0) ? 0 : v>=ep.Config.Number("vih_v",0) ? 1 : -1);
            return new SignalSample { Voltage=v, Level=level, Cause=!known ? "floating_network" : level<0 ? "threshold_indeterminate" : "resolved" };
        }
        private int Node(string name) { int index; if(!nodes.TryGetValue(name,out index)) { index=0;foreach(int existing in nodes.Values) index=Math.Max(index,existing+1);nodes.Add(name,index); } return index; }
        private double Source(Endpoint ep,double at) { if(at<=ep.Changed) return ep.Start; return ep.Tau==0 ? ep.Target : ep.Target+(ep.Start-ep.Target)*Math.Exp(-(at-ep.Changed)/ep.Tau); }
        private static double Incident(List<Wave> history,double at) { bool known;return Incident(history,at,out known); }
        private static double Incident(List<Wave> history,double at,out bool known) {
            if(at<=history[0].Time) { known=history[0].Known;return history[0].Value; }
            int low=0,high=history.Count-1; while(low+1<high) { int mid=(low+high)/2; if(history[mid].Time<=at) low=mid; else high=mid; }
            if(at>=history[high].Time) { known=history[high].Known;return history[high].Value; }
            var a=history[low];var b=history[high];known=a.Known && (at==a.Time || b.Known);
            return a.Value+(b.Value-a.Value)*(at-a.Time)/(b.Time-a.Time);
        }
        private static void RecordWave(List<Wave> history,Wave wave) {
            // Retain receiver history for genuine historical reads. Only exactly
            // flat spans with identical validity can lose interior knots; no
            // tolerance-based decimation or substituting the latest voltage.
            int count=history.Count;
            if(count>=2 && history[count-2].Value==wave.Value && history[count-1].Value==wave.Value &&
               history[count-2].Known==wave.Known && history[count-1].Known==wave.Known) history[count-1]=wave;
            else history.Add(wave);
        }
        private static void TrimDelayHistory(List<Wave> history,double retain) {
            if(history.Count<=4096) return;
            int keep=0;while(keep+1<history.Count && history[keep+1].Time<retain) keep++;
            if(keep>0) history.RemoveRange(0,keep);
        }
        private void ApplyTransitions() {
            foreach(var ep in endpoints.Values) while(ep.Pending.Count>0 && ep.Pending.Peek().Time<=time) {
                var transition=ep.Pending.Dequeue();ep.Start=Source(ep,time);ep.State=transition.State;
                bool dominant=ep.Config.Text("drive","")=="can_dominant";
                ep.Target=ep.State<0 ? 0 : ep.Config.Number(dominant || ep.State==1 ? "v_high_v" : "v_low_v",0);
                ep.Changed=time;ep.Tau=ep.Config.Number(ep.Target>=ep.Start?"rise_s":"fall_s",0)/Math.Log(9);
                stableSteps=0;
            }
        }
        private void Advance(double target) {
            if(target<time) throw new ArgumentException("Electrical time cannot run backwards");
            if(definition==null) return;
            ApplyTransitions();
            while(time<target) {
                double dt=Math.Min(step,target-time);
                bool moving=false; foreach(var ep in endpoints.Values) if(ep.Pending.Count>0 || Math.Abs(Source(ep,time)-ep.Target)>1e-9) moving=true;
                // Locally quiet line ports can still have travelling waves.
                // Always retain the bounded timestep for Branin delay histories.
                if(lines.Count==0 && stableSteps>1024 && !moving) dt=target-time;
                foreach(var ep in endpoints.Values) if(ep.Pending.Count>0) dt=Math.Min(dt,ep.Pending.Peek().Time-time);
                double next=time+dt; Array.Clear(matrix,0,matrix.Length); Array.Clear(rhs,0,rhs.Length);
                foreach(var r in resistors) { matrix[r.A,r.A]+=r.G; if(r.B>=0) { matrix[r.B,r.B]+=r.G;matrix[r.A,r.B]-=r.G;matrix[r.B,r.A]-=r.G; } else rhs[r.A]+=r.G*r.Source; }
                foreach(var ep in endpoints.Values) {
                    int activeState=ep.State;
                    if(activeState<0) continue;
                    double r=ep.Config.Number(activeState==0 && ep.Config.Text("drive","")!="can_dominant" ? "r_low_ohm" : "r_high_ohm",0);
                    matrix[ep.Node,ep.Node]+=1/r;rhs[ep.Node]+=Source(ep,next)/r;
                }
                for(int i=0;i<voltage.Length;i++) { double g=capacitance[i]/dt;matrix[i,i]+=g;rhs[i]+=g*voltage[i]; }
                foreach(var line in lines) { matrix[line.A,line.A]+=1/line.Z;matrix[line.B,line.B]+=1/line.Z;rhs[line.A]+=Incident(line.BHistory,next-line.Delay)/line.Z;rhs[line.B]+=Incident(line.AHistory,next-line.Delay)/line.Z; }
                unknown=false;
                for(int k=0;k<voltage.Length;k++) {
                    int pivot=k;for(int j=k+1;j<voltage.Length;j++) if(Math.Abs(matrix[j,k])>Math.Abs(matrix[pivot,k])) pivot=j;
                    if(Math.Abs(matrix[pivot,k])<1e-24) { unknown=true;break; }
                    if(pivot!=k) { for(int j=k;j<voltage.Length;j++) { double swap=matrix[k,j];matrix[k,j]=matrix[pivot,j];matrix[pivot,j]=swap; } double swapR=rhs[k];rhs[k]=rhs[pivot];rhs[pivot]=swapR; }
                    for(int i=k+1;i<voltage.Length;i++) { double factor=matrix[i,k]/matrix[k,k];for(int j=k+1;j<voltage.Length;j++) matrix[i,j]-=factor*matrix[k,j];rhs[i]-=factor*rhs[k]; }
                }
                double delta=0;
                if(!unknown) for(int i=voltage.Length-1;i>=0;i--) { double value=rhs[i];for(int j=i+1;j<voltage.Length;j++) value-=matrix[i,j]*rhs[j];rhs[i]=value/matrix[i,i];delta=Math.Max(delta,Math.Abs(rhs[i]-voltage[i])); }
                if(!unknown) Array.Copy(rhs,voltage,voltage.Length);
                foreach(var ep in endpoints.Values) RecordWave(ep.History,new Wave(next,voltage[ep.Node],!unknown));
                stableSteps=delta<1e-10 ? stableSteps+1 : 0;
                foreach(var line in lines) {
                    double ea=Incident(line.BHistory,next-line.Delay),eb=Incident(line.AHistory,next-line.Delay);
                    RecordWave(line.AHistory,new Wave(next,2*voltage[line.A]-ea));RecordWave(line.BHistory,new Wave(next,2*voltage[line.B]-eb));
                    // Each port can settle independently. Keep the interpolation
                    // bracket for a complete delay, regardless of its knot count.
                    double retain=next-line.Delay-step*2;
                    TrimDelayHistory(line.AHistory,retain);TrimDelayHistory(line.BHistory,retain);
                }
                time=next;
                ApplyTransitions();
            }
        }
        private readonly BusConfig definition;
        private readonly bool differential;
        private readonly Dictionary<string,Endpoint> endpoints=new Dictionary<string,Endpoint>();
        private readonly Dictionary<string,int> nodes=new Dictionary<string,int>();
        private readonly Dictionary<string,int> ideal=new Dictionary<string,int>();
        private readonly List<Resistor> resistors=new List<Resistor>();
        private readonly List<Line> lines=new List<Line>();
        private double[] voltage,capacitance,rhs;
        private double[,] matrix;
        private double time,step=1e-7,horizon;
        private bool unknown;
        private int stableSteps;
    }
}
