import subprocess
import tempfile
from pathlib import Path

from aevia_mcp.vault import Vault


def make_vault(tmp: str) -> Vault:
    root = Path(tmp) / "v"
    (root / "Uni").mkdir(parents=True)
    (root / "Privat").mkdir()
    (root / ".obsidian").mkdir()
    (root / "00 Home.md").write_text("---\naliases: [home]\n---\nWelcome #index see [[Uni/Stats]] and [[Missing]]\n")
    (root / "Uni" / "Stats.md").write_text("Bayes theorem notes. #math/stats\nLink back to [[00 Home]]\n")
    (root / "Privat" / "Secret.md").write_text("bayes in private\n")
    (root / ".obsidian" / "x.md").write_text("bayes hidden\n")
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=root, check=True)
    return Vault(root, exclude=["Privat"])


def test_excludes_private_and_hidden():
    with tempfile.TemporaryDirectory() as tmp:
        v = make_vault(tmp)
        names = [str(p) for p in v.notes()]
        assert names == ["00 Home.md", "Uni/Stats.md"]
        assert [h.path for h in v.search("bayes")] == ["Uni/Stats.md"]


def test_read_links_tags_and_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        v = make_vault(tmp)
        n = v.read("00 Home.md")
        assert n["links"] == ["Missing", "Uni/Stats"] and n["tags"] == ["index"] and "aliases" in n["frontmatter"]
        for bad in ("../etc/passwd", "Privat/Secret.md", ".obsidian/x.md", "Uni"):
            try:
                v.read(bad)
                raise AssertionError(bad)
            except (FileNotFoundError, ValueError):
                pass


def test_backlinks_tags_recent():
    with tempfile.TemporaryDirectory() as tmp:
        v = make_vault(tmp)
        assert v.backlinks("00 Home.md") == ["Uni/Stats.md"]
        assert v.by_tag("math/stats") == ["Uni/Stats.md"]
        assert {r["path"] for r in v.recent()} == {"00 Home.md", "Uni/Stats.md"}
        assert v.overview()["notes"] == 2


def test_sync_recovers_from_an_empty_remote():
    with tempfile.TemporaryDirectory() as tmp:
        bare, work = Path(tmp) / "remote.git", Path(tmp) / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        v = Vault(Path(tmp) / "clone", exclude=[], remote=str(bare))
        v.sync()
        assert v.last_error is None and v.notes() == []          # empty repository: clone works, nothing to read yet
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
        (work / "Home.md").write_text("hello #x\n")
        for cmd in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first"], ["push", "-q", "origin", "HEAD:main"]):
            subprocess.run(["git", *cmd], cwd=work, check=True)
        v.sync()
        assert v.last_error is None and [str(p) for p in v.notes()] == ["Home.md"]
