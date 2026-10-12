# Release gates

The destination is `spacedock-dev/behavior-diff-action`, a public repository.
Creating implementation files does not publish an Action. Do not publish
private PoC content, real report fixtures or credentials in this repository.

## Implementation verification recorded

The local implementation session verified:

- 71 deterministic tests passing in an Ubuntu 24.04 ARM64 container. These cover
  authorization, staging, purpose isolation, partial evidence, timeout cleanup,
  archive validation and publication ownership/freshness. This container is not
  a GitHub-hosted workflow run.
- Python compilation; root Action metadata against the GitHub Action JSON
  schema; both CI and the caller example with Actionlint 1.7.7.
- Actual installation/version discovery for Claude Code 2.1.296 and fetching
  the exact core v0.4.4 commit on the local macOS host. The installation smoke
  caught and fixed npm's rejection of a shared user/global config-file path.
- Execution of the actual pinned core runner, purpose validator and renderer
  through the Action orchestration, using synthetic responses instead of paid
  model calls. Both trials completed; deliberately unavailable extraction
  correctly produced `partial`, a usable report and explicit evidence limits.
- Browser inspection of the generated HTML Summary and retained Before/After
  trial evidence; the real core report passed artifact/result validation and
  deterministic comment projection.
- Independent request/execution and publication-security reviews, with material
  findings fixed and re-reviewed to approval.

The v0.4.5 dependency update pins core to
`415aa72db6d3c764cdca32a642d21d1d034f2cc8`. Its runner, purpose validator,
extractor interface and Markdown renderer are unchanged from v0.4.4; the
release changes HTML Summary presentation. A local smoke through the actual
v0.4.5 runner/validator/renderer completed both synthetic trials, retained the
report with the expected `partial` status when extraction was unavailable,
and passed the Action's four-section Summary projection. No paid model calls
were made for this check.

No live model authentication, GitHub artifact access, real comment upsert,
candidate push, release or Marketplace publication has been verified here.
Those still require the approved live smoke below.


## Before a release

1. Run `python3 -m unittest discover -s tests -v` and compile the Python files.
   Validate `action.yml`, CI and the complete caller example. Normal CI must
   remain deterministic and secret-free.
2. Complete an independent correctness/security review against `HANDOFF.md` and
   `SECURITY.md`; fix material findings and have those fixes reviewed.
3. Recheck the core commit and CLI dependency availability. Changes to core or
   Claude pins require the corresponding contract and integration checks; do
   not float the core revision to `main`.
4. Obtain explicit approval for a controlled live smoke: named synthetic test
   repository/PR, exact reviewed head, model credential/provider, disclosure
   choice, maximum trials, concurrent jobs and provider spending limit. Do not
   use the internal verified PoC just to reconfirm its result.
5. Commit with DCO sign-off. Publish an immutable candidate SHA only when
   authorized, then use that SHA in the test repository's three-job workflow.

## Controlled Action smoke

This check calls models and writes artifacts/comments. It is not part of CI.
Use an isolated public synthetic repository or a private repository with an
explicit reviewed publication policy. Use the least-privilege workflow token
and a short-lived/revocable caller-owned model credential.

- Make one synthetic instruction change. Approve the exact head SHA with a
  manual dispatch using fast mode. Confirm the report's Before merge-base,
  After head, selected file, two trial sessions, task and purpose separation.
- Download the actual GitHub artifact with a permitted account; open its HTML
  report, inspect the Summary and trial evidence. Confirm no raw project tree
  was uploaded. Test that repository/artifact access and retention match policy.
- Rerun the same head. Verify the same bot-owned PR comment is updated, not
  duplicated, and the run attempt/report links change.
- Push another commit and attempt to reuse the previous approval. It must fail
  before model execution. Approve the new SHA explicitly.
- Exercise an approved fork, an unauthorized command and a malformed command.
  Denials must make no paid calls. Same-repository automatic runs require the
  documented opt-in; never add `pull_request_target`.
- Exercise plugin relocation on a synthetic plugin with a sibling reference,
  an addition and a rename. Confirm only the intended instruction differs and
  no stale copy is read. Existing `.claude` collisions must fail.
- Record a partial/failed run and an artifact failure. Verify honest status and
  preserved available evidence, not an automatic behavior verdict.
- Cancel a controlled run; verify workflow status and inspect whether any
  partial evidence/comment was possible. Do not claim cancellation guarantees
  a report or halts provider billing immediately.

Keep evidence URLs, tested candidate/core/CLI SHAs and approval details in the
release review. Synthetic local tests cannot establish provider authentication,
real artifact access or GitHub comment updates. Those remain unverified until
this live check runs.

## Marketplace publication

After the live smoke and review pass, an authorized owner must:

1. Confirm the public repository contains the root `action.yml`, license,
   implementation, consumer documentation and necessary tests/workflows.
   Review or omit `HANDOFF.md` before publication: it contains internal source
   provenance and local checkout links, not a public consumer example.
2. Confirm the proposed name **Behavior Diff for Agent Instructions** is unique
   in Marketplace. Name availability has not been reserved by this code.
3. Accept the Marketplace Developer Agreement and satisfy GitHub's two-factor
   authentication requirement. These account/legal steps cannot be inferred
   from repository access or an API authentication status.
4. Create a signed-off release commit and version tag, then explicitly select
   **Publish this Action to the GitHub Marketplace** in the GitHub release flow.
   Suggested primary category: Testing. Validate the metadata in that flow.
5. Update the moving `v1` major tag only to the reviewed release commit under
   the maintainer's release policy. Prefer full SHA pins in consumer workflows.
   Remove the README's unreleased notice and record actual smoke evidence only
   after it exists. Do not invent a release SHA for pre-release examples.

Official references:

- [Publishing actions in GitHub Marketplace](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace)
- [Action metadata syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax)
- [Claude Code GitHub Actions authentication](https://code.claude.com/docs/en/github-actions)

No tag push, release, Marketplace publication, agreement acceptance, or paid
model run is authorized merely by completing these preparation steps.
