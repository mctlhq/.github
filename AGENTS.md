# Commit attribution

- Every commit ends with an `Agent:` trailer naming the tool that wrote it: `claude`, `codex`, `agy`, or `none` for a person working without an assistant. Merge commits and commits by a bot account are exempt. It sits in the last paragraph of the message, next to `Claude-Session` where that applies, and never replaces it. Do not use `Co-Authored-By`.
- A commit without the trailer counts as `unknown`, never as human. Older commits stay `unknown`; history is not rewritten.
- The trailer is self-declared. It records what the tool stated, so it is not a security or approval signal, and neither is the `agent:<name>` PR label derived from it by `.github/workflows/agent-trailer.yml` (mctlhq/.github#316).

# Active plans

- [Agent Commerce](docs/plans/agent-commerce.md): capability commerce foundation,
  live audit and acceptance rationale (root mctlhq/.github#173). The dependency
  graph is owned by `roadmap/epics/agent-commerce.yaml`, not by the plan.

# Spike issue creation rules

- Every spike issue MUST be created from the current `.github/ISSUE_TEMPLATE/spike.yml` on `main`. Before creating a spike through an API, connector, CLI, or agent, fetch the template first; do not rely on remembered or cached fields.
- Preserve the template title prefix and every required body field, including `Source`, `Question`, `Outcomes`, `Lifecycle`, `Related`, and `Why mctl`. Apply all labels declared by the template. Extra detail may follow the template block, but must not replace or remove required fields.
- Keep `Source:` as the canonical deduplication key. Check for an existing spike with the same source before creating a new one.
- Before handing a spike to `agents:intake` or starting DevLoop, re-read the created issue and verify its title, body, and labels against the current template. If it does not conform, fix the issue first and do not start the workflow.
- If the template changes, the latest version on `main` wins over these examples or any remembered convention.

# Spikes

- [Agent Governance and Decision Assurance](docs/spikes/agent-governance-decision-assurance.md):
  current-state audit report for mctlhq/.github#182 (carrier #199).
- [Security finding lifecycle](docs/spikes/security-finding-lifecycle.md):
  spike report and reference model for mctlhq/.github#209.
- [CVE advisory to affected-service PR](docs/spikes/cve-advisory-impact.md):
  spike report and reference model for mctlhq/.github#222.
- [MCP gateway G0](docs/spikes/mcp-gateway-g0.md): identity, per-tool policy and
  tenant isolation decision spike for mctlhq/.github#195, with a Go reference
  model in `docs/spikes/mcp-gateway-g0/`.
- [Agent UI: AG-UI 1.0](docs/spikes/agent-ui-ag-ui.md): Phase A evaluation of AG-UI
  as the northbound agent-to-user protocol, spike report for mctlhq/.github#258.
- [Edge config: Workers KV Instant vs Valkey](docs/spikes/edge-config-kv-instant.md):
  spike report for mctlhq/.github#272, proposed verdict KEEP_VALKEY.
- [Journey-level correlation: `mctl.intent.id`](docs/spikes/journey-intent-correlation.md):
  spike report for mctlhq/.github#306, proposed verdict REUSE_WORK_ITEM_ID.
- [Automation control plane](docs/spikes/automation-control-plane.md): decided
  EXTEND_MCTL_API for mctlhq/.github#318 (research PR #319); implementation
  planning epic #322 remains proposed, with a Go reference model and real
  Temporal dev-server proof in `docs/spikes/automation-control-plane/`.
- [Finance Assistant](docs/spikes/finance-assistant.md): read-only personal
  finance for the Assistant Catalog, spike report for mctlhq/.github#325,
  proposed verdict RESEARCH_ONLY_DEFER, with a stdlib Python reference model in
  `docs/spikes/finance-assistant/`.
- [Web platform unification](docs/spikes/web-platform-unification.md): decided
  STANDARDIZE_CONTRACTS_KEEP_RUNTIMES for mctlhq/.github#328; architecture-only
  owner verdict, with proposed adoption epic #330 and independently gated followups.
  Live HTTP/browser observations still have explicit unknown/not-run boundaries.
