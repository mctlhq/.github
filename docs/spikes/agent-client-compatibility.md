# Agent-client compatibility with api.mctl.ai/mcp (draft)

Status: **Draft, slice S1a of mctlhq/.github#290 (carved out in #340).**
Verdict: **pending live run (#290)**. This draft proposes no outcome.

Spike manifest: `roadmap/spikes/agent-client-compatibility.yaml` (unchanged).
Reference probe and tests: `docs/spikes/agent-client-compatibility/`.

## Question

How many of the twelve coding agents listed at
<https://developers.cloudflare.com/agent-setup/> can connect to
`https://api.mctl.ai/mcp`, list the platform skills and read one, and at which
step do the others fail.

## Non-claims

- No agent has been run. Nothing here says that any real client connects, or
  fails to connect, to api.mctl.ai.
- No authenticated call was made, and no client was registered
  (`--register` was off). Steps 1d, 2, 3 and 4 are untested.
- `[DOC]` cells come from the vendor page linked from the Cloudflare index and
  are what that page states, not what the product does today.
- `unknown` is never evidence of absence.
- The token-less run covers step 1a and 1b only as observation. For 1c it
  checked advertised metadata (registration endpoint present, CIMD
  advertised) and sent no registration request.

## Evidence tags

- `[DOC]`: stated on the vendor page named in `agents.json`, quoted there (accessed 2026-11-13).
- `[OBSERVED date, probe commit]`: produced by the committed probe against
  `https://api.mctl.ai/mcp`, token-less, steps 1a-1c only.
- `UNTESTED`: with the reason in the cell.

## Method

Step ladder, stopping the main ladder at the first failing step:

| Step | Action | Pass predicate |
| --- | --- | --- |
| 1a transport | POST `initialize` | 401 with `WWW-Authenticate` carrying `resource_metadata` |
| 1b discovery | GET protected-resource and authorization-server metadata | fields the profile auth needs are present |
| 1c registration | DCR POST, only with `--register` | 201 with `client_id` |
| 1d token | `initialize` with `MCTL_PROBE_TOKEN` as bearer | 200 with `serverInfo` |
| 2 list | `tools/call mctl_list_platform_skills` | non-empty list |
| 3 read | `tools/call mctl_read_platform_skill` on the first skill | non-empty body |
| 4 prompt | `prompts/list`, `prompts/get platform-skill` | prompt with content; reported separately |

Profiles are in `agents.json`: one entry per agent, a `doc_url` per field, and
`unknown` wherever the page is silent. A profile whose auth method is
`unknown` is not probed and is reported `UNTESTED`. `oauth-unspecified` means
the page says OAuth but not DCR or CIMD; its 1b passes if either is advertised.
The token is read from `MCTL_PROBE_TOKEN` only, there is no flag for it, and
all output is redacted. Offline tests (`python3 -I -m unittest discover -s
docs/spikes/agent-client-compatibility/tests -v`) run the probe against
loopback fakes for the red cases `wrong_path`, `bad_bearer`, `no_dcr`,
`empty_skills` and `no_prompt`.

## Token-less discovery run

Command, 2026-11-13, probe commit `4f0a5ee`, no token, no `--register`:

```
python3 docs/spikes/agent-client-compatibility/probe.py --endpoint https://api.mctl.ai/mcp --markdown
```

Result: all eleven profiles with a documented auth method passed 1a (401
with a `resource_metadata` challenge) and 1b (protected-resource and
authorization-server metadata carry what the profile needs: a registration
endpoint for DCR, `client_id_metadata_document_supported` for CIMD). Bionic
was not probed. Steps 1c-4 were not run (`--register` off, no token). [OBSERVED 2026-11-13, probe 4f0a5ee]

Side observation from the same session: with the default `Python-urllib`
User-Agent every request got Cloudflare's 403 error 1010 ("browser signature
banned") before reaching mctl; the probe now sends its own agent string. None
of the twelve clients is documented to use that agent, so this is recorded
as a property of the api.mctl.ai edge, not as a client failure. [OBSERVED 2026-11-13, probe 4f0a5ee]

Profile sources: every value in `agents.json` carries the vendor page and
the sentence it was taken from (`quote`), accessed 2026-11-13. Values the
page does not state are `unknown`. Earlier drafts took values from the
Cloudflare index only, which left eight profiles unprobed.

## Matrix

| agent | version | transport | auth method supported | step reached | failing error verbatim | what mctl would have to change | step 4 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Codex | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Cursor | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Devin | unknown | streamable-http [DOC] | OAuth, registration method not stated [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Droid | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| GitHub Copilot | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| OpenCode | unknown | streamable-http [DOC] | OAuth with a client ID metadata document [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c n/a: CIMD needs no registration call; `client_id_metadata_document_supported` advertised [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Pi | unknown | streamable-http [DOC] | OAuth with a client ID metadata document [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c n/a: CIMD needs no registration call; `client_id_metadata_document_supported` advertised [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Vibe | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Visual Studio Code | unknown | streamable-http [DOC] | OAuth with dynamic client registration [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Command Code | unknown | streamable-http [DOC] | OAuth, registration method not stated [DOC] | 1b [OBSERVED 2026-11-13, probe 4f0a5ee]; 1c metadata only (registration endpoint advertised, no POST sent) [OBSERVED 2026-11-13, probe 4f0a5ee]; 1d-3 UNTESTED: no token (operator step on #290) | none: no failing step observed | none observed | UNTESTED: no token |
| Bionic | unknown | unknown | unknown | UNTESTED: LM Studio's Bionic documentation does not cover MCP at all; only Cloudflare's page claims OAuth support | n/a | n/a | UNTESTED: not probed |

## Verdict

pending live run (#290)

## Remaining on #290

- Obtain a tenant-user token and run the probe with `MCTL_PROBE_TOKEN` set
  (steps 1d-4), and with `--register` if the owner approves a registration.
- Hand checks in real Claude Code, Codex and Cursor clients.
- Compute the verdict with `verdict()` and confirm its thresholds.
