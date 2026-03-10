# shared/session_store.py
# ─────────────────────────────────────────────────────────────────────────────
# Session persistence — framework-agnostic, reusable by any agent.
#
# Stores project files + conversation history on disk so sessions survive
# server restarts. Any agent that manages multi-turn file state can use this.
# ─────────────────────────────────────────────────────────────────────────────

import json
import re
import shutil
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

SESSION_FILE = ".session.json"


class Session:
    """
    A single project session: files on disk + conversation history in memory.
    Agent-agnostic — callers decide what goes in `files` and `history`.
    """

    def __init__(self, session_id: str, project_name: str,
                 created_at: str = None, output_dir: Path = None):
        self.session_id   = session_id
        self.project_name = project_name
        self.history:  list[dict] = []
        self.files:    dict[str, str] = {}
        self.created_at  = created_at or datetime.now().isoformat()
        self.project_dir = (output_dir or Path("output")) / session_id

    # ── Disk persistence ──────────────────────────────────────────────────────

    def save(self):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / SESSION_FILE).write_text(
            json.dumps({
                "session_id":   self.session_id,
                "project_name": self.project_name,
                "created_at":   self.created_at,
                "history":      self.history,
                "file_paths":   list(self.files.keys()),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, project_dir: Path,
             output_dir: Path = None) -> Optional["Session"]:
        meta = project_dir / SESSION_FILE
        if not meta.exists():
            return None
        try:
            data    = json.loads(meta.read_text(encoding="utf-8"))
            session = cls(
                data["session_id"],
                data["project_name"],
                data.get("created_at"),
                output_dir or project_dir.parent,
            )
            session.history = data.get("history", [])
            for p in data.get("file_paths", []):
                full = project_dir / p
                if full.exists():
                    session.files[p] = full.read_text(encoding="utf-8")
            return session
        except Exception as e:
            print(f"[SessionStore] Could not load {project_dir}: {e}")
            return None

    # ── File operations ───────────────────────────────────────────────────────

    def write_files(self, new_files: list[dict], deleted_files: list[str]):
        """Apply a file diff: upsert new_files, remove deleted_files."""
        for f in new_files:
            path, content = f["path"], f["content"]
            self.files[path] = content
            full = self.project_dir / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        for path in deleted_files:
            self.files.pop(path, None)
            full = self.project_dir / path
            if full.exists():
                full.unlink()
            try:
                full.parent.rmdir()
            except OSError:
                pass

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "session_id":   self.session_id,
            "project_name": self.project_name,
            "created_at":   self.created_at,
            "file_count":   len(self.files),
            "files":        list(self.files.keys()),
            "turn_count":   len([h for h in self.history
                                 if h["role"] == "user"]),
        }


class SessionStore:
    """
    In-process session registry backed by disk.
    Thread-safe for read-heavy workloads (single writer per session).
    """

    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or Path("output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._restore()

    def _restore(self):
        count = 0
        for child in sorted(self.output_dir.iterdir()):
            if not child.is_dir():
                continue
            s = Session.load(child, self.output_dir)
            if s:
                self._sessions[s.session_id] = s
                count += 1
        if count:
            print(f"[SessionStore] Restored {count} session(s)")

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, project_name: str) -> Session:
        ts         = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug       = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
        session_id = f"{slug}-{ts}"
        session    = Session(session_id, project_name, output_dir=self.output_dir)
        session.project_dir.mkdir(parents=True, exist_ok=True)
        session.save()
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def list_all(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def delete(self, session_id: str):
        s = self._sessions.pop(session_id, None)
        if s and s.project_dir.exists():
            shutil.rmtree(s.project_dir)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def get_zip(self, session_id: str) -> bytes:
        session = self.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in session.files.items():
                zf.writestr(f"{session.project_name}/{path}", content)
        return buf.getvalue()
