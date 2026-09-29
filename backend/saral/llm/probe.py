"""Startup probe: are the configured free-tier models actually served?

Free tiers drift (models retire, move to paid tiers, close to new users), and a retired model
fails silently into the stub fallback. This probe lists each keyed provider's models once at
boot and logs, per role, which chain entries are live — an ERROR when a role that has keys set
resolves to the stub alone.
"""

from __future__ import annotations

import httpx

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)

_PROBE_TIMEOUT_S = 10.0


def _endpoints() -> dict[str, tuple[str, str | None, str]]:
    """provider -> (base_url, api_key, default_model) for the OpenAI-compatible providers."""
    s = get_settings()
    return {
        "gemini": (s.gemini_base_url, s.gemini_api_key, s.gemini_model),
        "groq": (s.groq_base_url, s.groq_api_key, s.groq_model),
        "cerebras": (s.cerebras_base_url, s.cerebras_api_key, s.cerebras_model),
    }


async def _list_models(client: httpx.AsyncClient, base_url: str, key: str) -> set[str]:
    r = await client.get(
        f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}
    )
    r.raise_for_status()
    # Gemini ids come back as "models/<id>"; strip it so ids match the config spelling.
    return {str(m["id"]).removeprefix("models/") for m in r.json().get("data", [])}


async def probe_chains() -> dict[str, list[str]]:
    """Return {role: [live chain entries]} and log dead / unknown entries. Never raises."""
    s = get_settings()
    endpoints = _endpoints()
    served: dict[str, set[str] | None] = {}
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
        for name, (base, key, _) in endpoints.items():
            first_key = next((k.strip() for k in (key or "").split(",") if k.strip()), None)
            if not first_key:
                continue
            try:
                served[name] = await _list_models(client, base, first_key)
            except Exception as e:  # noqa: BLE001 — probe is advisory, never blocks boot
                log.warning("llm.probe_failed", provider=name, error=str(e))
                served[name] = None  # unknown: don't claim the model is dead

    live: dict[str, list[str]] = {}
    for role in ("triage", "synthesis", "judge"):
        chain = s.role_chain(role)
        ok: list[str] = []
        for spec in chain:
            provider, _, model = spec.partition(":")
            if provider == "stub":
                continue
            if provider not in endpoints:
                ok.append(spec)  # sarvam/anthropic: not probed
                continue
            models = served.get(provider, set())
            if provider not in served:
                continue  # no key configured
            model = model or endpoints[provider][2]
            if models is None or model in models:
                ok.append(spec)
            else:
                log.error("llm.model_not_served", role=role, provider=provider, model=model)
        live[role] = ok
        if not ok and served:
            log.error("llm.role_stub_only", role=role, chain=chain)
        else:
            log.info("llm.role_chain_live", role=role, live=ok)
    return live
