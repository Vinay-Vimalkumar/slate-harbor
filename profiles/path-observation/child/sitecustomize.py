"""Record filesystem lookups in a spawned pristine interpreter."""
import atexit
import importlib.machinery
import os
import sys

root = os.environ["BASE_DIR_PREFIX"] + os.sep
target = os.path.join(os.environ["QUERY_CHILD_DIR"],str(os.getpid()))
open(target+".start","wb").close()
dirs, probes = set(), set()
def record(group,path):
    if isinstance(path,(str,bytes,os.PathLike)):
        value=os.path.abspath(os.fsdecode(path))
        if value.startswith(root) and "/__pycache__" not in value:
            group.add(value[len(root):])

def audit(event,args):
    if event in ("os.listdir","os.scandir") and not sys._getframe(1).f_code.co_filename.startswith("<frozen importlib."):
        record(dirs,args[0])
    elif event == "open":
        record(probes,args[0])

def observed(fn):
    def lookup(path,*args,**kwargs):
        record(probes,path)
        return fn(path,*args,**kwargs)
    return lookup

finder=importlib.machinery.FileFinder
original=finder.find_spec
def find(self,name,target=None):
    spec=original(self,name,target)
    if spec is None or spec.loader is None:
        record(probes,os.path.join(self.path,name.rsplit(".",1)[-1]+".py"))
    return spec

@atexit.register
def save():
    import json
    with open(target+".json","w") as dest:
        json.dump({"dirs":sorted(dirs),"probes":sorted(probes)},dest)
os.stat,os.lstat=observed(os.stat),observed(os.lstat)
finder.find_spec=find
sys.addaudithook(audit)
