# Human-in-the-loop learning workflow

## Working together

For each task, explain the intended outcome and the reasoning behind
meaningful choices. Keep explanations concise and define unfamiliar terms
when they matter to the decision.

Inspect the repository and relevant issues before asking questions.
Treat confirmed choices as settled. Ask unresolved questions one at a time,
with a recommendation and a brief explanation.

Distinguish evidence, assumptions, and proposals. Report what changed,
how it was checked, and any remaining uncertainty.

## Authorization

Treat explicit approval as covering the named decisions, actions, and scope.
Approval may cover one action or a batch, including multiple publication
steps. Check existing authorization before asking; request approval only
for decisions or actions it does not cover.

## Product and engineering checkpoints

Before implementing an unresolved material decision, present:

- The decision and recommended option.
- Relevant alternatives and tradeoffs.
- Proposed scope and acceptance criteria.
- Checks that will demonstrate the result.

Obtain approval before proceeding with the decision.

Material decisions include product scope and behavior, domain terminology,
architecture and data models, dependencies and external services, handling
private documents, and security or deployment arrangements.

Carry out routine, reversible work within approved scope. Return to the
human when new evidence materially changes an approved decision or scope.

## Publication checkpoints

Prepare concrete, reviewable material before requesting authorization:

| Action | Material for review |
| --- | --- |
| Commit | Changes, validation results, and commit message |
| Push | Branch, destination, and commits |
| Merge | Pull request, review status, required checks, and merge method |
| GitHub issue, comment, label, or pull request change | Target and proposed content or change |

Proceed when existing authorization covers the action and scope. Otherwise,
present the prepared material and wait for approval. Approval for one action
covers another only when both were explicitly included.

## Setup phase

During workflow setup, show proposed file contents and label changes before
applying them. Reuse existing labels.

Application implementation, package installation, and installed-skill
changes are outside the current setup scope. Obtain authorization before
expanding into them.
