"""Baseline-only recorder installed in a spawned Python process by isolated-index.py."""
import atexit
from collections import defaultdict
import os
from pathlib import Path
import sys
from time import time_ns

source = os.environ["BASELINE_TRACE_SOURCE"] + os.sep
destination = Path(os.environ["BASELINE_TRACE_CHILD"])
lines = defaultdict(set)
reads = set()
modules = set()
mon = sys.monitoring

def trace(code, number):
    path = code.co_filename
    if path.startswith(source):
        p = path[len(source):]
        lines[p].add(number)
        if code.co_name == "<module>":
            modules.add(p)
    return mon.DISABLE

def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)) and args[2] & os.O_ACCMODE != os.O_WRONLY:
        p = os.path.abspath(os.fsdecode(args[0]))
        if p.startswith(source) and "/__pycache__/" not in p and not sys._getframe(1).f_code.co_filename.startswith("<frozen importlib."):
            reads.add(p[len(source):])

@atexit.register
def save():
    import json
    mon.set_events(mon.COVERAGE_ID, 0)
    dest = destination / f"{os.getpid()}-{time_ns()}.json"
    dest.write_text(json.dumps({"lines":{p:sorted(v) for p,v in lines.items()}, "modules":sorted(modules), "reads":sorted(reads)}))
mon.use_tool_id(mon.COVERAGE_ID, "baseline-child")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, trace)
mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
sys.addaudithook(audit)
