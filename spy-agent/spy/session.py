"""Conversation persistence (resume / continue)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class Session:
    def __init__(self, home: Path, sid: Optional[str] = None):
        """home: the per-project data dir (config.project_data_dir) so each project
        has its own list of sessions."""
        self.dir = home / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.id = sid or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.path = self.dir / (self.id + ".json")

    def save(self, agent) -> None:
        title = next((m["content"][:80] for m in agent.messages if m["role"] == "user"
                      and not m["content"].startswith("[")), "")
        data = {"id": self.id, "saved": time.time(), "cwd": str(agent.ctx.cwd), "model": agent.llm.name,
                "title": title, "todos": agent.ctx.todos, "messages": agent.messages}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def load_into(self, agent) -> Dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        agent.messages = data.get("messages", [])
        agent.ctx.todos[:] = data.get("todos", [])
        return data

    @staticmethod
    def list_recent(home: Path, n: int = 15) -> List[Dict[str, Any]]:
        d = home / "sessions"
        if not d.is_dir():
            return []
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
        out = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({"id": data.get("id", f.stem), "title": data.get("title", ""), "cwd": data.get("cwd", ""),
                            "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("saved", 0))),
                            "messages": len(data.get("messages", []))})
            except Exception:
                continue
        return out
