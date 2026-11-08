"""Claude Code hook that reports session activity to Kanbot.

Claude Code runs this for each hook event, with the event JSON on stdin. It forwards a
trimmed copy to the board and always exits 0, so a stopped board never affects a
session. It deliberately imports only the standard library and nothing from the
package, so it runs by path with any Python 3.

Token counts come from the session transcript, whose format is internal to Claude Code
and can change between releases. They are best-effort and are skipped if parsing fails.
"""

import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("KANBOT_URL", "http://127.0.0.1:8787").rstrip("/") + "/api/agent-events"
# Events after which the transcript holds new assistant messages worth counting.
USAGE_EVENTS = {"Stop", "StopFailure", "SessionEnd", "SubagentStop", "PreCompact"}
KEEP = {
    "session_id",
    "hook_event_name",
    "cwd",
    "model",
    "agent_id",
    "agent_type",
    "tool_name",
    "notification_type",
    "message",
    "error",
    "source",
    "reason",
}


def transcript_usage(path):
    """Sum token usage and find the latest model in a transcript, or return None."""
    totals = dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"), 0
    )
    seen, model = set(), None
    with open(path, encoding="utf-8") as transcript:
        for line in transcript:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            # A streamed reply is written as several entries sharing one message ID.
            if not isinstance(usage, dict) or message.get("id") in seen:
                continue
            seen.add(message.get("id"))
            model = message.get("model") or model
            totals["input_tokens"] += usage.get("input_tokens") or 0
            totals["output_tokens"] += usage.get("output_tokens") or 0
            totals["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
            totals["cache_creation_tokens"] += usage.get("cache_creation_input_tokens") or 0
    return totals, model


def build_payload(event, fired=None):
    payload = {key: event[key] for key in KEEP if key in event}
    # Hooks run in parallel, so the board orders events by when they fired.
    payload["kanbot_ts"] = fired if fired is not None else time.time()
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        # Only shell commands are sent, so the board can spot ticket claims.
        payload["tool_input"] = {"command": tool_input["command"][:4000]}
    if event.get("hook_event_name") == "StopFailure" and event.get("last_assistant_message"):
        payload["last_assistant_message"] = str(event["last_assistant_message"])[:200]
    if event.get("hook_event_name") in USAGE_EVENTS and not event.get("agent_id"):
        try:
            totals, model = transcript_usage(event["transcript_path"])
            payload["kanbot_usage"] = totals
            if model and not model.startswith("<"):
                payload["kanbot_model"] = model
        except Exception:
            pass
    return payload


def main():
    fired = time.time()
    try:
        event = json.load(sys.stdin)
        data = json.dumps(build_payload(event, fired)).encode()
        request = urllib.request.Request(
            URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(request, timeout=3).close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
