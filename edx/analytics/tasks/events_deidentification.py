"""Deidentify course event files by removing/stubbing user information."""

import logging
import re
import luigi
import luigi.date_interval
import gzip
import csv
import cjson
import os
import sys
from collections import namedtuple

from edx.analytics.tasks.pathutil import PathSetTask, EventLogSelectionMixin, EventLogSelectionDownstreamMixin
from edx.analytics.tasks.mapreduce import MultiOutputMapReduceJobTask, MapReduceJobTaskMixin
from edx.analytics.tasks.url import ExternalURL, url_path_join
from edx.analytics.tasks.util.deid_util import DeidentifierMixin, DeidentifierParamsMixin
import edx.analytics.tasks.util.opaque_key_util as opaque_key_util
import edx.analytics.tasks.util.csv_util
import edx.analytics.tasks.util.deid_util as deid_util
from edx.analytics.tasks.util import eventlog

log = logging.getLogger(__name__)

ExplicitEventType = namedtuple("ExplicitEventType", ["event_source", "event_type"])


class DeidentifyCourseEventsTask(DeidentifierMixin, MultiOutputMapReduceJobTask):
    """
    Task to deidentify events for a particular course.

    Uses the output produced by EventExportByCourseTask as source for this task.
    """

    course = luigi.Parameter(default=None)
    explicit_event_whitelist = luigi.Parameter(
        config_path={'section': 'events-deidentification', 'name': 'explicit_event_whitelist'}
    )
    dump_root = luigi.Parameter(default=None)

    # TODO: placeholder for now.  Update with something real once plumbing works.
    user_info = None

    def requires(self):
        filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(self.course)
        event_files_url = url_path_join(self.dump_root, filename_safe_course_id, 'events')
        return PathSetTask([event_files_url], ['*'])

    def requires_local(self):
        filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(self.course)
        results = {
            'auth_user': PathSetTask([url_path_join(self.dump_root, filename_safe_course_id, 'state')], ['*-auth_user-*'])
        }
        if os.path.basename(self.explicit_event_whitelist) != self.explicit_event_whitelist:
            results['explicit_events'] = ExternalURL(url=self.explicit_event_whitelist)

        return results

    def init_local(self):
        super(DeidentifyCourseEventsTask, self).init_local()

        self.explicit_events = []
        if self.input_local().get('explicit_events') is not None:
            with self.input_local()['explicit_events'].open('r') as explicit_events_file:
                self.explicit_events = self.parse_explicit_events_file(explicit_events_file)
        else:
            default_filepath = os.path.join(sys.prefix, 'share', 'edx.analytics.tasks', self.explicit_event_whitelist)
            with open(default_filepath, 'r') as explicit_events_file:
                self.explicit_events = self.parse_explicit_events_file(explicit_events_file)

        # read the latest auth_user for this course in memory, needed to remap usernames
        latest_auth_user_target = sorted(self.input_local()['auth_user'], key=lambda target: target.path)[-1]

        self.username_map = {}

        with latest_auth_user_target.open('r') as auth_user_file:
            reader = csv.reader(auth_user_file, dialect='mysqlexport')
            next(reader, None)  # skip header
            for row in reader:
                user_id = row[0]
                username = row[1]
                self.username_map[username] = user_id

    def parse_explicit_events_file(self, explicit_events_file):
        """Parse explicit_events_file and load in memory."""

        events = set()
        for line in explicit_events_file:
            if not line.strip():
                continue
            if line.startswith('#'):
                continue
            _, event_source, event_type = line.rstrip('\n').split("\t")
            events.add(ExplicitEventType(event_source, event_type))

        return events

    def mapper(self, line):
        event = eventlog.parse_json_event(line)
        date_string = event['time'].split("T")[0]

        filtered_event = self.filter_event(event)

        if filtered_event is None:
            return

        deidentified_event = self.deidentify_event(filtered_event)
        if deidentified_event is None:
            return

        yield date_string.encode('utf-8'), cjson.encode(deidentified_event)

    def multi_output_reducer(self, _key, values, output_file):
        with gzip.GzipFile(mode='wb', fileobj=output_file) as outfile:
            try:
                for value in values:
                    outfile.write(value.strip())
                    outfile.write('\n')
                    # WARNING: This line ensures that Hadoop knows that our process is not sitting in an infinite loop.
                    # Do not remove it.
                    self.incr_counter('Deidentified Event Exports', 'Raw Bytes Written', len(value) + 1)
            finally:
                outfile.close()

    def output_path_for_key(self, key):
        filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(self.course)

        return url_path_join(
            self.output_root,
            filename_safe_course_id,
            'events',
            '{course}-events-{date}.log.gz'.format(
                course=filename_safe_course_id,
                date=key,
            )
        )

    def filter_event(self, event):
        """Filter event using different event filtering criteria."""
        if event is None:
            return None

        event_type = event.get('event_type')

        event_source = event.get('event_source')

        if event_type is None or event_source is None:
            return None

        # TODO: filter out synthetic events as well.

        if event_source == 'server' and event_type.startswith('/'):
            return self.filter_implicit_event(event)
        elif ExplicitEventType(event_source, event_type) in self.explicit_events:
            return event

        return None

    def filter_implicit_event(self, event):
        """Filter implicit event using the whitelist patterns."""

        event_type = event.get('event_type')

        match = opaque_key_util.COURSE_REGEX.match(event_type)
        if match:
            course_id_string = match.group('course_id')
            event_type = event_type.replace(course_id_string, '(course_id)')

        for included_event_type in deid_util.IMPLICIT_EVENT_TYPE_PATTERNS:
            match = re.match(included_event_type, event_type)
            if match:
                return event

        return None

    def get_user_id_as_int(self, user_id):
        """Convert possible str value of user_id to int or None."""
        if user_id is not None and not isinstance(user_id, int):
            if len(user_id) == 0:
                user_id = None
            else:
                user_id = int(user_id)
        return user_id

    def get_user_id_for_username(self, username):
        """Return user_id (as an int) for a username, or None if not found."""
        try:
            user_id = self.username_map[username]
        except KeyError:
            log.error("Unable to find username: %s in the username_map", username)
            return None
        return user_id

    def remap_username(self, username):
        """Return remapped version of username, or None if not remapped."""
        user_id = self.get_user_id_for_username(username)
        if user_id is not None:
            return self.generate_deid_username_from_user_id(user_id)
        return None

    def deidentify_event(self, event):
        """Deidentify event by removing/stubbing user information."""

        # Create a string to use when logging errors, to provide some context.
        event_type = event.get('event_type')
        if isinstance(event_type, str):
            event_type = event_type.decode('utf8')
        event_source = event.get('event_source')
        debug_str = u" [source='{}' type='{}']".format(event_source, event_type)

        # Fetch and then remap username.
        username_entry = None
        username = eventlog.get_event_username(event)
        if username is not None:
            # TODO: use whatever the lookup structure for getting fullname, given username.
            if self.user_info is not None:
                decoded_username = username.decode('utf8')
                username_entry = self.user_info.get(decoded_username)
                if username_entry is None:
                    log.error(u"username ('%s') is unknown to user_info %s", username, debug_str)

            try:
                event['username'] = self.generate_deid_username_from_user_id(self.username_map[event['username']])
            except KeyError:
                log.error("Unable to find username: %s in the username_map", event['username'])
                return None

        # Get the user_id from context, either as an int or None, and remap.
        userid_entry = None
        user_id = self.get_user_id_as_int(event.get('context', {}).get('user_id'))
        if user_id is not None:
            # TODO: use whatever the lookup structure for getting fullname, given user_id.
            if self.user_info is not None:
                userid_entry = self.user_info.get(user_id)
                if userid_entry is None:
                    log.error(u"user_id ('%s') is unknown to user_info %s", user_id, debug_str)
                elif username_entry and userid_entry != username_entry:
                    log.error(
                        u"user_id ('%s'='%s') does not match username ('%s'='%s') %s",
                        userid_entry.user_id, userid_entry.username, username_entry.username, username_entry.user_id, debug_str,
                    )
            event['context']['user_id'] = self.remap_id(user_id)

        # Clean context.
        if 'context' in event:
            # These aren't present in current events, but are generated historically by some implicit events.
            # TODO: should these be removed, or set to ''?
            event['context'].pop('host', None)
            event['context'].pop('ip', None)
            # Remap value of username in context, if it is present.  (Removed in more recent events.)
            if 'username' in event['context']:
                event['context']['username'] = self.remap_username(event['context']['username'])

            # Clean event.context.path.
            if 'path' in event['context']:
                updated_context_path = self.deidentifier.deidentify_structure(event['context']['path'], u"context.path", userid_entry)
                if updated_context_path is not None:
                    event['context']['path'] = updated_context_path
                    if self.deidentifier.is_logging_enabled():
                        log.info(u"Deidentified event.context.path for user_id '%s' %s", user_id, debug_str)

            # TODO: check event.context.client.ip
            # TODO: check event.context.client.device.id
            # TODO: check event.context.client.device.userid

        # Look into the event payload.
        event_data = eventlog.get_event_data(event)
        if event_data:
            # TODO: I had a test to see if event_data were a dict.  I think that this was at least
            # due to the non-JSON string versions of event data getting through.

            # Get the user_id from payload and remap.
            event_userid_entry = None
            event_user_id = self.get_user_id_as_int(event_data.get('user_id'))
            if event_user_id is not None:
                # TODO: use whatever the lookup structure for getting fullname, given username.
                if self.user_info is not None:
                    event_userid_entry = self.user_info.get(event_user_id)
                    if event_userid_entry is None:
                        log.error(u"Event_user_id ('%s') is unknown to user_info %s", event_user_id, debug_str)
                event_data['user_id'] = self.remap_id(event_user_id)

            # Remap value of username in context, if it is present.  (Removed in more recent events.)
            # Also remap usernames appearing with different names.
            # TODO: confirm that these values are usernames, not user_id values.
            # TODO: decide how to handle requesting_student_id (on openassessmentblock.get_peer_submission events).
            if 'username' in event_data:
                event_data['username'] = self.remap_username(event_data['username'])
            if 'instructor' in event_data:
                event_data['instructor'] = self.remap_username(event_data['instructor'])
            if 'student' in event_data:
                event_data['student'] = self.remap_username(event_data['student'])
            if 'user' in event_data:
                event_data['user'] = self.remap_username(event_data['user'])

            # Remove sensitive payload fields.
            # TODO: decide how to handle 'url' and 'report_url'.
            for key in ['certificate_id', 'certificate_url', 'source_url', 'fileName', 'GET', 'POST']:
                event_data.pop(key, None)

            for key in ['answer', 'saved_response']:
                if key in event_data and 'file_upload_key' in event_data[key]:
                    event_data[key].pop('file_upload_key')

            # Determine what user_info to use. We choose the event
            # user_id over the context, and fall back on the username.
            if event_userid_entry is not None:
                user_info = event_userid_entry
            elif userid_entry is not None:
                user_info = userid_entry
            else:
                user_info = username_entry

            # Clean up remaining event payload recursively.
            updated_event_data = self.deidentifier.deidentify_structure(event_data, u"event", user_info)
            if updated_event_data is not None:
                if self.deidentifier.is_logging_enabled():
                    log.info(u"Deidentified payload: %s", debug_str)
                event_data = updated_event_data

            # Re-encode payload as a json string if it originally was.
            if isinstance(event.get('event'), basestring):
                event['event'] = cjson.encode(event_data)
            else:
                event['event'] = event_data

        # Delete or clean base properties other than username.
        event.pop('host', None)  # delete host
        event.pop('ip', None)  # delete ip
        # Clean page
        # Clean referer

        return event

    def extra_modules(self):
        import numpy
        return [numpy]


class EventDeidentificationTask(DeidentifierParamsMixin, MapReduceJobTaskMixin, luigi.WrapperTask):
    """Wrapper task for course events deidentification."""

    course = luigi.Parameter(is_list=True)
    dump_root = luigi.Parameter()
    output_root = luigi.Parameter()
    explicit_event_whitelist = luigi.Parameter(
        config_path={'section': 'events-deidentification', 'name': 'explicit_event_whitelist'}
    )

    def requires(self):
        for course in self.course:
            # we already have course events dumped separately, so each DeidentifyCourseEventsTask would have a different source.
            filename_safe_course_id = opaque_key_util.get_filename_safe_course_id(course)
            kwargs = {
                'course': course,
                'dump_root': self.dump_root,
                'output_root': self.output_root,
                'explicit_event_whitelist': self.explicit_event_whitelist,
                'n_reduce_tasks': self.n_reduce_tasks,
                'entities': self.entities,
                'log_context': self.log_context,
            }
            yield DeidentifyCourseEventsTask(**kwargs)
