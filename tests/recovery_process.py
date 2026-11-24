"""External process-stop harness using only the agreed public ingestion API."""
import json
from pathlib import Path
import sys
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nilaris.admission import Ingestion
from test_recovery import literal_output


def main():
    request = json.load(sys.stdin)
    client = Ingestion(request['database'])
    if request.get('pause_inventory_marker'):
        # Fault injection only at the external process-command boundary.
        real_run = subprocess.run
        supervisor_start = real_run(['/bin/ps', '-p', str(os.getpid()), '-o', 'lstart='],
                                    capture_output=True, text=True, check=True).stdout.strip()

        def pause_inventory(command, *args, **kwargs):
            if (command == ['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=,lstart='] and
                    Path(request['pause_inventory_marker'] + '.armed').exists()):
                command = ['/bin/sh', str(Path(__file__).with_name('pause_inventory.sh')),
                           str(os.getpid()), supervisor_start, request['pause_inventory_marker'],
                           'unavailable' if request.get('inventory_unavailable') else 'available',
                           *command[1:]]
            return real_run(command, *args, **kwargs)

        subprocess.run = pause_inventory
    client.run_controlled(request['job_id'], request['revision'], literal_output(),
                          test_delay_seconds=20)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit(1) from None
