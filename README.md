# NILARIS

Technical-office teams need to know which pump specifications to request, who should supply them, and at which procurement stage. NILARIS uses historical documents to propose a product schema. An expert inspects the source passage, records a decision for each procurement scenario, and saves a revision for approval.

## Project 3 presentation

1. Open the [seven-slide presentation](presentation/project3_slides.pdf).
2. Open the local [walkthrough HTML file](presentation/NILARIS-prototype-walkthrough.html) in Chrome. If you are reading on GitHub, download the HTML file first.
3. Select **Start walkthrough** and keep the tab in the foreground for about 2 minutes 20 seconds. The final screen shows **Review walkthrough complete**, 29 selected attributes, a draft saved in prototype memory, and explicit approval pending.

This is the walkthrough presented at bootcamp. It runs offline with synthetic examples and simulates intake and sign-in. The [storyboard](presentation/storyboard.csv) describes what each scene demonstrates.

## Run the independent tests

From the repository root, run the following with Node.js installed (verified with v24.20.0). The default run uses Node’s built-in test runner and the files in this checkout.

```sh
node --test tests/review_first_b_save.test.mjs tests/schema_preview.test.mjs
```

Expected with the optional browser and QA settings unset: **7 passed, 0 failed, 4 skipped**. The independent checks cover draft-save recovery and validation of the saved state. Two skipped checks require a configured Chrome test runner; the other two exercise authenticated workflows in a prepared QA environment.

## Implementation snapshot

Source revision: `21201d419d70665098782b405f260b97a72cff84`. The code captures development after the presentation. In the accepted two-scenario workflow, a Reviewer can save a single stage decision, preview all unresolved cells, mark the displayed set optional, and reopen the persisted result. An Approver can inspect a specific saved revision, see which included cells remain unresolved, and follow an evidence link. Formal acceptance of whole-Schema confirmation and reopening the approved ID after restart remains pending at this revision.

| Start here | What to inspect |
| --- | --- |
| [Intake](src/nilaris/intake167.py) and [architecture decision](docs/adr/0001-python-owned-provider-independent-intake.md) | How documents enter the Python-owned workflow |
| [Review service](src/nilaris/customer_review.py) and [Reviewer UI](src/nilaris/review_web/) | Saved decisions, revision views and the browser interface |
| [Tests](tests/) | Behavioral checks for the implementation |
| [Runtime guide](runtime/a1/README.md) | PostgreSQL, dependency overlays and the private configuration needed by the engineering runtime |
| [Stack decision](stack_decision.md) and [GTM experiments](gtm_future_sprints.md) | Why Python/LangGraph is primary and how to test demand |
| [Agent instructions](AGENTS.md) | Repository workflow and skill provenance |

## Engineering runtime prerequisites

`conda env create -f environment.yml` followed by `conda activate nilaris` prepares the base Python 3.12 environment with pip. Running the documented `pip check` and Python tests through `with_overlay.py` also requires the installed dependency overlay kept outside Git. Starting the PostgreSQL-backed Reviewer or running intake through to a saved proposal requires additional runtime configuration and credentials supplied by the operator. The [runtime guide](runtime/a1/README.md) describes that private setup. The offline walkthrough and independent Node tests above can be run directly from this snapshot.

## Copyright

Copyright © 2026 Morteza (Armin) Seyfi. See the [copyright notice](COPYRIGHT.md) and [third-party notices](third_party/mattpocock-skills/).
