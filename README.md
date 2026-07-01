# KIRA 🔍 — Kubernetes Intelligent Response Agent

> **AI-powered Kubernetes incident response — from alert to resolution in under 2 minutes.**

KIRA automatically investigates your cluster when an alert fires, dispatches 4 specialist AI agents for parallel analysis, synthesises their findings into a definitive root cause with an exact remediation command, executes the fix, and validates recovery — all streamed live to a real-time dashboard.

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
- [Local Setup (Ollama + kind)](#-local-setup-ollama--kind)
- [Architecture](#architecture)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running](#running)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Demo Walkthrough](#demo-walkthrough)
- [Incident Lifecycle](#incident-lifecycle)
- [Remediation Playbooks](#remediation-playbooks)
- [Development](#development)
- [Known Limitations](#known-limitations)
- [License](#license)

---

## ⚡ Quick Start

```bash
# 1. Install Ollama and pull a model
brew install ollama
ollama serve &
ollama pull llama3.2

# 2. Clone and enter the project
cd kira

# 3. Copy env config (pre-filled for local use)
cp .env.example .env
# Edit KUBE_CONTEXT to match your cluster: kubectl config get-contexts

# 4. Run
chmod +x run.sh
./run.sh

# 5. Open dashboard
open http://localhost:8000

# 6. (Optional) Deploy demo workload and trigger an incident
bash demo/setup.sh
bash demo/trigger_incident.sh
```

> **Note** — KIRA uses [Ollama](https://ollama.com) to run the LLM locally on your machine.
> No API keys, no cloud costs. Works great on Apple Silicon (M1/M2/M3/M4/M5).

---

## 🛠 Local Setup (Ollama + kind)

This section walks you through a complete local setup on macOS with [Ollama](https://ollama.com) as the LLM and [kind](https://kind.sigs.k8s.io) as the Kubernetes cluster.

### Step 1 — Install prerequisites

```bash
# Ollama (local LLM runtime)
brew install ollama

# kind (Kubernetes in Docker)
brew install kind

# kubectl
brew install kubectl

# Python 3.11+
brew install python@3.11
```

---

### Step 2 — Start Ollama

```bash
ollama serve
```

Keep this terminal open (or it runs automatically in the background after installing the Ollama Mac app from https://ollama.com).

Verify it's running:
```bash
curl http://localhost:11434/api/tags
# → returns JSON with a "models" key
```

---

### Step 3 — Pull the LLM model

```bash
ollama pull llama3.2
```

This downloads ~2 GB. `llama3.2` works well on 16 GB RAM.

**Better quality options (all fit in 16 GB):**

| Model | Size | Best for |
|-------|------|----------|
| `llama3.2` (default) | ~2 GB | Speed |
| `mistral` | ~4 GB | Balanced |
| `qwen2.5:7b` | ~4 GB | Reasoning |
| `llama3.1:8b` | ~5 GB | Best quality |
| `gemma3:4b` | ~3 GB | Fast + smart |

To use a different model, edit `OLLAMA_MODEL` in `.env` and run `ollama pull <model-name>`.

---

### Step 4 — Create a kind cluster (skip if already running)

```bash
kind create cluster --name dev-cluster
```

Verify it's up:
```bash
kubectl get nodes
# → shows your node as Ready

kubectl config get-contexts
# → shows kind-dev-cluster as current context
```

---

### Step 5 — Configure the project

```bash
cd kira
cp .env.example .env
```

The `.env` file is pre-filled with local defaults. Check these two values match your setup:

```bash
# In .env:
KUBE_CONTEXT=kind-dev-cluster   # must match output of: kubectl config get-contexts
OLLAMA_MODEL=llama3.2           # must match the model you pulled
```

---

### Step 6 — Run KIRA

**Option A — Direct (recommended for development)**

```bash
chmod +x run.sh
./run.sh
```

The script will automatically:
- ✅ Check Ollama is running
- ✅ Pull the model if not already downloaded
- ✅ Verify the Kubernetes cluster is reachable
- ✅ Create a Python virtualenv and install all dependencies
- ✅ Start the FastAPI server on port 8000 with hot reload

**Option B — Docker Compose**

> ⚠️ On macOS with Docker Desktop, the backend container cannot reach the kind API server on `127.0.0.1` by default. Use `run.sh` (Option A) for the simplest Mac experience, or apply the workaround below.

```bash
# Mac workaround: rewrite the API server address for Docker
kubectl config view --minify --raw \
  | sed 's|https://127.0.0.1|https://host.docker.internal|g' \
  > .kubeconfig-local

# Then start both Ollama + backend:
docker compose up --build
```

---

### Step 7 — Open the dashboard

| URL | What |
|-----|------|
| http://localhost:8000 | 🔍 KIRA Dashboard |
| http://localhost:8000/api/docs | 📖 Swagger / OpenAPI docs |
| http://localhost:8000/health | ❤️  Health check (LLM + cluster) |

---

### Step 8 — Run a demo scan

```bash
# Fire a manual infrastructure scan against your cluster
curl -X POST http://localhost:8000/scan

# Or inject a fault and watch the full pipeline:
bash demo/setup.sh            # creates demo namespace + workload
bash demo/trigger_incident.sh # breaks the workload
```

---

## Architecture

KIRA implements a **closed-loop autonomous incident response** architecture. The system operates as a multi-stage pipeline with event-driven orchestration, parallel multi-agent reasoning, and self-healing feedback loops — all observable in real time via Server-Sent Events.

### System Overview

```
                            ┌─────────────────────────────────────────────────────────┐
                            │              KIRA Control Plane                  │
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

KIRA accepts alerts from multiple sources with built-in parsers:

| Source | Endpoint | Parser |
|---|---|---|
| **Grafana Alertmanager** | `POST /webhook/grafana` | Extracts service from pod/deployment/statefulset labels, maps severity, skips internal alerts |
| **OpsGenie / JSM** | `POST /webhook/grafana` | Parses OpsGenie tag format, processes `Create` actions only |
| **Manual / generic** | `POST /webhook/alert` | Direct `AlertPayload` JSON |
| **Infrastructure scan** | `POST /scan` | Scans all Deployments + StatefulSets across configured namespaces |

### Pipeline Stages

The pipeline executes as a **directed acyclic graph** with conditional branching at the approval gate and retry loops at validation. Each stage emits structured SSE events consumed by the real-time dashboard.

| Stage | Name | Behaviour |
|:---:|---|---|
| **0** | **Initialisation** | Registers incident, creates Jira ticket, optionally notifies Teams, establishes SSE channel |
| **1** | **Evidence Acquisition** | Executes 8 parallel-safe cluster probes via K8s API and kubectl: pod status, container logs, resource utilisation, rollout history, deployment spec, cluster events, cross-service correlation, and structured deployment metadata |
| **2** | **Multi-Agent Analysis** | Dispatches evidence to 4 domain-specialist LLM agents concurrently (SRE, App, Security, Cost); each returns structured JSON — findings, confidence score, cited evidence, and flagged concerns |
| **3** | **Decision Synthesis** | Pre-analysis layer computes health vectors and evidence quality scores (0–100), checks outcome history for circular remediation patterns, then feeds all agent opinions + raw evidence into a single LLM call → produces definitive RCA, blast radius, confidence score, and exact remediation command |
| **4** | **Approval Gate** | Confidence ≥ threshold → auto-approved with audit trail · Below threshold → blocks for human approval via dashboard prompt (or optional Teams actionable card) |
| **5** | **Remediation Execution** | Runs the generated kubectl command through a safety validator (injection prevention, namespace enforcement, dangerous verb blocking); StatefulSet-aware — auto-deletes unhealthy pods post-patch for controller recreation |
| **6** | **Recovery Validation** | Polls pod health at 4s intervals for 90s with **progress-aware extension** — if the rollout is actively progressing (ready count increasing, zero error pods), validation recognises the fix is working and avoids false-negative retries. On genuine failure: runs deep diagnostic analysis, generates AI-powered recovery hypothesis, re-gathers evidence, and retries decision + execution (max 2 attempts) |
| **7** | **Closure & Feedback** | Records outcome in the feedback memory (prevents repeating failed remediations), transitions Jira to `Done`, optionally sends Teams summary, emits `Incident Closed` event |

### Design Principles

- **Observe → Orient → Decide → Act (OODA)** — modeled after the military decision loop; each stage maps to an OODA phase with full observability
- **Fan-out / fan-in concurrency** — specialist agents run in parallel; their opinions are merged at the decision engine
- **Self-healing retry loop** — validation failures trigger re-investigation with fresh evidence, preventing stale-state decisions
- **Fast-path short-circuit** — healthy clusters skip the LLM entirely (deterministic exit at pre-analysis)
- **Feedback memory** — outcome tracker builds institutional knowledge across incidents, preventing circular remediation

<details>
<summary><strong>Expanded internal architecture (ASCII)</strong></summary>

```
Ingestion:
  POST /scan              — On-demand infrastructure scan (⚡ Analyse)
  POST /webhook/grafana   — Grafana Alertmanager webhook
  POST /webhook/alert     — Generic alert ingest (curl, PagerDuty, OpsGenie, etc.)
  Crash Monitor           — Background LLM-driven anomaly detector (continuous)
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
│  │  🔒 Security Agent ┤ evidence corpus, analyses from its       │ │
│  │  💰 Cost Agent ────┘ domain lens → structured JSON response   │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 3: LLM Decision Engine ────────────────────────────────┐ │
│  │  Pre-analysis: health vector computation + evidence scoring    │ │
│  │  Fast-path:    all healthy → deterministic "none" (skip LLM)  │ │
│  │  Synthesis:    agent opinions + evidence + outcome history     │ │
│  │                → RCA + blast radius + confidence + command     │ │
│  │  Side-effect:  Jira RCA comment + transition to In Progress   │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Stage 4–7: Execution & Feedback Loop ────────────────────────┐ │
│  │  Approval → Remediation → Validation → Outcome                │ │
│  │             kubectl exec   health poll   feedback memory       │ │
│  │                            retry (max 2)  Jira Done            │ │
│  │                                           + Teams (optional)   │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  Auxiliary Services:                                                 │
│   • /incidents/{id}/chat  — Context-aware conversational Q&A        │
│   • /incidents/{id}/postmortem — AI-generated post-incident report  │
│   • Crash Monitor (continuous) · Memory Monitor · Outcome Tracker   │
│                                                                      │
│  SSE Event Bus ═══════════════════════════════════════════▶ UI      │
└──────────────────────────────────────────────────────────────────────┘
```

</details>

---

## Features

### 🧠 LLM Decision Engine

A single LLM call (via Ollama) that synthesises all specialist agent opinions + raw cluster evidence into a definitive root cause analysis and exact remediation command.

| Capability | Description |
|---|---|
| **Pre-analysis** | Computes structured health signals (`all_running`, `crash_loop`, `oom_killed`, `image_pull_error`, `not_ready`, etc.) before calling the LLM |
| **Fast-path guard** | Skips LLM entirely when all pods are Running/Ready with 0 restarts — prevents hallucinated problems |
| **Outcome tracking** | Records success/failure of past fixes to avoid repeating failed actions (circular remediation detection) |
| **Evidence scoring** | Scores evidence completeness (0–100) and caps confidence when data is sparse |
| **Structured data** | `get_deployment_info()` returns typed K8s API dicts (replicas, images, resource limits/requests) |
| **Smart node pressure** | Distinguishes between over-provisioned replicas (fix: scale down) and genuine node capacity exhaustion (escalate to human) |

### 🤖 Specialist Agents

Four domain-specific LLM agents analyse cluster evidence in parallel:

| Agent | Domain Focus |
|---|---|
| 🔧 **SRE** | Pod lifecycle, restarts, resource limits, rollout health, node pressure |
| 📱 **App** | Application logs, error patterns, startup failures, dependency errors |
| 🔒 **Security** | Image security, RBAC, secrets exposure, config anomalies |
| 💰 **Cost** | Resource requests/limits, CPU throttling, HPA, right-sizing |

Agents receive structured evidence with clear separation between **current state** (describe, status, usage) and **historical data** (rollout history).

### 💬 Ask Sherlock — AI Chat

Slide-in chat drawer for asking anything about an open incident. Full incident context (evidence, agent opinions, RCA) is injected into every prompt.

### ⚙️ Progress-Aware Validation

The recovery validation loop uses `get_rollout_progress()` to track rollout state in real time:

| Signal | Behaviour |
|---|---|
| **Ready count increasing** | Extends patience — fix is working, just needs time |
| **Zero error pods** | Confirms no CrashLoopBackOff / ImagePullBackOff / OOMKilled regressions |
| **Rollout nearly complete** | Declares success when updated ≥ desired−1, even if timeout elapsed |
| **Ready count regressed** | Triggers retry — fix may be making things worse |
| **Terminal error pods detected** | Triggers deep diagnostic + AI recovery suggestions |

### 📄 Auto-Postmortem

One-click AI-generated postmortem: executive summary, timeline, root cause, blast radius, remediation steps, prevention recommendations, and action items.

### 🎫 Jira Lifecycle Integration

Full incident-to-ticket automation with Jira Cloud (REST API v3, Basic Auth, ADF):

| Hook Point | Jira Action |
|---|---|
| Alert received | Create ticket (`To Do`) with summary, namespace, severity |
| RCA complete | Add RCA comment + transition to `In Progress` |
| Remediation executed | Add command + output comment |
| Resolved | Add closing comment + transition to `Done` |
| Failed | Add failure comment — ticket stays open |

> Configure: `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` · Set `JIRA_ENABLED=false` to disable (default for local dev).

### 🎨 Pipeline UI Dashboard

Real-time 6-stage pipeline visualization:

```
Alert → Evidence → War Room → Decision → Remediation → Close
```

- **Evidence card** — groups 8 steps with progress bar (`6/8`) and status icons
- **War Room card** — 4-agent grid with live status; expands to full findings
- **Decision card** — action badge (PATCH / ROLLBACK / SCALE / RESTART), confidence %, kubectl command
- **RCA card** — root cause summary, contributing factors, blast radius, confidence bar
- **Jira card** — clickable ticket link (e.g. `KAN-10 ↗`)
- **Closing summary** — outcome icon, elapsed time, Jira link, postmortem button

Deep violet glassmorphism theme · animated mesh background · frosted-glass panels · zero framework dependencies (vanilla HTML/CSS/JS).

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | FastAPI (Python 3.11) + Uvicorn + sse-starlette |
| **Frontend** | Vanilla HTML / CSS / JS — served by FastAPI StaticFiles |
| **LLM** | [Ollama](https://ollama.com) (local) — `llama3.2` default; any Ollama model supported |
| **Kubernetes** | `kubernetes` Python SDK + `kubectl` CLI |
| **Ticketing** | Jira Cloud REST API v3 (Basic Auth, ADF) *(optional)* |
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
│   │   ├── chat.py              # Ask Sherlock Q&A
│   │   ├── postmortem.py        # Postmortem generator
│   │   ├── crash_monitor.py     # Background health watcher
│   │   ├── memory_monitor.py    # Memory threshold monitor
│   │   ├── outcome_tracker.py   # Remediation feedback loop
│   │   └── rca.py               # Legacy single-agent RCA
│   ├── integrations/
│   │   ├── k8s_client.py        # K8s API + kubectl wrapper
│   │   ├── openai_client.py     # Ollama LLM client
│   │   ├── jira_client.py       # Jira lifecycle integration
│   │   └── teams.py             # Teams webhook sender (optional)
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
│   ├── manifests/               # Fault injection YAMLs (25 scenarios)
│   ├── test-results/            # E2E run logs
│   ├── advanced/                # Multi-service cascade demo
│   └── memory/                  # Memory threshold demo
├── docker-compose.yml           # Ollama + backend services
├── Dockerfile
├── requirements.txt
├── run.sh                       # Local run script (recommended)
├── .env                         # Your local config (git-ignored)
└── .env.example                 # Environment template
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11+** | Required for `run.sh` (direct mode) |
| **Ollama** | Local LLM runtime — https://ollama.com |
| **kubectl** | Kubernetes CLI |
| **kind** | Local Kubernetes cluster — https://kind.sigs.k8s.io |
| **Docker** | Only needed for Docker Compose mode |

> Works on macOS (Apple Silicon and Intel), Linux. Windows WSL2 is untested.

---

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

<details>
<summary><strong>All environment variables</strong></summary>

#### Ollama (LLM)

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Model to use (must be pulled with `ollama pull`) |

#### Kubernetes

| Variable | Default | Description |
|---|---|---|
| `KUBE_CONTEXT` | — | kubectl context name (e.g. `kind-dev-cluster`) |
| `DEFAULT_NAMESPACE` | `default` | Namespace to watch |

#### Remediation

| Variable | Default | Description |
|---|---|---|
| `APPROVAL_MODE` | `true` | Require human approval below auto-approve threshold |
| `AUTO_APPROVE_THRESHOLD` | `90` | Auto-approve if confidence ≥ this % |

#### Integrations (all optional)

| Variable | Default | Description |
|---|---|---|
| `TEAMS_WEBHOOK_URL` | — | Microsoft Teams incoming webhook URL |
| `PUBLIC_URL` | `http://localhost:8000` | Public URL for callback links |
| `JIRA_ENABLED` | `false` | Enable Jira integration |
| `JIRA_URL` | — | Jira Cloud URL (e.g. `https://yourorg.atlassian.net`) |
| `JIRA_EMAIL` | — | Jira account email |
| `JIRA_API_TOKEN` | — | API token from [id.atlassian.com](https://id.atlassian.com) |
| `JIRA_PROJECT_KEY` | `KS` | Project key for ticket creation |
| `JIRA_ISSUE_TYPE` | `Task` | Issue type (Task, Bug, Story) |

</details>

---

## Running

### Option A — Direct with run.sh (recommended)

```bash
chmod +x run.sh
./run.sh
```

The script handles everything: checks Ollama, pulls missing models, verifies the cluster, creates the Python venv, installs deps, and starts the server.

### Option B — Docker Compose

```bash
docker compose up --build
```

> **macOS note:** Kind's API server runs on `127.0.0.1`, which Docker containers can't reach by default.
> Rewrite the kubeconfig for Docker:
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

### Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook/alert` | Ingest alert → start investigation |
| `POST` | `/webhook/grafana` | Grafana alertmanager webhook |
| `POST` | `/scan` | Manual infrastructure scan |
| `GET` | `/incidents` | List all incidents |
| `GET` | `/incidents/{id}` | Full incident detail |
| `GET` | `/incidents/{id}/stream` | SSE timeline stream |
| `POST` | `/incidents/{id}/action` | Approve or skip remediation |
| `GET` | `/incidents/{id}/postmortem` | Generate AI postmortem |
| `POST` | `/incidents/{id}/chat` | Ask Sherlock a question |
| `GET` | `/health` | Liveness check |

### Examples

**Trigger an alert:**

```bash
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "cart-web",
    "namespace": "demo",
    "message": "Health check failures detected — elevated restart count",
    "severity": "critical",
    "source": "grafana"
  }'
```

**Scan for unhealthy workloads:**

```bash
curl -X POST http://localhost:8000/scan
```

---

## Testing

### End-to-End Test Suites

Two test suites are available — a quick 10-scenario suite and a comprehensive 25-scenario suite.

#### Quick Suite (10 scenarios)

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

Each test: inject fault → trigger webhook → wait for resolution (up to 180s) → verify pods healthy → restore baseline.

---

## Demo Walkthrough

```bash
bash demo/setup.sh              # 1. Set up the cluster
open http://localhost:8000       # 2. Open the dashboard
bash demo/trigger_incident.sh   # 3. Break cart-web
```

Watch the pipeline UI progress through each stage:

1. **🚨 Alert** — receives alert, creates Jira ticket
2. **🔍 Evidence** — collects 8 evidence types in a grouped card with progress bar
3. **🤖 War Room** — 4 agents analyse in parallel, shown as a live grid
4. **🧠 Decision** — synthesises into action badge, verdict, confidence %, and kubectl command
5. **⚡ Remediation** — executes fix (auto if ≥90% confidence, else waits for approval)
6. **✅ Resolution** — validates recovery, closes Jira, shows summary with postmortem button

---

## Incident Lifecycle

```
pending → investigating → rca_complete → awaiting_approval* → remediating → validating → resolved
                                                                                         ↘ failed
                                                                           ↘ skipped
```

\* `awaiting_approval` only when confidence < `AUTO_APPROVE_THRESHOLD` (default 90%).

**Jira ticket lifecycle:** `To Do` → `In Progress` (on RCA) → `Done` (on resolution). Failed incidents keep the ticket open.

---

## Remediation Playbooks

| Type | When Used | Command |
|---|---|---|
| `rollback` | Bad image, crashloop, probe failure | `kubectl rollout undo` |
| `set_image` | Incorrect image tag | `kubectl set image` |
| `patch` | Undersized resources, OOM | `kubectl set resources` or `kubectl patch` |
| `scale` | Insufficient replicas or over-provisioned | `kubectl scale --replicas=N` |
| `restart` | Stuck pods, correct config | `kubectl rollout restart` |
| `none` | Healthy deployment or config-level issue | No action taken |

---

## Development

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --reload-dir backend --reload-dir frontend --port 8000
```

---

## Known Limitations

| Limitation | Details |
|---|---|
| **In-memory state** | Incidents are lost on restart (no persistence layer) |
| **Ollama required** | Ollama must be running and a model must be pulled before starting |
| **Jira Cloud only** | REST API v3 with Basic Auth; Data Center / Server is untested |
| **macOS tested** | Linux works; Windows WSL2 is untested |
| **Single-deployment** | Each incident targets one deployment/statefulset; cascade analysis is manual via chat |
| **Validation timeout** | Recovery polling runs for up to 90 seconds; progress-aware logic extends patience for active rollouts but cannot wait indefinitely |
| **First-response latency** | Ollama loads the model into RAM on the first request (~5–10 sec); subsequent calls are fast |

---

## License

MIT
