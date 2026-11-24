# NILARIS

Technical-office teams need to know which pump specifications to request, who should supply them, and at which procurement stage. NILARIS uses historical documents to propose a product schema. An expert inspects the source passage, records a decision for each procurement scenario, and saves a revision for approval.

## Project 3 presentation

Read the [seven-slide presentation](presentation/project3_slides.pdf). Download and open the [standalone walkthrough](presentation/NILARIS-prototype-walkthrough.html) in Chrome, then select **Start walkthrough**. The [storyboard](presentation/storyboard.csv) identifies each scene and its evidence. The walkthrough presented at bootcamp uses synthetic examples, simulated intake and sign-in, and in-memory review. It runs locally without accounts or API keys.

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

For Python development, start with `conda env create -f environment.yml` and `conda activate nilaris`. This creates the base Python 3.12 environment. Follow the runtime guide for its pinned dependencies and service setup. The presentation walkthrough runs independently of this environment.

## Copyright

See the [copyright notice](COPYRIGHT.md) and [third-party notices](third_party/mattpocock-skills/).
