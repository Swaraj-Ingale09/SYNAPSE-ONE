# SYNAPSE ONE — Future Workstation Specification

> **Purpose:** Product vision, system context, architecture direction, and a
> phased implementation guide for future coding agents.
>
> **Audience:** The SYNAPSE ONE owner, coding agents, and future contributors.
>
> **Status:** Forward-looking product specification. It is not a statement that
> every capability below already exists.
>
> **Core product identity:** A personal, persistent, multi-capability AI
> workstation for research, knowledge, monitoring, communication, planning,
> media, and approved work across connected services. SYNAPSE ONE is **not a
> coding agent**. Coding may be one capability, but it is not the product's
> purpose or organizing metaphor.

---

## 0. Instructions for future coding agents

This document describes a long-term direction. It does **not** authorize a
coding agent to implement the entire roadmap in one change.

Before making a change:

1. Inspect the current repository, current branch, dirty worktree, existing
   tests, current documentation, and all related architecture decisions.
2. Treat uncommitted work as user-owned. Never discard or overwrite it.
3. Check what is actually implemented today. This document may describe future
   capabilities that do not yet exist.
4. Choose one small, complete, user-visible vertical slice from the active
   phase. Do not opportunistically implement unrelated future phases.
5. Preserve existing behavior unless a deliberate, documented behavior change
   is part of the task.
6. Make precise changes, keep the existing technology and patterns unless
   there is a concrete reason to change them, and update directly related docs.
7. Enforce authentication, workspace isolation, permission policy, budget
   limits, and audit requirements in backend services—not only in the UI.
8. Never add fake success, fabricated metrics, simulated model output, silent
   security fallbacks, or hidden external side effects.
9. Run the smallest relevant validation unless the user specifically says not
   to run tests or other validation. Report validation that was actually run.
10. Do not claim a capability is finished until its behavior and failure paths
    are implemented and verified.

The finished product should be ambitious. Individual changes should remain
bounded, reviewable, reversible, and honest.

---

## 1. Product vision

SYNAPSE ONE is a **personal AI workstation** that lets its owner request
outcomes in ordinary language and coordinates appropriate capabilities to
achieve them.

The user describes what they want, not the implementation steps. SYNAPSE
figures out whether it should search, read an authorized source, retrieve
workspace knowledge, call a model, create an artifact, monitor a topic, or ask
for a decision. It carries out the steps that are permitted, requests approval
when necessary, reports progress, and shows what it accomplished.

### 1.1 Product promise

> **Tell SYNAPSE the outcome you want. It will plan the work, use the services
> and sources you authorized, ask only for decisions or permissions it needs,
> do the permitted work, keep you informed, and report the result with evidence.**

The user should not need to understand model APIs, database migrations, worker
processes, prompt templates, connector names, or deployment commands to use the
product. Those details belong behind a reliable interface. When consent or a
choice is required, ask a short, contextual question with a clear explanation.
Do not hand the user a technical checklist when the system can safely complete
the setup itself.

### 1.2 What SYNAPSE ONE should become

One coherent platform for:

- Asking questions and learning unfamiliar topics.
- Searching approved public sources and connected services.
- Researching subjects and producing traceable reports.
- Organizing knowledge across user-selected workspaces.
- Monitoring sources, technologies, topics, and user-defined interests.
- Alerting the user when meaningful changes occur.
- Drafting and transforming text.
- Generating or processing media where an authorized provider supports it.
- Planning and coordinating multi-step tasks.
- Managing approved work across connected accounts and tools.
- Connecting to the same personal core from the CLI, dashboard, MCP hosts,
  IDEs, and future clients.

The workstation should feel alert, capable, and personal. It must not pretend
that it can access every service, know the entire web, or do every possible
task. Unsupported requests should receive a truthful explanation and, where
possible, an actionable choice such as connecting a supported provider.

### 1.3 What the product is not

- Not primarily a coding agent, autonomous software engineer, or code editor.
- Not a generic chatbot with decorative system graphics.
- Not an uncontrolled web scraper or a product claiming to contain the entire
  internet.
- Not an account-password collector or an API-key hunter.
- Not a system that silently spends money, sends messages, publishes content,
  changes account settings, or deletes data.
- Not a self-modifying program that updates or deploys itself without
  authorization and a recoverable update process.
- Not a collection of neural-network buzzwords without a measured product need.
- Not a product whose success is measured by file count or lines of code.

### 1.4 Product distinction

The strongest distinction is not “one model that beats every other model.”
Models change, and their quality differs by task, cost, speed, context size,
privacy, and modality. The defensible product is the **workstation layer** that
coordinates:

1. User context and workspace knowledge.
2. Multiple models and service providers.
3. Fresh, permissioned source access.
4. Durable, observable tasks.
5. User-controlled autonomy and approvals.
6. Results, citations, files, alerts, and a trustworthy action history.

SYNAPSE should help the user choose the right capability for the task, preserve
the user’s context and preferences, and make the combined workflow easier than
jumping between isolated tools.

### 1.5 Multi-agent workstation direction

SYNAPSE ONE should support a coordinated team of specialized AI workers, not
just one monolithic assistant and not an uncontrolled swarm. A user may see
multiple specialists researching, checking, summarizing, or preparing parts of
a task at the same time. A coordinator owns the user’s goal, delegates bounded
work, integrates the results, and remains accountable for the final response.

The experience may be inspired by the broad idea of a live, web-connected
multi-agent assistant, but SYNAPSE ONE must define its own product, design,
implementation, and terminology. It must not copy another product’s
proprietary interface, branding, prompts, source code, or non-public behavior.
Its differentiator should be user-owned workspaces, secure connections,
observable persistent workflows, source-grounded collaboration, and
user-controlled autonomy.

Every agent shown in the UI must correspond to real execution state. An agent
must never be depicted as “thinking,” searching, or working when no underlying
task or model call is taking place.

---

## 2. Current repository context

The current repository is already a meaningful backend foundation. Future
coding agents must inspect current code before relying on any description here.
The branch, worktree, and product may have changed since this document was
written.

### 2.1 Existing architectural shape

The project is a Python modular monolith with a React dashboard:

- `src/synapse/api/` — FastAPI application and routes.
- `src/synapse/cli/` — Typer CLI and HTTP client.
- `src/synapse/db/` — SQLAlchemy models and Alembic migrations.
- `src/synapse/worker/` — durable job execution.
- `src/synapse/gateway/` — model routing and provider adapters.
- `src/synapse/knowledge/` — document storage, chunking, embeddings, retrieval.
- `src/synapse/connectors/` — public-API connector registry.
- `src/synapse/net/` — outbound fetching and SSRF controls.
- `src/synapse/research.py` — evidence-first research pipeline.
- `src/synapse/graph/` and `src/synapse/telemetry/` — graph snapshot and
  measured host telemetry.
- `src/synapse/mcp/` — MCP server work; inspect current implementation and
  worktree status before extending.
- `dashboard/src/` — React dashboard and real-data graph/panels.

The existing design choices include a modular monolith, PostgreSQL and
pgvector, a single model-gateway egress point, bounded web fetching, knowledge
retrieval, public API connectors, dashboard serving/authentication, and
evidence-first research. Consult [docs/decisions/](./decisions/) before
changing those boundaries.

### 2.2 Existing capabilities to preserve and build upon

The current project includes or has implemented substantial parts of:

- Durable PostgreSQL jobs with worker claims, leases, lifecycle transitions,
  cancellation, recovery, and event history.
- A local authenticated FastAPI service and thin CLI.
- A model gateway with provider adapters and a durable model-usage ledger.
- Knowledge ingestion, chunking, embeddings, full-text/vector hybrid
  retrieval, and knowledge-grounded asks.
- A safe outbound fetcher with URL validation, redirect validation, size
  bounds, content-type controls, and robots handling.
- Public API connectors and search surfaces.
- An evidence-first research workflow that gathers source material before
  synthesis and reports source provenance.
- Dashboard telemetry, status panels, search, events, jobs/usage, and a graph
  based on real service records.
- MCP server implementation work; verify current state and make all behavior
  consistent with the shared API and permission model.

These are foundations, not proof that the full product vision has been
achieved.

### 2.3 Gaps that influence the roadmap

Treat these as items to verify against current code before implementation:

1. **Workspace isolation:** Job records have project-scope concepts, but
   verify that users, documents, chunks, retrieval, conversations, watches,
   artifacts, and API routes all enforce a consistent workspace boundary.
