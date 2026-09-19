<div align="center">
  <img src="../logo.svg" width="96" alt="mctl logo" />
  <h1>MCTL</h1>
  <h3>The AI-Native Kubernetes Platform</h3>
  <p>Your complete infrastructure stack unified: GitOps, secrets management, team isolation, and AI automation. Deploy services via a portal, REST API, or natural language.</p>
</div>

<br>

<div align="center">
  <table>
    <tr>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:refresh-cw.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>GitOps Engine</strong><br>
        Auditable infrastructure changes via ArgoCD and Argo Workflows.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:shield-check.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Secrets Vault</strong><br>
        Enterprise-grade security and secret management with HashiCorp Vault.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:users.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Team Isolation</strong><br>
        Secure multi-tenancy and RBAC for modern engineering teams.
      </td>
    </tr>
    <tr>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:layout-dashboard.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Service Catalog</strong><br>
        Self-service onboarding and scaffolding via Backstage portal.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:terminal.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>AI Management</strong><br>
        60+ MCP tools and platform skills — manage infrastructure in natural language from any AI assistant.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:database.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Automated Provisioning</strong><br>
        Automatic PostgreSQL and cloud resource lifecycle management.
      </td>
    </tr>
    <tr>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:activity.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Integrated Monitoring</strong><br>
        Full-stack observability with VictoriaMetrics, Grafana, and Loki.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:lock.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Network Security</strong><br>
        Automated TLS, Ingress, and strict zero-trust network policies.
      </td>
      <td width="33%" align="center">
        <img src="https://api.iconify.design/lucide:server.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Cost Control</strong><br>
        Optimized for Hetzner Cloud and K3s with efficient resource usage.
      </td>
    </tr>
  </table>
</div>

<div align="center">
  <table>
    <tr>
      <td width="50%" align="center">
        <img src="https://api.iconify.design/lucide:sparkles.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Self-Healing Agent</strong><br>
        mctl-agent diagnoses cluster incidents with AI and proposes remediation Pull Requests.
      </td>
      <td width="50%" align="center">
        <img src="https://api.iconify.design/lucide:bot.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Proposal Agents</strong><br>
        mctl-agents investigate issues, draft proposals, implement them, and shepherd PRs through review to merge.
      </td>
    </tr>
  </table>
</div>

---

## Architecture

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'lineColor': '#94a3b8', 'fontSize': '14px'}}}%%
graph LR
  classDef iface fill:#eff6ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  classDef ctrl fill:#fffbeb,stroke:#f59e0b,stroke-width:2px,color:#92400e
  classDef orch fill:#faf5ff,stroke:#a855f7,stroke-width:2px,color:#6b21a8
  classDef infra fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46
  classDef data fill:#fef2f2,stroke:#ef4444,stroke-width:2px,color:#991b1b


  subgraph interfaces ["  Interfaces  "]
    Portal(["Portal<br/>(Backstage)"]):::iface
    CLI(["mctl CLI"]):::iface
    AI(["AI clients<br/>(MCP)"]):::iface
    Push(["CI / CD<br/>git push"]):::iface
  end

  subgraph controlplane ["  Control Plane  "]
    API(["mctl-api<br/>REST + MCP + skills"]):::ctrl
  end

  subgraph orchestration ["  Orchestration + agents  "]
    Temporal(["Temporal<br/>durable dev-loop"]):::orch
    Argo(["Argo Workflows<br/>deploy · provision · agent runs"]):::orch
    Agents(["mctl-agents<br/>proposal agents"]):::ctrl
    Agent(["mctl-agent<br/>self-heal"]):::ctrl
  end

  subgraph delivery ["  Delivery  "]
    Gitops(["mctl-gitops<br/>Helm charts · desired state"]):::infra
    ArgoCD(["ArgoCD<br/>reconcile"]):::infra
    GHCR(["GHCR<br/>images"]):::infra
  end

  subgraph runtime ["  K3s runtime  "]
    Tenants(["Tenant namespaces<br/>admins · labs · ovk"]):::infra
    Vault(["Vault<br/>+ External Secrets"]):::data
    PG(["CloudNativePG<br/>per-service databases"]):::data
    Obs(["Observability<br/>VictoriaMetrics · Grafana · Loki"]):::data
    MinIO(["MinIO<br/>Loki object store"]):::data
  end

  Portal -- REST --> API
  CLI -- REST --> API
  AI -- MCP --> API
  Push -- git push --> Gitops

  API -- operations --> Argo
  API -- trigger runs --> Temporal
  Temporal -- submit CWFT --> Argo
  Argo -- sandboxed runs --> Agents
  Argo -- git commit --> Gitops
  Agents -- proposal PRs --> Gitops
  Agent -- remediation PR --> Gitops

  Gitops -- "GHA build" --> GHCR
  Gitops --> ArgoCD
  ArgoCD -- sync --> Tenants
  GHCR -. pull .-> Tenants

  Vault -. secrets .-> Tenants
  Tenants --> PG
  Tenants -. metrics · logs .-> Obs
  Obs --> MinIO

  style interfaces fill:#f0f5ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  style controlplane fill:#fefce8,stroke:#f59e0b,stroke-width:2px,color:#92400e
  style orchestration fill:#faf5ff,stroke:#a855f7,stroke-width:2px,color:#6b21a8
  style delivery fill:#f0fdf4,stroke:#10b981,stroke-width:2px,color:#065f46
  style runtime fill:#fef7f7,stroke:#ef4444,stroke-width:2px,color:#991b1b
