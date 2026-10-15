"""Record source bytes loaded by this process for inspectable experiments."""
import hashlib
import shutil
from pathlib import Path
_sources = {}

def register_source(module, filename):
    path = Path(filename).resolve()
    _sources[module] = (path, path.read_bytes())

def save_runtime_sources(output):
    output = Path(output)
    modules = {}
    for module, (path, data) in sorted(_sources.items()):
        relative = Path(*module.split('.')).with_suffix('.py')
        target = output / 'runtime_source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        modules[module] = {'file': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
                           'source_path': str(path)}
    return {'modules': modules}