2. **Credential lifecycle:** Provider configuration currently includes
   environment-based secrets. Design a complete connection lifecycle,
   secure secret storage, refresh, scope display, and revocation before adding
   many integrations.
3. **Continuous monitoring:** Durable jobs and events provide useful
   primitives, but watches, recurrence, scheduler coordination, freshness,
   alert history, and pause/resume are product-level capabilities still to
   design and implement.
4. **Interactive flow:** A dashboard prompt bar that submits work and a live
   operations display are useful, but a full workstation also needs persistent
   conversations, result inspection, approvals, and artifact management.
5. **Current docs:** The README and product brief may describe earlier phases.
   Keep “implemented,” “in progress,” and “planned” explicit and accurate.
6. **Cross-client parity:** CLI, dashboard, MCP, and future clients must share
   the same workspace, connection, permission, and capability semantics.
7. **Provider truth:** The provider catalog, model IDs, pricing, supported
   modalities, API limits, and OAuth scopes must be checked against current
   provider documentation and never assumed.

### 2.4 Existing product rules to retain

The current product has strong principles that should continue to guide future
work:

- A success indication must mean the underlying operation actually succeeded.
- Missing data must not be presented as a measured zero.
- External content is untrusted data, not policy or tool authorization.
- Provider calls should use one audited gateway.
- Outbound web access should use bounded, safe network code.
- Jobs and their events should be inspectable.
- Costs should be measured or explicitly unknown, never guessed.
- Tests should prove behavior, including failure paths, not only happy paths.

---

## 3. Product principles

### 3.1 User intent first

Users describe outcomes. SYNAPSE handles implementation details. Ask the user
for a choice only when the choice affects the outcome, security, privacy, cost,
or an external side effect.

Prefer:

> “I can monitor these two official release feeds daily and alert you when a
> stable release is published. Should I enable this in your Technology
> workspace?”

Avoid:

> “Install package X, edit environment variable Y, configure cron, paste this
> token, and run these five commands.”

The application may explain technical details on request, but the normal
experience should be outcome-oriented.

### 3.2 Honest capability boundaries

- Show which capabilities are connected, configured, unavailable, or not
  supported.
- Do not imply that SYNAPSE can access an account merely because it knows the
  user’s email address.
- Do not say that it searched a source it did not actually search.
- Distinguish source evidence from model inference and speculation.
- Never label a failed monitor check “no change.”
- Be clear when a provider requires a user-authorized connection or paid plan.
- Make uncertain or conflicting evidence visible.

### 3.3 Permission before consequential action

Permission is scoped, recorded, and revocable. The system may carry out
low-risk work within a narrow grant; it may not enlarge its own authority.

Actions requiring just-in-time approval by default include:

- Sending messages or emails.
- Publishing or posting content.
- Sharing data with a new external service.
- Spending money or exceeding an approved budget.
- Creating subscriptions, purchasing services, or changing billing.
- Changing account security, permissions, or recovery settings.
- Deleting or overwriting user data.
- Making commitments on behalf of the user.

### 3.4 Safe autonomy, not unbounded autonomy

“Always active” means scheduled, observable, resource-bounded work with a stop
button—not continuous arbitrary execution.

Every background task must have:

- A defined objective and owner.
- An allowlist of tools, accounts, destinations, and sources.
- A schedule, timeout, and maximum work per run.
- A budget and concurrency limit.
- A pause/resume/stop mechanism.
- A visible run history, failures, and next-run time.
- An expiry or reauthorization rule where appropriate.

### 3.5 Privacy by default

- Collect and retain only what the task needs.
- Tell the user when content will leave the device and which provider receives
  it.
- Offer local processing where supported and configured.
- Keep secrets out of prompts, job payloads, logs, database content, URLs,
  frontend storage, and error messages.
- Allow users to inspect, export, and delete their conversations, sources, and
  artifacts.

### 3.6 Useful capability over complexity theatre

Use NLP, LLMs, embeddings, classifiers, neural networks, forecasting, and
multimodal models when they solve a real problem and can be evaluated. Do not
add CNNs, RNNs, agents, vector databases, or visualizations simply to make the
project appear complex.

---

## 4. User and identity model

### 4.1 Initial deployment

The initial product is primarily a personal workstation on the user’s machine.
Do not introduce a multi-tenant identity system prematurely. However, design
entities and APIs so ownership and workspace boundaries are explicit, even
when there is only one local user.

### 4.2 Email is identity, not authorization

The user may identify themselves with an email address, but an email address
does not grant access to external accounts. SYNAPSE must not:

- Ask for or store the user’s email password.
- Search the web, source code, logs, or repositories for leaked API keys.
- Guess credentials or scrape a login session.
- Circumvent MFA, CAPTCHAs, access control, provider rate limits, or terms.
- Claim it can create credentials on every provider’s behalf.

Instead, implement provider-supported OAuth, device authorization, or secure
user-entered API keys. SYNAPSE should guide the user through the supported
authorization flow and then perform permitted work without repeatedly asking
them to operate the provider website.

### 4.3 Local user and future accounts

For local mode:

- Create a local owner identity or a single-owner context with explicit
  ownership on all protected records.
- Keep local API access authenticated.
- Store tokens/secrets in a secure OS credential store where available.
- Provide token rotation and revoke/reset flows.

If a hosted or multi-user edition is introduced later:

- Add real authentication, per-user ownership, team membership, and
  authorization checks.
- Isolate tenant data at the database and service layer.
- Revisit secret storage, audit retention, backups, rate limits, and threat
  model.
- Do not assume loopback-only protections apply to remote deployments.

---

## 5. Autonomy, approval, and user interaction

### 5.1 Autonomy modes

Offer clear, user-understandable modes, with workspace-level and workflow-level
overrides:

1. **Ask each time** — SYNAPSE may research and prepare work; ask before each
   consequential action.
2. **Prepare and notify** — SYNAPSE may perform bounded read-only work,
   generate drafts, and notify; it may not send, publish, spend, or modify
   protected data.
3. **Allowlisted automation** — SYNAPSE may perform specific low-risk actions
   under named limits. The grant must identify the action, account, destination,
   frequency, budget, and expiry.

No mode grants unlimited access or makes an arbitrary future action
pre-approved.

### 5.2 Approval prompt contract

Before requesting approval, present:

1. The specific action SYNAPSE proposes.
2. The result it expects to produce.
3. The exact account, workspace, source, destination, or recipient involved.
4. The information that will be sent or changed.
5. Whether the action has a cost, quota, or irreversible effect.
6. The permission scope and duration.
7. A one-time approval option and, only where safe, a narrow standing-rule
   option.
8. A clear decline, pause, or cancel option.

Approval must bind to the exact action and material input. If the recipient,
content, amount, permission scope, or other material detail changes, obtain a
new approval.

### 5.3 Approval record

Persist:

- Request ID, user, workspace, job/workflow, and capability.
- Canonical action description and sanitized input preview.
- Target account/destination.
- Permission scope and risk class.
- Estimated cost, if genuinely available.
- Creation and expiry times.
- Decision and decision timestamp.
- Execution attempt/result IDs.
- Audit event references.

Do not make an approval record itself a bearer token. Recheck live permissions
and account state at execution time.

### 5.4 “Only ask me” interaction design

SYNAPSE should own the operational work. It may ask the user to:

- Choose among meaningful outcomes or sources.
- Approve account access or a specific action.
- Resolve ambiguity that materially changes the result.
- Confirm a cost or irreversible operation.
- Provide a missing fact that cannot safely be inferred.

It should not ask the user to perform routine installation, copying, file
editing, or provider navigation when an in-product guided flow can handle it
safely. If an external provider requires a user interaction that SYNAPSE cannot
perform, explain exactly why and offer a secure, provider-supported
authorization flow.

### 5.5 User settings and preferences

Preferences may include:

- Default workspace.
- Preferred language and response style.
- Preferred model/routing policy.
- Quiet hours and alert delivery.
- Cost thresholds.
- Source trust preferences.
- Default autonomy mode.
- Retention and local-only preferences.

Preferences must be inspectable, editable, and deletable. Do not infer
sensitive traits or turn a temporary instruction into permanent memory without
clear user control.

---

## 6. Core user journeys

These journeys define the intended product shape. Implement them one vertical
slice at a time.

### 6.1 Ask and learn

