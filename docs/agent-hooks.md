# Agent sessions

Kanbot can show what each Claude Code session is doing. For each session it tracks:

- **State:** working, waiting for your input, idle, stopped by a usage limit, or failed.
- **Context:** the model, the project, and the ticket it's working on.
- **Activity:** how long it has been working, turns, tool calls and tokens used.

Claude Code hooks feed this. They're small commands Claude Code runs on events such as a prompt, a tool call or a permission prompt.

## Install

Run these yourself. They change your Claude Code settings, so agents aren't allowed to run them for you.

```sh
uv run kanbot-hooks install                 # reports to http://127.0.0.1:8787
uv run kanbot-hooks install --url http://nas.local:8787
uv run kanbot-hooks status
uv run kanbot-hooks uninstall
```

`install` adds the hooks to `~/.claude/settings.json`, after saving a timestamped backup beside it. Your other settings and hooks are left alone, and running `install` again replaces only Kanbot's entries. New sessions start reporting straight away, but sessions that are already running need restarting.

The hooks run asynchronously and the script always exits cleanly, so a slow or stopped board never slows down or interrupts a session.

## What's recorded

The hook script, `src/kanbot/hook.py`, sends the session ID, event name, working directory, model, tool name, notification type and error. It sends shell commands only, so the board can link a session to a ticket it claims or moves to In progress. The board keeps only derived figures: state, counters, token totals and the ticket ID. **Prompts, tool input and tool output are never stored.**

| Signal | Source |
|---|---|
| Working | A prompt or tool call |
| Waiting for input | A permission prompt or question, from the `PermissionRequest` and `Notification` hooks |
| Idle | The turn finished (`Stop`), or Claude is waiting for your next prompt |
| Usage limit | `StopFailure` with error `rate_limit` |
| Ticket | A shell command that claims a ticket, or moves one to `in_progress` |
| Tokens and model | The session transcript, read after each turn |

**Token counts are best-effort.** No hook reports tokens, so the script adds up the usage in the session transcript. That format is internal to Claude Code and can change between releases. If parsing fails, token counts stop updating, and everything else carries on working.

Sessions that end without a `SessionEnd` event, for example when the terminal is killed, are hidden after 12 hours of inactivity. You can also dismiss them.
