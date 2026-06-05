# Saral

Multilingual, multi-agent customer-support resolution system for regulated
enterprises (insurance / lending). It verifies a customer, classifies intent,
retrieves grounded answers scoped to that customer, takes authorized account
actions behind confirmation, enforces compliance + PII rules, and escalates to a
human with full context when it cannot resolve safely.

## Language

### Actors

**Customer**:
The authenticated end-user (policyholder / borrower) whose data is in scope for a
conversation. The single principal that drives every turn. Saral embeds in the
host bank/insurer app, which authenticates the customer; the customer identifier
is always token-derived, never read from message content.
_Avoid_: User, client, account holder, end-user.

**Operator**:
A human support agent inside the enterprise who works escalations. NOT a graph
actor — an operator never drives a conversation turn or fires a tool. The operator
appears only as audit metadata on an escalation: who handled what, and when.
_Avoid_: Agent (reserved for the AI specialists), support rep, human.

**Agent**:
An AI specialist node in the supervisor graph (triage, RAG, action, synthesis,
compliance). Reserve "agent" for these; never use it for the human operator.
_Avoid_: Bot, specialist (ok informally), node.

**Tenant**:
One enterprise institution (a bank/insurer). Saral embeds in one tenant's app per
deployment, but every table and query carries `tenant_id` as a mandatory filter
(defense in depth, leak-proof retrofit avoided). No tenant-management features
exist — a single tenant is hardcoded.
_Avoid_: Org, institution (informally ok), client, account.

### Identity

**Auth level**:
The verification tier derived from the customer's token: `unverified` → `session`
(read actions ok) → `step_up` (state-changing actions ok). Raised only by a valid
challenge response, never self-asserted.

**Session token**:
A signed, short-TTL JWT that Saral treats as host-app-issued. Saral validates and
consumes it (`sub` → customer identifier); it does not own the production identity
provider. The bundled token-minting and OTP endpoints are a labeled test harness
that simulates the host app so the full step-up path runs offline.
_Avoid_: Auth token, API key, JWT (in prose; "session token" is canonical).

**Step-up**:
A second verification (OTP or known-datum re-confirmation) that raises auth level
to `step_up`. Required before an action the regulatory regime treats as sensitive
(see Action risk tier). Conceptually the host app's job; simulated here.
_Avoid_: 2FA, MFA, re-auth.

**Action risk tier**:
A declarative per-tool attribute (`requires_step_up: bool`) deciding whether an
action needs step-up. Binary today: all state-changing tools require step-up; all
reads do not. `update_contact` is the canonical takeover vector (SIM-swap →
number change → OTP interception) and is the load-bearing reason writes are gated.
_Avoid_: Permission level, scope (reserved for OAuth-style scopes if ever added).

### Retrieval

**Generic corpus**:
Tenant-wide, customer-agnostic knowledge: product FAQs, public policy templates,
grievance/process docs. Searched unscoped. Citations from here read as "general."
_Avoid_: Knowledge base, FAQ (informally ok).

**Per-customer scope**:
The set of a customer's own records, isolated by a mandatory `customer_id`
pre-filter applied *before* scoring (never post-rank). A query naming another
customer's policy id can never widen this; entities from the message body do not
relax the filter. Citations from here read as "your policy/claim."
_Avoid_: User data, account data (too vague).

**Data domain**:
One of five customer-scoped partitions — Policy & coverage, Claims, Billing &
payments, Interaction history, Profile & account. The unit of purpose-limitation:
data is organised by domain and access is granted per domain. NOT to be called a
"state" (collides with run-status).
_Avoid_: State, category, bucket, partition (in prose; "data domain" is canonical).