1. User selects a workspace or uses the configured default.
2. User asks a question in natural language.
3. SYNAPSE determines whether the task can be answered from workspace
   knowledge, needs current sources, or needs a model/provider.
4. It displays the selected route and source strategy where relevant.
5. It streams a response or displays durable progress.
6. It shows citations, generated artifacts, provider/model, and uncertainty.
7. User can ask follow-ups, save the result, or correct a source.

### 6.2 Research and report

1. User asks for an evidence-based report.
2. SYNAPSE builds a bounded plan and selects permitted sources/connectors.
3. Unknown, unsupported, or disallowed sources are identified before
   execution.
4. The system searches and gathers evidence under rate and size limits.
5. Each source records whether content was fetched or only a provider snippet
   was available.
6. The model synthesizes only the evidence actually gathered.
7. The answer includes source links, dates, source-level failures, and usage.
8. User can save/export the report into a workspace.

### 6.3 Monitor a topic

1. User describes an interest, topic, source, or change they care about.
2. SYNAPSE suggests a supported source set and schedule.
3. It explains limits and asks for approval to create the watch.
4. The scheduler enqueues bounded runs.
5. The system distinguishes “no change” from “could not check.”
6. Meaningful changes create an in-app alert with evidence and explanation.
7. User can inspect, snooze, edit, pause, resume, or delete the watch.

### 6.4 Connect an account

1. User asks to connect a provider or requests a task requiring one.
2. SYNAPSE explains what the provider connection enables and which scopes are
   needed.
3. The user approves the official provider authorization flow.
4. SYNAPSE stores only a secure credential reference and records granted
   scopes.
5. The user can see the connection state, last use, permissions, and revoke
   action.
6. SYNAPSE reuses the connection only for the approved capabilities.

### 6.5 Create a media artifact

1. User requests an image, audio, video, or other supported artifact.
2. SYNAPSE identifies supported providers and required inputs.
3. It checks user preferences, privacy constraints, and budget.
4. It asks for approval if a new provider, paid operation, or external
   publication is involved.
5. A durable job tracks the actual generation/processing lifecycle.
6. The artifact is stored with provenance and a retention policy.
7. User previews, downloads, revises, or deletes it.

### 6.6 Complex multi-step task

1. SYNAPSE creates a plan with clear steps, dependencies, and success criteria.
2. It identifies capability, source, connection, budget, and approval needs.
3. User confirms the plan if scope/cost/risks warrant it.
4. Each step executes as a durable, observable job or workflow activity.
5. A failure blocks only dependent steps where safe; independent steps may
   continue if the plan allows it.
6. The system asks only when a new user decision is genuinely required.
7. Final response reports completed, partial, blocked, and failed work
   separately, with evidence and artifacts.

---

## 7. Target architecture

### 7.1 Architectural stance

Keep the current **modular monolith** until real operational requirements
justify a service boundary. Microservices add deployment, networking, data
consistency, and failure complexity. First establish stable domain contracts,
permission seams, and reliable single-process behavior.

The current PostgreSQL + pgvector design is a reasonable local product
foundation. Reconsider storage only for demonstrated requirements such as
multi-device synchronization, hosted multi-tenancy, very large media stores, or
analytical scale.

### 7.2 Logical modules

Use these as conceptual boundaries. Do not create empty modules in advance;
extract code when an implemented feature needs the boundary.

```text
api/                 HTTP transport, auth dependencies, versioned contracts
accounts/            Provider connections, scopes, refresh, revoke
automation/          User-defined workflows, schedule, run policy
capabilities/        Bounded tools/actions and schemas
conversations/       Conversations, messages, resumable interaction
connectors/          Public APIs, feeds, and approved source adapters
content/             Artifacts, media metadata, storage references
db/                  Models, sessions, migrations
events/              Durable lifecycle/progress event contracts
gateway/             Model providers, routing, usage, budgets
knowledge/           Workspaces, documents, chunks, retrieval, provenance
monitoring/           Watches, polling state, change detection, alerts
notifications/       In-app and explicitly connected delivery channels
policy/               Permissions, approvals, audit, autonomy rules
scheduler/           Due-work dispatch; no duplicate business logic
security/             Secrets, redaction, data classification
worker/               Durable jobs, execution, cancellation, recovery
```

### 7.3 Responsibility boundaries

- API routes validate transport input and delegate to domain services.
- CLI, dashboard, MCP, and IDE clients use shared API contracts. They do not
  become separate sources of truth.
- Domain services implement product behavior independent of HTTP/UI.
- The worker executes registered bounded capabilities and emits real progress.
- The scheduler only decides what is due and enqueues work; it does not
  duplicate task execution logic.
- The capability registry describes exactly what an action can do.
- The policy engine authorizes each capability before execution.
- Provider adapters isolate provider-specific wire formats and actual
  capabilities.
- The credential service retrieves credentials only for a specific approved
  provider operation.
- PostgreSQL remains the source of truth for durable user state and job
  history.

### 7.4 Capability registry

Each callable capability must declare:

- Stable name and version.
- Description suitable for users and model/tool selection.
- Input and output schema.
- Required connection scopes.
- Workspace/data access requirements.
- Read-only, local-write, or external-side-effect classification.
- Risk class.
- Cost and budget requirements.
- Maximum runtime, payload, and result size.
- Idempotency support.
- Cancellation behavior.
- Audit events to write.
- Whether a narrowly allowlisted policy may run it unattended.

Examples of future bounded capabilities:

- `search_public_connector`
- `fetch_approved_public_url`
- `retrieve_workspace_knowledge`
- `create_workspace_document`
- `monitor_source`
- `generate_text`
- `generate_image`
- `transcribe_audio`
- `summarize_media`
- `create_message_draft`
- `send_user_approved_message`
- `create_notification`

Do not add an unrestricted `do_anything`, generic browser, shell, or filesystem
capability as a shortcut.

### 7.5 Durable jobs

All work that may outlive an HTTP request or needs recovery should use the
durable job system.

Preserve and extend:

- Central legal state transitions.
- PostgreSQL row-lock-based claiming.
- Lease heartbeats and stale-job recovery.
- Short independent transactions for heartbeat, cancellation, and events.
- Cooperative cancellation with honest limitations.
- Bounded retry policy.
- Job correlation and causation identifiers.
- Durable event IDs and client resume.
- Clear timeout and terminal-result semantics.

Add deliberately:

- Submission idempotency keys.
- Queue priority/fairness for interactive work versus background monitors.
- Per-workspace and per-provider concurrency controls.
- Task deadlines and maximum resource budgets.
- Explicit retryability classification.
- Side-effect checkpoints to avoid duplicate sends or purchases after crashes.
- Partial completion representation where a workflow has multiple steps.
- Queue visibility, failed-job review, and operator health.

Do not blindly retry a provider request or external side effect if the remote
service may already have processed it.

### 7.6 Multi-agent execution model

Multi-agent orchestration is a **coordination pattern over registered
capabilities and durable jobs**, not a new source of authority. Implement only
after workspace ownership, capability policy, approvals, job lifecycle, and
usage accounting are reliable.

#### Roles

- **Coordinator:** interprets the user request, proposes a plan when useful,
  chooses bounded tasks, assigns specialists, tracks dependencies, resolves
  completion, and reports the final outcome.
- **Specialist:** performs one narrowly defined task, such as source discovery,
  research extraction, evidence comparison, summarization, monitoring
  classification, translation, or media preparation.
- **Verifier / critic:** checks a concrete property of another result, such as
  source support, schema validity, arithmetic, duplication, or policy
  compliance. It does not get blanket authority to take new actions.
- **Human approval gate:** represents a pending user decision. It is not
  bypassed by delegation or by a different agent agreeing with the proposed
  action.

These are role definitions, not a requirement to run a different model for
each role. A single configured model may perform multiple roles, or different
providers may be selected where capability, privacy, quality, availability,
and budget justify it. The system must report which provider/model actually
ran each task.

#### Initial specialist roster

Start with a small, explainable roster. These are task profiles over registered
capabilities, not free-running personas with independent authority.

| Specialist profile | Bounded responsibility | Example output |
|---|---|---|
| Source scout | Find candidate sources using selected connectors | Ranked source references and search errors |
| Evidence reader | Extract relevant claims from gathered source text | Claim snippets with exact source IDs/spans |
| Comparison analyst | Compare evidence across sources | Similarities, conflicts, and coverage gaps |
| Fact verifier | Check whether proposed statements are supported by supplied evidence | Supported/unsupported claim list with evidence references |
| Briefing writer | Assemble an answer or report from approved evidence | Draft with citations and disclosed uncertainty |
| Change monitor | Classify a source change against a watch definition | Change/no-change/uncertain result with source diff |
| Media preparer | Prepare a request for a supported media provider | Validated generation parameters and cost/permission needs |

