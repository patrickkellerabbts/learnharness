#!/usr/bin/env python3
"""Export LearnHarness learning data into an Obsidian vault.

Reads the LearnHarness data folder (read-only) and writes Markdown notes:

    <vault>/<ziel>/Lernen.md              Übersicht
    <vault>/<ziel>/Lektionen/<Titel>.md   eine Notiz pro Session
    <vault>/<ziel>/Aussagen/<Name>.md     eine Notiz pro Aussage (Proposition)

Generated notes carry `quelle: learnharness` and `lh-id` in their frontmatter.
Only such files are ever overwritten; the section `## Eigene Notizen` and
everything below it is preserved across runs. Files are never deleted: notes
whose LearnHarness record disappeared are only reported.

Standard library only. The graph ledgers are merged with the bundled skill
reader (skills/learnharness/scripts/graph.py); chat records are read here
directly because only their metadata is needed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import tempfile
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "learnharness" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from _data import DataError, data_root  # noqa: E402
from graph import load_graph  # noqa: E402

MARKER = "learnharness"
OWN_NOTES = "## Eigene Notizen"
OWN_NOTES_DEFAULT = f"{OWN_NOTES}\n\n"
RELATED_SEPARATOR = "\n---\n## Verwandt"
HUB = "[[Lernen]]"
INVALID_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]')
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


# ── reading ──────────────────────────────────────────────────────────────


def iso_date(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    return datetime.now().date().isoformat()


@dataclass
class Session:
    id: str
    title: str
    kind: str
    created: str
    updated: str
    goal: str | None
    plan_nodes: list[dict]
    plan_edges: list[dict]
    visited: list[str] = field(default_factory=list)


def tool_calls(record):
    for message in record.get("messages", []):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "toolCall":
                args = block.get("arguments")
                yield block.get("name"), args if isinstance(args, dict) else {}


def read_sessions(root: Path) -> list[Session]:
    sessions = []
    chats = root / "chats"
    if not chats.is_dir():
        return sessions
    for path in sorted(chats.glob("chat_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Warnung: {path.name} nicht lesbar ({exc}), übersprungen", file=sys.stderr)
            continue
        goal = None
        visited: list[str] = []
        for name, args in tool_calls(record):
            if name == "probe_done":
                summary = (args.get("assessment") or {}).get("summary")
                if isinstance(summary, str) and summary.strip():
                    goal = summary.strip()
            node = args.get("nodeId")
            if name in ("quiz", "explain") and isinstance(node, str) and node not in visited:
                visited.append(node)
        tree = record.get("tree") or {}
        sessions.append(
            Session(
                id=record.get("id", path.stem),
                title=(record.get("title") or path.stem).strip(),
                kind="Übung" if record.get("kind") == "practice" else "Lektion",
                created=iso_date(record.get("createdAt")),
                updated=iso_date(record.get("updatedAt")),
                goal=goal,
                plan_nodes=[n for n in tree.get("nodes", []) if isinstance(n, dict) and "id" in n],
                plan_edges=[e for e in tree.get("edges", []) if isinstance(e, dict)],
                visited=visited,
            )
        )
    return sessions


def read_sources(root: Path) -> dict[str, str]:
    """Imported-notes sources: id -> file name (display only)."""
    path = root / "graph" / "sources.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        key: Path(value.get("filePath", key)).name
        for key, value in data.items()
        if isinstance(value, dict)
    }


# ── vault bookkeeping ────────────────────────────────────────────────────


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
    return fields


def own_notes(text: str) -> str:
    """The preserved block: from `## Eigene Notizen` up to the generated `## Verwandt` footer."""
    index = text.find(OWN_NOTES)
    if index < 0:
        return OWN_NOTES_DEFAULT
    block = text[index:]
    footer = block.find(RELATED_SEPARATOR)
    return block[:footer] if footer >= 0 else block


def read_own_notes(path: Path) -> str:
    return own_notes(path.read_text(encoding="utf-8")) if path.exists() else OWN_NOTES_DEFAULT


def related(**groups) -> list[str]:
    """Vault convention (AGENTS.md → Vernetzung): every note ends with `## Verwandt`."""
    lines = ["---", "## Verwandt"]
    for label, links in groups.items():
        if links:
            lines.append(f"- {label.replace('_', ' ')}: {', '.join(links)}")
    return lines


def safe_name(name: str) -> str:
    cleaned = INVALID_NAME.sub("", swiss(name)).replace("\n", " ").strip().strip(".")
    return re.sub(r"\s+", " ", cleaned)[:120] or "Ohne Titel"


class Folder:
    """One generated folder: maps lh-id -> file, keeps names stable."""

    def __init__(self, path: Path, dry_run: bool = False):
        self.path = path
        self.dry_run = dry_run
        self.by_id: dict[str, Path] = {}
        self.taken: set[str] = set()
        if path.is_dir():
            for file in path.glob("*.md"):
                self.taken.add(file.stem.casefold())
                fields = parse_frontmatter(file.read_text(encoding="utf-8"))
                if fields.get("quelle") == MARKER and fields.get("lh-id"):
                    self.by_id[fields["lh-id"]] = self._swiss_name(file)

    def _swiss_name(self, file: Path) -> Path:
        """Generated files from before the ß rule get their Swiss name (links follow via stem)."""
        fixed = file.with_name(swiss(file.name))
        if fixed == file or (fixed.exists() and not fixed.samefile(file)):
            return file
        if self.dry_run:
            return fixed
        file.rename(fixed)
        self.taken.add(fixed.stem.casefold())
        return fixed

    def assign(self, lh_id: str, wanted: str, suffix: str) -> Path:
        if lh_id in self.by_id:
            return self.by_id[lh_id]
        base = safe_name(wanted)
        candidates = [base, f"{base} ({safe_name(suffix)})"] + [f"{base} ({n})" for n in range(2, 100)]
        for stem in candidates:
            if stem.casefold() not in self.taken:
                self.taken.add(stem.casefold())
                path = self.path / f"{stem}.md"
                self.by_id[lh_id] = path
                return path
        raise RuntimeError(f"Kein freier Dateiname für {wanted}")


def link(path: Path) -> str:
    return f"[[{path.stem}]]"


def frontmatter(**fields) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def mermaid_label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


# ── rendering ────────────────────────────────────────────────────────────


def render_session(s: Session, props: list[Path], chains: list[str], keep: str, see_also: list[Path]) -> str:
    parts = [
        frontmatter(
            erstellt=s.created,
            aktualisiert=s.updated,
            tags=["lernen", "uebung" if s.kind == "Übung" else "lektion"],
            quelle=MARKER,
            **{"lh-id": s.id},
        ),
        f"# {s.title}",
        "",
        f"**Art:** {s.kind} · **Datum:** {s.created} · LearnHarness-Session `{s.id}`",
    ]
    if s.goal:
        goal = re.sub(r"^Ziel:\s*", "", s.goal)
        parts += ["", "## Ziel und Ausgangslage", "", "*Einschätzung des Lehr-Agenten nach der Eingangsprüfung:*", "", goal]
    if s.plan_nodes:
        parts += ["", "## Lernplan", ""]
        done = sum(1 for n in s.plan_nodes if n["id"] in s.visited)
        parts.append(f"Bearbeitet: {done} von {len(s.plan_nodes)} Schritten.")
        parts += ["", "```mermaid", "flowchart TD"]
        for n in s.plan_nodes:
            label = mermaid_label(n.get("title", n["id"]))
            parts.append(f'    {n["id"]}["{label}"]')
        for e in s.plan_edges:
            if "from" in e and "to" in e:
                parts.append(f'    {e["from"]} --> {e["to"]}')
        if s.visited:
            parts.append("    classDef besucht fill:#2e7d32,color:#fff")
            parts.append(f"    class {','.join(s.visited)} besucht")
        parts.append("```")
    if props:
        parts += ["", "## Aussagen", ""] + [f"- {link(p)}" for p in props]
    if chains:
        parts += ["", "## Begründungsketten", ""] + chains
    parts += ["", keep.rstrip("\n"), ""]
    parts += related(Bereich=[HUB], Siehe_auch=[link(p) for p in see_also])
    parts.append("")
    return "\n".join(parts)


def render_proposition(node, derived_from, supports, sources, keep: str, lessons: list[str]) -> str:
    parts = [
        frontmatter(
            erstellt=iso_date(node.get("createdAt")),
            tags=["lernen", "aussage"],
            quelle=MARKER,
            **{"lh-id": node["id"]},
        ),
        f"# {node['name']}",
        "",
        f"> {node['statement']}",
    ]
    aliases = [a for a in node.get("aliases", []) if a and a != node["statement"]]
    if aliases:
        parts += ["", "## Andere Formulierungen", ""] + [f"- {a}" for a in aliases]
    if derived_from:
        parts += ["", "## Folgt aus", ""] + derived_from
    if supports:
        parts += ["", "## Begründet", ""] + supports
    if sources:
        parts += ["", "## Quellen", ""] + [f"- {s}" for s in sources]
    parts += ["", keep.rstrip("\n"), ""]
    parts += related(Bereich=[HUB], Lektion=lessons)
    parts.append("")
    return "\n".join(parts)


def render_index(sessions, session_paths, node_count, chain_count, keep: str) -> str:
    parts = [
        frontmatter(tags=["lernen"], quelle=MARKER, **{"lh-id": "index"}),
        "# Lernen",
        "",
        "Aus LearnHarness exportiert. Die Aussagen sind Wissen, das in Lektionen vorkam, "
        "keine Bewertung, ob es sitzt.",
        "",
        f"- {len(sessions)} Sessions, {node_count} Aussagen, {chain_count} Begründungsketten",
        "",
        "## Lektionen",
        "",
    ]
    for s in sorted(sessions, key=lambda s: s.created, reverse=True):
        parts.append(f"- {s.created} · {link(session_paths[s.id])} ({s.kind})")
    parts += ["", keep.rstrip("\n"), ""]
    parts += related(Siehe_auch=["[[LearnHarness-Integration]]"])
    parts.append("")
    return "\n".join(parts)


# ── main ─────────────────────────────────────────────────────────────────


def swiss(text: str) -> str:
    """Vault convention: Swiss orthography, never ß."""
    return text.replace("ß", "ss").replace("ẞ", "SS")


def swiss_generated(content: str) -> str:
    """Normalize generated text but leave the learner's own notes untouched."""
    start = content.find(OWN_NOTES)
    if start < 0:
        return swiss(content)
    end = content.find(RELATED_SEPARATOR, start)
    end = len(content) if end < 0 else end
    return swiss(content[:start]) + content[start:end] + swiss(content[end:])


