# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `Armin-Seyfi/nilaris`.
Use the `gh` CLI.

Follow the publication checkpoints in `docs/agents/learning-workflow.md`.

## Target repository

Use `--repo Armin-Seyfi/nilaris` for issue and label commands.

Before the first issue creation in a session, verify the repository:

```sh
gh repo view Armin-Seyfi/nilaris --json nameWithOwner,isPrivate,url
```

Proceed only when the target is `Armin-Seyfi/nilaris`.
If it differs or cannot be reached, resolve the mismatch before publishing.

## Common operations

Read an issue:

```sh
gh issue view <number> --repo Armin-Seyfi/nilaris \
  --json number,title,body,labels,comments
```

List open issues, adding label filters when needed:

```sh
gh issue list --repo Armin-Seyfi/nilaris --state open \
  --json number,title,body,labels
```

Create an approved issue using its reviewed body file:

```sh
gh issue create --repo Armin-Seyfi/nilaris \
  --title "<title>" --body-file <body-file>
```

Comment using a reviewed body file:

```sh
gh issue comment <number> --repo Armin-Seyfi/nilaris \
  --body-file <body-file>
```

Apply or remove a label:

```sh
gh issue edit <number> --repo Armin-Seyfi/nilaris \
  --add-label "<label>"
gh issue edit <number> --repo Armin-Seyfi/nilaris \
  --remove-label "<label>"
```

To close an issue, publish the approved explanation as a comment first,
then close it. This also preserves the explanation when the issue is
already closed.

## Skill terminology

When a skill says “publish to the issue tracker,” create a GitHub issue
after the required human checkpoint.

When a skill says “fetch the relevant ticket,” read the GitHub issue,
including its labels and comments.

## Parent issues and dependencies

Prefer GitHub's native sub-issues and blocking dependencies. Check the
installed CLI's help for supported commands before using them.

If native sub-issues are unavailable, add the child to a task list in the
parent issue and put `Part of #<parent>` at the top of the child body.

If native blocking dependencies are unavailable, put
`Blocked by: #<number>, #<number>` at the top of the child body.

A ticket is unblocked only when every blocker is closed. When inspecting
native dependencies, distinguish open blockers from closed ones.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub issues and pull requests share a number space. Confirm the item type
when a bare reference such as `#42` is ambiguous.

## Wayfinding operations

When using the wayfinder skill:

- Keep the map in one issue labelled `wayfinder:map`, with its Notes,
  Decisions-so-far, and Fog sections.
- Link child tickets as sub-issues, using the parent-link fallback above
  when needed.
- Use the skill's ticket labels: `wayfinder:research`,
  `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Select the first open child in map order that has no open blockers and
  no assignee.
- Claim an authorized ticket by assigning the driving developer.
- Resolve an authorized ticket by commenting with the answer, closing it,
  and adding a concise finding and link to the map's Decisions-so-far.

Create additional labels only when their use is authorized.
