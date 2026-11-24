# Installed skills provenance

## Verified source

- Repository: https://github.com/mattpocock/skills
- Content-matching upstream revision: `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`.
- Immutable source: https://github.com/mattpocock/skills/tree/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d
- Downloaded archive: https://api.github.com/repos/mattpocock/skills/tarball/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d

The candidate was resolved from upstream HEAD, then fetched by its immutable
commit SHA into a temporary directory outside this repository. Every installed
skill matched this revision. This is a content-matching upstream revision;
it is not a proven installation-time revision.

## Installation record

- Installer version: `1.7.6`, supplied by the human for this record.
- Observed installation method: repository-local skill files under
  `.agents/skills/`, with source metadata in `skills-lock.json`.
- Original installer invocation, options, download transport, and installation
  timestamp: unknown.
- Installation-time upstream revision: unknown.
- Independent evidence of the installer version used at installation: unknown.

No installer was run during verification. Skills and `skills-lock.json` were
neither updated nor reinstalled.

## Comparison method and results

A Python script read all 27 entries in `skills-lock.json`. For each entry,
it mapped `.agents/skills/<name>/` to the upstream directory containing the
entry's `skillPath`. It recursively enumerated regular files and symlinks
on both sides, compared relative file sets, and compared every corresponding
file's bytes and symlink status. The comparison included supporting documents,
templates, scripts, credits, and `agents/openai.yaml`, not just `SKILL.md`.

Result: 27 of 27 skill directories and 79 of 79 files matched byte-for-byte.
There were no installed-only files, upstream-only files, or differing files
within the corresponding skill directories. File permissions, empty directories,
and unrelated upstream skills or repository files were outside this comparison.
The lockfile's `computedHash` algorithm was not evaluated; the result comes
from direct file-set and byte comparisons.

| Installed skill | Upstream directory | Matching files |
| --- | --- | --- |
| `ask-matt` | `skills/engineering/ask-matt` | 3 |
| `code-review` | `skills/engineering/code-review` | 2 |
| `codebase-design` | `skills/engineering/codebase-design` | 4 |
| `diagnosing-bugs` | `skills/engineering/diagnosing-bugs` | 3 |
| `domain-modeling` | `skills/engineering/domain-modeling` | 4 |
| `grill-me` | `skills/productivity/grill-me` | 2 |
| `grill-with-docs` | `skills/engineering/grill-with-docs` | 2 |
| `grilling` | `skills/productivity/grilling` | 2 |
| `handoff` | `skills/productivity/handoff` | 2 |
| `implement` | `skills/engineering/implement` | 2 |
| `implement-spec` | `skills/engineering/implement-spec` | 2 |
| `improve-codebase-architecture` | `skills/engineering/improve-codebase-architecture` | 3 |
| `pr` | `skills/engineering/pr` | 3 |
| `prototype` | `skills/engineering/prototype` | 4 |
| `research` | `skills/engineering/research` | 2 |
| `retro` | `skills/engineering/retro` | 2 |
| `setup-matt-pocock-skills` | `skills/engineering/setup-matt-pocock-skills` | 7 |
| `tdd` | `skills/engineering/tdd` | 4 |
| `teach` | `skills/productivity/teach` | 6 |
| `to-questionnaire` | `skills/productivity/to-questionnaire` | 2 |
| `to-spec` | `skills/engineering/to-spec` | 2 |
| `to-tickets` | `skills/engineering/to-tickets` | 2 |
| `triage` | `skills/engineering/triage` | 4 |
| `wait-what` | `skills/productivity/wait-what` | 2 |
| `wayfinder` | `skills/engineering/wayfinder` | 2 |
| `wizard` | `skills/engineering/wizard` | 3 |
| `writing-for-agents` | `skills/productivity/writing-for-agents` | 3 |

## Retained licenses and credits

These texts are retained verbatim:

- [Matt Pocock skills LICENSE](../../third_party/mattpocock-skills/LICENSE),
  from [the verified revision's root LICENSE](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/LICENSE).
- [PR credits](../../third_party/mattpocock-skills/PR-CREDITS.md),
  from [the verified revision's PR credits](https://github.com/mattpocock/skills/blob/4ae2317ddb22b046e26455e4f9c9b3aaaf62212d/skills/engineering/pr/CREDITS.md).
  This is also identical to the existing `.agents/skills/pr/CREDITS.md`.
- [HumanLayer LICENSE](../../third_party/mattpocock-skills/HUMANLAYER-LICENSE),
  from [HumanLayer skills at an immutable revision](https://github.com/humanlayer/skills/blob/4d69d20f6e9b57b4fcf0d501ccfb1be961f0f329/LICENSE).

The verified Matt Pocock archive contains a root MIT license and the PR credits;
no additional license, copying, notice, or credits files were found by filename.
A text search of upstream skill metadata and credits identified the PR skill's
attribution to Dex Horthy / HumanLayer's `show-me` skill. Its metadata links to
`humanlayer/skills`; its credits also link to `humanlayer/humanlayer`.
The existing credits were retained without resolving that wording difference.

The referenced `humanlayer/skills` repository was inspected at
`4d69d20f6e9b57b4fcf0d501ccfb1be961f0f329`. Its recursive tree contains the referenced
`plugins/show-me/skills/show-me/SKILL.md` and a root MIT license; no separate
show-me license or notice file was found by filename. The retained HumanLayer
license comes from that pinned repository revision. The revision from which
Matt Pocock originally copied the attributed material is unknown. This record
does not establish that historical revision or draw legal conclusions about
the attributed material.

## Preservation checks

Before adding provenance, all 85 reviewed staged additions were snapshotted
by staged index entry and worktree SHA-256 digest. Verification checks preserve
those entries and bytes, including all installed skills and `skills-lock.json`.
Only this document and the three retained license/credits files are added.
