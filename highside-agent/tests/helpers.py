import tempfile
from pathlib import Path

from hsa.config import DEFAULTS, Config, deep_merge
from hsa.ui import UI


class ScriptedUI(UI):
    """Non-interactive UI: records output, answers permission prompts from a list."""

    def __init__(self, answers=None):
        super().__init__(None, color=False)
        self.answers = list(answers or [])
        self.log = []
        self.asked = []

    def _print(self, s="", end="\n"):
        self.log.append(s)

    def stream(self, kind, chunk):
        self.log.append(chunk)

    def start_thinking(self, label="thinking"):
        pass

    def confirm_tool(self, tool, args):
        self.asked.append(tool.describe_call(args))
        return self.answers.pop(0) if self.answers else ("yes", "")

    def ask(self, q, options):
        return "blue"

    def text(self):
        return "\n".join(self.log)


def make_config(tmp: Path, **overrides) -> Config:
    data = deep_merge(DEFAULTS, overrides)
    cfg = Config(data, tmp, ["test"])
    cfg.home = tmp / ".hsa_home"
    cfg.home.mkdir(parents=True, exist_ok=True)
    from hsa import net
    net.configure_web(cfg)
    return cfg


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="hsa_test_")).resolve()
