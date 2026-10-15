"""Rebuild the public source offer from the exact Docker build context."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
allowed_files = ['Dockerfile', '.dockerignore', 'pyproject.toml', 'requirements-demo.txt',
                 'render.yaml', 'README.txt', 'THIRD_PARTY_NOTICES.txt',
                 'third-party-inventory.json', 'LICENSE', 'RELEASE_STATUS.txt']
allowed_dirs = ['luckypath', 'deploy', 'data', 'licenses', 'tests']
paths = [root/name for name in allowed_files if (root/name).is_file()]
for name in allowed_dirs:
    paths.extend(p for p in (root/name).rglob('*') if p.is_file() and not p.is_symlink()
                 and not any(part in {'.env', '__pycache__', '.git', '.venv'} for part in p.parts)
                 and p.suffix not in {'.pyc', '.sqlite', '.db'})
target = root/'public/luckypath-source.zip'
target.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(set(paths)):
        archive.write(path, path.relative_to(root))
print('Application source archive regenerated from this build context.')
