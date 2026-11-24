# Engineering standards index

This index points to established rules; their source documents remain authoritative.
It adds review provenance requested by the owner, not new coding conventions.
Use the current approved task scope when interpreting historical setup statements.

## Operational and safety contracts

| Source | Applicable rules |
| --- | --- |
| [AGENTS.md](../../AGENTS.md#python-execution) | Run Python and Python tools through the NILARIS Conda environment; prefer module invocation. |
| [Runtime interpreter and overlay](../../runtime/a1/README.md#interpreter-and-overlay) | Load the pinned overlay after Conda starts; use the documented dependency artifacts. |
| [Task services and credentials](../../runtime/a1/README.md#task-services-and-credentials) | Keep secrets private and out of logs/arguments/Git; isolate disposable test databases; preserve retained databases and volumes; use the assigned roles and loopback endpoints. |
| [Guarded runtime and retained state](../../runtime/a1/README.md#guarded-runtime-and-retained-state) | Reconcile ownership before renewal, retain lock identity, preserve monitoring/resource bounds, fail closed on stale monitoring, stop only owned services and verify shutdown. |
| [Command adapter](../../runtime/a1/README.md#admission-command-service-and-textual-client) | Private authenticated configuration, bounded requests/responses, truthful unknown outcomes and original-command recovery; verify the whole process group stops. |

Feature-specific acceptance rules belong to the applicable approved spec/contract,
not a universal style guide. For example, [Drive155 spec](../execution/drive157/spec.md)
and [contract](../execution/drive157/contract.md) define its exact source, authority,
pinning, accounting and disabled-by-default boundaries. Preserve the distinction
between implemented paths, demonstrated behavior and unverified live behavior.

## Engineering workflow

- [Scope and authorization](../../AGENTS.md): honor scoped approval and return for
  material changes not already authorized.
- [Learning workflow](learning-workflow.md): reuse settled decisions; distinguish
  evidence, assumptions and proposals; prepare reviewable material before a
  publication checkpoint. Its setup-phase instructions apply to that phase.
- [Domain guidance](domain.md): when exploring domain concepts or architecture,
  consult the glossary and relevant ADRs when present, use established terminology,
  and surface conflicts/material semantic decisions.
- [Issue tracker](issue-tracker.md) and [triage labels](triage-labels.md): consult
  for the corresponding authorized remote operations; an index is not authorization.
- [README](../../README.md) records POC scope and environment context. Historical
  execution records are evidence at their stated revision, not current inventories.

## Automated checks

[pyproject.toml](../../pyproject.toml) declares Python/package requirements and
pytest discovery/import/default-output settings. It does not configure a formatter,
linter, type checker or coverage threshold. Existing pytest and Node tests check
behavior; `git diff --check` checks whitespace. Run proportionate checks required
by the actual change/approved workflow and record results, rather than treating
configuration or an old pass as a newly executed check. No new tool gate is adopted.

## Advisory code-quality heuristics

[code-review skill](../../.agents/skills/code-review/SKILL.md#3-identify-the-standards-sources)
defines the Fowler smell baseline: Mysterious Name, Duplicated Code, Feature Envy,
Data Clumps, Primitive Obsession, Repeated Switches, Shotgun Surgery, Divergent
Change, Speculative Generality, Message Chains, Middle Man and Refused Bequest.
These are labeled judgment calls, not hard violations. Documented repository rules
override the baseline; skip findings already enforced by tooling. Keep Standards
and Spec findings separate, as the skill requires.

## Review record

For future reviews, include:

1. Exact comparison-base SHA, reviewed SHA and diff scope, including any WIP.
2. Standards-source manifest: each path, source revision (or content hash for WIP
   or external files), applicable rules and category from this index. Distinguish
   sources actually consulted from references not consulted or unavailable.
3. Applicable spec/contract pointers and whether domain guidance was relevant;
   record the reason and any glossary/ADR sources consulted.
4. Tool checks actually run and evidence, separately from declared configuration
   and retained earlier results. Label documented violations and advisory smells.
5. Final delivery revision and changes after the reviewed revision, explicitly
   identifying any unreviewed final changes, including documentation-only changes.

Record “none” where appropriate; do not imply a signoff covers a later revision.
