# Behavior Diff Action

Compare recorded Claude behavior before and after a pull request changes
`SKILL.md`, `AGENTS.md`, or `CLAUDE.md`. Each comparison changes **one instruction
file**. The HTML report contains the evidence; the Action does not recommend
merging or turn behavior differences into a pass/fail verdict.

**Unreleased.** Implementation is being prepared for GitHub Marketplace. The
`@v1` examples become usable after a release; pin the audited release's full
commit SHA before enabling credentials. A Marketplace listing and a live
Action smoke are separate release gates, not implied by this repository.

## How to use it

Use the [complete caller workflow](examples/behavior-diff.yml). It covers:

- Automatic runs on newly opened, same-repository instruction PRs, by explicit
  trust-policy opt-in. Synchronize/reopened are supported if you add those events.
- A maintainer's exact-SHA comment approval, including fork PRs:
  `/behavior-diff <full-head-sha> ["path with spaces/SKILL.md"] [fast]`.
- Manual dispatch with PR number, the reviewed full head SHA, and optional file
  and fast mode.

The command is parsed as a whole, single-line command. No trailing prose,
unsupported flags, abbreviated SHAs, or inherited approval after a push. Authors
and original/rerun actors must currently have repository write, maintain, or
admin permission. Association labels in the example are only a cheap trigger
filter; the Action checks permissions through GitHub.

Create a protected `behavior-diff` environment and place the caller's
`CLAUDE_CODE_OAUTH_TOKEN` secret there. Scope the credential to the `run` step,
not workflow-wide. Review the entire tested repository and PR context before
approving: approval permits code execution, provider disclosure, and model cost.
Do not use production credentials or sensitive fixtures.

The example deliberately sets `publication: comment`. **In a public repository,
this permits public disclosure of the report and PR summary.** Choose `artifact`
for reports without comments, or `none` for local report files only. Keep the
same publication choice across the three phases. With `none`, omit the publish
job; local outputs last only as long as that runner.

### Three phases, three jobs

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

## Comparison scope and repository layouts

Before starts at the PR merge-base. After changes only the selected head file;
other PR changes, including referenced files, are held fixed at the base.
Multiple targets get independent comparisons, not a combined PR experiment.
Git renames use the old path's bytes as Before and remove the stale original
location. This normalizes layout for comparison; it does not test relocation
behavior as a second independent change.

Cross-directory skill renames must move the whole skill directory to a location
absent at the merge-base. Partial moves or existing destination layouts fail
before drafting rather than leaving relative references broken. Pure renames
without a content edit and empty-file additions have no instruction-content
difference and fail before model calls.

For a new file, Before is an **empty regular file, not true absence**. Do not use
this boundary to conclude how an agent behaves when a file is missing.
Deletions are not comparisons. Symlink targets, unsafe paths and unsupported
layouts fail rather than being silently transformed.
Repository attributes that omit or rewrite the Before target during
`git archive` also fail before model calls.

Project skills already in `.claude/skills/` remain there. For a plugin skill
such as `plugin/skills/review/SKILL.md`, set `plugin-root: plugin` explicitly.
The base plugin's skills, agents and references move together to `.claude`;
the original plugin directory is removed in both snapshots so agents cannot
read a stale competing copy. An existing `.claude` is a collision, not a merge
policy. Relative references within that plugin remain relative; dependencies
outside the plugin and changes to sibling files are not automatically tested.
The Action does not invent a core `--plugin-dir` trial flag.

Claude does not natively discover every `AGENTS.md` or arbitrary skill location.
Scenario preparation must make the target available to the trial without
revealing expected behavior. Read the report to verify whether the trial
actually reached and read the instruction; visibility is not proof of use.

Purpose is frozen in a separate, tool-free model call before drafting the
scenario. It is validated by core's `purpose.py`; invalid purpose fails closed
without trial spending. The scenario drafting call never receives that purpose
file. Purpose/task scratch is outside the comparison repository. `null` purpose
is allowed and core labels diff-derived intent as inference. PR text is context,
not trustworthy evidence of author history or a higher-priority instruction.

## Runtime, credentials and cost

The implementation targets **GitHub.com hosted `ubuntu-24.04` and Claude Code**.
macOS, Windows, self-hosted runners, GitHub Enterprise Server, other hosts,
API-key authentication, cloud providers and OIDC are not advertised as tested
Action configurations. Node 22.23.2 is selected for the pinned CLI's Node >=22
requirement; Ubuntu supplies Bash, Python 3, Git and jq.

| Dependency | Pin |
| --- | --- |
| Behavior Diff core | v0.4.4, `90a3f346b8a78be2360b43522c518f76e33ddfe4` |
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

## Security and release readiness

Read [SECURITY.md](SECURITY.md) before enabling model credentials. Separate
copies, a tool allowlist and stripped child environments are not a sandbox.
Approving a repository grants its instructions and executable content access
to an ephemeral runner with model credentials and network access.

Run the secret-free regression suite with:

```sh
python3 -m unittest discover -s tests -v
```

Normal CI uses synthetic fixtures only and never calls a paid model. See
[RELEASING.md](RELEASING.md) for the controlled live-smoke and Marketplace gates.
Do not equate a local synthetic run, a tagged release, or root `action.yml` with
verified Marketplace publication.