def write(path: Path, content: str, dry_run: bool, stats: dict):
    content = swiss_generated(content)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        stats["unverändert"] += 1
        return
    stats["neu" if old is None else "aktualisiert"] += 1
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)


def export(root: Path, target: Path, dry_run: bool) -> dict:
    graph = load_graph(root)
    sessions = read_sessions(root)
    sources = read_sources(root)
    nodes = {n["id"]: n for n in graph["nodes"]}
    chains = [c for c in graph["chains"] if c["conclusion"] in nodes and all(p in nodes for p in c["premises"])]

    lessons = Folder(target / "Lektionen", dry_run)
    props = Folder(target / "Aussagen", dry_run)
    session_by_id = {s.id: s for s in sessions}

    session_paths = {
        s.id: lessons.assign(s.id, s.title, s.created) for s in sorted(sessions, key=lambda s: s.created)
    }
    # Name collisions are disambiguated by the birth lesson's title.
    prop_paths = {}
    for n in sorted(nodes.values(), key=lambda n: (n.get("createdAt", ""), n["id"])):
        birth = n["provenance"][0]["chatId"] if n.get("provenance") else ""
        hint = session_by_id[birth].title if birth in session_by_id else sources.get(birth, "LearnHarness")
        prop_paths[n["id"]] = props.assign(n["id"], n["name"], hint)

    def chain_line(c) -> str:
        premises = " + ".join(link(prop_paths[p]) for p in c["premises"])
        return f"- {premises} → {link(prop_paths[c['conclusion']])}"

    def source_label(chat_id: str) -> str:
        if chat_id in session_paths:
            return link(session_paths[chat_id])
        if chat_id in sources:
            return f"Importierte Notiz: {sources[chat_id]}"
        return f"`{chat_id}`"

    stats = {"neu": 0, "aktualisiert": 0, "unverändert": 0}

    for nid, n in nodes.items():
        path = prop_paths[nid]
        keep = read_own_notes(path)
        derived = []
        supports = []
        for c in chains:
            if c["conclusion"] == nid:
                derived.append("- " + " + ".join(link(prop_paths[p]) for p in c["premises"]))
            if nid in c["premises"]:
                others = [link(prop_paths[p]) for p in c["premises"] if p != nid]
                extra = f" (zusammen mit {', '.join(others)})" if others else ""
                supports.append(f"- {link(prop_paths[c['conclusion']])}{extra}")
        origin = list(dict.fromkeys(p["chatId"] for p in n.get("provenance", [])))
        lessons_links = [link(session_paths[o]) for o in origin if o in session_paths]
        write(
            path,
            render_proposition(n, derived, supports, [source_label(o) for o in origin], keep, lessons_links),
            dry_run,
            stats,
        )

    # Lessons sharing propositions are each other's "Siehe auch" (symmetric by construction).
    lessons_of = {
        nid: {p["chatId"] for p in n.get("provenance", []) if p["chatId"] in session_paths}
        for nid, n in nodes.items()
    }

    for s in sessions:
        path = session_paths[s.id]
        keep = read_own_notes(path)
        see_also = sorted(
            {session_paths[o] for chats_ in lessons_of.values() if s.id in chats_ for o in chats_ if o != s.id},
            key=lambda p: p.stem.lower(),
        )
        mine = [
            nid for nid, n in nodes.items() if any(p["chatId"] == s.id for p in n.get("provenance", []))
        ]
        mine_set = set(mine)
        my_props = sorted((prop_paths[nid] for nid in mine), key=lambda p: p.stem.lower())
        my_chains = [
            chain_line(c)
            for c in chains
            if c["conclusion"] in mine_set or any(p["chatId"] == s.id for p in c.get("provenance", []))
        ]
        write(path, render_session(s, my_props, my_chains, keep, see_also), dry_run, stats)

    index = target / "Lernen.md"
    if index.exists() and parse_frontmatter(index.read_text(encoding="utf-8")).get("quelle") != MARKER:
        print(f"Warnung: {index} ist keine generierte Notiz, nicht überschrieben", file=sys.stderr)
    else:
        content = render_index(sessions, session_paths, len(nodes), len(chains), read_own_notes(index))
        write(index, content, dry_run, stats)

    orphans = [
        p for folder, live in ((lessons, session_paths), (props, prop_paths))
        for lh_id, p in folder.by_id.items() if lh_id not in live and p.exists()
    ]
    stats["verwaist"] = [str(p.relative_to(target)) for p in orphans]
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="LearnHarness → Obsidian-Export")
    parser.add_argument("--vault", required=True, help="Pfad zum Obsidian-Vault")
    parser.add_argument("--ziel", default="30 Ressourcen/Lernen", help="Zielordner im Vault")
    parser.add_argument("--data-dir", help="LearnHarness-Datenordner (Standard: App-Daten)")
    parser.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts schreiben")
    args = parser.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not (vault / ".obsidian").is_dir():
        print(f"Fehler: {vault} sieht nicht wie ein Obsidian-Vault aus (.obsidian fehlt)", file=sys.stderr)
        return 2
    target = (vault / args.ziel).resolve()
    if vault not in target.parents:
        print("Fehler: --ziel muss innerhalb des Vaults liegen", file=sys.stderr)
        return 2
    lock_file = open(Path(tempfile.gettempdir()) / "learnharness-obsidian-export.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Ein anderer Export läuft gerade, übersprungen.")
        return 0
    try:
        root = data_root(args.data_dir)
        stats = export(root, target, args.dry_run)
    except DataError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1

    prefix = "[Probelauf] " if args.dry_run else ""
    print(
        f"{datetime.now():%Y-%m-%d %H:%M:%S} {prefix}{target.relative_to(vault)}: {stats['neu']} neu, "
        f"{stats['aktualisiert']} aktualisiert, {stats['unverändert']} unverändert"
    )
    for path in stats["verwaist"]:
        print(f"  verwaist (nicht mehr in LearnHarness, nicht gelöscht): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
