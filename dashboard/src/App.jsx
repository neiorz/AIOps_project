import React, { useState, useEffect } from 'react';
import {
  ShieldAlert, Activity, Flame, GitMerge, RotateCw, Play, Info,
  CheckCircle, CheckCircle2, TrendingUp, Zap, Clock, Layers, Sparkles,
  BookOpen, LineChart, Terminal, GitCommit, Building2
} from 'lucide-react';

export default function App() {
  const [incidents, setIncidents] = useState([]);
  const [selectedIncident, setSelectedIncident] = useState(null);
  const [activeTab, setActiveTab] = useState('diagnosis');
  const [tenant, setTenant] = useState('all');
  const [isAutonomous, setIsAutonomous] = useState(true);
  const [benchmarks, setBenchmarks] = useState({
    rca_accuracy_percentage: 96.8,
    mean_time_to_detect_seconds: 3.2,
    sla_protection_rate: 99.4,
    alerts_compressed: 542
  });

  const [chaosForm, setChaosForm] = useState({
    service: 'cart-service',
    experiment: 'PodFailure',
    tenant: 'tenant_a'
  });

  const [toast, setToast] = useState(null);
  const [remediationLogs, setRemediationLogs] = useState([]);
  const [remediating, setRemediating] = useState(false);

  useEffect(() => {
    fetchIncidents();
    fetchBenchmarks();
    const interval = setInterval(fetchIncidents, 4000);
    return () => clearInterval(interval);
  }, [tenant]);

  const fetchIncidents = async () => {
    try {
      const url = tenant === 'all' ? '/api/v1/incidents' : `/api/v1/incidents?tenant_id=${tenant}`;
      const res = await fetch(url);
      const data = await res.json();
      setIncidents(data);
      if (data.length > 0 && !selectedIncident) {
        setSelectedIncident(data[0]);
      }
    } catch (e) {
      console.error(e);
    }
  };

  const fetchBenchmarks = async () => {
    try {
      const res = await fetch('/api/v1/benchmarks/scorecard');
      const data = await res.json();
      setBenchmarks(data);
    } catch (e) {
      console.error(e);
    }
  };

  const handleChaosInject = async () => {
    try {
      const res = await fetch('/api/v1/chaos/inject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service: chaosForm.service,
          experiment_type: chaosForm.experiment,
          tenant_id: chaosForm.tenant,
          duration_seconds: 60
        })
      });
      const data = await res.json();
      setToast(`Chaos injected: ${chaosForm.experiment} on ${chaosForm.service}`);
      await fetchIncidents();
      await fetchBenchmarks();
    } catch (e) {
      alert(e);
    }
  };

  return (
    <div className="bg-[#05070d] text-slate-100 min-h-screen font-sans">
      {/* Top Floating Island Header */}
      <header className="sticky top-3 z-50 px-6 max-w-[1780px] mx-auto">
        <div className="backdrop-blur-xl bg-[#090d16]/80 border border-white/10 rounded-2xl px-6 py-3.5 flex items-center justify-between shadow-2xl">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-teal-500/10 border border-teal-500/30 flex items-center justify-center text-teal-400">
              <ShieldAlert className="w-5 h-5" />
            </div>
            <div>
              <h1 className="font-extrabold text-sm tracking-tight text-white uppercase">AIOps Autonomous Command Center</h1>
              <p className="text-[11px] text-slate-400">Chaos-Grounded Multi-Tenant Incident Detection & Root-Cause Investigation</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={() => setIsAutonomous(!isAutonomous)}
              className={`px-3 py-1.5 rounded-xl border text-xs font-semibold flex items-center gap-2 ${
                isAutonomous ? 'bg-emerald-950/40 border-emerald-500/30 text-emerald-300' : 'bg-amber-950/40 border-amber-500/30 text-amber-300'
              }`}
            >
              <span className={`w-2 h-2 rounded-full ${isAutonomous ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`}></span>
              {isAutonomous ? 'Autonomous Self-Healing' : 'Human Approval Required'}
            </button>
          </div>
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-[1780px] mx-auto px-6 py-6 space-y-6">
        {/* Metric Scorecard */}
        <section className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="p-1 rounded-[1.5rem] bg-gradient-to-b from-white/10 to-transparent border border-white/5">
            <div className="bg-[#090d16] p-4 rounded-[calc(1.5rem-0.25rem)]">
              <span className="text-xs uppercase text-slate-400 font-semibold">RCA Accuracy</span>
              <div className="text-3xl font-mono font-bold text-white mt-1">{benchmarks.rca_accuracy_percentage}%</div>
            </div>
          </div>
          <div className="p-1 rounded-[1.5rem] bg-gradient-to-b from-white/10 to-transparent border border-white/5">
            <div className="bg-[#090d16] p-4 rounded-[calc(1.5rem-0.25rem)]">
              <span className="text-xs uppercase text-slate-400 font-semibold">MTTD Latency</span>
              <div className="text-3xl font-mono font-bold text-amber-400 mt-1">{benchmarks.mean_time_to_detect_seconds}s</div>
            </div>
          </div>
          <div className="p-1 rounded-[1.5rem] bg-gradient-to-b from-white/10 to-transparent border border-white/5">
            <div className="bg-[#090d16] p-4 rounded-[calc(1.5rem-0.25rem)]">
              <span className="text-xs uppercase text-slate-400 font-semibold">SLA Compliance</span>
              <div className="text-3xl font-mono font-bold text-emerald-400 mt-1">{benchmarks.sla_protection_rate}%</div>
            </div>
          </div>
          <div className="p-1 rounded-[1.5rem] bg-gradient-to-b from-white/10 to-transparent border border-white/5">
            <div className="bg-[#090d16] p-4 rounded-[calc(1.5rem-0.25rem)]">
              <span className="text-xs uppercase text-slate-400 font-semibold">Noise Compression</span>
              <div className="text-3xl font-mono font-bold text-indigo-400 mt-1">{benchmarks.alerts_compressed} → 1</div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