Add a profile only when it has a specific capability set, schema, budget,
evaluation, and user-visible value. Do not maintain a large menu of named
agents just to make the product look busy.

#### Example: complex technology briefing

For a request such as “Give me a briefing on the latest developments in a
technology and alert me if something important changes,” the coordinator could:

1. Clarify the topic or workspace only if it is ambiguous.
2. Select permitted connectors and show any unsupported sources.
3. Create parallel source-scout tasks for independent connector groups.
4. Deduplicate results and send bounded source material to evidence readers.
5. Ask a comparison analyst to identify agreement and conflict.
6. Ask a verifier to check the draft’s factual claims against cited evidence.
7. Produce a final report with citations, source failures, and coverage notes.
8. Separately propose a recurring watch and ask before enabling it.

Each child task has its own real status, provider usage, time/cost limits, and
evidence references. If the verifier or one connector fails, the coordinator
reports the limitation; it does not invent a successful verification. Creating
the future watch is a separate user-approved action, not an implicit side
effect of the one-time research request.

#### Delegation contract

Every delegated task must have:

- A parent workflow/run and a unique task ID.
- A bounded objective and a typed input/output schema.
- A declared workspace and data classification.
- An explicit allowlist of tools/capabilities.
- A source set or source policy.
- A maximum number of model/tool calls, runtime, result size, and cost.
- A timeout and cancellation route.
- An expected completion condition.
- A policy decision recorded before execution.
- A structured result, evidence references, and error state.

Agents may return recommendations or request a capability. They cannot grant
themselves permissions, read other workspaces, expand their source list, or
approve a side effect. Every requested capability is re-authorized by the
shared policy service at the point of use.

#### Coordination and shared state

- The coordinator owns the plan and parent workflow status.
- Specialists receive only the input required for their subtask; do not
  automatically copy the entire conversation or all workspace contents into
  every subtask.
- Shared state is explicit, typed, versioned, and scoped to the parent
  workflow/workspace.
- Results should reference evidence records and artifacts by ID rather than
  duplicating large documents or media in agent messages.
- Agent-to-agent communication is a structured internal message/task record,
  not an unrestricted prompt channel.
- Conflicting results remain visible and are resolved through evidence,
  verification, or a user question—not by hiding disagreement.
- The final response identifies meaningful delegation and partial failures
  when they affect the outcome.

#### Scheduling, parallelism, and efficiency

- Parallelize only independent tasks with declared dependencies.
- Set a strict per-workflow agent, task, provider-call, and token budget.
- Limit per-workspace and per-provider concurrency.
- Prioritize interactive user work over recurring background tasks.
- Cancel unneeded child tasks when a parent is cancelled or the plan changes.
- Avoid launching duplicate specialists for the same source/query unless
  comparison is intentional and budgeted.
- Use deterministic code for routing, permissions, limits, and aggregation
  wherever practical; reserve model calls for language/reasoning work.
- Cache only where privacy, freshness, provider terms, and invalidation
  behavior are explicitly defined.
- Measure whether multiple agents improve quality, coverage, or latency over a
  single-agent baseline. If not, use the simpler path.

#### Failure and recovery

- A child task failure is reported with its real reason and whether it is
  retryable.
- Independent branches may complete if a sibling fails, when the parent plan
  allows partial results.
- Dependent branches must not execute with missing or invalid required input.
- A parent may not be marked complete until its declared completion criteria
  are satisfied or it is explicitly marked partial.
- Worker restart/retry must not duplicate paid or external side effects.
- Agent timeouts, budget exhaustion, and cancellation are first-class states.
- The UI must distinguish active, queued, waiting for approval, completed,
  failed, cancelled, and timed-out work.

#### Why this is not an uncontrolled swarm

The number of agents is not a product objective. SYNAPSE should use the
smallest team that solves the task. Unbounded agent loops create cost,
confusion, duplicated work, hidden data exposure, and difficult cancellation.
Agent creation must be constrained by explicit limits and observable
orchestration records.

### 7.7 Event model

Events should describe real state transitions and useful progress. Include:

- Event ID and schema version.
- Event type and timestamp.
- Actor/service component.
- Correlation and causation IDs.
- Workspace, job, workflow, and source identifiers where relevant.
- Redacted structured metadata.
- Data classification and retention policy where appropriate.

Do not place raw secrets, full prompts, whole documents, or unnecessary
personal data in events.

---

## 8. Workspace, knowledge, and memory

### 8.1 Workspace as the primary context boundary

A workspace groups related conversations, knowledge, sources, workflows,
artifacts, and preferences.

Possible workspace fields:

- ID, owner, name, description.
- Created/updated timestamps.
- Default model or routing preference.
- Source inclusion/exclusion policy.
- Local authorized roots, if enabled.
- Connected account references permitted in the workspace.
- Retention and export policy.
- Budget and background-work limits.

Every relevant query must enforce workspace scope in the backend. UI filters
are not security controls.

### 8.2 Knowledge records

Document/source records should capture:

- Workspace and owner.
- Canonical origin identifier.
- Source type.
- Title and safe metadata.
- Content hash.
- Content size and storage reference.
- Created, fetched, updated, and indexed times.
- HTTP validators such as ETag or Last-Modified, when available.
- Parser/extractor version.
- Embedding provider, model, and version.
- License/usage notes when available.
- Indexing status and sanitized failure reason.
- Retention and deletion status.

Chunk records should capture:

- Document/workspace association.
- Exact source character/token spans where available.
- Stable chunk index.
- Content or protected storage reference.
- Embedding identity/version.
- Creation/indexing metadata.

### 8.3 Retrieval contract

Retrieval must:

- Enforce workspace and ownership boundaries.
- Support lexical, semantic, and hybrid search as actual configured.
- Label the retrieval mode honestly.
- Return exact provenance and source spans.
- Apply query length, result count, context, and cost bounds.
- Make stale or unindexed content distinguishable.
- Avoid mixing embeddings from incompatible models as if comparable.
- Offer an explicit re-embedding/migration path when embedding providers
  change.
- Support evaluation using a stable test/evaluation corpus.

### 8.4 User-visible knowledge actions

Users should be able to:

- Inspect what SYNAPSE knows in a workspace.
- Search and browse sources.
- See indexing status, freshness, and failures.
- Re-index, pause refresh, or remove a source.
- Delete derived chunks and embeddings with the parent source.
- Export source metadata and workspace data.
- View where a citation came from.
- Correct metadata while preserving original provenance.

### 8.5 Memory versus source knowledge

Keep these concepts distinct:

- **Source knowledge:** material from documents, websites, feeds, APIs, and
  files. It has provenance, freshness, rights, and deletion behavior.
- **User preference:** explicit settings such as preferred language or model.
- **Conversation context:** bounded recent dialogue for a specific thread.
- **Long-term user memory:** optional user-approved facts/preferences with
  explicit inspection, editing, and deletion.

Do not silently convert every conversation into permanent memory. Do not infer
sensitive user characteristics without explicit need and consent.

---

## 9. Internet research and source ecosystem

### 9.1 “Whole internet” product interpretation

No practical workstation can pre-download, own, or constantly understand the
entire internet. The product goal is:

> **Broad, fresh, relevant access to permitted sources, with visible coverage,
> provenance, and limits.**

Represent coverage as connected source catalogs and watch definitions. Clearly
show that an unconnected or unsupported source has not been searched.

### 9.2 Source priority

Prefer, in order:

1. Official APIs with documented access.
2. RSS/Atom feeds and provider webhooks.
3. Public websites that permit the requested retrieval.
4. User-provided content and authorized local files.
5. Other sources only after an explicit policy and rights review.

All source adapters must use the shared outbound HTTP/security layer. Avoid
connector-specific direct networking that bypasses SSRF protections,
rate-limits, content bounds, or error reporting.

### 9.3 Source catalog

For each source or connector, track:

