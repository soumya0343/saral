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
  language_match: number;
  answer_correctness: number;
  faithfulness: number;
  false_block_rate: number;
  multi_turn_success: number;
  rm_request_accuracy?: number;
  explanation_groundedness: number;
  pass_rate_by_language: Record<string, number>;
  judge_kappa_by_language: Record<string, number>;
  judge_untrusted_languages: string[];
  tokens_total: number;
};
type ScenarioResult = {
  scenario_id: string;
  category: string;
  passed: boolean;
  route: string | null;
  status: string | null;
  latency_ms: number;
  language: string;
  reply: string;
  metrics: Record<string, boolean>;
  failed_turns: number[];
  error: string | null;
};
type Report = {
  config_version: string;
  created_at: string;
  tier: "offline" | "live";
  judge_model: string;
  kappa_note: string;
  summary: Summary;
  results: ScenarioResult[];
  regressions: Record<string, number>;
  prior_version: string | null;
};

const METRICS: [keyof Summary, string][] = [
  ["pass_rate", "Pass rate"],
  ["language_match", "Reply in the customer's language"],
  ["answer_correctness", "Answer correctness (required facts + source)"],
  ["faithfulness", "Faithfulness (no unsupported numbers / claims)"],
  ["explanation_groundedness", "Explanation cites the customer's own documents"],
  ["multi_turn_success", "Multi-turn conversations"],
  ["rm_request_accuracy", "RM requests raised correctly"],
  ["routing_accuracy", "Routing accuracy"],
  ["tool_sequence_correctness", "Tool-sequence correctness"],
  ["groundedness", "Groundedness (has citations)"],
  ["resolution_accuracy", "Resolution accuracy (judge)"],
  ["cross_lingual_consistency", "Cross-lingual consistency"],
];

const LANG_LABEL: Record<string, string> = { en: "English", hi: "Hindi", hinglish: "Hinglish" };

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
      if (!r.ok) {
        // In prod, running the suite spends free-tier quota, so it isn't public: use `make eval`.
        setError(
          r.status === 404 || r.status === 401
            ? "Running the suite over HTTP is disabled on this deployment. Run `make eval` instead."
            : r.status === 409
              ? "This server uses real models: run the live eval from the CLI (`make eval-live`)."
              : `Eval run failed (${r.status}).`,
        );
        return;
      }
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
      {error && report && <p className="mt-4 text-sm text-amber-600">{error}</p>}

      {report && (
        <>
          <p className="mt-2 text-sm text-neutral-500">
            <span
              className={`mr-1 rounded px-1.5 py-0.5 text-xs font-medium ${
                report.tier === "live"
                  ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                  : "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"
              }`}
              title={
                report.tier === "live"
                  ? "Real free models, judged by a pinned LLM judge"
                  : "Stub LLM + hashing embedder: deterministic plumbing check, not model quality"
              }
            >
              {report.tier ?? "offline"}
            </span>
            config <span className="font-mono">{report.config_version}</span> ·{" "}
            {report.summary.n} scenarios · judge <span className="font-mono">{report.judge_model}</span>{" "}
            · p50/p95 {report.summary.latency_p50_ms}/{report.summary.latency_p95_ms} ms
            {report.summary.tokens_total > 0 && ` · ${report.summary.tokens_total} tokens`}
          </p>
          {report.kappa_note && (
            <p className="mt-1 text-xs text-neutral-400">
              Judge validation: {report.kappa_note}
              {Object.keys(report.summary.judge_kappa_by_language ?? {}).length > 0 &&
                ` · κ ${JSON.stringify(report.summary.judge_kappa_by_language)}`}
              {(report.summary.judge_untrusted_languages ?? []).length > 0 &&
                ` · not validated: ${report.summary.judge_untrusted_languages.join(", ")}`}
            </p>
          )}

          {Object.keys(report.regressions).length > 0 && (
            <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/40">
              Regressions vs {report.prior_version}:{" "}
              {Object.entries(report.regressions)
                .map(([k, v]) => `${k} ${v}`)
                .join(", ")}
            </div>
          )}

          <section className="mt-6">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Safety</h2>
            <Bar
              label="Attacks blocked"
              value={report.summary.compliance_block_rate}
              target
            />
            <Bar
              label="Benign look-alikes NOT blocked (1 − false-block rate)"
              value={1 - (report.summary.false_block_rate ?? 0)}
              target
            />
          </section>

          {report.summary.pass_rate_by_language && (
            <section className="mt-6">
              <h2 className="mb-2 text-sm font-medium text-neutral-500">Pass rate by language</h2>
              {Object.entries(report.summary.pass_rate_by_language).map(([lang, v]) => (
                <Bar key={lang} label={LANG_LABEL[lang] ?? lang} value={v} />
              ))}
            </section>
          )}

          <section className="mt-6">
            <h2 className="mb-2 text-sm font-medium text-neutral-500">Quality</h2>
            {METRICS.filter(([key]) => report.summary[key] !== undefined).map(([key, label]) => (
              <Bar key={key} label={label} value={report.summary[key] as number} />
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
                    <th className="p-2">Lang</th>
                    <th className="p-2">Pass</th>
                  </tr>
                </thead>
                <tbody>
                  {report.results.map((r) => (
                    <tr
                      key={r.scenario_id}
                      className="border-t border-neutral-100 align-top dark:border-neutral-800"
                    >
                      <td className="p-2 font-mono text-xs">
                        {r.scenario_id}
                        {!r.passed && (
                          <div className="mt-1 font-sans text-[11px] text-red-600 dark:text-red-400">
                            {r.error
                              ? `crashed: ${r.error}`
                              : Object.entries(r.metrics ?? {})
                                  .filter(([, ok]) => !ok)
                                  .map(([k]) => k)
                                  .join(", ")}
                            {r.failed_turns?.length > 0 && ` (turns ${r.failed_turns.join(", ")})`}
                          </div>
                        )}
                        {r.reply && (
                          <div
                            className="mt-1 max-w-md truncate font-sans text-[11px] text-neutral-500"
                            title={r.reply}
                          >
                            {r.reply}
                          </div>
                        )}
                      </td>
                      <td className="p-2">{r.category}</td>
                      <td className="p-2">{r.route ?? "—"}</td>
                      <td className="p-2">{r.status ?? "—"}</td>
                      <td className="p-2">{r.language}</td>
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
