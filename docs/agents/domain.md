# Domain docs

NILARIS uses a single-context domain documentation layout:

- `GLOSSARY.md` at the repository root.
- Architecture decision records under `docs/adr/`.

## Before exploration

Read the root `GLOSSARY.md` before exploring domain concepts.
Read ADRs relevant to the area being investigated or changed.

If these documents do not exist, proceed silently. Create them lazily
through the domain-modeling skill as terms and decisions are resolved.
Apply `docs/agents/learning-workflow.md` when a material decision needs
human review.

## Vocabulary

Use the glossary's terms in issue titles, proposals, code, and tests.
Respect any synonyms it explicitly discourages.

When a needed concept is missing, distinguish a real vocabulary gap from
an unnecessary new term. Treat unresolved material terminology as a
decision under `docs/agents/learning-workflow.md`.

## Architecture decisions

When a proposal conflicts with an existing ADR, identify the ADR and
explain why the decision may need reconsideration. Apply the decision
checkpoint in `docs/agents/learning-workflow.md` before proceeding.

Record accepted decisions under `docs/adr/` using the domain-modeling
skill's ADR format.