- Stable source/provider ID.
- Domain/topic and concise description.
- Access mechanism and documentation reference.
- Required credentials and scopes.
- Supported search/filter/refresh capabilities.
- Real rate limits and current quota behavior.
- Content types and maximum payloads.
- Robots/terms restrictions where applicable.
- Freshness expectations and last successful check.
- Provider-specific failure reasons.

Do not hardcode a rate limit, robots verdict, model ID, price, or response
shape without verifying it from an authoritative current source.

### 9.4 Search and synthesis

Research tasks should:

- Validate requested connectors before network calls.
- Search independently where practical and preserve per-source failures.
- Deduplicate by canonical URL or provider ID.
- Fetch only a bounded number of sources.
- Treat provider snippets as snippets, not full-page evidence.
- Use a clear whole-source/context budget.
- Preserve source title, URL, connector, fetch status, date, and text length.
- Never say “no results” if the request was blocked or failed.
- Never synthesize an evidence-based answer if no usable evidence was gathered.
- Keep citations tied to the exact material provided to the model.
- Report disagreements and uncertainty instead of smoothing them away.

### 9.5 Source trust and data rights

Provide source labels such as:

- Official/primary source.
- Reputable secondary source.
- Community source.
- Unknown provenance.
- User-provided source.

These labels should be evidence-based and explainable. Do not present a
heuristic trust label as a verified fact. Preserve rights/retention metadata
when known. Respect provider usage terms and do not republish or expose content
outside allowed use.

---

## 10. Monitoring, schedules, forecasts, and alerts

### 10.1 Watch model

A watch is an explicitly user-created recurring observation.

Fields may include:

- Owner, workspace, display name.
- User’s intent/question.
- Source or connector definition.
- Search/filter query.
- Schedule, timezone, minimum interval.
- Alert threshold and deduplication policy.
- Notification destination.
- Budget, request cap, result cap.
- State: active, paused, needs attention, failed, deleted.
- Last run, last successful check, next due time.
- Creation/update/expiry timestamps.

### 10.2 Scheduler

The scheduler should:

- Use database-coordinated claiming so multiple scheduler processes do not
  duplicate a due run.
- Enqueue jobs to the existing durable worker rather than executing product
  tasks inside a scheduler loop.
- Respect source/provider rate limits and per-watch minimum intervals.
- Spread due work where appropriate to avoid request bursts.
- Handle time zones and daylight-saving transitions explicitly.
- Back off after transient errors.
- Pause watches when credentials are revoked or the source is disallowed.
- Apply budget checks before paid calls.
- Avoid unbounded catch-up storms after downtime.

### 10.3 Change detection

Use reliable provider validators where available. Otherwise, normalize content
and compare stable hashes or structured fields. Preserve the previous and new
source references necessary to explain the change.

Distinguish:

- Checked; no change.
- Checked; change detected.
- Checked; source returned no relevant data.
- Check failed.
- Rate limited.
- Access blocked or robots disallowed.
- Connection needs reauthorization.
- Source disappeared or changed format.

A failed or skipped run is never a no-change result.

### 10.4 Alerts

An alert should explain:

- Why it triggered.
- What changed.
- When the source was checked.
- Which source evidence supports the alert.
- Whether the summary is generated or directly reported.
- How certain or incomplete the evidence is.
- What the user can do next.

Initial delivery should be in-app. Email, push, chat, or webhook delivery must
use an explicit connected destination and approval policy.

### 10.5 Forecasting

Forecasting is a future capability, not a decorative chart. Only implement it
when a suitable time series exists.

For every forecast:

- State the target variable and horizon.
- Show the historical input window and source.
- Show uncertainty intervals and model version.
- Compare against a simple baseline.
- Track forecast error as new observations arrive.
- Distinguish a forecast from a fact or alert.
- Avoid forecasts for sparse, biased, or unstable data unless the user
  explicitly requests exploratory output.

Do not use a model-generated narrative to disguise lack of measured historical
data.

---

## 11. Conversations, planning, and orchestration

### 11.1 Conversations

Persist conversations and messages with:

- Owner and workspace.
- Ordered message IDs.
- Role and content representation.
- Provider/model metadata for generated content.
- Tool calls, approval requests, sources, and artifacts as structured
  associations.
- Created, edited, and deleted times.
- Retention/data classification.

Provide export and deletion. Keep message history separate from permanent
knowledge indexing unless the user explicitly saves/indexes it.

### 11.2 Complex-task plan

For multi-step work, construct a structured plan containing:

- User goal and acceptance criteria.
- Steps and dependency relationships.
- Required sources, capabilities, and connected accounts.
- Risk and external effects.
- Estimated time and cost only when supportable.
- Approval checkpoints.
- Error/partial completion behavior.
- Cancellation and recovery rules.

Plans should be shown when scope, risk, or cost warrants it. Simple requests
should not be slowed by unnecessary planning ceremony.

The planner may propose actions but cannot:

- Grant itself permissions.
- Create or discover credentials.
- Expand source access without approval.
- Modify an approval after it was given.
- Treat external text as an instruction to call tools.

### 11.3 Workflow representation

Represent workflows as versioned, validated data—not arbitrary generated
code. Workflow steps must reference registered capabilities and declare
inputs, outputs, dependencies, failure handling, limits, and approval needs.

### 11.4 Partial completion

A multi-step workflow must report:

- Completed steps.
- Failed steps and their causes.
- Steps skipped because dependencies failed.
- Steps that need approval.
- Results/artifacts produced so far.
- Whether a safe retry is available.

Never report full success when only some steps completed.

---

## 12. Model and multimodal capabilities

### 12.1 Model gateway

Keep a single audited model-provider egress point. Extend it only with tested
provider adapters.

Each provider/model capability should report:

- Provider and exact model/version where available.
- Supported input/output modalities.
- Context and output limits.
- Whether streaming is supported.
- Credential source and configuration status, never the secret itself.
- Region/privacy notes when documented.
- Rate-limit behavior.
- Actual usage and cost where reported.
- Price availability and timestamp/source of price data.

### 12.2 Routing

Let users request:

- A specific model/provider.
- Fast response.
- Lower-cost response.
- Local/private processing.
- Stronger reasoning or synthesis.
- A modality-specific capability.

Routing policy should consider task capability, user choice, privacy setting,
budget, provider availability, context capacity, and current limits. Do not
silently move user data to a provider with a different privacy or cost
characteristic.

Every answer should retain the actual route used. If a fallback was applied,
state it.

### 12.3 Provider integration requirements

Before adding a provider:

1. Consult current official provider documentation.
2. Confirm legal/terms and supported access method.
3. Implement secure authentication and scopes.
4. Bound request, response, and media sizes.
5. Validate response shape and token/usage fields.
6. Redact provider errors and secrets.
7. Define timeout, cancellation, and retry semantics.
8. Never retry ambiguous paid/side-effecting operations blindly.
9. Add provider capability metadata and health state.
10. Record actual usage and pricing source/version or mark price unknown.
11. Add a user-visible configuration and disconnect path.

Potential classes include text models, embeddings, image generation, speech,
transcription, translation, video generation/processing, and local models.
Support only capabilities that can be used lawfully and reliably.

### 12.4 Content/artifact service

Artifacts should have:

- Owner/workspace and creating job/conversation.
- Media type, size, checksum, and storage reference.
- Provider/model and generation metadata.
- Source/input references and transformation history.
- Created time, retention rule, and deletion state.
- User-visible generated/edited labels.

Do not put large media blobs into job JSON or event metadata. Use bounded
storage and safe preview/download handling.

### 12.5 Model evaluation

Maintain a versioned evaluation set for actual product workflows:

- Retrieval relevance and workspace isolation.
- Citation support and factual grounding.
- Research coverage and source handling.
- Summary fidelity.
- Translation quality if supported.
- Classification/monitoring usefulness.
- Media output quality where measurable.
- Latency, error rate, token use, and cost.

Record model and prompt versions. Do not make claims that are not supported by
repeatable evaluation.

---

## 13. Dashboard and live workstation visualization

### 13.1 Product areas

Build real, connected sections in a deliberate sequence:

1. **Home / Activity** — current work, recent results, pending approvals,
   system health, and active background workflows.
2. **Workspaces** — selected context, conversations, knowledge, source
   inventory, and artifacts.
3. **Ask / Learn** — persistent interactive conversations with citations and
   task progress.
4. **Research** — source selection, research runs, evidence, comparisons, and
   saved reports.
5. **Monitoring** — watches, schedules, freshness, alerts, and run history.
6. **Connections** — provider accounts, granted scopes, health, revoke, and
   reconnect.
