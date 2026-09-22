"""Remove audit entries older than a given age.

Neither page of the audit trail offers a way to remove anything, on purpose: a
trail the person being looked at can empty is not a trail. This command is the way
out for an installation that does not want to keep everything forever, and it asks
for an age every time - there is no default, so it cannot empty the log by being
run with no arguments at all.

It writes no entry about what it removed. This report is the record of a pruning.
"""
import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit_log.models import ChangeRecord, LoginRecord, removal_allowed_for_pruning


class Command(BaseCommand):
    help = ('Remove audit entries older than a given age. This is the only way to '
            'remove them: the pages and the admin do not offer one.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than', metavar='DAYS', required=True,
            help='Remove entries created more than this many days ago. Required.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be removed without removing it.')
        parser.add_argument(
            '--login-history', action='store_true',
            help='Only the login history.')
        parser.add_argument(
            '--change-log', action='store_true',
            help='Only the change log.')

    def handle(self, *args, **options):
        days = self.parse_days(options['older_than'])
        dry_run = options['dry_run']
        cutoff = timezone.now() - datetime.timedelta(days=days)

        self.stdout.write(f'Entries created before {self.stamp(cutoff)} are older than '
                          f'{days} {"day" if days == 1 else "days"}.')
        self.stdout.write('')

        found = 0
        for title, model in self.targets(options):
            queryset = model.objects.filter(created__lt=cutoff)
            count = queryset.count()
            found += count
            if count and not dry_run:
                # The one place that opens the door the model keeps shut
                with removal_allowed_for_pruning():
                    queryset.delete()
            self.stdout.write(f'  {title}: {count}')

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {found} entries would be removed, nothing was.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'{found} entries removed.'))

    def targets(self, options):
        """Both logs, unless one of them was asked for on its own."""
        only_login = options['login_history'] and not options['change_log']
        only_change = options['change_log'] and not options['login_history']
        targets = []
        if not only_change:
            targets.append(('login history', LoginRecord))
        if not only_login:
            targets.append(('change log', ChangeRecord))
        return targets

    def parse_days(self, value):
        try:
            days = int(str(value).strip())
        except (TypeError, ValueError):
            raise CommandError(f'--older-than: "{value}" is not a number of days.')
        if days < 1:
            raise CommandError('--older-than: give at least 1 day, so that running this '
                               'cannot empty the audit trail by accident.')
        return days

    def stamp(self, moment):
        return timezone.localtime(moment).strftime('%Y-%m-%d %H:%M')
