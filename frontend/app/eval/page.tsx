"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Summary = {
  n: number;
  pass_rate: number;
  routing_accuracy: number;
  tool_sequence_correctness: number;
  compliance_block_rate: number;
  groundedness: number;
  resolution_accuracy: number;
  cross_lingual_consistency: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  judge_human_agreement: number | null;
};
type ScenarioResult = {
  scenario_id: string;
  category: string;
  passed: boolean;
  route: string | null;
  status: string | null;
  latency_ms: number;
};
type Report = {
  config_version: string;
  created_at: string;
  summary: Summary;
  results: ScenarioResult[];
  regressions: Record<string, number>;
  prior_version: string | null;
};

const METRICS: [keyof Summary, string][] = [
  ["pass_rate", "Pass rate"],
  ["routing_accuracy", "Routing accuracy"],
  ["tool_sequence_correctness", "Tool-sequence correctness"],
  ["compliance_block_rate", "Compliance block rate"],
  ["groundedness", "Groundedness"],
  ["resolution_accuracy", "Resolution accuracy"],
  ["cross_lingual_consistency", "Cross-lingual consistency"],
];

function Bar({ label, value, target }: { label: string; value: number; target?: boolean }) {
  const pct = Math.round(value * 100);
  const ok = target ? value >= 1 : value >= 0.85;
  return (
    <div className="mb-3">
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span className="font-mono">{pct}%{target ? " · target 100%" : ""}</span>
      </div>
      <div className="mt-1 h-2 w-full rounded bg-neutral-200 dark:bg-neutral-800">
        <div
          className={`h-2 rounded ${ok ? "bg-green-500" : "bg-amber-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function EvalDashboard() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = () =>
    fetch(`${API_URL}/eval/report`)
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setReport)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, []);

  const runEval = async () => {
    setRunning(true);
    setError(null);
    try {
      const r = await fetch(`${API_URL}/eval/run`, { method: "POST" });
      setReport(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <main className="mx-auto max-w-3xl p-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Saral · Eval Dashboard</h1>
        <button
          onClick={runEval}
          disabled={running}
          className="rounded-lg bg-neutral-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {running ? "Running…" : "Run eval"}
        </button>
      </div>

      {error && !report && (
        <p className="mt-6 text-sm text-neutral-500">
          No report yet ({error}). Click “Run eval”.
        </p>
      )}

      {report && (
        <>
          <p className="mt-2 text-sm text-neutral-500">
            config <span className="font-mono">{report.config_version}</span> ·{" "}
            {report.summary.n} scenarios · p50/p95 {report.summary.latency_p50_ms}/
            {report.summary.latency_p95_ms} ms
            {report.summary.judge_human_agreement != null &&
              ` · judge↔human ${Math.round(report.summary.judge_human_agreement * 100)}%`}
          </p>

          {Object.keys(report.regressions).length > 0 && (
            <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/40">
              Regressions vs {report.prior_version}:{" "}
              {Object.entries(report.regressions)
                .map(([k, v]) => `${k} ${v}`)
                .join(", ")}
            </div>
          )}

          <section className="mt-6">
            {METRICS.map(([key, label]) => (
              <Bar
                key={key}
                label={label}
                value={report.summary[key] as number}
                target={key === "compliance_block_rate"}
              />
            ))}
          </section>

          <section className="mt-8">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Scenarios</h2>
            <div className="overflow-hidden rounded-lg border border-neutral-200 dark:border-neutral-800">
              <table className="w-full text-sm">
                <thead className="bg-neutral-50 text-left dark:bg-neutral-900">
                  <tr>
                    <th className="p-2">ID</th>
                    <th className="p-2">Category</th>
                    <th className="p-2">Route</th>
                    <th className="p-2">Status</th>
                    <th className="p-2">ms</th>
                    <th className="p-2">Pass</th>
                  </tr>
                </thead>
                <tbody>
                  {report.results.map((r) => (
                    <tr key={r.scenario_id} className="border-t border-neutral-100 dark:border-neutral-800">
                      <td className="p-2 font-mono text-xs">{r.scenario_id}</td>
                      <td className="p-2">{r.category}</td>
                      <td className="p-2">{r.route ?? "—"}</td>
                      <td className="p-2">{r.status ?? "—"}</td>
                      <td className="p-2">{r.latency_ms}</td>
                      <td className="p-2">{r.passed ? "✅" : "❌"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
