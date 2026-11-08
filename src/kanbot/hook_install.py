"""Install or remove Kanbot's Claude Code hooks in a settings.json file.

Usage: kanbot-hooks install|uninstall|status [--settings PATH] [--url URL]
"""

import argparse
import json
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOOK_SCRIPT = Path(__file__).with_name("hook.py")
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
MARKER = "kanbot-hook"
EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": None,
    "PostToolUse": None,
    "PostToolUseFailure": None,
    "PermissionRequest": None,
    "Notification": "permission_prompt|idle_prompt|elicitation_dialog|agent_needs_input",
    "Stop": None,
    "StopFailure": None,
    "SubagentStart": None,
    "SubagentStop": None,
    "SessionEnd": None,
}


def hook_command(url):
    # The marker comment lets uninstall find these entries without touching others.
    python = shlex.quote(sys.executable if Path(sys.executable).exists() else "python3")
    return f"KANBOT_URL={shlex.quote(url)} {python} {shlex.quote(str(HOOK_SCRIPT))} # {MARKER}"


def is_ours(handler):
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def remove_ours(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def add_ours(settings, url):
    remove_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        group = {
            "hooks": [
                {"type": "command", "command": hook_command(url), "async": True, "timeout": 10}
            ]
        }
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return settings


def load(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def save(path, settings):
    if path.exists():
        backup = path.with_name(f"{path.name}.kanbot-backup-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(f"Backed up {path} to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage Kanbot's Claude Code hooks.")
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--url", default="http://127.0.0.1:8787", help="Kanbot's address")
    args = parser.parse_args(argv)

    settings = load(args.settings)
    installed = sorted(
        event
        for event, groups in settings.get("hooks", {}).items()
        if any(is_ours(h) for g in groups for h in g.get("hooks", []))
    )
    if args.action == "status":
        print(f"Installed for: {', '.join(installed)}" if installed else "Not installed.")
        return 0
    if args.action == "install":
        save(args.settings, add_ours(settings, args.url))
        print(f"Installed hooks for {len(EVENTS)} events, reporting to {args.url}.")
        print("New Claude Code sessions report to Kanbot; running ones need restarting.")
    else:
        if not installed:
            print("Not installed; nothing to remove.")
            return 0
        save(args.settings, remove_ours(settings))
        print("Removed Kanbot's hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