```

---

## AI Automation Loop

Every change an agent makes lands as a Pull Request and passes an AI code
review gate before it reaches the cluster — no unreviewed writes. Proposals
wait for a human to accept them before any code is written, and a shepherd
drives the PR through review rounds until it is clean or handed back for triage.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'lineColor': '#94a3b8', 'fontSize': '14px'}}}%%
graph LR
  classDef trig fill:#fdf2f8,stroke:#ec4899,stroke-width:2px,color:#9d174d
  classDef orch fill:#faf5ff,stroke:#a855f7,stroke-width:2px,color:#6b21a8
  classDef agent fill:#fffbeb,stroke:#f59e0b,stroke-width:2px,color:#92400e
  classDef human fill:#fef2f2,stroke:#ef4444,stroke-width:2px,color:#991b1b
  classDef gate fill:#eff6ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  classDef done fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46

  Issue(["GitHub issue<br/>label: agents:intake"]):::trig --> Poll(["issue poll<br/>every 12h"]):::orch
  Ask(["mctl_trigger_issue<br/>on demand"]):::trig --> DevLoop
  Poll --> DevLoop(["DevLoopWorkflow<br/>durable · one issue"]):::orch
  Alert(["Alert<br/>(AlertManager)"]):::trig --> SelfHeal(["mctl-agent<br/>AI diagnosis"]):::agent
  Incidents(["incident sweep<br/>hourly"]):::orch --> Responder(["incident responder<br/>unmatched incidents"]):::agent
  Scan(["scheduled scan"]):::trig --> Tier1(["service agents<br/>+ mentor"]):::agent

  DevLoop -- submit --> Investigator(["investigator<br/>sandboxed run"]):::agent
  Investigator -- "proposal (proposed)" --> Approve
  Tier1 -- proposal --> Approve
  Responder -- proposal --> Approve(["human approval<br/>proposed → accepted"]):::human
  Approve -- accepted --> Implementer(["implementer<br/>sandboxed run"]):::agent

  SelfHeal -. "no skill match" .-> Responder
  SelfHeal -- remediation PR --> Review
  Implementer -- implementation PR --> Review(["AI code review<br/>0 unaddressed P1/P2"]):::gate
  TeamPR(["team & bot PRs"]):::trig --> Review

  Review -- findings --> Shepherd(["PR shepherd<br/>fix loop · max 3"]):::agent
  Shepherd -- fix push --> Review
  Shepherd -. "3 rounds unresolved" .-> Stuck(["review-stuck<br/>human triage"]):::human
  Review -- clean + CI green --> Merge(["merge<br/>--match-head-commit"]):::done
  Steward(["pr-steward<br/>PR watchdog"]):::agent -- merge when green --> Merge
  Merge --> Sync(["ArgoCD sync<br/>to cluster"]):::done
  Reconcile(["reconcile<br/>every 15m"]):::orch -. projects state .-> Shepherd

  Skills(["platform skills catalog<br/>GitOps · served over MCP"]):::gate
  Skills -.-> SelfHeal
  Skills -.-> Investigator
  Skills -.-> Implementer
```

