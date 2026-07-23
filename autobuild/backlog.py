from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from autobuild.spec import parse_spec, SpecDoc

_STATUS_LINE = re.compile(r'(?m)^(status\s*=\s*)".*?"')


@dataclass
class Item:
    path: Path
    meta: dict
    doc: SpecDoc


def scan_backlog(backlog_dir: Path) -> list[Item]:
    items = []
    for p in sorted(Path(backlog_dir).glob("*.md")):
        doc = parse_spec(p.read_text())
        doc.path = p
        items.append(Item(path=p, meta=doc.meta, doc=doc))
    return items


def select_pending(items: list[Item], n: int) -> list[Item]:
    pend = [i for i in items if i.meta.get("status") == "pending"]
    pend.sort(key=lambda i: (-int(i.meta.get("priority", 0)),
                             str(i.meta.get("created", "")), i.path.name))
    return pend[:n]


def set_status(item: Item, status: str) -> None:
    text = item.path.read_text()
    new = _STATUS_LINE.sub(rf'\g<1>"{status}"', text, count=1)
    item.path.write_text(new)
    item.meta["status"] = status


def read_lock(path: Path) -> list[dict]:
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else []


def add_lock(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [e for e in read_lock(path) if e.get("slug") != entry.get("slug")]
    rows.append(entry)
    path.write_text(json.dumps(rows, indent=2))


def clear_lock(path: Path, slug: str) -> None:
    path = Path(path)
    rows = [e for e in read_lock(path) if e.get("slug") != slug]
    path.write_text(json.dumps(rows, indent=2))
