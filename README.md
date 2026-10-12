# Behavior Diff Action

See how an instruction change affects your coding agent before you merge it.
Behavior Diff runs the same task with the **Before** and **After** versions of
`SKILL.md`, `AGENTS.md`, or `CLAUDE.md`, then gives reviewers an HTML evidence
report and an optional pull request summary.

Use it when updating a skill, tightening a rule, or simplifying instructions
and checking whether the behavior you care about survives. You review the
recorded actions and answers; the Action does not make an automatic merge
recommendation.

> **Pre-release:** no `v1` Action release is available yet. The workflow example
> uses `@v1` for the planned release. Replace it with a reviewed commit SHA to
> try the Action before release. Live GitHub/model validation is still pending.

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

A useful scenario would be:

> The build is failing because of stale generated files. Investigate the
> problem and help me get the build working again.

The task gives the agent a reason to consider cleanup without telling it to
ask for approval. Behavior Diff prepares a scenario from the selected change
and runs fresh trials on both versions. In the report, you can check:

- Did Before delete files, propose a deletion, or take another approach?
- Did After ask for approval before executing a deletion?
- Did every trial reach the cleanup decision, or only some of them?
- Did anything else change, such as whether the agent investigated the failure?

**This is an illustrative case, not a measured result.** The report may show a
consistent change, mixed behavior, unchanged behavior, or insufficient evidence.
A proposed deletion and an executed deletion are different findings.

## What you get

With `publication: comment`, the Action creates or updates one bot-owned comment
per instruction file. It includes the tested Before/After versions, trial
completion counts, a summary of the evidence and a link to the full report.

Download the report artifact and open `report.html` to inspect the instruction
diff, behavior comparisons, recorded actions and final answers. Artifacts are
ZIP downloads subject to GitHub access rules and retention, not hosted HTML
pages.

Each comparison changes one instruction file while holding other repository
content fixed. Multiple changed instruction files get separate comparisons.
See [comparison scope and repository layouts](docs/comparison-scope.md) for
additions, renames, plugin skills and other comparison boundaries.

## Get started

The Action targets GitHub.com hosted **Ubuntu 24.04** runners and **Claude Code**.
It uses a caller-provided `CLAUDE_CODE_OAUTH_TOKEN`. Other agent hosts, API-key
authentication, self-hosted runners and GitHub Enterprise Server are not
advertised as tested configurations.

1. Copy the [complete workflow](examples/behavior-diff.yml) into your repository
   as `.github/workflows/behavior-diff.yml`. Replace each Action version reference
   with the same reviewed full commit SHA.
2. Create a protected GitHub environment named `behavior-diff`. Add your Claude
   subscription token as its `CLAUDE_CODE_OAUTH_TOKEN` secret. Anthropic documents
   token setup in its [GitHub Actions guide](https://code.claude.com/docs/en/github-actions).
   Confirm your account permits this use.
3. Choose where results may be published. The example uses `publication: comment`.
   **In a public repository, this permits public disclosure of the report and
   summary.** Use `artifact` for reports without comments, or `none` for local
   report files only. Keep this choice consistent across the workflow; omit the
   publish job when using `none`.
4. Open an instruction-change PR, or approve an existing PR using one of the
   methods below. Review the repository and PR context before allowing execution.

Keep the example's three jobs separate. Preparation and model execution use
read-only GitHub permissions; only publication gets PR write permission, and
that job receives no model credential. Use the workflow's `GITHUB_TOKEN`, not a
personal write token. No checkout step is needed.

Preserve the example's concurrency settings across workflows publishing these
comments so older runs cannot race to replace newer results. For plugin skills
outside `.claude/skills/`, read the [layout guide](docs/comparison-scope.md)
before configuring `plugin-root`.

### When comparisons run

The example workflow supports:

- **Automatic:** newly opened, same-repository instruction PRs, using the explicit
  `trust-policy: same-repository` opt-in. Fork PRs require approval.
- **PR comment:** a maintainer approves the exact current head commit:

  ```text
  /behavior-diff <full-head-sha> ["path with spaces/SKILL.md"] [fast]
  ```

  Replace `<full-head-sha>` with the actual 40-character SHA. Brackets indicate
  optional arguments; do not type the brackets. Include `fast` for one trial per
  side. Use a single-line comment without trailing prose.
- **Manual:** choose **Actions → Behavior Diff → Run workflow**, then provide the
  PR number, reviewed full head SHA and optional file/fast settings.

Request authors and original/rerun actors must currently have repository write,
maintain or admin permission. A new push needs a new SHA approval. Do not use
`pull_request_target` to execute PR code; the Action rejects it.

## Cost and privacy

By default, each selected file uses two preparation calls, six trial sessions
(three per side) and one extraction call. Fast mode reduces trials to two but
keeps preparation and extraction. Trial sessions can use multiple model turns;
timeouts and trial counts are **not a dollar cap**. Set provider spending limits
and keep the workflow's file count, concurrency and runtime limits appropriate
for your repository.

The default models are Opus for preparation/trials and Sonnet for extraction.
You can configure these separately. There is no additional model call to write
the PR comment.

Reports can contain code, instructions, prompts, answers and captured tool
returns. Repository content also goes to the configured model provider. Only
run comparisons on content you have approved for that disclosure.

**This Action is not an untrusted-code sandbox.** Agents can execute programs
and access the network. Use ephemeral runners, keep unrelated secrets out of
the execution job, and read [SECURITY.md](SECURITY.md) before enabling credentials.

## Reading the result

`complete` means execution and extraction completed; it does not mean the rule
is correct. `partial` means a report is available but some evidence is missing.
`failed` means no usable report was produced. Available evidence is uploaded
before incomplete execution fails the run step.

Unchanged behavior is not a failed test. Check whether the trials reached the
situation the instruction governs, and distinguish stated plans from completed
actions. The report preserves trial counts and evidence limitations so you can
make that judgment.

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
