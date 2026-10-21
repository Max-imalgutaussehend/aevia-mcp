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


def _remote_vault(tmp):
    """A vault that is a clone of a bare remote, like in the cluster."""
    src = make_vault(tmp)
    bare = Path(tmp) / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src.root), str(bare)], check=True)
    clone = Path(tmp) / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    return Vault(clone, exclude=["Privat"], remote=str(bare)), bare


def test_write_create_append_overwrite_and_push():
    with tempfile.TemporaryDirectory() as tmp:
        v, bare = _remote_vault(tmp)
        r = v.write("Ideen/Neu", "Erste Zeile #idee", "create", "Idee festhalten")
        assert r["path"] == "Ideen/Neu.md" and r["created"] and r["pushed"]
        assert (v.root / "Ideen" / "Neu.md").read_text() == "Erste Zeile #idee\n"
        v.write("Ideen/Neu.md", "Zweite Zeile", "append")
        assert (v.root / "Ideen" / "Neu.md").read_text() == "Erste Zeile #idee\nZweite Zeile\n"
        v.write("Ideen/Neu.md", "Ersetzt", "overwrite")
        log = subprocess.run(["git", "log", "--format=%an|%s", "-3"], cwd=bare, capture_output=True, text=True, check=True).stdout.splitlines()
        assert log[2] == "Aevia MCP|Idee festhalten" and "update Ideen/Neu.md" in log[0]
        shown = subprocess.run(["git", "show", "HEAD:Ideen/Neu.md"], cwd=bare, capture_output=True, text=True, check=True).stdout
        assert shown == "Ersetzt\n"
        assert v.write("Heute", "x", "append")["created"]  # append to a missing note creates it


def test_write_refuses_bad_paths_and_overwriting_create():
    with tempfile.TemporaryDirectory() as tmp:
        v, _ = _remote_vault(tmp)
        for bad in ("../x.md", "a/../../x.md", ".obsidian/x.md", ".git/config", "Uni/bild.png", ""):
            try:
                v.write(bad, "x")
                raise AssertionError(bad)
            except ValueError:
                pass
        try:
            v.write("00 Home.md", "x", "create")
            raise AssertionError("create must not overwrite")
        except FileExistsError:
            pass
        try:
            v.write("A.md", "x", "delete")
            raise AssertionError("mode")
        except ValueError:
            pass
        assert (v.root / "00 Home.md").read_text().startswith("---")
        assert v.write("/Wurzel/Notiz", "x")["path"] == "Wurzel/Notiz.md"  # a leading slash means vault-relative, never the filesystem root


def test_failed_push_leaves_no_trace():
    with tempfile.TemporaryDirectory() as tmp:
        v, bare = _remote_vault(tmp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=v.root, capture_output=True, text=True, check=True).stdout
        v.remote = str(Path(tmp) / "gone.git")
        subprocess.run(["git", "remote", "set-url", "origin", v.remote], cwd=v.root, check=True)
        try:
            v.write("Neu.md", "x")
            raise AssertionError("push should fail")
        except RuntimeError:
            pass
        assert not (v.root / "Neu.md").exists()
        assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=v.root, capture_output=True, text=True, check=True).stdout == head


def test_private_folder_is_writable_but_not_readable_unless_configured():
    with tempfile.TemporaryDirectory() as tmp:
        v, _ = _remote_vault(tmp)
        assert v.write("Privat/Tagebuch", "Heute war gut", "create")["path"] == "Privat/Tagebuch.md"
        v.write("Privat/Tagebuch", "Noch ein Satz", "append")
        assert (v.root / "Privat" / "Tagebuch.md").read_text() == "Heute war gut\nNoch ein Satz\n"
        assert "Privat/Tagebuch.md" not in [str(p) for p in v.notes()]  # still invisible to reads and search
        assert v.search("Tagebuch") == [] and v.search("gut") == []
        try:
            v.read("Privat/Tagebuch.md")
            raise AssertionError("private notes must stay unreadable")
        except FileNotFoundError:
            pass
        v.write_exclude = {"Privat"}  # the operator can lock writing again
        try:
            v.write("Privat/Neu.md", "x")
            raise AssertionError("write exclude")
        except ValueError:
            pass
