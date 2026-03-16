<div align="center">
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
        Manage infrastructure via REST API or natural language (MCP).
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
        Full-stack observability with Prometheus, Grafana, and Loki.
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
      <td width="100%" align="center">
        <img src="https://api.iconify.design/lucide:sparkles.svg?color=%2300f5ff" width="32" height="32" /><br><br>
        <strong>Autonomous AI Agent</strong><br>
        The mctl-agent automatically diagnoses cluster issues via AI and proposes remediation Pull Requests.
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
  classDef infra fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46

  subgraph interfaces ["  Interfaces  "]
    Portal(["Portal<br/>(Backstage)"]):::iface
    CLI(["mctl CLI"]):::iface
    AI(["AI clients<br/>(MCP)"]):::iface
    GitOps(["CI / CD<br/>git push"]):::iface
  end

  subgraph controlplane ["  Control Plane  "]
    API(["mctl-api<br/>REST + MCP"]):::ctrl
    Agent(["mctl-agent<br/>(self-heal)"]):::ctrl
  end

  subgraph infra_group ["  Infrastructure  "]
    Argo(["Argo Workflows<br/>deploy · provision"]):::infra
    Gitops(["mctl-gitops<br/>(Helm charts)"]):::infra
    K8s(["Kubernetes<br/>cluster"]):::infra
  end

  Portal -- REST --> API
  CLI -- REST --> API
  AI -- MCP --> API
  API -- run workflow --> Argo
  Argo -- git commit --> Gitops
  Gitops -- "sync (ArgoCD)" --> K8s
  GitOps -- git push --> Gitops
  Agent -- PR --> Gitops
  Agent -. poll .-> API
  K8s -. "AlertManager" .-> Agent

  style interfaces fill:#f0f5ff,stroke:#3b82f6,stroke-width:2px,color:#1e40af
  style controlplane fill:#fefce8,stroke:#f59e0b,stroke-width:2px,color:#92400e
  style infra_group fill:#f0fdf4,stroke:#10b981,stroke-width:2px,color:#065f46
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

---

## The Open-Source Stack

We stand on the shoulders of giants. mctl orchestrates best-in-class open source tools:

* **Infrastructure:** K3s, Terraform, Longhorn, Traefik
* **Delivery:** ArgoCD, Argo Workflows, Helm, GitHub Actions
* **Security:** HashiCorp Vault, External Secrets, cert-manager, Dex (OIDC)
* **Data:** CloudNativePG (PostgreSQL)
* **Observability:** Prometheus, Grafana, AlertManager, Loki
* **Developer Experience:** Backstage

---

## Current Status

mctl is currently in a private beta phase. Access to the repositories, documentation, and the hosted control plane is restricted to invited teams. 

<div align="center">
  <br />
  <a href="https://mctl.ai">mctl.ai</a> &nbsp;&bull;&nbsp; <a href="mailto:support@mctl.ai">support@mctl.ai</a>
</div>

<!-- SEO Tags: mcp, mcp-server, model-context-protocol, claude-connector, gemini-mcp, kubernetes, k8s, gitops, argocd, platform-engineering, developer-portal, self-healing, ai-agent, mcp.so -->
