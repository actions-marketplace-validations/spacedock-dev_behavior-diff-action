# Configuration reference

[Back to the README](../README.md) · [Comparison scope and repository layouts](comparison-scope.md)

## Three phases, three jobs


| Mode | Responsibility | Job permissions |
| --- | --- | --- |
| `prepare` | Authorize event, freeze head and merge-base, select targets | `contents: read`, `pull-requests: read` |
| `run` | Reauthorize, stage one target, draft purpose/task, invoke core, upload selected reports | `contents: read`, `pull-requests: read`; model credential only here |
| `publish` | Read the current invocation's artifacts; create/update bot-owned comments | `actions: read`, `pull-requests: write`, `contents: read`; no model credential |

Use the workflow's `GITHUB_TOKEN`, not a personal write token. Do not combine
these jobs. GitHub permissions and process isolation are job boundaries, not
Action-step boundaries. Never check out PR code in a privileged publication
job. No caller checkout is needed: the Action fetches exact commit objects in
private scratch and never persists checkout credentials.

Triggers, protected environments, job permissions, matrices, `max-parallel`,
concurrency and job timeouts belong to the caller workflow. Use the example's
shared per-repository/PR concurrency group with `cancel-in-progress: false` in
**every** workflow publishing these comments. GitHub comment updates have no
atomic compare-and-swap; without serialization, two publishers can race. The
publisher also suppresses stale heads and older run/attempt results.

## Inputs

| Input | Default | Contract |
| --- | --- | --- |
| `mode` | required | `prepare`, `run`, or `publish` |
| `github-token` | `github.token` | Read-only during preparation/execution; workflow token for publication |
| `selection` | empty | In run/publish, `toJSON(matrix)` from prepare's frozen `matrix`; never PR-supplied JSON |
| `pr` | event identity | Manual dispatch: canonical decimal number (no leading zeros); cannot override a different event PR |
| `approved-sha` | empty | Manual dispatch: current lowercase 40-character head SHA; comment approval comes from the command |
| `file` | all eligible | Exact changed instruction path; quote spaces in comment commands |
| `trust-policy` | `explicit` | `same-repository` additionally permits automatic same-repository PR events |
| `publication` | `none` | `none`, `artifact`, or `comment` (artifact plus PR summary) |
| `trials` | `3` | 1–3 trials per side |
| `fast` | `false` | One trial per side; comments obtain this from their `fast` argument |
| `max-files` | `3` | 1–10; exceeding it fails before spending model calls, never silently truncates |
| `model` | `opus` | Claude trial model; use an exact provider ID for model-version reproducibility |
| `extract-model` | `sonnet` | Claude evidence extraction model |
| `draft-model` | `opus` | Claude purpose/scenario drafting model |
| `plugin-root` | empty | Explicit plugin directory to relocate to `.claude`; collisions fail closed |
| `timeout-minutes` | `45` | 1–50, execution deadline; keep the job timeout longer for evidence upload |
| `retention-days` | `7` | 1–30, subject to GitHub repository retention policy |

Pass the same request/trial/trust inputs to prepare and run: execution checks the
request again. A moved head or base invalidates the frozen selection before
paid calls. Each matrix entry is bound to the repository, PR, head/base,
merge-base, target, trial count, workflow run ID and attempt.

### Outputs and status

- Prepare: `matrix` (`{"include":[...]}`), `count`, `head`, `pr`, `status`.
  `ready` means selected; `skipped` means an automatic PR has no eligible changes.
  An explicit request with no eligible change fails visibly.
- Run: `status` (`complete`, `partial`, `failed`), `report-directory`,
  `report-path`, `result-path`, `artifact-name`, and, when uploaded, `artifact-url`.
  Report paths are local to that job. `result.json` carries separate Before/After
  completion counts, extraction availability, runner exit and report availability.
- Publish: `status`, `report-status`, `comment-url`, `report-url`, `run-url`.
  `published` means the comment was written, **not** that evidence was complete;
  `stale`/`superseded` mean no older result replaced a newer one.

Core's `REVIEW` grade means a trial produced a final answer, not correctness;
`BLOCKED` means broken/incomplete evidence. `complete` is execution completeness,
not proof of the rule's effectiveness. A runner exit of zero alone is insufficient.
Incomplete execution fails the run step **after** available evidence is uploaded.
Behavioral disagreement itself does not fail it. Upload failure fails visibly.
A publisher can post an honest missing-artifact failure if execution was killed
before it could write results. Cancellation can prevent even that publication;
check the workflow run status.

Artifacts contain only `report.html`, `report.md`, and `result.json`. No raw
trial trees, environment dumps or credential files are intentionally uploaded.
Reports themselves contain source, prompts, answers and recorded evidence.
Artifacts are ZIP downloads behind GitHub's access rules, **not durable public
HTML hosting**. Download and open `report.html` locally. Links expire with
artifact retention or deletion. No Pages or gists are used.

The PR comment deterministically projects the core Markdown Summary; there is
no extra summarization model call. Counts, minority behavior, unchanged results
and limitations stay together. If the full summary cannot fit safely, it is
omitted with a report link rather than selectively truncated.

## Runtime, credentials and cost


The implementation targets **GitHub.com hosted `ubuntu-24.04` and Claude Code**.
macOS, Windows, self-hosted runners, GitHub Enterprise Server, other hosts,
API-key authentication, cloud providers and OIDC are not advertised as tested
Action configurations. Node 22.23.2 is selected for the pinned CLI's Node >=22
requirement; Ubuntu supplies Bash, Python 3, Git and jq.

| Dependency | Pin |
| --- | --- |
| Behavior Diff core | v0.4.5, `415aa72db6d3c764cdca32a642d21d1d034f2cc8` |
| Claude Code | `@anthropic-ai/claude-code@2.1.296` |
| upload-artifact | v4.6.2, immutable SHA in `action.yml` |
| setup-node | v4.4.0, immutable SHA in `action.yml` |

Action releases and core versions are separate. Core is fetched by immutable
commit, never mutable `main`; the Action reuses its validator, trial launcher,
extractor and renderer rather than maintaining a second comparison engine.
The repository is Apache-2.0 licensed. The fetched core retains its own license
and notices. No internal PoC content or reports are bundled as examples.

The internal PoC verified Claude subscription-token authentication. Anthropic's
[current GitHub Actions guidance](https://code.claude.com/docs/en/github-actions)
also documents subscription tokens generated with `claude setup-token`. This
is not a guarantee about every account, plan, token lifetime or future provider
policy. Confirm your account's automation permission and data/cost policy;
use caller-owned credentials. Do not reuse a developer's organization secret
name or assume the token is a team-wide service credential. The same guidance
recommends API keys for organization-wide shared credentials; API-key support
is not claimed by this Action implementation.

For each target, default execution makes two preparation calls, six concurrent
trial sessions, and one extraction call. Fast mode reduces trial sessions to
two; it does not skip preparation or extraction. There are no wrapper-paid
retries. A session can itself use multiple model turns. The example serializes
targets with `max-parallel: 1`; increasing it multiplies concurrent sessions.
Timeouts and trial counts are bounds on orchestration, **not a dollar cap**.
Use provider spending limits and a reviewed workflow. Do not enable automatic
runs on every push without considering the cost.

