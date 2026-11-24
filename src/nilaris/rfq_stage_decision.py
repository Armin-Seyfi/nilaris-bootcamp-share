"""Local decisions for an authenticated expert in the Case_001 procurement scenario."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

STAGES = {
    "customer-rfq": "Customer RFQ",
    "supplier-offer": "Supplier offer",
    "before-manufacture": "After order and before manufacture",
}
EXPERT = "Authenticated technical expert"
DIAMETER_ID = "plunger-diameter"
IMPORT_CONTRACT = "case001-explicit-test-import-v1"


class StaleDecision(Exception):
    pass


class StageDecision:
    """The Reviewer holds the private directory's exclusive lock for the session."""

    def __init__(self, directory: Path, identity: str):
        self.path = directory / f"stage-decision-{identity}.json"

    def read(self):
        try:
            return json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {"revision": 0, "stages": [], "decided_by": None}

    def save(self, command):
        if set(command) != {"revision", "stages"} or type(command["revision"]) is not int:
            raise ValueError("Invalid stage decision")
        selected = command["stages"]
        if (not isinstance(selected, list)
                or any(not isinstance(stage, str) or stage not in STAGES for stage in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("Unknown or repeated procurement stage")
        current = self.read()
        if command["revision"] != current["revision"]:
            raise StaleDecision
        decision = {"revision": current["revision"] + 1,
                    "stages": [stage for stage in STAGES if stage in selected],
                    "decided_by": EXPERT}
        with NamedTemporaryFile(dir=self.path.parent, mode="w", delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(decision, output)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return decision
