"""Root-only controlled HTTP fixture; creates synthetic key, never loads a real key."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nilaris.command_service import Configuration, serve
from nilaris.drive155_journey import DriveJourney
from test_drive155_source import synthetic_sa, source_fixture

if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--controlled-offline':
        raise SystemExit(2)
    private = Configuration.load(sys.argv[2])
    reader, _, _, _ = source_fixture(synthetic_sa.__wrapped__())
    private.source_operator = DriveJourney(None, {'synthetic-binding': reader})
    serve(private)