7. **Tasks** — durable jobs, workflow runs, progress, errors, pause, cancel,
   retry.
8. **Models & Usage** — actual configured models, routing, measured usage,
   cost, and unknown prices.
9. **Artifacts** — generated content, previews, downloads, provenance, and
   retention.
10. **Settings** — permissions, autonomy, budgets, alerts, privacy, and
    retention.

Do not show an item as navigable/available until its actual page exists. A
smaller, cohesive set of working screens is better than a mock product shell.

### 13.2 Live system graph

The graph can be a signature visualization of real workstation activity.
Possible node types:

- Workspace.
- Conversation.
- Source, document, or knowledge chunk.
- Watch and watch run.
- Job/workflow and worker.
- Agent task and specialist role.
- Model/provider call.
- Approval request.
- Notification/alert.
- Generated artifact.

### 13.3 Multi-agent workbench

For a multi-agent task, provide a dedicated, inspectable execution view rather
than only animating the graph. It should show:

- The user’s requested outcome and the coordinator’s current phase.
- The plan/task dependency tree.
- Which specialist tasks are queued, running, waiting, complete, failed, or
  cancelled.
- Each task’s bounded objective and allowed capability scope.
- Sources and artifacts produced by that task.
- Real progress events, not invented percentages.
- Provider/model and measured cost/usage where available.
- The reason for a retry, wait, failure, or approval request.
- Controls to cancel a child, cancel the parent, pause background work, or
  open the approval prompt.
- The final synthesis and which task/evidence records contributed to it.

Do not expose private chain-of-thought, hidden prompts, credentials, or
provider-internal reasoning traces. Explain operational actions and cite
evidence instead. If a specialist has no new event to report, show its actual
status and last update rather than an artificial typing indicator.

### 13.4 Graph behavior and integrity

Requirements:

- Every node maps to a real record or clearly defined persisted event.
- Every edge has documented provenance.
- Agent nodes identify their real parent workflow, assigned task, capability
  scope, status, and bounded objective; do not expose hidden chain-of-thought
  or private provider internals.
- The graph updates from real events/snapshots.
- Animation indicates actual status/progress, not imaginary neural firing.
- Hover shows what the node is doing, source/account, real status, and
  relevant time.
- Selection opens a detail view or related work item.
- Filters by workspace, time, status, source, and task.
- Caps/truncation and stale snapshots are visible.
- Sensitive prompts, credentials, and full documents are not leaked in
  labels or hover details.
- Provide a conventional activity list for accessibility and users who do not
  want graph navigation.

### 13.5 Visual and interaction quality

- Maintain a polished, futuristic design without sacrificing legibility.
- Make state, source, and permission labels readable at a glance.
- Support keyboard navigation and screen readers.
- Distinguish loading, empty, stale, unauthorized, unavailable, and failed
  states.
- Avoid high-frequency polling that creates unnecessary load.
- Prefer event-driven updates with durable resume and bounded fallback
  polling.
- Respect reduced-motion preferences.
- Make mobile/small-window behavior usable before claiming responsive support.

---

## 14. Client ecosystem

### 14.1 Shared core

The persistent SYNAPSE core owns user/workspace state, knowledge, jobs,
connections, approvals, workflows, artifacts, and events. CLI, dashboard, MCP,
IDE, and future clients are surfaces over the same capabilities and policy
engine.

### 14.2 API design

- Keep versioned contracts.
- Enforce auth and workspace scoping on every protected endpoint.
- Use consistent structured error responses with stable reason codes.
- Add pagination for growing collections.
- Add idempotency for job/workflow creation.
- Support resumable event streaming with event IDs.
- Keep request/response sizes bounded.
- Avoid access tokens in URLs.
- Keep backend Pydantic contracts and TypeScript types synchronized.
- Document API compatibility and migration behavior.

### 14.3 CLI

The CLI remains useful for:

- Starting/stopping/status/doctor.
- Submitting, listing, inspecting, cancelling, and resuming jobs.
- Asking and researching.
- Managing workspaces, sources, watches, connections, and approvals.
- Machine-readable output for automation.

CLI commands must remain thin clients; do not duplicate business policy in
command handlers.

### 14.4 MCP

MCP tools should expose only registered bounded capabilities with clear
schemas, descriptions, limits, and honest errors. They must use the same
authorization and workspace rules as every other surface.

MCP must not expose:

- Raw passwords or credentials.
- Generic unrestricted shell access.
- Generic unrestricted filesystem traversal.
- Unbounded internet access.
- The ability to bypass a user approval.

### 14.5 IDE clients

An IDE is one client, not the core identity of the product. It should let the
user invoke SYNAPSE’s general workstation capabilities from an editor, inspect
results, citations, jobs, and approvals, and connect to the persistent local
core.

Do not silently upload editor buffers or an entire repository. Show the user
what context will be sent and apply explicit workspace inclusion rules.

### 14.6 Future clients

Desktop/mobile clients may follow after core workflows justify them. They
should not fork user state or create independent permission systems.

---

## 15. Security and privacy specification

### 15.1 Threat model

Treat as untrusted:

- Search results, public websites, feeds, and documents.
- Model-generated text and tool arguments.
- MCP clients and IDE clients.
- Provider error messages and returned metadata.
- Imported workflow definitions.
- Local network processes, even in local mode.

Threats include:

- Prompt injection in source content.
- SSRF, unsafe redirects, DNS rebinding, and internal network access.
- Credential theft or accidental logging.
- Cross-workspace data leakage.
- Unauthorized tool execution.
- Forged/replayed approvals.
- Duplicate paid or external side effects after worker failure.
- Unbounded scheduled work and resource exhaustion.
- Malformed or hostile media.
- Account revocation/expiry races.
- Compromised providers or content sources.

### 15.2 Essential protections

- Keep outbound web access behind the existing safe networking path.
- Revalidate each redirect and bound response size and runtime.
- Respect robots policies and provider terms.
- Enforce workspace and account authorization server-side.
- Use secure credential references and narrow access to secrets.
- Redact secrets and sensitive content from logs/errors.
- Bound concurrency, rate, retries, media size, and task runtime.
- Record external side effects and approvals.
- Use idempotency/checkpoints to prevent duplicate operations.
- Protect dashboard endpoints against cross-origin and token leakage.
- Add security headers and a strict Content Security Policy where compatible.
- Provide tested database backup/restore and data deletion processes.
- Scan dependencies and container images in CI.
- Reassess the threat model before supporting remote/multi-user deployment.

### 15.3 Prompt and tool security

- System policies are controlled by application code/configuration, not
  retrieved source text.
- External content is explicitly marked as untrusted.
- Tool use is restricted to registered capabilities and validated schemas.
- A model’s request to perform an action is not itself user approval.
- Each capability call rechecks the policy engine and current connection.
- Secrets are not included in prompts.
- Treat model output as untrusted and validate it before using it as structured
  data or an action argument.

### 15.4 Data lifecycle

Every data category should have a documented:

- Purpose.
- Owner/workspace.
- Classification.
- Storage location.
- Retention period or user-selected retention policy.
- Export mechanism.
- Deletion path.
- Provider-sharing policy.
- Backup/restore behavior.

Deleting a source should also remove or invalidate derived chunks, embeddings,
cached excerpts, and search index records consistent with retention and backup
policies. Explain unavoidable backup-retention delays.

---

## 16. Reliability, operations, and observability

### 16.1 Runtime health

The workstation should expose real health for:

- API process.
- Worker(s).
- Database and migrations.
- Scheduler.
- Provider connections.
- Queue depth/age.
- Event delivery.
- Source monitor freshness.
- Storage availability.

Health must distinguish “process alive” from “capability ready.” A running API
with a disconnected provider is not full provider health.

### 16.2 Structured observability

Collect redacted structured logs/metrics for:

- Job duration, queue wait, attempts, and terminal state.
- Provider/model latency, errors, limits, and measured usage.
- Retrieval latency and evaluation results.
- Watch freshness and success/failure states.
- Alert delivery.
- Approval requests and decisions.
- Database errors and worker heartbeats.
- Actual and unknown price/cost status.

Do not use user content, secrets, or high-cardinality prompt text as metric
labels. Allow the local owner to control diagnostic export.

### 16.3 Backup, recovery, and upgrades

Design:

