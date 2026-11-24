"""Load the task overlay only after the NILARIS interpreter has started."""
import os
from pathlib import Path
import runpy
import sys

OVERLAY = Path('/private/tmp/nilaris-a1-setup-02/overlay')
if not OVERLAY.is_dir():
    raise SystemExit('task overlay unavailable; no baseline fallback')
sys.path.insert(0, str(OVERLAY))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
if sys.argv[1:2] == ['-m']:
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name='__main__', alter_sys=True)
else:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name='__main__')
