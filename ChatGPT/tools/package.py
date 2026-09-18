#!/usr/bin/env python3
"""Reproducible allowlist package; never includes docs, logs, keys or baselines."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]

def build(output):
    paths=[ROOT/'main3.py',ROOT/'pyproject.toml']+sorted((ROOT/'src'/'agent').rglob('*.py'))
    files={str(p.relative_to(ROOT)):p.read_bytes() for p in paths}
    try:
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    except (subprocess.CalledProcessError,FileNotFoundError):
        revision='unknown'
    manifest={'format':1,'base_commit':revision,'source_identity':'sha256 of packaged working-tree files; not a committed revision',
              'runtime':'Python >=3.11, standard library only','entrypoint':'python3 main3.py PORT','launcher':'platform supplied; run.sh is local test helper',
              'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    files['MANIFEST.json']=(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as gz:
        with tarfile.open(fileobj=gz,mode='w') as archive:
            for name,data in sorted(files.items()):
                info=tarfile.TarInfo(name)
                info.size=len(data)
                info.mode=0o755 if name=='run.sh' else 0o644
                info.mtime=0
                archive.addfile(info,io.BytesIO(data))
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(f'{digest}  {output.name}\n')
    print(json.dumps({'package':str(output),'sha256':digest,'files':len(files)},ensure_ascii=False))
    return digest

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts'/'coregeek-v0.5.tar.gz')
    build(parser.parse_args().output.resolve())
