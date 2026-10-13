# Security boundary

This Action executes agent-selected commands against repository content. It is
for code and context a maintainer has reviewed and accepted for model-backed
execution. **It is not an untrusted-PR sandbox.**

## Before enabling credentials

- Use GitHub-hosted ephemeral Ubuntu 24.04 runners. No persistent/self-hosted
  runner support is claimed. Discard the entire runner after the job.
- Use three separate jobs as in `examples/behavior-diff.yml`. Execution has only
  read-capable GitHub permissions and the model token. Publication has a write
  token but never runs repository code or a model. Do not grant OIDC, deployment,
  package-write or unrelated secrets to the execution job.
- Protect the workflow and the Action pin with code review. No Action can
  enforce job permissions against a caller who changes the workflow itself.
- Keep model credentials in a protected environment. Forks require a writer's
  approval of the exact current head SHA by comment or manual dispatch. A new
  push requires a new approval. Same-repository opt-in is an authorization
  policy, not proof that the repository is safe.
- A `core-ref` override selects executable code from the core repository. Review
  that exact SHA before exposing model credentials; comparison-head approval
  does not separately approve the selected core. Protect this input with the
  workflow and environment, and do not automatically trust arbitrary PR heads.
  SHA validation and checkout verification establish identity, not safety.
- Never use `pull_request_target` to execute PR code; the Action rejects it.
  If adding your own checkout steps, set `persist-credentials: false` and do not
  check out PR code in preparation/publication jobs.
- Decide disclosure policy before execution. Reports and summaries may contain
  repository code, instructions, tasks, answers and captured tool returns.
  Public-repository artifacts/comments are not a private channel. Report text
  is untrusted data; do not execute commands copied from it without review.
- Limit provider spending, target count, trials, concurrent jobs, retention and
  runtime. Revocation/cancellation can leave partial evidence and may not stop
  provider-side billing immediately.

## What the implementation does

Request authorization checks live repository write permission for the author
and original/rerun actors. Exact SHA and repository identities are frozen and
rechecked before trials. Git uses NUL-delimited names for spaces and renames;
paths, regular-file modes and selection membership are validated. No event
text is interpolated into executable shell source. Fetch authentication is
transient, not persisted in `.git/config`.

Child environments use an allowlist and do not intentionally receive GitHub
tokens, Actions command-file paths, artifact tokens or unrelated inherited
secrets. Purpose/task files and isolated model homes live in mode-0700 private
scratch outside the variant projects; files are mode 0600. Evidence collection
uses a fixed allowlist, and private working material is removed after execution.
The published result is separately validated before it can affect a comment.

These measures prevent accidental inheritance and common path/injection bugs.
They **do not isolate hostile code from the same operating-system user**.
An agent can execute shell programs, use the network, inspect same-user process
state or reach files outside its project. Repository hooks/configuration,
instructions and PR text may contain prompt injection. Purpose kept outside a
project is not protected from a malicious shell command. Artifact/runtime and
model credentials on the runner remain at risk. Only approve content you are
willing to expose to this environment, and never treat a report as attestation.

Publication accepts only the current run/attempt/target's artifact, checks its
bounded data rather than extracting arbitrary ZIP paths, and writes only its
own `github-actions[bot]` comment. It rejects stale heads and suppresses newer
run overwrites. All publishers must still share the caller's serialized per-PR
concurrency group: GitHub's comment API cannot make the read/compare/update
sequence atomic.

## Reporting a vulnerability

Do not put credentials, private code or exploit-bearing reports into a public
issue. Contact the repository maintainers through an available private GitHub
security-reporting channel. If that channel is not enabled, request a private
contact without including sensitive details. No response-time guarantee is
claimed before the first release.
