"""Access to an Obsidian vault that is a git clone (kept fresh by `git pull`). Reads are free; a write is one git commit that is pushed at once."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/-]*)")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
MAX_READ = 200_000
MAX_WRITE = 200_000
WRITE_MODES = ("create", "append", "overwrite")
AUTHOR = ["-c", "user.name=Aevia MCP", "-c", "user.email=mcp@aevia.invalid"]


@dataclass
class Hit:
    path: str
    score: int
    snippets: list[str]


class Vault:
    def __init__(self, root: str | Path, exclude: list[str] | None = None, remote: str | None = None, write_exclude: list[str] | None = None):
        self.root = Path(root).resolve()  # resolve once: /var vs /private/var on macOS, symlinked mounts in pods
        self.exclude = {e.strip("/") for e in (exclude or []) if e.strip()}  # not readable, not searchable
        self.write_exclude = {e.strip("/") for e in (write_exclude or []) if e.strip()}  # not writable (hidden folders always are)
        self.remote = remote
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()

    # --- git -------------------------------------------------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        out = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, timeout=120, env=env)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[-400:] or f"git {args[0]} failed")
        return out.stdout

    def sync(self) -> None:
        """Clone on first use, fast-forward afterwards. Errors are kept for the overview instead of raised."""
        with self._lock:
            try:
                if (self.root / ".git").exists():
                    try:
                        self._git("rev-parse", "--verify", "HEAD")
                        has_commits = True
                    except RuntimeError:
                        has_commits = False
                        shutil.rmtree(self.root, ignore_errors=True)  # only an empty clone (nothing pushed yet) is thrown away
                    if has_commits:
                        self._git("pull", "--ff-only", "--quiet")  # a failing pull keeps the data and is reported in the overview
                        self.last_sync, self.last_error = time.time(), None
                        return
                if not self.remote:
                    raise RuntimeError("no remote configured")
                self.root.parent.mkdir(parents=True, exist_ok=True)
                self._git("clone", "--depth", "50", self.remote, str(self.root), cwd=self.root.parent)
                self.last_sync, self.last_error = time.time(), None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)

    def start_background_sync(self, interval: int = 120) -> None:
        def loop() -> None:
            while True:
                self.sync()
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True, name="vault-sync").start()

    # --- files -----------------------------------------------------------------------------------------------
    def _excluded(self, rel: Path, folders: set[str] | None = None) -> bool:
        parts = rel.parts
        if any(p.startswith(".") for p in parts):  # .git, .obsidian, .trash
            return True
        folders = self.exclude if folders is None else folders
        return bool(parts) and parts[0] in folders

    def notes(self, folder: str = "") -> list[Path]:
        base = (self.root / folder).resolve()
        if base != self.root and self.root not in base.parents or not base.exists():
            return []
        out = []
        for p in sorted(base.rglob("*.md")):
            rel = p.relative_to(self.root)
            if not self._excluded(rel):
                out.append(rel)
        return out

    def _safe(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        rel = p.relative_to(self.root)  # raises ValueError if it escapes the vault
        if self._excluded(rel) or p.suffix.lower() != ".md" or not p.is_file():
            raise FileNotFoundError(rel_path)
        return p

    def _target(self, rel_path: str) -> Path:
        """Where a note may be written: inside the vault, a .md file, not hidden, not in an excluded folder."""
        raw = Path(rel_path.strip().lstrip("/"))
        if not raw.parts or raw.suffix.lower() not in ("", ".md"):
            raise ValueError("path must be a note (.md) inside the vault")
        if raw.suffix == "":
            raw = raw.with_suffix(".md")
        p = (self.root / raw).resolve()
        rel = p.relative_to(self.root)  # raises ValueError if it escapes the vault
        if self._excluded(rel, self.write_exclude):
            raise ValueError("this folder is not available")
        return p

    def write(self, rel_path: str, content: str, mode: str = "create", message: str = "") -> dict:
        """Create, append to or overwrite one note, commit and push. If the push fails the commit is dropped again, so the vault never diverges."""
        if mode not in WRITE_MODES:
            raise ValueError(f"mode must be one of {', '.join(WRITE_MODES)}")
        if len(content.encode()) > MAX_WRITE:
            raise ValueError(f"content is larger than {MAX_WRITE // 1000} KB")
        with self._lock:
            p = self._target(rel_path)
            if self.remote:
                self._git("pull", "--ff-only", "--quiet")
            rel = str(p.relative_to(self.root))
            exists = p.exists()
            if mode == "create" and exists:
                raise FileExistsError(f"{rel} exists already: use mode append or overwrite")
            old = p.read_text(errors="replace") if exists else ""
            if mode == "append" and old:
                text = old + ("" if old.endswith("\n") else "\n") + content
            else:
                text = content
            if text and not text.endswith("\n"):
                text += "\n"
            before = self._git("rev-parse", "HEAD").strip()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            action = "update" if exists else "add"
            msg = (message.strip() or f"{action} {rel}")[:200] + "\n\nvia Aevia MCP"
            try:
                self._git("add", "--", rel)
                self._git(*AUTHOR, "commit", "-q", "-m", msg)
                pushed = False
                if self.remote:
                    try:
                        self._git("push", "-q", "origin", "HEAD")
                    except RuntimeError:
                        self._git(*AUTHOR, "pull", "--rebase", "--quiet")
                        self._git("push", "-q", "origin", "HEAD")
                    pushed = True
            except Exception:
                try:  # leave the clone exactly as the remote has it (or as it was before this call without a remote)
                    self._git("reset", "--hard", "@{u}" if self.remote else before)
                except RuntimeError:
                    pass
                if not exists:
                    p.unlink(missing_ok=True)
                raise
            return {"path": rel, "mode": mode, "created": not exists, "pushed": pushed, "head": self._git("log", "-1", "--format=%h").strip()}

    def read(self, rel_path: str) -> dict:
        p = self._safe(rel_path)
        text = p.read_text(errors="replace")
        truncated = len(text) > MAX_READ
        body = text[:MAX_READ]
        fm = FRONTMATTER.match(body)
        return {
            "path": str(p.relative_to(self.root)),
            "frontmatter": fm.group(1) if fm else "",
            "content": body[fm.end():] if fm else body,
            "links": sorted({m.strip() for m in WIKILINK.findall(body)}),
            "tags": sorted(set(TAG.findall(body))),
            "truncated": truncated,
        }

    def search(self, query: str, folder: str = "", limit: int = 20) -> list[Hit]:
        terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 1]
        if not terms:
            return []
        hits: list[Hit] = []
        for rel in self.notes(folder):
            try:
                text = (self.root / rel).read_text(errors="replace")
            except OSError:
                continue
            low = text.lower()
            title = rel.stem.lower()
            score = sum(low.count(t) for t in terms) + 5 * sum(t in title for t in terms)
            if not all(t in low or t in title for t in terms):
                continue
            snippets = []
            for line in text.splitlines():
                if any(t in line.lower() for t in terms):
                    snippets.append(line.strip()[:200])
                    if len(snippets) == 3:
                        break
            hits.append(Hit(str(rel), score, snippets))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    def backlinks(self, rel_path: str) -> list[str]:
        name = Path(rel_path).stem.lower()
        out = []
        for rel in self.notes():
            if rel.stem.lower() == name:
                continue
            try:
                links = {m.strip().lower().split("/")[-1] for m in WIKILINK.findall((self.root / rel).read_text(errors="replace"))}
            except OSError:
                continue
            if name in links:
                out.append(str(rel))
        return out

    def tags(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rel in self.notes():
            try:
                text = (self.root / rel).read_text(errors="replace")
            except OSError:
                continue
            for t in set(TAG.findall(text)):
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def by_tag(self, tag: str) -> list[str]:
        tag = tag.lstrip("#")
        out = []
        for rel in self.notes():
            try:
                if tag in TAG.findall((self.root / rel).read_text(errors="replace")):
                    out.append(str(rel))
            except OSError:
                continue
        return out

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            raw = self._git("log", "-n", "300", "--name-only", "--format=@@%cI")
        except RuntimeError:
            return []
        seen: dict[str, str] = {}
        date = ""
        for line in raw.splitlines():
            if line.startswith("@@"):
                date = line[2:]
            elif line.endswith(".md") and line not in seen and not self._excluded(Path(line)):
                if (self.root / line).is_file():
                    seen[line] = date
        return [{"path": p, "changed": d} for p, d in list(seen.items())[:limit]]

    def overview(self) -> dict:
        notes = self.notes()
        folders: dict[str, int] = {}
        for rel in notes:
            top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            folders[top] = folders.get(top, 0) + 1
        head = ""
        try:
            head = self._git("log", "-1", "--format=%h %cI").strip()
        except Exception:  # noqa: BLE001
            pass
        return {
            "notes": len(notes), "folders": folders, "excluded": sorted(self.exclude),
            "head": head, "last_sync_epoch": self.last_sync, "last_error": self.last_error,
        }
