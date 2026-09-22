"""Show or set what a new command schedule starts out as.

The same values the Schedule Defaults page holds, for an installation that is
set up from a script or a container instead of from the browser.
"""
import datetime

from django.core.management.base import BaseCommand, CommandError

from fleet_commander.models import ScheduleDefaults, format_interval, parse_interval

TIME_FORMATS = ('%H:%M', '%H:%M:%S')


class Command(BaseCommand):
    help = ('Show the values a new command schedule is created with, or change them. '
            'Without an option the values that are in use are printed.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--start-time', metavar='HH:MM',
            help='The time of day a new schedule begins at (for example 03:00).')
        parser.add_argument(
            '--repeat', metavar='INTERVAL',
            help="How often a new schedule repeats: 7d, 24h or 30m. 0 is a single run.")
        parser.add_argument(
            '--reset', action='store_true',
            help='Put the built-in defaults back.')

    def handle(self, *args, **options):
        schedule_defaults = ScheduleDefaults.load()
        changed = []

        if options['reset']:
            schedule_defaults.start_time = self.field_default('start_time')
            schedule_defaults.repeat_interval = self.field_default('repeat_interval')
            changed.append('reset')

        if options['start_time'] is not None:
            schedule_defaults.start_time = self.parse_time(options['start_time'])
            changed.append('start time')

        if options['repeat'] is not None:
            try:
                schedule_defaults.repeat_interval = parse_interval(options['repeat'])
            except ValueError as error:
                raise CommandError(f'--repeat: {error}')
            changed.append('repeat interval')

        if changed:
            schedule_defaults.save()
            self.stdout.write(self.style.SUCCESS(
                f'Schedule defaults saved ({", ".join(changed)}).'))

        self.report(schedule_defaults)

    def report(self, schedule_defaults):
        """The values in use, so the command says what it did even when it only
        looked."""
        self.stdout.write(f'Start time:      {schedule_defaults.start_time.strftime("%H:%M")}')
        self.stdout.write(f'Repeat interval: {format_interval(schedule_defaults.repeat_interval)}')
        self.stdout.write('')
        self.stdout.write('A new schedule starts at the next time the start time comes around and '
                          'repeats in this interval.')
        self.stdout.write('Schedules that already exist keep the values they were saved with.')

    def parse_time(self, value):
        for time_format in TIME_FORMATS:
            try:
                return datetime.datetime.strptime(str(value).strip(), time_format).time()
            except ValueError:
                continue
        raise CommandError(
            f'--start-time: "{value}" is not a time of day. Write it as HH:MM, for example 03:00.')

    def field_default(self, name):
        return ScheduleDefaults._meta.get_field(name).get_default()
