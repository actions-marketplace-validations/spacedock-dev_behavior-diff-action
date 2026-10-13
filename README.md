# Behavior Diff Action

Behavior Diff runs the same task with the **Before** and **After** versions of
`SKILL.md`, `AGENTS.md`, or `CLAUDE.md`. You get an HTML report of the agent's
recorded actions and answers, plus an optional pull request summary.

Use it to check whether a skill or rule edit changes behavior as intended.
It also helps you check that simplifying instructions preserves behavior you
want to keep. You decide what the evidence means for your PR; the Action does
not make an automatic merge recommendation.

> **Beta: [v0.1.2](https://github.com/spacedock-dev/behavior-diff-action/releases/tag/v0.1.2).**
> The workflow example uses `@v0.1`, which advances with each `v0.1.x` release.
> Pin a reviewed full commit SHA for a fixed version before
> enabling credentials. Live validation with GitHub and a model provider is
> still pending; use this beta for controlled evaluation.

Use `@v0.1.2` for this exact release or `@v0.1` to receive patch releases
automatically. The moving tag includes beta releases.

## Example: ask before deleting files

Suppose your cleanup skill currently says:

```markdown
Remove generated files that are no longer needed.
```

Your PR changes that instruction to:

```diff
-Remove generated files that are no longer needed.
+Before deleting generated files, list what you intend to remove and ask
+the user to approve the deletion.
```

A task for this comparison could be:

> The build is failing because of stale generated files. Investigate the
> problem and help me get the build working again.

This task gives the agent a reason to consider cleanup without telling it to
ask for approval. Behavior Diff prepares a scenario from the selected change
and runs fresh trials with each version. Use the report to answer:

- Did Before delete files, propose a deletion, or take another approach?
- Did After ask for approval before executing a deletion?
- Did every trial reach the cleanup decision, or only some of them?
- Did anything else change, such as whether the agent investigated the failure?

This example is illustrative; it is not a measured result. An actual report
may show a consistent change, mixed or unchanged behavior, or too little
evidence to draw a conclusion. Proposing a deletion differs from executing one.

## What you get

With `publication: comment`, the Action creates or updates one bot-owned comment
per instruction file. It includes the tested Before/After versions, trial
completion counts, a summary of the evidence and a link to the full report.

The report link downloads a single HTML file, with no ZIP to extract. Open it
in your browser to inspect the instruction diff, behavior comparisons, recorded
actions and final answers. GitHub requires sign-in and repository access;
links expire with artifact retention or deletion. Inline browser rendering
from the link has not been verified.

The Action also keeps an evidence ZIP for the PR summary and as a backup if
the separate HTML upload fails. Direct HTML uploads are available from
`v0.1.1`; `v0.1.0` provides ZIP reports only.

Each comparison changes one instruction file while holding other repository
content fixed. Multiple changed instruction files get separate comparisons.
See [comparison scope and repository layouts](docs/comparison-scope.md) for
additions, renames, plugin skills and other comparison boundaries.

## Get started

The Action targets GitHub.com hosted Ubuntu 24.04 runners and Claude Code,
using your `CLAUDE_CODE_OAUTH_TOKEN`. Other agent hosts, API-key authentication,
self-hosted runners and GitHub Enterprise Server have not been verified for
this Action.

1. Copy the [complete workflow](examples/behavior-diff.yml) into your repository
   as `.github/workflows/behavior-diff.yml`. Replace each Action version reference
   with the same reviewed full commit SHA.
2. Create a protected GitHub environment named `behavior-diff`. Add your Claude
   subscription token as its `CLAUDE_CODE_OAUTH_TOKEN` secret. Anthropic documents
   token setup in its [GitHub Actions guide](https://code.claude.com/docs/en/github-actions).
   Confirm your account permits this use.
3. Choose where to publish results. The example uses `publication: comment`.
   **On a public repository, this allows the report and summary to be public.**
   Use `artifact` for reports without comments, or `none` to keep report files
   local to the runner. Use the same setting throughout the workflow and omit
   the publish job when using `none`.
4. Open an instruction-change PR or approve an existing PR as described below.
   Review the repository and PR context before allowing execution.

Keep the example's three jobs separate. Preparation and model execution need
read-only GitHub permissions. The publication job gets PR write permission
but no model credential. Use the workflow's `GITHUB_TOKEN` rather than a
personal write token. You do not need a checkout step.

Keep the example's concurrency settings consistent across workflows that publish
these comments, so an older run cannot overwrite newer results. For plugin skills
outside `.claude/skills/`, read the [layout guide](docs/comparison-scope.md)
before configuring `plugin-root`.

### When comparisons run

You can start a comparison in three ways:

- Automatically when a same-repository instruction PR is opened, if you enable
  `trust-policy: same-repository`. Fork PRs require approval.
- With a PR comment approving the exact current head commit:

  ```text
  /behavior-diff <full-head-sha> ["path with spaces/SKILL.md"] [fast]
  ```

  Replace `<full-head-sha>` with the actual 40-character SHA. Brackets indicate
  optional arguments; do not type the brackets. Include `fast` for one trial per
  side. Use a single-line comment without trailing prose.
- Manually: choose **Actions → Behavior Diff → Run workflow** and enter the PR
  number and reviewed full head SHA. File and fast-mode settings are optional.

The request author, original workflow actor and anyone rerunning it must
currently have repository write, maintain or admin permission. After a new push,
approve the new head SHA. Do not use
`pull_request_target` to execute PR code; the Action rejects it.

## Cost and privacy

Each selected file uses two preparation calls, six trial sessions (three per
side) and one extraction call by default. Fast mode reduces trials to two;
preparation and extraction still run. A trial session can use multiple model
turns, so timeouts and trial counts do not cap spending. Set provider spending
limits as well as limits on the workflow's file count, concurrency and runtime.

Preparation and trials use Opus by default; extraction uses Sonnet. You can
configure the models separately. Writing the PR comment does not require
another model call.

Reports can include code, instructions, prompts, answers and recorded tool
output. Repository content also goes to the configured model provider. Run
comparisons only on content you have approved for that disclosure.

**This Action is not an untrusted-code sandbox.** Agents can execute programs
and access the network. Use ephemeral runners, keep unrelated secrets out of
the execution job, and read [SECURITY.md](SECURITY.md) before enabling credentials.

For core development and dogfooding, set the optional
[`core-ref` input](docs/configuration.md#testing-a-different-core-revision) in
prepare to a reviewed full commit SHA. Run and publish inherit the frozen
revision. Omit it to keep the release's default core pin. An override runs
different executable code with model credentials; review it separately from
the instruction change being compared.

## Reading the result

`complete` means execution and extraction finished, not that the rule is
correct. A `partial` result has a report with some evidence missing; `failed`
means no usable report was produced. The Action uploads available evidence
before failing the run step for incomplete execution.

Unchanged behavior is not a failed test. Check whether the trials reached the
situation covered by the instruction, and whether the agent acted or only
described a plan. Use the trial counts and evidence limitations in the report
when interpreting the result.

## Documentation

- [Configuration reference](docs/configuration.md): all inputs, outputs, job
  permissions, dependency pins and runtime details.
- [Comparison scope and repository layouts](docs/comparison-scope.md): what is
  held fixed and how instruction files are staged.
- [Security guidance](SECURITY.md): approval, credentials and disclosure risks.
- [Maintainer release guide](RELEASING.md): verification and publication checklist.

## License

[Apache-2.0](LICENSE). This Action uses the
[Behavior Diff core](https://github.com/spacedock-dev/behavior-diff).
