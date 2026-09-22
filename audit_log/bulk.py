"""Several changes that are one change.

A housekeeping pass removes every expired backup, an import brings in a whole CSV,
a bulk edit touches a hundred devices. Row by row that is a log nobody can read and
a table nobody can search, so a block that does this kind of work says what it did
once and the entries it would have written are counted into it instead.

Two ways of saying it, and the difference between them is the point:

* collect('...') writes one entry for the block, with the number of rows and how
  they were spread over the models. A block that changed nothing writes nothing,
  so a pass that runs every ten minutes and finds nothing stays quiet.
* suspend() writes nothing at all. It is for work the application does for itself,
  which is not something anybody did.
"""
import threading
from contextlib import contextmanager

from .models import ChangeRecord

_state = threading.local()

BULK_NOTE_LIMIT = 200


class Batch:
    """What a block did, counted."""

    def __init__(self, note):
        self.note = str(note)[:BULK_NOTE_LIMIT]
        self.counts = {}

    def suppressed(self, name):
        """One record that would have been written, counted instead."""
        self.counted(name, 1)

    def removed(self, deleted):
        """Add what a delete() call reported.

        delete() hands back (total, {model: count}) - and the breakdown is the
        useful half, because a cascade took other rows with it and those rows are
        as removed as the ones that were asked for. Taking what delete() returns
        directly means a sweep over a model that is not watched row by row still
        leaves a number behind without a query of its own.
        """
        if isinstance(deleted, tuple):
            deleted = deleted[1]
        for label, count in (deleted or {}).items():
            self.counted(str(label).rsplit('.', 1)[-1], count)

    def counted(self, name, count):
        self.counts[name] = self.counts.get(name, 0) + count

    @property
    def total(self):
        return sum(self.counts.values())

    def changes(self):
        """The models it touched, as the popup shows them."""
        return {name: {'old': None, 'new': count} for name, count in self.counts.items()}


def current_batch():
    return getattr(_state, 'batch', None)


def is_suspended():
    return bool(getattr(_state, 'suspended', False))


@contextmanager
def collect(note):
    """Count what the block does and write one entry for it.

    Nested blocks count into the innermost one, and the entry of an inner block is
    written when it ends - which is what a caller expects from a block it wrapped
    itself.
    """
    batch = Batch(note)
    previous = current_batch()
    _state.batch = batch
    try:
        yield batch
    finally:
        _state.batch = previous
        if batch.total:
            write_batch(batch)


@contextmanager
def suspend():
    """Write nothing for the block: this is the application doing something for
    itself, not somebody making a change."""
    previous = is_suspended()
    _state.suspended = True
    try:
        yield
    finally:
        _state.suspended = previous


def write_batch(batch):
    """The one entry a collect block leaves behind."""
    if is_suspended():
        return
    from .signals import build_record

    record = build_record(None, ChangeRecord.ACTION_BULK, changes=batch.changes(),
                          count=batch.total, note=batch.note)
    try:
        record.save()
    except Exception:  # pragma: no cover - a failed audit write must not spread
        import logging

        logging.getLogger(__name__).warning(
            'The audit trail could not write a summary of a bulk change.', exc_info=True)