- PostgreSQL backup and verified restore.
- Knowledge/document export.
- Migration rollback or forward-recovery policy.
- Safe application upgrade with version reporting.
- Recovery from interrupted updates.
- Clear data format/version compatibility.
- Local database and asset health diagnostics.

Self-update must be an explicit, signed, staged, recoverable operation. Do not
let an autonomous model rewrite production files or deploy unreviewed code.

### 16.4 Windows/local-first quality

The project is used in a Windows environment. Preserve and improve:

- Windows-safe paths and process supervision.
- Credential Manager integration where feasible.
- Robust detached service lifecycle and clean shutdown.
- UTF-8 output/log behavior.
- Docker/PostgreSQL diagnostics.
- Upgrade and recovery flows.
- Clear local-only/network bind behavior.

Do not assume Unix process semantics in shared code.

---

## 17. Cost, quotas, and compute budgets

Paid and resource-consuming capabilities need explicit controls.

### 17.1 Budget model

Support global, workspace, workflow, and per-run budgets where useful:

- Maximum model cost per run/day/month.
- Maximum requests per provider and interval.
- Maximum generated media size/duration.
- Maximum task runtime and concurrency.
- Maximum source count and fetched bytes.
- Storage usage thresholds.

### 17.2 Cost honesty

- Use provider-reported token/usage quantities.
- Keep price metadata versioned with source and effective time.
- Store exact money using decimal/numeric representations.
- Unknown price remains unknown; do not render as free.
- Estimates must be labeled estimates, not charges.
- Never change provider/model class silently to fit a budget.
- Ask for approval before exceeding the user’s configured limit.

### 17.3 Local compute

If local models or media tools are added:

- Detect actual available hardware and runtime.
- Report unavailable capabilities honestly.
- Let the user opt in to downloads and disk usage.
- Bound CPU/GPU/memory use and support cancellation where possible.
- Do not imply that a GPU is doing work unless the runtime reports it.

---

## 18. Data model direction

The schema must evolve incrementally with migrations. This list is conceptual,
not an instruction to create all tables now.

| Entity | Purpose |
|---|---|
| `users` or local owner | Identity and ownership boundary |
| `workspaces` | User context, policy, source, and data boundary |
| `conversations` | Persistent interaction threads |
| `messages` | User/assistant/tool/approval messages |
| `jobs` | Durable execution units |
| `job_events` | Lifecycle/progress event history |
| `documents` | Ingested source records |
| `chunks` | Searchable provenance-bearing spans |
| `sources` | Source catalog and connection references |
| `watches` | Recurring user-defined monitoring |
| `watch_runs` | Durable monitor execution and check result |
| `workflow_definitions` | Versioned validated workflow configuration |
| `workflow_runs` | Workflow execution history |
| `integrations` | Provider metadata and capability catalog |
| `connections` | User account state and granted scopes |
| `secret_references` | Opaque pointers to secure credential storage |
| `capabilities` | Registered bounded action descriptions |
| `approval_requests` | Exact action approval and result |
| `audit_events` | Security and external side-effect record |
| `artifacts` | Generated files/media metadata and storage reference |
| `notifications` | In-app alert/delivery state |
| `model_usage` | Provider/model usage and cost ledger |

For each entity, specify:

- Ownership and workspace constraints.
- Foreign keys and deletion behavior.
- Unique/idempotency constraints.
- Indexes based on actual queries.
- Retention and export policy.
- PII/secrets classification.
- Migration and backfill behavior.

Do not build an abstract “universal entity framework” unless real query and
domain requirements justify it.

---

## 19. API and event contracts

### 19.1 API rules

- Use typed request/response models.
- Keep transport routes thin.
- Validate all user-controlled inputs at the boundary and domain service.
- Use stable error reason codes with human-readable detail.
- Paginate growing collections.
- Bound all payloads and timeouts.
- Use idempotency keys for submissions that may be retried.
- Require auth on protected routes.
- Enforce workspace/ownership at each query.
- Keep token material out of URLs.
- Provide compatibility/versioning policy.

### 19.2 Event rules

Events should allow the dashboard, CLI, MCP, and future clients to resume
progress after disconnecting. Use monotonically increasing IDs or an
equivalent cursor. Keep event payloads compact and redacted. Do not rely on
client-side timers to invent progress.

Potential event families:

- `job_queued`, `job_started`, `job_progress`, `job_completed`,
  `job_failed`, `job_cancelled`, `job_retried`.
- `agent_task_created`, `agent_task_started`, `agent_task_progress`,
  `agent_task_completed`, `agent_task_failed`, `agent_task_cancelled`.
- `workflow_plan_created`, `workflow_plan_updated`,
  `workflow_waiting_approval`, `workflow_paused`, `workflow_resumed`.
- `workflow_started`, `workflow_step_started`, `workflow_step_completed`,
  `workflow_completed`.
- `source_check_started`, `source_check_completed`, `source_check_failed`,
  `source_change_detected`.
- `approval_requested`, `approval_granted`, `approval_denied`,
  `approval_expired`.
- `connection_created`, `connection_expired`, `connection_revoked`.
- `artifact_created`, `artifact_ready`, `artifact_failed`.

The exact catalog should be versioned and reflect only implemented events.
Avoid duplicating events for the same state transition; choose one canonical
event contract and have clients derive their views from it.

---

## 20. Testing and verification strategy

Testing is part of delivery, but obey the user’s instructions for each task. If
the user says to skip tests, do not run them; state that no tests were run.

For features where validation is requested or appropriate:

- Unit-test pure policy, parsing, scheduling, and ranking logic.
- Integration-test migrations, ownership constraints, transactions, and
  worker recovery using the supported real database.
- Test unauthorized, cross-workspace, expired-credential, denied-permission,
  over-budget, timeout, rate-limit, and partial-failure paths.
- Test cancellation and restart behavior.
- Test idempotency and duplicate-side-effect protection.
- Test provider wire behavior using sanitized fixtures/official test
  environments where permitted.
- Test UI loading, empty, stale, failure, and approval states.
- Run type-check/lint/build for changed surfaces as appropriate.
- Add end-to-end flows for critical user journeys.
- Record only actual validation results.

Do not use production-mode fake responses. Test doubles at external boundaries
are acceptable if they remain test-only and do not create success-shaped
fallbacks in runtime behavior.

---

## 21. Phased roadmap with gates

Phases are ordered by dependency. Each phase should end in one working
user-visible slice with docs and acceptance criteria. Reorder only when a
specific dependency or user need justifies it.

### Phase 0 — Baseline and truthful project state

**Goal:** Make the declared project state match current code.

Work:

1. Inventory routes, tasks, CLI commands, dashboard behavior, models,
   migrations, connectors, MCP tools, and deployment assumptions.
2. Mark each capability as implemented, partial, experimental, or planned.
3. Update README and BRIEF without erasing architecture rationale.
4. Record current system boundaries and known limitations.
5. Preserve dirty work and resolve in-progress feature failures in the relevant
   implementation task.
6. Establish CI for existing checks when the repository owner approves.

Gate:

- A new contributor can understand what works now without mistaking a roadmap
  item for a shipped feature.
- Existing user changes remain intact.

### Phase 1 — Workspace-aware knowledge

**Goal:** Give SYNAPSE reliable context boundaries.

Work:

1. Define workspace ownership and lifecycle.
2. Add scope to documents/chunks and relevant job/API contracts.
3. Enforce scope in every ingestion, retrieval, graph, and listing query.
4. Decide how to handle existing unscoped data.
5. Add workspace create/list/select behavior across API and clients.
6. Add knowledge inventory, status, re-index, remove, and export controls.
7. Add embedding model/version metadata and a re-embedding policy.
8. Evaluate retrieval quality and isolation.

Gate:

- A user can ingest into a workspace, ask a question in it, inspect citations,
  and remove/re-index sources.
- Private data from another workspace cannot appear in retrieval or API
  output.

### Phase 2 — Secure account connection framework

**Goal:** Connect supported services without collecting passwords or exposing
secrets.

Work:

1. Define provider/integration metadata and capability scopes.
2. Implement secure credential references and the local secret-store adapter.
3. Add a connection state machine and token expiry/revocation.
4. Implement one official authorization flow end to end.
5. Add connection management UI and sanitized audit events.
6. Add one read-only account capability behind the shared policy engine.
7. Make scopes and usage visible; provide disconnect/revoke.

Gate:

- The user can authorize one provider through its official flow, inspect
  granted access, perform one bounded read, and revoke the connection.
