# Security Policy — MetaSearch

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's **Security Advisories → "Report a vulnerability"**
on this repository (Private vulnerability reporting), or contact the
maintainer directly through the repository owner profile.

Include: affected component (`api/app/...` path), a minimal reproduction
(request/response or PoC), impact assessment, and affected versions.

## Response SLA

| Severity | First response | Target fix |
|---|---|---|
| Critical (RCE, auth bypass, secret leak) | ≤ 48 h | ≤ 7 days |
| High (BOLA/IDOR, SSRF, injection) | ≤ 72 h | ≤ 14 days |
| Medium / Low | ≤ 7 days | Best effort next release |

## Supported Versions

Only the latest tagged release receives security fixes (this is a
fast-moving research/engineering repo; see CHANGELOG.md).

## Security Architecture Invariants (for reviewers)

- **Zero-trust tenancy**: every user-data table (`queries`, `watchlist`,
  `key_boosts`, `memory_items`, `spaces`, `kg_*`) is owner-scoped by
  `api_key_id`; routes returning shared resources are `require_admin` /
  `require_operator` gated (enforced by `api/tests/test_w5_guardrails.py`).
- **SSRF defense**: every user-supplied URL passes
  `app/extraction/ssrf.resolve_and_check` + `app/extraction/pinning` with
  per-hop redirect re-checks and a 5 MB response cap.
- **Prompt-injection defense (SEC-07)**: untrusted web content is wrapped in
  per-request random delimiters (`app/llm/prompts.render_context`,
  `orchestration/research._render_pack`) before entering any LLM prompt.
- **No fabricated data**: runtime never invents financial numbers, dates, or
  sources; missing data is returned as `null`/empty with provenance.
- **Local runtime data** (`api/*.db`, `api/.env.*`, `*.retention`) is never
  committed and never deleted by tooling without owner confirmation.
- **Secrets**: `api/.env.*` and SQLite DBs are gitignored + pre-commit
  guarded; CI runs gitleaks; dependency audit runs via `pip-audit` and
  `npm audit`.
