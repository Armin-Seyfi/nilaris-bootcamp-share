# Python-owned intake without a required workflow platform

Status: accepted architecture direction by the owner, 2026-11-15; bounded Transition167 local implementation/verification separately authorized on2026-11-15. No provider calls or core promotion.

NILARIS will retain Python/LangGraph processing, PostgreSQL authoritative durable state and the working Google Drive connector, and remove n8n from its required product stack. Python owns intake orchestration and resume, with provider authentication/discovery/acquisition behind a thin adapter interface, so adding an intake channel does not require rewriting processing or expert review. Retaining a required n8n coordinator was rejected because it adds an operational dependency without owning the authoritative state; replacing it with another workflow platform is also excluded.

Historical n8n requirements and actual engine evidence remain intact. Only their mandatory-platform clauses are superseded; durability, provenance, fencing, accounting, uncertain-outcome recovery and explicit human approval requirements remain. Existing operation identities, cursors, reservations, initial publications and approved versions must survive the transition. The [Project 3 stack decision](../../stack_decision.md) explains the choice for the bootcamp project. Ticket05 completion requires separate verification.
