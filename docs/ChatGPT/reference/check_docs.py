#!/usr/bin/env python3
"""Offline documentation validation; does not call platform or change game code."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[4]
errors = []
required = [
    'README.md', '01-游戏目标与策略分析.md', '02-Loop工程计划.md',
    '03-日志与加密方案.md', '04-执行Agent手册.md', 'REVIEW.md',
    'reference/source-manifest.json',
]
for name in required:
    if not (ROOT / name).is_file():
        errors.append(f'Missing required document: {name}')
count = 0
for path in sorted(ROOT.rglob('*.json')):
    try:
        json.loads(path.read_text())
        count += 1
    except (ValueError, OSError) as exc:
        errors.append(f'{path.relative_to(ROOT)}: {exc}')
for path in ROOT.rglob('*.md'):
    for dest in re.findall(r'\]\(([^)]+)\)', path.read_text()):
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', dest) or dest.startswith('#'):
            continue
        dest = dest.split('#', 1)[0]
        if dest and not (path.parent / dest).exists():
            errors.append(f'Broken link in {path.name}: {dest}')
manifest = json.loads((ROOT / 'reference/source-manifest.json').read_text())
for entry in manifest['files']:
    path = WORKSPACE / entry['path']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        errors.append(f'Source changed since analysis: {entry["path"]}')
if errors:
    print('\n'.join(errors))
    sys.exit(1)
print(f'PASS: required docs, {count} JSON files, local Markdown links, {len(manifest["files"])} source hashes.')
print('Not tested: platform integration, strategy match performance, encryption runtime, JSON Schema conformance.')
