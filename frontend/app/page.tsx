"use client";

import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Health = { status: string; env: string; version: string };

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_URL}/health`)
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-6 p-8">
      <div>
        <h1 className="text-3xl font-semibold">सरल · Saral</h1>
        <p className="mt-1 text-neutral-500">
          Multilingual multi-agent support-resolution engine
        </p>
      </div>

      <div className="rounded-xl border border-neutral-200 p-4 dark:border-neutral-800">
        <div className="text-sm font-medium text-neutral-500">API health</div>
        {health ? (
          <div className="mt-2 flex items-center gap-2">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-green-500" />
            <span className="font-mono text-sm">
              {health.status} · {health.env} · v{health.version}
            </span>
          </div>
        ) : error ? (
          <div className="mt-2 flex items-center gap-2">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-red-500" />
            <span className="font-mono text-sm text-red-500">{error}</span>
          </div>
        ) : (
          <div className="mt-2 text-sm text-neutral-400">checking…</div>
        )}
      </div>

      <p className="text-xs text-neutral-400">
        Phase 0 scaffold. Chat, run-trace, and eval dashboard arrive in later phases.
      </p>
    </main>
  );
}
