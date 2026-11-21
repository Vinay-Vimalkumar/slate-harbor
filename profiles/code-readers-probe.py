"""Audit one cold pytest process for the actual source files behind inspected CPython code objects."""
import argparse
import ast
from functools import lru_cache
import inspect
import json
import os
from pathlib import Path
import sys
import types


def trace(base, nodeid, out):
    import pytest
    root = str(base.resolve()) + os.sep
    os.chdir(base)
    sys.path.insert(0, str(base))
    objects, raw, event_sites, unknown, opaque = set(), set(), set(), set(), set()
    busy = False
    child = False
    if any(isinstance(getattr(m, "__file__", None), str) and m.__file__.startswith(root)
           for m in tuple(sys.modules.values())):
        unknown.add("project module imported before audit")

    def relative(name):
        return name[len(root):] if isinstance(name, str) and name.startswith(root) else None

    code_cache = {}
    def code_files(code):
        entry = code_cache.get(id(code))
        if entry is not None:
            return entry[1]
        files = {name} if (name := relative(code.co_filename)) else set()
        pending=list(code.co_consts)
        while pending:
            value=pending.pop()
            if isinstance(value, types.CodeType):
                files.update(code_files(value))
            elif type(value) in (tuple,frozenset):
                pending.extend(value)
        code_cache[id(code)] = (code, frozenset(files))
        return code_cache[id(code)][1]

    def audit(event, args):
        nonlocal busy, child
        if busy:
            return
        if event in {"subprocess.Popen","os.system","os.posix_spawn","os.fork","os.exec","_posixsubprocess.fork_exec"}:
            child = True
        if event not in {"object.__getattr__","open","code.__new__"}:
            return
        busy = True
        try:
            caller = sys._getframe(1)
            if event == "open":
                name = args[0]
                if isinstance(name, (str,bytes)) and (path := relative(os.path.abspath(os.fsdecode(name)))):
                    if path.endswith(".py") and not caller.f_code.co_filename.startswith("<frozen importlib."):
                        raw.add(path)
                return
            if event == "code.__new__":
                # A test that manufactures code objects can supply arbitrary filenames or constants.
                if relative(caller.f_code.co_filename):
                    unknown.add("constructed code object in project")
                return
            obj, attr = args
            if attr == "__code__" and isinstance(obj, types.FunctionType):
                code = obj.__code__
            elif attr == "f_code" and isinstance(obj, types.FrameType):
                code = obj.f_code
            elif attr == "tb_frame" and isinstance(obj, types.TracebackType):
                code = obj.tb_frame.f_code
            else:
                return
            paths = code_files(code)
            objects.update(paths)
            if paths:
                origin = relative(caller.f_code.co_filename) or caller.f_globals.get("__name__", "<external>")
                event_sites.add((origin,caller.f_lineno,attr,*sorted(paths)))
        finally:
            busy = False

    # Detect direct project __code__ lookups through custom properties, which do not
    # emit CPython's function.__code__ audit event. A site must have a simple named receiver.
    @lru_cache(maxsize=4096)
    def receiver_sites(filename):
        path = relative(filename)
        if not path or not filename.endswith(".py"):
            return {}
        tree = ast.parse(Path(filename).read_bytes())
        sites = {}
        def dotted(node):
            if isinstance(node,ast.Name):
                return (node.id,)
            if isinstance(node,ast.Attribute) and (owner := dotted(node.value)):
                return (*owner,node.attr)
            return None
        for node in ast.walk(tree):
            receiver = None
            if isinstance(node,ast.Attribute) and node.attr=="__code__":
                receiver=node.value
            elif (isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in {"getattr","hasattr"}
                  and len(node.args)>1 and isinstance(node.args[1],ast.Constant) and node.args[1].value=="__code__"):
                receiver=node.args[0]
            if receiver is not None:
                sites.setdefault(node.lineno,[]).append(dotted(receiver))
        return sites

    def line(code, lineno):
        nonlocal busy
        if busy:
            return
        busy = True
        try:
            names=receiver_sites(code.co_filename).get(lineno)
            if not names:
                return sys.monitoring.DISABLE
            frame=sys._getframe(1)
            for path in names:
                obj=None
                if path:
                    obj=frame.f_locals.get(path[0],frame.f_globals.get(path[0]))
                    for part in path[1:]:
                        if type(obj) not in (types.ModuleType,type):
                            obj=None
                            break
                        obj=inspect.getattr_static(obj,part,None)
                        if type(obj) in (staticmethod,classmethod):
                            obj=obj.__func__
                if not isinstance(obj,(types.FunctionType,types.MethodType)):
                    opaque.add((relative(code.co_filename),lineno, type(obj).__name__))
        finally:
            busy=False

    sys.addaudithook(audit)
    monitor=sys.monitoring
    tool=monitor.COVERAGE_ID
    monitor.use_tool_id(tool,"code-readers")
    monitor.register_callback(tool,monitor.events.LINE,line)
    monitor.set_events(tool,monitor.events.LINE)
    try:
        status=pytest.main(["-q","-p","no:cacheprovider","--timeout=120",nodeid])
    finally:
        monitor.set_events(tool,0)
        monitor.register_callback(tool,monitor.events.LINE,None)
        monitor.free_tool_id(tool)
    report={"node":nodeid,"platform":sys.platform,"exit":int(status),"subprocess":child,"code":sorted(objects),"raw":sorted(raw),
            "origins":sorted(event_sites),"unknown":sorted(unknown),"opaque":sorted(opaque)}
    out.write_text(json.dumps(report,indent=2))
    print(f"{len(objects)} inspected source files, {len(raw)} source opens, {len(opaque)} opaque receiver cases")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--base",type=Path,required=True);p.add_argument("--out",type=Path,required=True);p.add_argument("nodeid")
    a=p.parse_args();trace(a.base.resolve(),a.nodeid,a.out.resolve())
