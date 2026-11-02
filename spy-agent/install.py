"""Install the `spy` command so you can type `spy` in any terminal, in any folder.

    python install.py              install (run it with your Anaconda python)
    python install.py --uninstall  remove the command (your ~/.spy settings are kept)

What it does - no admin rights, nothing downloaded:
  Windows : writes %USERPROFILE%\\.spy\\bin\\spy.cmd and adds that folder to YOUR user PATH
            (registry HKCU\\Environment, the same place the Windows settings dialog edits).
  Linux/mac: writes ~/.local/bin/spy and tells you if that folder isn't on PATH.
The launcher pins the Python you ran this with (your Anaconda python.exe), so `spy`
works from cmd, PowerShell and Windows Terminal - not only the Anaconda Prompt.
Then it creates ~/.spy/config.json (from config.example.json) and the master ~/.spy/SPY.md.
Open a NEW terminal afterwards so the PATH change takes effect.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPY_HOME = Path(os.environ.get("SPY_HOME", str(Path.home() / ".spy")))


def _bin_dir() -> Path:
    return SPY_HOME / "bin" if os.name == "nt" else Path.home() / ".local" / "bin"


def _write_launcher() -> Path:
    b = _bin_dir()
    b.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    if os.name == "nt":
        pyw = Path(py)
        if pyw.name.lower() == "pythonw.exe":
            py = str(pyw.with_name("python.exe"))
        target = b / "spy.cmd"
        prefix = sys.prefix
        # Anaconda's python needs its DLL folders on PATH (normally done by `conda activate`),
        # otherwise e.g. the ssl module fails to load. setlocal keeps this change inside spy only.
        dll_dirs = [prefix, prefix + r"\Library\mingw-w64\bin", prefix + r"\Library\usr\bin",
                    prefix + r"\Library\bin", prefix + r"\Scripts"]
        target.write_text("@echo off\r\nsetlocal\r\nset \"PATH=%s;%%PATH%%\"\r\n\"%s\" \"%s\" %%*\r\n"
                          % (";".join(dll_dirs), py, HERE / "run_spy.py"), encoding="utf-8")
    else:
        target = b / "spy"
        target.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (py, HERE / "run_spy.py"), encoding="utf-8")
        target.chmod(0o755)
    return target


def _win_user_path(add: bool, folder: str) -> bool:
    """Add/remove folder in the user PATH. Returns True if PATH changed."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in value.split(";") if p]
        present = any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(folder.rstrip("\\")) for p in parts)
        if add and not present:
            parts.append(folder)
        elif not add and present:
            parts = [p for p in parts if os.path.normcase(p.rstrip("\\")) != os.path.normcase(folder.rstrip("\\"))]
        else:
            return False
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                          ";".join(parts))
    try:  # tell Explorer/new terminals that the environment changed
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x2
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                                                 SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()))
    except Exception:
        pass
    return True


def _setup_home():
    SPY_HOME.mkdir(parents=True, exist_ok=True)
    cfg = SPY_HOME / "config.json"
    if not cfg.exists():
        cfg.write_text((HERE / "config.example.json").read_text(encoding="utf-8"), encoding="utf-8")
        print("  created  %s   <- fill in the <PLACEHOLDERS> (API URL, key, model ids)" % cfg)
    else:
        print("  kept     %s" % cfg)
    master = SPY_HOME / "SPY.md"
    if not master.exists():
        sys.path.insert(0, str(HERE))
        from spy.context import GLOBAL_TEMPLATE
        master.write_text(GLOBAL_TEMPLATE, encoding="utf-8")
        print("  created  %s   <- your master SPY.md (applies to every project)" % master)
    else:
        print("  kept     %s" % master)
    cmds = SPY_HOME / "commands"
    if not cmds.exists():
        cmds.mkdir()
        for f in (HERE / "examples" / "commands").glob("*.md"):
            (cmds / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        print("  created  %s   <- custom /commands (review, commit, brief)" % cmds)


def install():
    if sys.version_info < (3, 7):
        sys.exit("Spy needs Python 3.7+ (you ran %s). Run this with your Anaconda python." % sys.version.split()[0])
    print("Installing the `spy` command (Python: %s)" % sys.executable)
    target = _write_launcher()
    print("  created  %s" % target)
    if os.name == "nt":
        changed = _win_user_path(True, str(target.parent))
        print("  %s %s on your user PATH" % ("added   " if changed else "already ", target.parent))
    else:
        on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
        if not on_path:
            print("  NOTE: add this line to ~/.bashrc (or ~/.zshrc):  export PATH=\"%s:$PATH\"" % target.parent)
    _setup_home()
    print("\nDone. Open a NEW terminal, then:")
    print("   cd <your project folder>")
    print("   spy doctor      (checks the API connection - after filling in config.json)")
    print("   spy             (starts Spy in that folder)")


def uninstall():
    target = _bin_dir() / ("spy.cmd" if os.name == "nt" else "spy")
    if target.exists():
        target.unlink()
        print("removed %s" % target)
    if os.name == "nt":
        if _win_user_path(False, str(target.parent)):
            print("removed %s from your user PATH" % target.parent)
    print("Your settings in %s were kept (delete that folder to remove everything)." % SPY_HOME)


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv else install()
