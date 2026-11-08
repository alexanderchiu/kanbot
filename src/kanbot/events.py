"""Change notifications for browsers, sent as server-sent events from GET /api/events.

After each successful write the server publishes what changed: "tickets" or "agents".
Pages listening to the stream then refetch at once, rather than waiting for a poll.

With SQLite, changes come from this server's own writes. With Postgres, they go through
NOTIFY and come back through LISTEN, so every server sharing the database, such as one
on a laptop and one on a NAS, hears about changes made through any of them.
"""

import logging
import queue
import threading
import time

CHANNEL = "kanbot_changes"
KINDS = ("tickets", "agents")
log = logging.getLogger(__name__)


class Changes:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.subscribers = set()
        self.closed = threading.Event()
        self.listener = None

    def subscribe(self):
        """A queue that receives a set of changed kinds per change, or None when closing."""
        subscriber = queue.SimpleQueue()
        with self.lock:
            self.subscribers.add(subscriber)
            # Only listen to Postgres while someone is watching.
            if not self.db.is_sqlite and self.listener is None:
                self.listener = threading.Thread(target=self.listen, daemon=True)
                self.listener.start()
        return subscriber

    def unsubscribe(self, subscriber):
        with self.lock:
            self.subscribers.discard(subscriber)

    def publish(self, kind):
        """Announce a change made through this server."""
        if self.db.is_sqlite:
            self.deliver(kind)
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    "SELECT pg_notify(:channel, :kind)", {"channel": CHANNEL, "kind": kind}
                )
        except Exception as error:
            # Pages still poll now and then, so a lost notification only delays them.
            log.warning("Couldn't send a change notification: %r", error)
            self.deliver(kind)

    def deliver(self, *kinds):
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.put(frozenset(kinds))

    def listen(self):
        """Pass Postgres notifications to subscribers, reconnecting if the link drops."""
        delay = 1
        while not self.closed.is_set():
            try:
                conn = self.db.listen_connection(CHANNEL)
            except Exception as error:
                log.warning("Can't listen for changes, retrying in %ss: %r", delay, error)
                self.closed.wait(delay)
                delay = min(delay * 2, 60)
                continue
            delay = 1
            # Changes made while the link was down were missed, so have pages refetch.
            self.deliver(*KINDS)
            try:
                with conn:
                    while not self.closed.is_set():
                        for notification in conn.notifies(timeout=5):
                            if notification.payload in KINDS:
                                self.deliver(notification.payload)
            except Exception as error:
                if not self.closed.is_set():
                    log.warning("Lost the change listener, reconnecting: %r", error)
                    time.sleep(1)

    def close(self):
        self.closed.set()
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.put(None)
