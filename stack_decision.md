# Primary stack for Project 3

NILARIS uses Python and LangGraph to turn historical procurement documents into a structured schema proposal. A technical-office reviewer needs to trace a proposed field back to its source and recover a saved decision after an interruption. Python owns intake and processing. PostgreSQL retains review state and revisions.

The presentation depicted n8n as a secondary intake adapter in the target architecture. Earlier project iterations explored that route. The current Python-owned intake path can run without n8n, so the bootcamp demo does not require an n8n installation or workflow credentials.

The autonomous part of the demo ends when a structured proposal is saved. The technical expert can then inspect the evidence and edit the draft. Final engineering approval is an explicit human action.