**Intent→domain map**:
A deterministic table (code, not LLM) mapping a classified intent to the data
domain(s) it may read. Triage classifies; this map authorizes data access;
retrieval enforces `customer_id` ∧ allowed-domains as a hard pre-filter. It IS the
DPDP purpose-limitation boundary — testable, and immune to injection widening
scope because the LLM never controls it. Default-deny on unmapped intents.
_Avoid_: Routing table (reserved for the supervisor's specialist routing).

### Gates

**Identity gate**:
The deterministic node (after triage) that validates the session token, derives
`auth_level`, and — for action intents — decides whether step-up is owed. Nothing
touches customer data before it runs.
_Avoid_: Auth check, login.

**Authorize gate**:
The pre-action gate: injection check + ownership/authz + step-up-satisfied check.
Runs before any state-changing tool fires; a failure blocks → escalates. Fails
closed.
_Avoid_: Compliance (too broad — name the specific gate), permission check.

**Redact gate**:
The pre-response gate: PII redaction (Presidio) on the outbound message before it
is emitted or stored. Distinct from the authorize gate — different check, different
point in the graph.
_Avoid_: Compliance, PII filter, scrubber.

### Lifecycle

**Conversation**:
The durable anchor for one customer thread. Holds the full transcript and survives
forever (per retention). Carries `customer_id` as the data-scope anchor. A
conversation spans many runs.
_Avoid_: Session, chat, thread (informally ok).

**Run**:
One worker execution within a conversation. May change id across a resume. Records
a terminal `close_reason` (`resolved | escalated | degraded | crashed |
timed_out | orphaned | abandoned`) + `closed_at` — so abnormal closes are never
context-loss.
_Avoid_: Job, task, execution (in prose; "run" is canonical), turn.

**Confirmation resume**:
Resuming a write after the customer answers "yes/no". Implemented as persisted
`pending_write` + a brand-new run — NOT a paused in-memory run. The fresh run
re-validates the token for free. Distinct from crash resume (checkpoint +
XAUTOCLAIM, same run).
_Avoid_: Suspend/resume (too broad — name which kind).

**Pending write**:
A state-changing action read back and awaiting the customer's confirmation,
persisted on the conversation. Its *execution authority* is mortal: past TTL it is
marked `abandoned`. Context is immortal; authority is not. A stale pending write
never auto-fires — resume re-earns step-up + confirm.
_Avoid_: Queued action, deferred write.

**Escalation**:
A terminal handoff of a run to a human, fired only on a *hard* signal (compliance
block, step-up failure, retrieval below the score floor, action-critical tool
failure, orphan TTL) — never on the LLM's self-assessment of its own answer. It
produces a durable record (intent, attempts, blocking reason, transcript ref,
pending action, SLA) plus operator audit fields (who handled it, when). Terminal
for the run; the conversation stays durable.
_Avoid_: Handoff (informally ok), fallback, transfer.

**Clarification**:
Pausing the run to ask the customer one question (`awaiting_input`) instead of
escalating — the *soft*-signal path (e.g. low intent confidence, ambiguous args).
Distinct from escalation: it expects a customer reply, not a human operator.
_Avoid_: Re-ask (informally ok), prompt.

**Retrieval score floor**:
The minimum retrieval relevance score below which an information answer is treated
as ungrounded → escalate rather than generate. The measurable groundedness guard
that stops the bot inventing a clause it has no passage for.
_Avoid_: Threshold, cutoff (in prose; "score floor" is canonical).

### Audit

**Audit log**:
A hash-chained, append-only record of every identity, authorize, and action
decision, scoped per conversation. Holds NO raw PII — only reason codes and
tokenized refs — so the 7-year regulatory hold stays erasure-compatible.
_Avoid_: Decision log (ok informally), event log, trace.

**Reason code**:
An enum value (not free text) recording *why* a decision was made. Replaces
prose-with-identifiers so the audit log leaks no PII; the human-readable story is
reconstructed from the code.
_Avoid_: Reason string, message.

**Tokenized ref**:
A hash standing in for an identifier in the audit log (`user_ref =
hash(tenant_id, user_id)`, `args_hash = hash(canonical_args)`). Lets the log point
at an entity without storing erasable PII.
_Avoid_: Token (collides with session token), pseudonym, hash (in prose).

### Data governance

**Consent status**:
A single host-app-supplied flag on the conversation (`granted` / `withdrawn`).
Saral records it, does not collect it. Processing proceeds only when `granted`;
withdrawal halts processing and fires an erasure event. Not per-purpose —
purpose-limitation is the intent→domain map's job.
_Avoid_: Permission, opt-in, GDPR flag.

**Erasure event**:
The act of tombstoning a customer's PII-bearing rows (message content, action
args) while leaving the hash-chained audit log untouched — which stays valid
because it holds no raw PII. The mechanism that makes the 7-year audit hold and
right-to-erasure coexist.
_Avoid_: Delete, wipe, GDPR delete, right-to-be-forgotten (in prose).

