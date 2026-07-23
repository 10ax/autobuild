from __future__ import annotations
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from autobuild.spec import parse_spec, SpecDoc

_STATUS_LINE = re.compile(r'(?m)^(status\s*=\s*)".*?"')
_LOCK_MUTEX = threading.Lock()


@dataclass
class Item:
    path: Path
    meta: dict
    doc: SpecDoc


def scan_backlog(backlog_dir: Path) -> list[Item]:
    items = []
    for p in sorted(Path(backlog_dir).glob("*.md")):
        try:
            doc = parse_spec(p.read_text())
        except OSError:
            continue
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


def _write_lock_atomic(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, path)


def read_lock(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []  # tolerate a torn lock file


def add_lock(path: Path, entry: dict) -> None:
    with _LOCK_MUTEX:
        rows = [e for e in read_lock(path) if e.get("slug") != entry.get("slug")]
        rows.append(entry)
        _write_lock_atomic(path, rows)


def clear_lock(path: Path, slug: str) -> None:
    with _LOCK_MUTEX:
        rows = [e for e in read_lock(path) if e.get("slug") != slug]
        _write_lock_atomic(path, rows)
