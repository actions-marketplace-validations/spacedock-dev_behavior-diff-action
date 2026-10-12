# Comparison scope and repository layouts

[Back to the README](../README.md) · [Configuration reference](configuration.md)

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

