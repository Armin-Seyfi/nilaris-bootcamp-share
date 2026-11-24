# Triage labels

Map the skills' five canonical triage roles to these GitHub labels.

| Canonical role | GitHub label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate the issue |
| `needs-info` | `needs-info` | Waiting for more information |
| `ready-for-agent` | `ready-for-agent` | Fully specified and suitable for agent implementation |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be actioned |

When a skill refers to a triage role, use the corresponding GitHub label.

`ready-for-agent` describes implementation suitability. It does not bypass
the human checkpoints in `docs/agents/learning-workflow.md`.

Reuse existing labels, including `wontfix`. After setup approval, create
only missing labels from this mapping.

These labels describe triage status. Existing labels such as `bug` and
`enhancement` may remain alongside them.
