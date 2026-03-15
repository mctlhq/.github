# MCTL Platform Operations

Skill for managing services on the mctl Kubernetes platform via MCP connector.

## Overview

mctl is a self-service Kubernetes platform. You interact with it through 30 MCP tools
(prefixed `mctl_*`). Every write operation triggers an Argo Workflow and produces a git commit.

**MCP Server URL:** `https://api.mctl.ai/mcp`

## Architecture

```
User prompt → Claude → MCP tool call → mctl-api → Argo Workflow → GitOps commit → ArgoCD → K8s
```

- **Builds:** GitHub Actions (docker build+push to ghcr.io/mctlhq/)
- **Config:** Helm values in git → ArgoCD auto-sync
- **Secrets:** HashiCorp Vault → ExternalSecrets → K8s Secrets
- **Domains:** auto `{team}-{service}.mctl.ai` + `.mctl.me` mirror
- **Logs:** Loki (query via `mctl_get_service_logs`)

## Quick Start

### Create workspace + deploy a service
```
mctl_whoami                    → check identity
mctl_create_tenant("my-team")  → provision workspace
mctl_deploy_service(
  action="onboard",
  team_name="my-team",
  component_name="hello-world",
  dockerfile_repo="user/hello-world",
  git_tag="v1.0.0"
)
→ Service at https://my-team-hello-world.mctl.ai
```

### Deploy OpenClaw (AI Gateway)
```
mctl_deploy_service(
  action="onboard",
  team_name="my-team",
  component_name="openclaw",
  dockerfile_repo="openclaw/openclaw",
  git_tag="main",
  service_template="openclaw"
)
→ Dashboard at https://my-team-openclaw.mctl.ai/#token={auto-generated}
```

The `openclaw` template pre-configures: 1Gi memory, 5min startup probe,
gateway config (LAN bind, token auth, trusted K8s proxies), Control UI enabled.

## Tool Reference

### Identity & Workspace
| Tool | Description |
|------|-------------|
| `mctl_whoami` | Your user ID, teams, admin status |
| `mctl_create_tenant(tenant_name)` | Create workspace (1 per user) |
| `mctl_get_tenant(name)` | Workspace details, members, quotas |
| `mctl_delete_tenant(tenant_name)` | ⚠️ Delete workspace permanently |

### Service Lifecycle
| Tool | Description |
|------|-------------|
| `mctl_deploy_service(action, team, component, repo, tag)` | Onboard / deploy / update-config |
| `mctl_get_service_status(team, service)` | Sync state + health |
| `mctl_get_service_config(team, service)` | Full config from GitOps |
| `mctl_get_service_logs(team, service, lines, since)` | Logs from Loki |
| `mctl_rollback_service(team, component, target_tag)` | Revert to previous tag |
| `mctl_scale_service(team, component, autoscaling_enabled)` | HPA configuration |
| `mctl_retire_service(team, component)` | ⚠️ Delete service permanently |

### Repository Management
| Tool | Description |
|------|-------------|
| `mctl_list_repos(team)` | Available repos |
| `mctl_sync_repos(team)` | Discover new repos from GitHub App |
| `mctl_grant_repo_access(team, repo)` | Get GitHub App install URL |

### Preview Environments
| Tool | Description |
|------|-------------|
| `mctl_create_preview(team, component, image_tag)` | Ephemeral env (24h TTL) |
| `mctl_list_previews(team)` | Active previews |
| `mctl_delete_preview(team, component, preview_id)` | Remove preview |

### Custom Domains
| Tool | Description |
|------|-------------|
| `mctl_add_custom_domain(team, service, domain)` | Add custom domain |
| `mctl_verify_domain(team, service)` | Check CNAME config |
| `mctl_list_domains(team)` | All domains + status |
| `mctl_remove_custom_domain(team, service, domain)` | Remove domain |

### Database
| Tool | Description |
|------|-------------|
| `mctl_provision_database(team, app)` | PostgreSQL on shared CNPG cluster |

Credentials auto-injected: `DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`.

### Monitoring & Audit
| Tool | Description |
|------|-------------|
| `mctl_get_resource_usage(team)` | CPU, memory, pods vs quotas |
| `mctl_get_workflow_status(workflow_name)` | Workflow progress + logs |
| `mctl_list_workflows(team)` | Recent workflow runs |
| `mctl_list_recent_operations` | Audit log (last 50) |
| `mctl_list_operations` | All available operations |
| `mctl_get_operation(name)` | Operation parameter schema |

## Deploy Actions Explained

| Action | When | What happens |
|--------|------|--------------|
| `onboard` | First deploy | Build image → create Helm manifests → commit to GitOps → ArgoCD sync |
| `deploy` | Version update | Rebuild image → update image tag → ArgoCD sync |
| `update-config` | Env/secret change | Update values.yaml only → ArgoCD sync (no rebuild) |

## Service Templates

| Template | Port | Memory | Special Config |
|----------|------|--------|----------------|
| `default` | 8080 | 256Mi | Standard HTTP service |
| `openclaw` | 18789 | 1Gi | Gateway config ConfigMap, 5min startup probe, `NODE_OPTIONS=--max-old-space-size=768` |

## Repo Access Patterns

1. **Org repos** (mctlhq/*) → automatic via GitHub App
2. **User public repos** → install GitHub App via `mctl_grant_repo_access` URL → `mctl_sync_repos`
3. **External public repos** (e.g. `openclaw/openclaw`) → deploy directly, no registration needed
4. **Private external repos** → store PAT in Vault: `secret/data/teams/{team}/{service}/repo-pat → {"pat": "ghp_..."}`

## Troubleshooting

### Service not starting
```
mctl_get_service_status → check if Synced/Healthy
mctl_get_service_logs(since="15m", lines="200") → look for errors
mctl_get_resource_usage → check quota headroom
```

### Build failed
```
mctl_get_workflow_status(workflow_name) → read build logs
```
Common causes: Dockerfile error, repo not accessible, out of memory during build.

### OOM / Restart loop
Check `mctl_get_service_logs` for "OOMKilled" or exit code 137.
Fix: redeploy with higher memory via `update-config` or use appropriate service template.

## Safety Rules

- **Always confirm** before calling `mctl_retire_service` or `mctl_delete_tenant` — these are irreversible
- **Track workflows**: every write op returns `workflow_name` — call `mctl_get_workflow_status` to report result
- **Team-scoped**: you can only access workspaces the user belongs to
- All operations produce git commits → full audit trail
