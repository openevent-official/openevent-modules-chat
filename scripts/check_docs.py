"""Check translation structure and local documentation links."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
pairs = [(root / 'README_cn.md', root / 'README.md')]
pairs.extend((path, path.with_name(path.name.replace('_cn.md', '.md')))
             for path in (root / 'docs').glob('*_cn.md'))
errors = []


def headings(path):
    return [len(match.group(1)) for match in re.finditer(r'^(#{1,6}) ', path.read_text(encoding='utf-8'), re.M)]


for chinese, english in pairs:
    if not english.is_file() or headings(chinese) != headings(english):
        errors.append(f'{chinese.name}: translation heading structure differs')
    for path in (chinese, english):
        text = path.read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)]+)\)', text):
            local = target.split('#', 1)[0]
            if not local or '://' in local:
                continue
            if not (path.parent / local).exists():
                errors.append(f'{path.name}: missing linked document {local}')
        if re.search(r'openevent-sdk\s*(?:>=)?0\.7|timeout=1\.0|rpc_timeout["`\n]', text):
            errors.append(f'{path.name}: old SDK timeout/version contract remains')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'PASS: {len(pairs)} Chinese/English document pairs and local links')
