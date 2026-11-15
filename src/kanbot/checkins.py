"""Check-ins: ask whoever holds a quiet in-progress or blocked ticket whether it's still live.

A background job in the server looks at assigned tickets that are in progress or blocked
and haven't changed for a while, and adds a note from "kanbot" asking about them. The
question shows on the board, and agents with Kanbot's hooks also see it in their session
(see message_for), so they can say where it stands or move the ticket on.

Each ticket has at most one open question. Any change to the ticket, such as a note
or a status change, answers it, and the ticket is asked about again only once it has
been quiet for another full period. Unassigned tickets are left alone: a blocked ticket
with no assignee is usually waiting on a person, not an agent.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta

from kanbot.db import now

AUTHOR = "kanbot"
DEFAULT_MINUTES = 60
# The first run waits a little, so restarting the server doesn't ask about everything
# straight away.
FIRST_RUN_SECONDS = 300
QUESTIONS = {
    "in_progress": (
        "Check-in: are you still working on this? If so, add a note saying where it"
        " stands. If not, move it to follow_up, done or cancelled."
    ),
    "blocked": (
        "Check-in: is this still blocked? If so, add a note saying what it's waiting on."
        " If not, move it on."
    ),
}
log = logging.getLogger(__name__)

# The ticket's newest note, when that note is an unanswered check-in. A note updates the
# ticket's updated_at, so any later change to the ticket leaves updated_at past it.
OPEN_QUESTION = """
    SELECT n.id, n.body FROM notes n
    WHERE n.ticket_id = :id AND n.author = :author AND n.created_at >= :updated_at
      AND n.id = (SELECT max(m.id) FROM notes m WHERE m.ticket_id = :id)
"""


def stamp_before(minutes, at=None):
    at = at or datetime.now(UTC)
    return (at - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_checkins(db, minutes=DEFAULT_MINUTES):
    """Ask about each quiet ticket that has no open question. Returns the ticket IDs."""
    asked = []
    # The write lock makes servers sharing a database take turns, so none ask twice.
    with db.transaction() as conn:
        tickets = conn.execute(
            "SELECT id, status, assignee, updated_at FROM tickets"
            " WHERE status IN ('in_progress', 'blocked') AND assignee IS NOT NULL"
            " AND updated_at < :cutoff ORDER BY id",
            {"cutoff": stamp_before(minutes)},
        ).all()
        stamp = now()
        for ticket in tickets:
            params = {"id": ticket["id"], "author": AUTHOR, "updated_at": ticket["updated_at"]}
            if conn.execute(OPEN_QUESTION, params).first():
                continue
            conn.execute(
                "INSERT INTO notes (ticket_id, author, body, created_at)"
                " VALUES (:id, :author, :body, :stamp)",
                {
                    "id": ticket["id"],
                    "author": AUTHOR,
                    "body": f"@{ticket['assignee']} {QUESTIONS[ticket['status']]}",
                    "stamp": stamp,
                },
            )
            conn.execute(
                "UPDATE tickets SET updated_at = :stamp WHERE id = :id",
                {"stamp": stamp, "id": ticket["id"]},
            )
            asked.append(ticket["id"])
    return asked


class Checkins:
    """Runs run_checkins every period on a daemon thread, and remembers which agent
    sessions have already been shown each question."""

    def __init__(self, db, changes, minutes=DEFAULT_MINUTES, first_run=FIRST_RUN_SECONDS):
        self.db, self.changes, self.minutes, self.first_run = db, changes, minutes, first_run
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.shown = set()
        self.thread = None

    def start(self):
        if self.minutes and self.minutes > 0:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set()

    def loop(self):
        wait = self.first_run
        while not self.stopped.wait(wait):
            wait = self.minutes * 60
            try:
                asked = run_checkins(self.db, self.minutes)
            except Exception as error:
                log.warning("Couldn't run check-ins: %r", error)
                continue
            if asked:
                log.info("Asked for check-ins on tickets %s", asked)
                self.changes.publish("tickets")

    def message_for(self, session_key, ticket_id):
        """The open question on the session's ticket, once per session and question."""
        if ticket_id is None:
            return None
        with self.db.connect() as conn:
            ticket = conn.execute(
                "SELECT id, title, status, updated_at FROM tickets WHERE id = :id",
                {"id": ticket_id},
            ).first()
            if ticket is None or ticket["status"] not in QUESTIONS:
                return None
            params = {"id": ticket_id, "author": AUTHOR, "updated_at": ticket["updated_at"]}
            question = conn.execute(OPEN_QUESTION, params).first()
        if question is None:
            return None
        with self.lock:
            if (session_key, question["id"]) in self.shown:
                return None
            self.shown.add((session_key, question["id"]))
        return (
            f"Kanbot asks about ticket #{ticket['id']}, {ticket['title']!r}"
            f" ({ticket['status']}): {QUESTIONS[ticket['status']]}"
            f" Answer with a note on the ticket or by changing its status."
        )
