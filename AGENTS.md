# NILARIS agent instructions

Respond in English.

Work within approved scope. Return to the human for material decisions or
publication actions not already authorized. Honor scoped or batch approval;
ask again only when the proposed action exceeds it.

For product or engineering decisions, setup changes, or publication actions,
consult `docs/agents/learning-workflow.md` for checkpoint details.

Before engineering changes or reviews, read
`docs/agents/engineering-standards.md` for applicable sources and review records.

## Python execution

Run every Python command through the NILARIS Conda environment:

```sh
/opt/miniconda3/bin/conda run --no-capture-output -n nilaris python ...
```

Use the same Conda prefix for Python tools and test runners. Prefer
`python -m <module>` where supported.

## Agent skills

Use the repository’s [ask-matt skill](.agents/skills/ask-matt/SKILL.md) to choose a workflow for the authorized task. Read the selected skill’s instructions. Local skills are under `.agents/skills/`; the [provenance record](docs/agents/skills-provenance.md) identifies their source and bundled notices.

- Before GitHub issue or pull request operations, read
  `docs/agents/issue-tracker.md`.
- Before triaging issues or managing triage labels, read
  `docs/agents/triage-labels.md`.
- Before exploring domain concepts or architecture, read
  `docs/agents/domain.md` for glossary and ADR guidance.
