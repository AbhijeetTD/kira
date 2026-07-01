# KIRA 🔍
### Kubernetes Intelligent Response Agent

> AI-powered Kubernetes incident response — investigates alerts, finds root cause, and auto-remediates, all running **100% locally** with Ollama.

![Python](https://img.shields.io/badge/Python-3.11-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green) ![Ollama](https://img.shields.io/badge/LLM-Ollama-orange) ![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## What does KIRA do?

When a Kubernetes pod crashes, OOMs, or gets stuck, KIRA:

1. 🔍 **Investigates** — collects logs, events, resource usage, rollout history
2. 🤖 **Analyses** — 4 specialist AI agents (SRE, App, Security, Cost) work in parallel
3. 🧠 **Decides** — synthesises findings into a root cause + exact `kubectl` command
4. ⚡ **Fixes** — executes the remediation (rollback / patch / scale / restart)
5. ✅ **Validates** — polls until pods are healthy, retries if needed
6. 📄 **Reports** — generates a postmortem, closes the Jira ticket

All streamed live to a real-time dashboard.

---

## Quick Start (5 minutes)

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

Open `.env` and set these **2 required values**:

```bash
# Your kubectl context name — find it with:
# kubectl config get-contexts
KUBE_CONTEXT=kind-dev-cluster    # ← change this

# Namespace you want KIRA to monitor
DEFAULT_NAMESPACE=default        # ← change this if needed
```

Everything else has working defaults. See [Configuration](#configuration) for optional integrations (Jira, Teams).

### 4. Run KIRA

```bash
chmod +x run.sh
./run.sh
```

### 5. Open the dashboard

```
http://localhost:8000
```

**Trigger a test scan:**
```bash
curl -X POST http://localhost:8000/scan
```

---

## Configuration

All settings live in `.env`. Here's what each section does:

### Section 1 — Ollama (Required)

```bash
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=llama3.2
```

**Choosing a model** — all work on 16 GB RAM:

| Model | Size | Quality |
|-------|------|---------|
| `llama3.2` | 2 GB | ⭐⭐⭐ Fast |
| `mistral` | 4 GB | ⭐⭐⭐⭐ Balanced |
| `qwen2.5:7b` | 4 GB | ⭐⭐⭐⭐ Smart |
| `llama3.1:8b` | 5 GB | ⭐⭐⭐⭐⭐ Best |

To switch model: edit `OLLAMA_MODEL`, run `ollama pull <model>`, restart KIRA.

---

### Section 2 — Kubernetes (Required)

```bash
# Find your context name:
kubectl config get-contexts

KUBE_CONTEXT=kind-dev-cluster    # paste your context name here
DEFAULT_NAMESPACE=default        # namespace KIRA watches
```

Common context names:
- `kind-dev-cluster` → kind
- `minikube` → Minikube
- `docker-desktop` → Docker Desktop
- `rancher-desktop` → Rancher Desktop

---

### Section 3 — Behaviour (Optional, defaults work)

```bash
# Confidence threshold for auto-remediation (0–100)
# If confidence >= this, KIRA fixes automatically
# If confidence <  this, KIRA asks for your approval first
AUTO_APPROVE_THRESHOLD=90

# Set to false to make KIRA fully autonomous (never asks for approval)
APPROVAL_MODE=true
```

---

### Section 4 — Jira (Optional)

Automatically creates tickets when incidents start and closes them when resolved.

**Step 1** — Enable it:
```bash
JIRA_ENABLED=true
```

**Step 2** — Set your Jira details:
```bash
# Your Jira Cloud URL
JIRA_URL=https://your-company.atlassian.net

# Your Jira login email
JIRA_EMAIL=you@yourcompany.com

# API token — generate at:
# https://id.atlassian.com/manage-profile/security/api-tokens
JIRA_API_TOKEN=your_token_here

# Project key — the letters shown in brackets in your Jira project
# e.g. if your project is "Ops [OPS]", use OPS
JIRA_PROJECT_KEY=KS

# Issue type to create
JIRA_ISSUE_TYPE=Task
```

How the Jira ticket lifecycle works:
```
Alert fires     → ticket created   (status: To Do)
RCA complete    → comment added    (status: In Progress)
Fix executed    → command logged
Resolved        → ticket closed    (status: Done)
```

---

### Section 5 — Microsoft Teams (Optional)

Sends incident alerts and resolution summaries to a Teams channel.

**Step 1** — Get the webhook URL:
> Teams → open your channel → `···` menu → Connectors → Incoming Webhook → Create → Copy URL

**Step 2** — Add to `.env`:
```bash
TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/webhookb2/...
```

---

## How to run

### Option A — Direct (recommended)

```bash
./run.sh
```

The script auto-checks Ollama, pulls missing models, verifies the cluster, installs Python deps, and starts the server.

### Option B — Docker Compose

```bash
docker compose up --build
```

> ⚠️ **Mac + kind note:** kind's API server runs on `127.0.0.1`, which Docker containers can't reach by default. Use `run.sh` for the easiest Mac experience. If you need Docker Compose, rewrite the kubeconfig first:
> ```bash
> kubectl config view --minify --raw \
>   | sed 's|https://127.0.0.1|https://host.docker.internal|g' \
>   > .kubeconfig-local
> ```

### Option C — Manual

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Trigger an incident

**Manual scan** — scans all workloads in the namespace:
```bash
curl -X POST http://localhost:8000/scan
```

**Specific alert:**
```bash
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "service": "my-app",
    "namespace": "default",
    "message": "Pod is crash looping",
    "severity": "critical"
  }'
```

**Grafana webhook** — point your Grafana Alertmanager contact point to:
```
http://<your-server>:8000/webhook/grafana
```

---

## Useful URLs

| URL | Description |
|-----|-------------|
| `http://localhost:8000` | 🔍 Live dashboard |
| `http://localhost:8000/api/docs` | 📖 API reference (Swagger) |
| `http://localhost:8000/health` | ❤️ Health check |
| `http://localhost:8000/incidents` | 📋 All incidents (JSON) |

---

## Architecture

```
Alert / Webhook / Scan
        │
        ▼
┌─────────────────────────────────────────────────┐
│              KIRA Control Plane                  │
│                                                  │
│  1. Evidence Collection                          │
│     8 probes: logs, events, resources, history   │
│                                                  │
│  2. Multi-Agent War Room (parallel)              │
│     🔧 SRE  📱 App  🔒 Security  💰 Cost         │
│                                                  │
│  3. LLM Decision Engine (Ollama)                 │
│     → Root cause + confidence + kubectl command  │
│                                                  │
│  4. Approval Gate                                │
│     Auto-fix if confidence ≥ 90%                 │
│     Ask human if confidence < 90%                │
│                                                  │
│  5. Remediation + Validation                     │
│     Execute → Poll health → Retry if needed      │
│                                                  │
│  6. Closure                                      │
│     Postmortem · Jira Done · Teams summary       │
└─────────────────────────────────────────────────┘
        │
        ▼
  Real-time SSE Dashboard
```

---

## Project structure

```
kira/
├── backend/
│   ├── main.py                  # FastAPI app + all routes
│   ├── config.py                # Reads .env settings
│   ├── agent/
│   │   ├── decision_engine.py   # LLM root cause analysis
│   │   ├── war_room.py          # 4-agent dispatcher
│   │   ├── remediation.py       # kubectl execution
│   │   ├── validator.py         # Post-fix health polling
│   │   ├── chat.py              # Ask KIRA questions
│   │   └── postmortem.py        # Report generator
│   └── integrations/
│       ├── openai_client.py     # Ollama LLM client
│       ├── k8s_client.py        # Kubernetes API wrapper
│       ├── jira_client.py       # Jira integration
│       └── teams.py             # Teams notifications
├── frontend/                    # Dashboard (HTML/CSS/JS)
├── demo/                        # Test scripts + fault manifests
├── .env.example                 # ← Start here for configuration
├── docker-compose.yml
├── run.sh                       # ← Easiest way to start
└── requirements.txt
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Ollama is not running` | Run `ollama serve` in a terminal |
| `model not found` | `ollama pull llama3.2` |
| `Cannot reach Kubernetes cluster` | Check `kubectl cluster-info` and `KUBE_CONTEXT` in `.env` |
| `Slow first response` | Normal — Ollama loads the model on first request (~5–10s) |
| `Port 8000 already in use` | `lsof -ti :8000 \| xargs kill -9` |
| `Jira tickets not creating` | Check `JIRA_ENABLED=true` and verify your API token at id.atlassian.com |
| `Teams notifications not sending` | Verify the webhook URL is complete and the connector is active |

---

## License

MIT