- No password or secret is stored in normal application records or logs.

### Phase 3 — Monitoring and in-app alerts

**Goal:** Keep selected sources fresh and surface meaningful changes.

Work:

1. Define watch and watch-run records.
2. Add coordinated scheduler that enqueues durable jobs.
3. Implement one source type (for example RSS/Atom or an official API).
4. Add change detection and source freshness.
5. Distinguish no-change, failed-check, rate-limited, and blocked states.
6. Add pause/resume/edit/delete and in-app alert history.
7. Enforce request, concurrency, storage, and budget limits.

Gate:

- A user can create a watch, inspect real runs and freshness, pause it, and
  receive a source-backed alert for a real change.
- A failed poll never appears as “nothing changed.”

### Phase 4 — Workstation conversation and plan experience

**Goal:** Make the dashboard a primary place to do useful work.

Work:

1. Add persistent conversations and messages.
2. Add interactive streaming or resumable job progress.
3. Add workspace/context selection.
4. Add citations, errors, provider metadata, and result inspection.
5. Add simple structured plans for complex tasks.
6. Add explicit approval prompts for consequential steps.
7. Add export, delete, retention, and accessibility behavior.

Gate:

- A user can complete and resume a research/knowledge workflow in one
  conversation, inspect sources, and see approval needs without losing task
  history.

### Phase 4.5 — Measured multi-agent coordination

**Goal:** Coordinate bounded specialist tasks for complex requests, while
keeping one accountable user-facing coordinator.

Work:

1. Define parent workflow, child agent-task, dependency, and structured result
   contracts.
2. Register a small set of useful specialist roles backed by existing
   capabilities (for example source discovery, evidence extraction, comparison,
   and verification).
3. Use the existing durable job/worker system where practical; do not create a
   parallel queue or an unconstrained recursive agent loop.
4. Enforce per-task capabilities, workspace scope, runtime, call count,
   concurrency, and cost limits.
5. Add parent/child events and a real agent activity view.
6. Make cancellation cascade to children and make partial completion explicit.
7. Compare multi-agent results against a single-agent baseline for quality,
   coverage, latency, and cost.
8. Keep approvals outside the agent team: no agent may approve another agent’s
   consequential action.

Gate:

- One complex research task can delegate independent bounded subtasks, gather
  their evidence-linked results, identify disagreements, and return a coherent
  answer with a visible task tree.
- Every child task can be inspected, cancelled, and attributed to its actual
  provider/capabilities.
- A failed, cancelled, over-budget, or approval-blocked branch is never shown
  as completed.
- The multi-agent path demonstrates a measured benefit over the simpler
  single-agent path for its target workflow.

### Phase 5 — Bounded action orchestration

**Goal:** Let SYNAPSE prepare and perform authorized actions safely.

Work:

1. Implement capability declarations and policy checks.
2. Implement durable approvals bound to exact action inputs.
3. Add autonomy modes and narrow allowlists.
4. Add budgets and approval expiry.
5. Add audit records and idempotent side-effect checkpoints.
6. Add one low-risk external action end to end.
7. Add global pause/stop and reviewable activity history.

Gate:

- The system cannot exceed the user’s grant.
- A consequential action is previewed, approved, executed once, and recorded.
- A crash/retry cannot silently duplicate it.

### Phase 6 — Multimodal generation and artifact management

**Goal:** Deliver real media capabilities through supported providers.

Work:

1. Implement artifact metadata and storage references.
2. Add one real media provider capability.
3. Enforce input/output size, duration, cost, and runtime limits.
4. Show generation progress and failure.
5. Provide preview/download/delete and provenance.
6. Ask before external publication or sharing.

Gate:

- User can request, retrieve, inspect, and delete a real generated artifact.
- Provider/model, cost, and generation status are accurate.

### Phase 7 — Client ecosystem and resilience

**Goal:** Use the same workstation from all needed surfaces.

Work:

1. Stabilize API contracts and resumable event streams.
2. Complete MCP parity with policy and workspace boundaries.
3. Add IDE client for general workstation tasks.
4. Add backup/restore, upgrades, and local operations support.
5. Profile actual workload before proposing service decomposition.
6. Consider hosted/multi-user operation only with a revised threat model.

Gate:

- CLI, dashboard, MCP, and IDE operate on the same core state and policies.
- The user can recover data and understand service health.

### Phase 8 — Evaluation and adaptive optimization

**Goal:** Improve quality using measured outcomes, not uncontrolled self-change.

Work:

1. Build stable evaluations for retrieval, research, classification, and
   routing.
2. Track actual latency, cost, source coverage, error rate, and user feedback.
3. Propose routing/configuration improvements with clear evidence.
4. Keep production policy changes reviewable and reversible.
5. Use canary or shadow evaluation before changing defaults.

Gate:

- A quality/cost improvement is supported by repeatable data and can be rolled
  back.
- SYNAPSE does not rewrite or deploy its own code autonomously.

---

## 22. Feature acceptance checklist

Before calling a feature complete, confirm:

### Product

- Does it solve a user outcome rather than add surface area for its own sake?
- Is the user journey clear, including failure, pause, and cancellation?
- Does it reduce unnecessary user chores?
- Does it fit the non-coding-agent product identity?

### Capability

- Is the capability real and connected to its real provider/source?
- Are input, output, runtime, rate, and cost bounded?
- Are unsupported states explicit?
- Is its status based on real work?
- For multi-agent work: are parent/child ownership, dependencies, limits,
  permissions, cancellation, and partial completion explicit?

### Permission and privacy

- Is the data source authorized?
- Are credentials protected?
- Is the required permission explicit and narrowly scoped?
- Are approval and revocation behavior clear?
- Are data retention and deletion considered?

### Reliability

- Is the operation durable when needed?
- Is it idempotent or protected from duplicate effects?
- Are partial completion and provider ambiguity handled?
- Can the user inspect errors and recover?

### Evidence

- Can the user inspect the sources/results behind an answer?
- Are unknowns and uncertainty preserved?
- Are provider, model, and actual usage visible where relevant?

### Engineering

- Are contracts and migrations updated?
- Are failure paths covered?
- Are docs accurate?
- Was relevant validation performed—or explicitly skipped at the user’s
  request?

---

## 23. First recommended implementation slice

Start with **workspace-aware knowledge and retrieval**, not broad account
automation.

Implement this complete path:

```text
Create/select workspace
  -> ingest one permitted source into that workspace
  -> show indexing state and provenance
  -> ask a question in that workspace
  -> retrieve only its knowledge
  -> return traceable citations and retrieval mode
  -> inspect, refresh, export, or remove the source
```

Why this first:

- It improves an existing core capability.
- It creates the isolation boundary required for account data and monitoring.
- It strengthens the product’s trust and evidence model.
- It supplies real workspace activity for the dashboard.
- It does not require granting the system broad external action permissions.

Then add one secure account connection and one monitor source. Do not start by
promising access to every service or scraping the entire web.

---

## 24. Long-term definition of “futuristic”

SYNAPSE ONE should feel futuristic because it:

- Understands the user’s outcome rather than requiring technical commands.
- Remembers selected context without silently over-collecting.
- Coordinates multiple real services and model capabilities.
- Uses a small team of bounded specialists when that measurably improves a
  complex task, and a simpler single path when it does not.
- Runs useful background workflows while remaining visible and controllable.
- Keeps its knowledge fresh from permitted sources.
- Asks concise, meaningful permission questions.
- Produces work with source evidence, artifacts, and action history.
- Lets the same personal core follow the user across supported clients.
- Can explain what it is doing, why, and what it needs next.
- Shows the real workflow tree and meaningful specialist activity without
  pretending to expose private model reasoning.

It should not feel futuristic merely because it has:

- A large codebase.
- Many model/provider names.
- A moving graph with no real operational meaning.
- A claim of “full internet access.”
- Unreviewed self-modification.
- Hidden account access.
- Unbounded background activity.

**The product should be ambitious in capability and conservative in authority.**
That is how SYNAPSE becomes powerful enough to be useful and trustworthy enough
to remain always available.

---

## 25. Related project documentation

Before changing architecture or product behavior, consult and update the
relevant current documents:

- [Project README](../README.md)
- [Current project brief](./BRIEF.md)
- [Architecture decisions](./decisions/)

When an implementation changes a durable architectural trade-off, add or amend
an architecture decision record rather than allowing code and product
assumptions to drift.