---

## Multi-Tenancy & Data

Each team gets an isolated namespace with its own quotas, network policy and
RBAC. Databases and secrets are provisioned into it on request — no shared
credentials, no hand-managed connection strings.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'lineColor': '#94a3b8', 'fontSize': '14px'}}}%%
graph LR
  classDef tenant fill:#eff6ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  classDef guard fill:#fef2f2,stroke:#ef4444,stroke-width:2px,color:#991b1b
  classDef data fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46

  Ingress(["Traefik ingress<br/>automated TLS · *.mctl.ai"]):::guard

  subgraph ns ["  Every tenant namespace — admins · labs · ovk  "]
    Svc(["tenant services"]):::tenant
    Quota(["ResourceQuota<br/>cpu · memory · pods · PVCs"]):::guard
    NetPol(["NetworkPolicy<br/>intra-namespace + egress flags"]):::guard
    RBAC(["RBAC<br/>owners · members"]):::guard
  end

  subgraph shared ["  Shared platform data  "]
    PG(["CloudNativePG · shared-pg<br/>a database per service"]):::data
    Pooler(["connection pooler"]):::data
    Backup(["scheduled backups"]):::data
    Vault(["Vault → External Secrets"]):::data
  end

  Ingress --> Svc
  Quota -.- Svc
  NetPol -.- Svc
  RBAC -.- Svc
  Vault -. secrets .-> Svc
  Svc -- provisioned on request --> PG
  PG --> Pooler
  PG --> Backup

  style ns fill:#f0f5ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  style shared fill:#f0fdf4,stroke:#10b981,stroke-width:2px,color:#065f46
```

---

## Interaction Modes

| Interface | Endpoint | Primary Use Case |
|-----------|----------|------------------|
| **AI / MCP** | `api.mctl.ai/mcp` | Natural language infrastructure management directly from your IDE or AI assistant. |
| **Portal** | `app.mctl.ai` | Visual GUI via Backstage for service catalogs, templated deployment, and tech docs. |
| **CLI** | `mctl deploy` | Traditional terminal-based workflows for fast, scriptable deployments. |
| **REST API** | `api.mctl.ai` | OpenAPI compliant backend for custom automation and integrations. |
| **GitOps** | `git push` | Advanced: Direct cluster delivery for DevOps/Platform engineers. |
| **Docs** | `docs.mctl.ai` | Guides, runbooks, and API reference. |

---

## The Open-Source Stack

We stand on the shoulders of giants. mctl orchestrates best-in-class open source tools:

* **Infrastructure:** K3s, Terraform, Traefik
* **Delivery:** ArgoCD, Argo Workflows, Argo Rollouts, Helm, GitHub Actions
* **Security:** HashiCorp Vault, External Secrets, cert-manager, Dex (OIDC)
* **Data:** CloudNativePG (PostgreSQL), Temporal, MinIO
* **Observability:** VictoriaMetrics, Grafana, AlertManager, Loki
* **Developer Experience:** Backstage, VitePress

---

## Current Status

mctl is open source. All platform repositories are public under Apache 2.0. Sign in with GitHub at [mctl.ai](https://mctl.ai) to self-provision a tenant on the hosted control plane — no invitation required.

<div align="center">
  <br />
  <a href="https://mctl.ai">mctl.ai</a> &nbsp;&bull;&nbsp; <a href="https://docs.mctl.ai">docs.mctl.ai</a> &nbsp;&bull;&nbsp; <a href="mailto:support@mctl.ai">support@mctl.ai</a>
</div>

<!-- SEO Tags: mcp, mcp-server, model-context-protocol, claude-connector, gemini-mcp, gemini-cli, kubernetes, k8s, gitops, argocd, platform-engineering, developer-portal, self-healing, ai-agent, mcp.so -->
