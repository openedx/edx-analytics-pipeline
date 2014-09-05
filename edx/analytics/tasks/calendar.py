"""Generate a calendar table"""

from datetime import timedelta
import logging

import luigi

from edx.analytics.tasks.database_imports import ImportIntoHiveTableTask
from edx.analytics.tasks.url import get_target_from_url
from edx.analytics.tasks.util.hive import ImportIntoHiveTableTask, HivePartition, TABLE_FORMAT_TSV
from edx.analytics.tasks.util.overwrite import OverwriteOutputMixin

log = logging.getLogger(__name__)

try:
    from isoweek import Week
except ImportError:
    log.warning('isoweek module not available')

MONTHS_PER_YEAR = 12
MONTHS_PER_QUARTER = MONTHS_PER_YEAR / 4


class CalendarMixin(OverwriteOutputMixin):
    """
    All parameters needed to generate a calendar.

    Parameters:
        interval (date interval): The set of dates that should be populated in the calendar.
        fiscal_year_beings (int): A number indicating a month of the year. The first day of this month is the first day
            of each fiscal year.
    """

    interval = luigi.DateIntervalParameter(
        default_from_config={'section': 'calendar', 'name': 'interval'}
    )
    fiscal_year_begins = luigi.IntParameter(
        default_from_config={'section': 'calendar', 'name': 'fiscal_year_begins'}
    )


class CalendarTask(CalendarMixin, luigi.Task):
    """
    Generate calendar information that is aware of leap years and other edge cases.

    This is generally used as an authoritative source for calendar information when running SQL queries and provides
    information about various dates, including the weeks they belong to, the day of the week and relation to fiscal
    years and quarters.

    Parameters:
        output_root (string): A URL to a folder that the data should be stored in.

    """

    output_root = luigi.Parameter()

    def output(self):
        return get_target_from_url(self.output_root, 'data.tsv')

    def run(self):
        self.remove_output_on_overwrite()

        with self.output().open('w') as output_file:
            for date in self.interval:
                if date.month >= self.fiscal_year_begins:
                    fiscal_year = date.year + 1
                    fiscal_quarter = (date.month - self.fiscal_year_begins) / MONTHS_PER_QUARTER
                else:
                    fiscal_year = date.year
                    fiscal_quarter = ((date.month + MONTHS_PER_YEAR) - self.fiscal_year_begins) / MONTHS_PER_QUARTER

                fiscal_quarter += 1

                iso_year, iso_weekofyear, iso_weekday = date.isocalendar()
                week = Week(iso_year, iso_weekofyear)

                column_values = (
                    date.isoformat(),
                    date.year,
                    date.month,
                    date.day,
                    '{0:04d}W{1:02d}'.format(iso_year, iso_weekofyear),
                    week.monday().isoformat(),
                    (week.sunday() + timedelta(1)).isoformat(),
                    iso_weekday,
                    '{0:04d}Q{1}'.format(fiscal_year, fiscal_quarter),
                    fiscal_year,
                    fiscal_quarter,
                )
                output_file.write('\t'.join([unicode(v) for v in column_values]) + '\n')


class ImportCalendarToHiveTask(CalendarMixin, ImportIntoHiveTableTask):
    """Ensure hive has a copy of the calendar table."""

    @property
    def table_name(self):
        return 'calendar'

    @property
    def columns(self):
        return [
            ('date', 'STRING'),
            ('year', 'INT'),
            ('month', 'INT'),
            ('day', 'INT'),
            ('iso_weekofyear', 'STRING'),
            ('iso_week_start', 'STRING'),
            ('iso_week_end', 'STRING'),
            ('iso_weekday', 'INT'),
            ('fiscal_quarter', 'INT'),
            ('fiscal_year', 'INT'),
            ('fiscal_quarterofyear', 'INT'),
        ]

    @property
    def table_format(self):
        return TABLE_FORMAT_TSV

    @property
    def partition(self):
        return HivePartition('interval', str(self.interval))

    def requires(self):
        return CalendarTask(
            output_root=self.partition_location,
            interval=self.interval,
            fiscal_year_begins=self.fiscal_year_begins,
            overwrite=self.overwrite,
        )
