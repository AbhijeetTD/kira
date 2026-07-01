# KIRA 🔍 — Kubernetes Intelligent Response Agent

> **AI-powered Kubernetes incident response — from alert to resolution in under 2 minutes.**

KIRA automatically investigates your cluster when an alert fires, dispatches 4 specialist AI agents for parallel analysis, synthesises their findings into a definitive root cause with an exact remediation command, executes the fix, and validates recovery — all streamed live to a real-time dashboard. Runs **100% locally** using Ollama — no cloud API keys required.

![Python](https://img.shields.io/badge/Python-3.11-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green) ![Ollama](https://img.shields.io/badge/LLM-Ollama-orange) ![License](https://img.shields.io/badge/License-MIT-lightgrey)

**Key highlights:**

- 🤖 **4 specialist agents** — SRE, App, Security, and Cost analyse in parallel
- 🧠 **LLM Decision Engine** — single unified decision-maker for root cause + remediation
- 🎫 **Jira lifecycle** — auto-creates tickets, comments at each stage, closes on resolution
- 💬 **AI chat** — ask anything about an open incident with full context injection
- 📄 **One-click postmortem** — AI-generated post-incident reports
- ⚡ **Auto-remediation** — high-confidence fixes execute without human approval
- 🦙 **Ollama-powered** — runs fully locally, no cloud LLM required

---

## Table of Contents

- [Quick Start](#-quick-start)
- [Configuration](#️-configuration)
- [Architecture](#architecture)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Running](#running)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Demo Walkthrough](#demo-walkthrough)
- [Incident Lifecycle](#incident-lifecycle)
- [Remediation Playbooks](#remediation-playbooks)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [License](#license)

---

## ⚡ Quick Start

### Prerequisites

| Tool | Install |
|------|---------|
| Python 3.11+ | `brew install python@3.11` |
| Ollama | `brew install ollama` |
| kubectl | `brew install kubectl` |
| kind | `brew install kind` (or use any running cluster) |

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/kira.git
cd kira
```

### 2. Start Ollama + pull a model

```bash
# Start Ollama (keep this running)
ollama serve

# Pull the default model (~2 GB, one-time download)
ollama pull llama3.2
```

### 3. Configure your environment

```bash
cp .env.example .env
```

Open `.env` and set **2 required values**:

```bash
# Find your context name with: kubectl config get-contexts
KUBE_CONTEXT=kind-dev-cluster    # ← your cluster context

# Namespace KIRA will monitor and remediate
DEFAULT_NAMESPACE=default
```

Everything else has working defaults. See [Configuration](#️-configuration) for Jira, Teams, and all other options.

### 4. Run KIRA

```bash
chmod +x run.sh
./run.sh
```

### 5. Open the dashboard

| URL | Description |
|-----|-------------|
| `http://localhost:8000` | 🔍 Live dashboard |
| `http://localhost:8000/api/docs` | 📖 Swagger / OpenAPI |
| `http://localhost:8000/health` | ❤️ Health check |

### 6. Trigger a test scan

```bash
curl -X POST http://localhost:8000/scan
```

---

## ⚙️ Configuration

All configuration is done in `.env`. Copy the template and fill in your values:

```bash
cp .env.example .env
```

### 1 — Ollama (Required)

```bash
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=llama3.2
```

**Choosing a model** — all fit in 16 GB RAM:

| Model | Size | Quality | Pull command |
|-------|------|---------|--------------|
| `llama3.2` *(default)* | ~2 GB | ⭐⭐⭐ Fast | `ollama pull llama3.2` |
| `mistral` | ~4 GB | ⭐⭐⭐⭐ Balanced | `ollama pull mistral` |
| `qwen2.5:7b` | ~4 GB | ⭐⭐⭐⭐ Smart | `ollama pull qwen2.5:7b` |
| `llama3.1:8b` | ~5 GB | ⭐⭐⭐⭐⭐ Best quality | `ollama pull llama3.1:8b` |

To switch: edit `OLLAMA_MODEL` in `.env`, pull the model, restart KIRA.

---

### 2 — Kubernetes (Required)

```bash
# Find your context name:
kubectl config get-contexts

KUBE_CONTEXT=kind-dev-cluster    # paste your context name here
DEFAULT_NAMESPACE=default        # namespace KIRA watches and remediates
```

Common context names by tool:

| Tool | Typical context name |
|------|---------------------|
| kind | `kind-<cluster-name>` e.g. `kind-dev-cluster` |
| minikube | `minikube` |
| Docker Desktop | `docker-desktop` |
| Rancher Desktop | `rancher-desktop` |

---

### 3 — Remediation Behaviour (Optional)

```bash
# Auto-approve remediation if LLM confidence >= this percentage (0–100)
# Below this threshold → KIRA asks for human approval in the dashboard
AUTO_APPROVE_THRESHOLD=90

# true  = ask for approval when confidence < AUTO_APPROVE_THRESHOLD
# false = always auto-fix, never ask (fully autonomous mode)
APPROVAL_MODE=true
```

---

### 4 — Jira Integration (Optional)

Automatically creates and closes Jira tickets for every incident.

**Step 1** — Enable:
```bash
JIRA_ENABLED=true
```

**Step 2** — Fill in your details:
```bash
# Your Jira Cloud URL (not the issue URL, just the base)
JIRA_URL=https://your-company.atlassian.net

# Your Jira account email
JIRA_EMAIL=you@yourcompany.com

# Generate an API token at:
# https://id.atlassian.com/manage-profile/security/api-tokens
# → "Create API token" → copy the value
JIRA_API_TOKEN=your_token_here

# Project key — the uppercase letters shown in your Jira project URL
# e.g. https://company.atlassian.net/jira/software/projects/OPS → key is OPS
JIRA_PROJECT_KEY=KS

# Issue type (Task | Bug | Story | Incident)
JIRA_ISSUE_TYPE=Task
```

**Ticket lifecycle:**
```
Alert fires     → ticket created      [To Do]
RCA complete    → RCA comment added   [In Progress]
Fix executed    → command logged
Resolved        → ticket closed       [Done]
Failed          → failure comment     [stays open]
```

---

### 5 — Microsoft Teams (Optional)

Sends incident start, approval requests, and resolution summaries to a Teams channel.

**Step 1** — Get the webhook URL in Teams:
> Channel → `···` menu → **Connectors** → **Incoming Webhook** → **Create** → copy the URL

**Step 2** — Add to `.env`:
```bash
TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/webhookb2/...
```

---

## Architecture

KIRA implements a **closed-loop autonomous incident response** architecture — a multi-stage pipeline with event-driven orchestration, parallel multi-agent reasoning, and self-healing feedback loops, all observable in real time via Server-Sent Events.

### System Overview

```
                            ┌─────────────────────────────────────────────────────────┐
                            │                   KIRA Control Plane                    │
  ┌──────────────────┐      │                                                         │
  │  Ingestion Layer │      │  ┌───────────┐   ┌────────────────┐   ┌─────────────┐  │
  │                  │      │  │ Evidence   │   │  Multi-Agent   │   │  Decision   │  │
  │  Webhook API     │─────▶│  │ Collector  │──▶│  War Room      │──▶│  Engine     │  │
  │  Grafana Hook    │      │  │            │   │                │   │  (Ollama)   │  │
  │  Scan Endpoint   │      │  │  8 probes  │   │  4 specialist  │   │             │  │
  │  Crash Monitor   │      │  │  K8s API + │   │  agents (SRE,  │   │  Synthesis  │  │
  │  (LLM-driven)   │      │  │  kubectl   │   │  App, Security │   │  + RCA +    │  │
  └──────────────────┘      │  │  + struct  │   │  Cost) — async │   │  Command    │  │
                            │  └───────────┘   └────────────────┘   └──────┬──────┘  │
                            │                                              │          │
                            │  ┌───────────┐   ┌────────────────┐   ┌─────▼───────┐  │
                            │  │ Outcome   │◀──│  Validation    │◀──│  Execution  │  │
                            │  │ Tracker   │   │  Loop          │   │  Engine     │  │
                            │  │           │   │                │   │             │  │
                            │  │ Feedback  │   │  Health poll   │   │  Approval   │  │
                            │  │ memory +  │   │  4s × 90s      │   │  gate +     │  │
                            │  │ Jira close│   │  Retry (max 2) │   │  kubectl    │  │
                            │  └───────────┘   └────────────────┘   └─────────────┘  │
                            │                                                         │
                            │  SSE Event Bus ════════════════════════════════▶ UI     │
                            └─────────────────────────────────────────────────────────┘
                                         │                  │
                            ┌────────────▼──┐  ┌────────────▼──┐
                            │  Jira Cloud   │  │  MS Teams     │
                            │  (lifecycle)  │  │  (optional)   │
                            └───────────────┘  └───────────────┘
```

### Alert Ingestion

| Source | Endpoint | Parser |
|---|---|---|
| **Grafana Alertmanager** | `POST /webhook/grafana` | Extracts service from pod/deployment/statefulset labels, maps severity, skips internal alerts |
| **OpsGenie / JSM** | `POST /webhook/grafana` | Parses OpsGenie tag format, processes `Create` actions only |
| **Manual / generic** | `POST /webhook/alert` | Direct `AlertPayload` JSON |
| **Infrastructure scan** | `POST /scan` | Scans all Deployments + StatefulSets across configured namespaces |

### Pipeline Stages

| Stage | Name | Behaviour |
|:---:|---|---|
| **0** | **Initialisation** | Registers incident, creates Jira ticket, optionally notifies Teams, establishes SSE channel |
| **1** | **Evidence Acquisition** | Executes 8 parallel-safe cluster probes: pod status, container logs, resource utilisation, rollout history, deployment spec, cluster events, cross-service correlation, and deployment metadata |
| **2** | **Multi-Agent Analysis** | Dispatches evidence to 4 domain-specialist LLM agents concurrently (SRE, App, Security, Cost); each returns structured JSON — findings, confidence score, cited evidence, flagged concerns |
| **3** | **Decision Synthesis** | Pre-analysis computes health vectors and evidence quality scores (0–100), checks outcome history for circular patterns, then feeds all agent opinions + evidence into a single LLM call → produces RCA, blast radius, confidence, and exact remediation command |
| **4** | **Approval Gate** | Confidence ≥ threshold → auto-approved · Below threshold → blocks for human approval in dashboard (or Teams actionable card) |
| **5** | **Remediation Execution** | Runs generated kubectl command through a safety validator (injection prevention, namespace enforcement, dangerous verb blocking); StatefulSet-aware — auto-deletes unhealthy pods post-patch |
| **6** | **Recovery Validation** | Polls pod health at 4s intervals for 90s with **progress-aware extension** — if the rollout is actively progressing, validation recognises the fix is working and avoids false-negative retries. On genuine failure: deep diagnostic analysis, AI recovery hypothesis, retry (max 2 attempts) |
| **7** | **Closure & Feedback** | Records outcome in feedback memory (prevents repeating failed remediations), transitions Jira to `Done`, sends Teams summary, emits `Incident Closed` |

### Design Principles

- **OODA Loop** — Observe → Orient → Decide → Act; each stage maps to one phase with full observability
- **Fan-out / fan-in** — specialist agents run in parallel, merged at the decision engine
- **Self-healing retry** — validation failures trigger re-investigation with fresh evidence
- **Fast-path short-circuit** — healthy clusters skip the LLM entirely
- **Feedback memory** — outcome tracker builds institutional knowledge, preventing circular remediation

<details>
<summary><strong>Expanded internal architecture (ASCII)</strong></summary>

```
Ingestion:
  POST /scan              — On-demand infrastructure scan
  POST /webhook/grafana   — Grafana Alertmanager webhook
  POST /webhook/alert     — Generic alert ingest
  Crash Monitor           — Background LLM-driven anomaly detector
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   FastAPI Orchestration Layer (Python 3.11)          │
│                                                                      │
│  ┌─ Stage 0: Initialisation ──────────────────────────────────────┐ │
│  │  • Jira ticket creation (To Do) + SSE channel init            │ │
│  │  • Optional: Teams webhook notification                       │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 1: Evidence Acquisition ────────────────────────────────┐ │
│  │  8 probes via K8s API + kubectl:                               │ │
│  │  Pod Status → Container Logs → Resource Utilisation            │ │
│  │  → Rollout History → Deployment Spec → Cluster Events          │ │
│  │  → Cross-Service Correlation → Structured Deployment Metadata  │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 2: Multi-Agent War Room (concurrent) ──────────────────┐ │
│  │  🔧 SRE Agent ─────┐                                          │ │
│  │  📱 App Agent ─────┤ fan-out: each agent receives full        │ │
│  │  🔒 Security Agent ┤ evidence corpus → structured JSON        │ │
│  │  💰 Cost Agent ────┘                                          │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 3: LLM Decision Engine ────────────────────────────────┐ │
│  │  Pre-analysis: health vector computation + evidence scoring    │ │
│  │  Fast-path:    all healthy → deterministic "none" (skip LLM)  │ │
│  │  Synthesis:    agent opinions + evidence + outcome history     │ │
│  │                → RCA + blast radius + confidence + command     │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 4–7: Execution & Feedback Loop ────────────────────────┐ │
│  │  Approval → Remediation → Validation → Outcome                │ │
│  │             kubectl exec   health poll   feedback memory       │ │
│  │                            retry (max 2)  Jira Done            │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  Auxiliary:                                                          │
│   • /incidents/{id}/chat       — Context-aware Q&A                  │
│   • /incidents/{id}/postmortem — AI-generated report                │
│   • Crash Monitor · Memory Monitor · Outcome Tracker                │
│                                                                      │
│  SSE Event Bus ═══════════════════════════════════════════▶ UI      │
└──────────────────────────────────────────────────────────────────────┘
```

</details>

---

## Features

### 🧠 LLM Decision Engine

A single LLM call (via Ollama) that synthesises all specialist agent opinions + raw cluster evidence into a definitive root cause and exact remediation command.

| Capability | Description |
|---|---|
| **Pre-analysis** | Computes structured health signals (`crash_loop`, `oom_killed`, `image_pull_error`, `not_ready`, etc.) before calling the LLM |
| **Fast-path guard** | Skips LLM entirely when all pods are Running/Ready with 0 restarts |
| **Outcome tracking** | Records success/failure of past fixes to avoid repeating failed actions |
| **Evidence scoring** | Scores evidence completeness (0–100) and caps confidence when data is sparse |
| **Structured data** | `get_deployment_info()` returns typed K8s API dicts (replicas, images, resource limits) |
| **Smart node pressure** | Distinguishes over-provisioned replicas (scale down) from genuine node exhaustion (escalate) |

### 🤖 Specialist Agents

Four domain-specific LLM agents analyse cluster evidence in parallel:

| Agent | Domain Focus |
|---|---|
| 🔧 **SRE** | Pod lifecycle, restarts, resource limits, rollout health, node pressure |
| 📱 **App** | Application logs, error patterns, startup failures, dependency errors |
| 🔒 **Security** | Image security, RBAC, secrets exposure, config anomalies |
| 💰 **Cost** | Resource requests/limits, CPU throttling, HPA, right-sizing |

### 💬 Ask KIRA — AI Chat

Slide-in chat drawer for asking anything about an open incident. Full incident context (evidence, agent opinions, RCA) is injected into every prompt.

### ⚙️ Progress-Aware Validation

| Signal | Behaviour |
|---|---|
| **Ready count increasing** | Extends patience — fix is working, just needs time |
| **Zero error pods** | Confirms no regressions (CrashLoopBackOff / OOMKilled / ImagePullBackOff) |
| **Rollout nearly complete** | Declares success when updated ≥ desired−1, even if timeout elapsed |
| **Ready count regressed** | Triggers retry — fix may be making things worse |
| **Terminal error pods** | Triggers deep diagnostic + AI recovery suggestions |

### 📄 Auto-Postmortem

One-click AI-generated postmortem: executive summary, timeline, root cause, blast radius, remediation steps, prevention recommendations, and action items.

### 🎨 Pipeline UI Dashboard

Real-time 6-stage pipeline visualization:

```
Alert → Evidence → War Room → Decision → Remediation → Close
```

- **Evidence card** — groups 8 steps with progress bar (`6/8`) and status icons
- **War Room card** — 4-agent grid with live status; expands to full findings
- **Decision card** — action badge (PATCH / ROLLBACK / SCALE / RESTART), confidence %, kubectl command
- **RCA card** — root cause summary, contributing factors, blast radius, confidence bar
- **Jira card** — clickable ticket link
- **Closing summary** — outcome icon, elapsed time, postmortem button

Deep violet glassmorphism theme · animated mesh background · zero framework dependencies (vanilla HTML/CSS/JS).

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | FastAPI (Python 3.11) + Uvicorn + sse-starlette |
| **Frontend** | Vanilla HTML / CSS / JS — served by FastAPI StaticFiles |
| **LLM** | [Ollama](https://ollama.com) — `llama3.2` default; any Ollama-compatible model |
| **Kubernetes** | `kubernetes` Python SDK + `kubectl` CLI |
| **Ticketing** | Jira Cloud REST API v3 *(optional)* |
| **Notifications** | Microsoft Teams incoming webhook *(optional)* |
| **Config** | pydantic-settings + python-dotenv (`.env`) |
| **Container** | Docker + Docker Compose |
| **State** | In-memory (no database) |

---

## Project Structure

```
kira/
├── backend/
│   ├── main.py                  # FastAPI app, routes, SSE, pipeline
│   ├── config.py                # Pydantic settings (.env)
│   ├── agent/
│   │   ├── decision_engine.py   # LLM Decision Engine
│   │   ├── war_room.py          # 4-agent dispatcher
│   │   ├── investigator.py      # kubectl evidence gathering
│   │   ├── remediation.py       # Safe command execution
│   │   ├── validator.py         # Post-fix health polling
│   │   ├── chat.py              # Ask KIRA Q&A
│   │   ├── postmortem.py        # Postmortem generator
│   │   ├── crash_monitor.py     # Background health watcher
│   │   ├── memory_monitor.py    # Memory threshold monitor
│   │   ├── outcome_tracker.py   # Remediation feedback loop
│   │   └── rca.py               # Legacy single-agent RCA
│   ├── integrations/
│   │   ├── openai_client.py     # Ollama LLM client
│   │   ├── k8s_client.py        # K8s API + kubectl wrapper
│   │   ├── jira_client.py       # Jira lifecycle integration
│   │   └── teams.py             # Teams webhook sender
│   ├── models/
│   │   └── incident.py          # Pydantic models
│   └── playbooks/
│       ├── rollback.py          # Smart rollback
│       ├── restart_pods.py      # Rolling restart
│       ├── scale.py             # Scale deployment
│       └── patch_resources.py   # Resource patching
├── frontend/
│   ├── index.html               # Dashboard shell
│   ├── app.js                   # Pipeline UI + SSE router
│   └── styles.css               # Violet glassmorphism theme
├── demo/
│   ├── setup.sh                 # Create demo namespace
│   ├── trigger_incident.sh      # Inject fault + fire alert
│   ├── run_e2e_tests.sh         # 10-scenario test suite
│   ├── run_full_e2e.sh          # 25-scenario comprehensive suite
│   └── manifests/               # Fault injection YAMLs
├── .env.example                 # ← Start here for configuration
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── run.sh                       # ← Easiest way to run
```

---

## Running

### Option A — run.sh (recommended)

```bash
chmod +x run.sh
./run.sh
```

The script automatically: checks Ollama is running, pulls missing models, verifies the cluster, creates a Python venv, installs dependencies, and starts the server with hot reload.

### Option B — Docker Compose

```bash
docker compose up --build
```

> ⚠️ **Mac + kind:** kind's API server runs on `127.0.0.1`, which Docker containers can't reach by default. Use `run.sh` (Option A) for the simplest Mac experience, or apply this workaround:
> ```bash
> kubectl config view --minify --raw \
>   | sed 's|https://127.0.0.1|https://host.docker.internal|g' \
>   > .kubeconfig-local
> ```
> Then update the volume mount in `docker-compose.yml` to use `.kubeconfig-local`.

### Option C — Manual

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook/alert` | Ingest alert → start investigation |
| `POST` | `/webhook/grafana` | Grafana Alertmanager webhook |
| `POST` | `/scan` | Manual infrastructure scan |
| `GET` | `/incidents` | List all incidents |
| `GET` | `/incidents/{id}` | Full incident detail |
| `GET` | `/incidents/{id}/stream` | SSE timeline stream |
| `POST` | `/incidents/{id}/action` | Approve or skip remediation |
| `GET` | `/incidents/{id}/postmortem` | Generate AI postmortem |
| `POST` | `/incidents/{id}/chat` | Ask KIRA a question |
| `GET` | `/health` | Liveness + LLM reachability |

**Trigger an alert:**

```bash
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "cart-web",
    "namespace": "default",
    "message": "Health check failures — elevated restart count",
    "severity": "critical",
    "source": "grafana"
  }'
```

**Grafana Alertmanager** — set your contact point URL to:
```
http://<your-server>:8000/webhook/grafana
```

---

## Testing

### Quick Suite (10 scenarios)

```bash
export KUBE_CONTEXT=kind-dev-cluster
bash demo/run_e2e_tests.sh
```

| # | Scenario | Injected Fault | Expected Fix |
|---|---|---|---|
| 1 | Bad image tag | `nginx:99.99-nonexistent` | Rollback |
| 2 | CrashLoopBackOff | `busybox` exit 1 | Rollback |
| 3 | OOMKilled | 4Mi memory limit | Patch resources |
| 4 | CPU throttling | 1m CPU limit | Patch resources |
| 5 | Bad readiness probe | Probe on port 9999 | Rollback |
| 6 | Invalid registry | `invalid-corp.example.com` | Rollback |
| 7 | Undersized both | 1m CPU + 4Mi memory | Patch resources |
| 8 | Bad entrypoint | `cat /etc/app/missing-config.yaml` | Rollback |
| 9 | Bad liveness probe | `/healthz/nonexistent` (404) | Rollback |
| 10 | Low replicas | 1 replica (needs 3) | Scale |

### Full Suite (25 scenarios)

```bash
bash demo/run_full_e2e.sh
```

---

## Demo Walkthrough

```bash
bash demo/setup.sh              # 1. Create demo namespace + workload
open http://localhost:8000       # 2. Open dashboard
bash demo/trigger_incident.sh   # 3. Break cart-web
```

Watch the pipeline progress through each stage:

1. **🚨 Alert** — incident registered, Jira ticket created
2. **🔍 Evidence** — 8 probes run, grouped card with progress bar
3. **🤖 War Room** — 4 agents analyse in parallel, shown as a live grid
4. **🧠 Decision** — action badge, verdict, confidence %, kubectl command
5. **⚡ Remediation** — auto-fix if ≥90% confidence, else waits for approval
6. **✅ Resolution** — validates recovery, closes Jira, shows postmortem button

---

## Incident Lifecycle

```
pending → investigating → rca_complete → awaiting_approval* → remediating → validating → resolved
                                                                                         ↘ failed
                                                                           ↘ skipped
```

\* `awaiting_approval` only when confidence < `AUTO_APPROVE_THRESHOLD` (default 90%).

**Jira ticket lifecycle:** `To Do` → `In Progress` (on RCA) → `Done` (on resolution).

---

## Remediation Playbooks

| Type | When Used | Command |
|---|---|---|
| `rollback` | Bad image, crashloop, probe failure | `kubectl rollout undo` |
| `set_image` | Incorrect image tag | `kubectl set image` |
| `patch` | Undersized resources, OOM | `kubectl set resources` / `kubectl patch` |
| `scale` | Insufficient or excess replicas | `kubectl scale --replicas=N` |
| `restart` | Stuck pods, correct config | `kubectl rollout restart` |
| `none` | Healthy deployment | No action taken |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Ollama is not running` | Run `ollama serve` in a terminal |
| `model not found` | `ollama pull llama3.2` |
| `Cannot reach Kubernetes cluster` | Run `kubectl cluster-info`; check `KUBE_CONTEXT` in `.env` |
| First response is slow | Normal — Ollama loads the model into RAM on first request (~5–10s) |
| Port 8000 already in use | `lsof -ti :8000 \| xargs kill -9` |
| Jira tickets not creating | Verify `JIRA_ENABLED=true`, check API token at id.atlassian.com |
| Teams notifications missing | Verify the webhook URL is complete and connector is active |
| Docker backend can't reach kind | Use `run.sh` or apply the kubeconfig rewrite (see [Running](#running)) |

---

## Known Limitations

| Limitation | Details |
|---|---|
| **In-memory state** | Incidents are lost on restart — no persistence layer |
| **Ollama required** | Must be running with a model pulled before starting |
| **Jira Cloud only** | REST API v3 with Basic Auth; Data Center / Server is untested |
| **macOS tested** | Linux works; Windows WSL2 is untested |
| **Single-deployment** | Each incident targets one deployment/statefulset; cascade analysis is manual via chat |
| **Validation timeout** | Recovery polling runs up to 90 seconds; progress-aware logic extends patience but cannot wait indefinitely |

---

## License

MIT