**Retention**:
Per-table TTL with a documented purge job (redacted messages 90d, audit 7y). The
audit's long hold is erasure-compatible by design (no PII to erase).
_Avoid_: Expiry, lifecycle, archival.

### Evaluation & data

**Hero customer**:
One of ~5–8 hand-authored synthetic customers with full document-fidelity records
across all 5 data domains, including planted dirt. Heroes carry the differentiator
and explanation-groundedness; most eval scenarios draw from them. Distinct from
thin customers (structured rows only, ~15–25) used for leak tests and statistical
N.
_Avoid_: Test user, sample customer.

**Document fidelity**:
Synthetic data authored at the level of real documents — actual clause text,
per-customer riders, adjudication notes, correspondence — internally consistent
with the structured rows. The opposite of generated rows alone. The project's
stated long pole and real cost.
_Avoid_: Realistic data, rich data.

**Dirt**:
Deliberate messiness planted in synthetic data so it can't flatter the bot —
typos, duplicates, missing fields, a claim with no matching policy, a lapsed
policy, a partial rejection citing a specific rider clause.
_Avoid_: Noise, edge cases (broader concept).

**Explanation-groundedness**:
The metric that a rejection/partial-claim explanation cites the customer's *own*
policy variant clause, not a generic template. The differentiator's measurable
proof; only testable against hero customers.
_Avoid_: Groundedness (that's the general claims-trace-to-citations metric).

**Judge**:
The evaluator that scores resolution accuracy (LLM-as-judge with a deterministic
rubric fallback for offline/CI). It is itself validated per language against
human labels (floor κ ≥ 0.6); a metric whose judge is below floor in a language is
gated behind human review, not trusted.
_Avoid_: Grader, evaluator (informally ok), critic.

**Cost**:
On the free-model stack, "cost" means **tokens per resolution + rate-limit budget
consumed**, NOT dollars. `cost_usd` is kept only as a 0-valued derived column for
forward-compat with a future paid swap. The scarce resource is rate-limit
headroom, and the efficiency signal that must trend down across prompt versions is
tokens/run.
_Avoid_: Dollar cost, spend, price (when discussing the demo's economics).

### Synthesis

**Grounding check**:
The runtime guard run before a response is emitted: every number, amount, clause
ref, and policy id in the message must trace to a retrieved passage or action
result, else the system falls back to the deterministic grounded draft. The
factual core is always deterministic; the LLM only rephrases tone/connective
wording, never facts.
_Avoid_: Hallucination check, fact-check, validation.

### Memory

**Short-term memory**:
The last N conversation turns carried in `RunState`, enabling multi-turn
follow-ups within a conversation.
_Avoid_: Context window, history (informally ok).

**Long-term memory**:
A customer's prior resolutions and interactions — NOT eagerly injected at
conversation start. It is exactly the Interaction-history data domain, retrieved
on-demand through the deterministic intent→domain map, `tenant_id ∧ customer_id`
pre-filtered and purpose-bound. There is no separate memory-injection path.
_Avoid_: Profile, user context, session memory.
