"""Embed the real CAD snapshot data into the offline HTML template."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
template = (root / 'viewer-template.html').read_text(encoding='utf-8')
assert template.count('__TOPOLOGY_DATA__') == 1
data = json.loads((root / 'topology-data.json').read_text(encoding='utf-8'))
serialized = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
page = template.replace('__TOPOLOGY_DATA__', serialized)
assert '__TOPOLOGY_DATA__' not in page
(root / 'solidcraft-topology.html').write_text(page, encoding='utf-8')
print(f'Built {root / "solidcraft-topology.html"} ({len(page.encode("utf-8")):,} bytes)')
