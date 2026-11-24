"""Bounded authenticated loopback adapter for the admission interface."""
import argparse
from dataclasses import dataclass, field
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys

from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.review_http import MAX_BODY
from nilaris.review import Review
from nilaris.drive import PinnedDrive
from nilaris.synthetic_fixture_drive import SyntheticFixtureDrive
from nilaris.fixture_execution import FixtureExecution

ADDRESS = ('127.0.0.1', 18765)
MAX_RESPONSE = 131072


@dataclass(repr=False)
class Configuration:
    database: dict = field(repr=False)
    token: str = field(repr=False)
    profile: AdmissionProfile | None = None
    drive: PinnedDrive | SyntheticFixtureDrive | None = field(default=None, repr=False)
    source_operator: object | None = field(default=None, repr=False)
    command_guard: object | None = field(default=None, repr=False)

    def __repr__(self):
        return '<private command configuration; credentials redacted>'

    @classmethod
    def load(cls, path):
        try:
            with Path(path).open('rb') as source:
                if os.fstat(source.fileno()).st_mode & 0o077:
                    raise ValueError
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data['database'], dict) or not isinstance(data['token'], str):
                raise ValueError
            if not 16 <= len(data['token']) <= 256 or any(
                    not 33 <= ord(char) <= 126 for char in data['token']):
                raise ValueError
            profile = data.get('admission_profile')
            return cls(data['database'], data['token'],
                       AdmissionProfile(**profile) if profile is not None else None,
                       (SyntheticFixtureDrive(data['drive'])
                        if isinstance(data.get('drive'), dict) and
                        data['drive'].get('mode') == SyntheticFixtureDrive.label
                        else PinnedDrive(data['drive'])) if 'drive' in data else None)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            pass
        raise ValueError('private command configuration unavailable or invalid') from None


def unavailable(command=None):
    command = command if isinstance(command, dict) else {}
    affected = {key: command[key] for key in ('namespace', 'command_id', 'job_id')
                if isinstance(command.get(key), str)}
    return dict(known=False, category='transport_unavailable', coverage='unknown',
                affected=affected, reason='command response unavailable',
                next_action=('retry_status' if command.get('operation') == 'status'
                             else 'resolve_or_retry_same_key'))


def refused(category='invalid_request'):
    reason, action = {
        'invalid_request': ('Command syntax, route or bounded metadata is invalid.', 'correct_request'),
        'unauthorized': ('Command access was denied.', 'configure_authorized_credentials'),
        'transport_limit': ('Command exceeds the transport metadata limit.', 'reduce_request'),
        'configuration_unavailable': ('Private command configuration is unavailable or invalid.',
                                      'configure_approved_private_configuration'),
    }[category]
    # Refused before interpreting identities: no durable outcome or coverage is inferred.
    return dict(known=True, category=category, affected={}, coverage='unknown',
                reason=reason, next_action=action)


def request(configuration, command):
    """A lost response is uncertainty, never an admission refusal."""
    connection = HTTPConnection(*ADDRESS, timeout=3)
    try:
        body = json.dumps(command, allow_nan=False).encode('utf-8')
        if len(body) > MAX_BODY:
            return refused('transport_limit')
        connection.request('POST', '/command', body,
                           {'Authorization': 'Bearer ' + configuration.token,
                            'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            return unavailable(command)
        return json.loads(raw)
    except (OSError, ValueError, TypeError, HTTPException, RecursionError):
        return unavailable(command)
    finally:
        connection.close()


def serve(configuration):
    ingestion = Ingestion(configuration.database, configuration.profile, drive_policy=configuration.drive)
    review = Review(configuration.database)
    execution = FixtureExecution(ingestion, configuration.drive,
                                 captured_policy=configuration.source_operator)
    if configuration.source_operator is not None:
        configuration.source_operator.ingestion = ingestion
        ingestion.drive_policy = configuration.source_operator

    class Handler(BaseHTTPRequestHandler):
        # One request at a time; no per-request threads or unbounded work queue.
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.reply(code, refused())

        def reply(self, status, result):
            body = json.dumps(result, allow_nan=False).encode('utf-8')
            if len(body) > MAX_RESPONSE:
                status, body = 503, json.dumps(unavailable()).encode('ascii')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # Commit may already exist: caller must resolve the same key.

        def do_POST(self):
            if self.path != '/command':
                self.reply(404, refused())
                return
            tokens = self.headers.get_all('Authorization', [])
            if len(tokens) != 1 or not hmac.compare_digest(
                    tokens[0].encode('utf-8'), ('Bearer ' + configuration.token).encode('ascii')):
                self.reply(401, refused('unauthorized'))
                return
            lengths = self.headers.get_all('Content-Length', [])
            try:
                if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
                    raise ValueError
                size = int(lengths[0])
                if not 0 < size <= MAX_BODY:
                    raise ValueError
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError
                operation = command.pop('operation')
                fields = {'offer': {'namespace', 'command_id', 'members'},
                          'resolve': {'namespace', 'command_id'}, 'status': {'job_id'},
                          'result': {'job_id'},
                          'execute_fixture_v1': {'job_id', 'expected_revision'},
                          'cancel': {'job_id', 'expected_revision'},
                          'retry': {'job_id', 'expected_revision', 'expected_generation',
                                    'actor', 'reason'},
                          'reprocess': {'namespace', 'command_id', 'job_id', 'expected_revision',
                                        'expected_generation', 'actor', 'reason'},
                          'reconcile': {'job_id', 'expected_revision', 'expected_generation',
                                        'actor', 'reason'}}
                fields.update({
                    'drive_discover': {'binding_id','operation_id','page_id'},
                    'drive_operation': {'binding_id','operation_id'},
                    'drive_admit': {'binding_id','operation_id','page_id'},
                    'drive_acquire': {'binding_id','operation_id','job_id','expected_revision'},
                    'discovery_stage': {'namespace','page_id','expected_cursor','next_cursor','members'},
                    'discovery_read': {'namespace','page_id'},
                    'discovery_admit': {'namespace','page_id'},
                    'discovery_reconcile': {'namespace','page_id'},
                    'review_create': {'namespace','command_id','job_id','proposal_id','product_type','expected_base','actor'},
                    'review_read': {'draft_id'}, 'review_resume': {'draft_id'},
                    'review_save': {'namespace','command_id','draft_id','expected_revision','definitions','associations','actor','reason'},
                    'review_approve': {'namespace','command_id','draft_id','expected_revision','product_type','actor','explicit'},
                    'review_resolve': {'namespace','command_id'},
                    'review_incoming': {'draft_id','job_id','proposal_id'}})
                optional = ({'cursor','revision'} if operation == 'review_read' else
                            {'expected_namespace'} if operation in ('drive_discover','drive_acquire') else
                            {'cursor','proposal_id','provenance'} if operation == 'result' else
                            {'cursor'} if operation in ('review_resume','review_incoming') else
                            {'disposition'} if operation == 'reconcile' else
                            {'scenario_review','equipment_review'} if operation == 'review_save' else set())
                if (operation not in fields or not fields[operation] <= set(command) or
                        not set(command) <= fields[operation] | optional):
                    raise ValueError
                integers = {'expected_revision', 'expected_generation', 'revision'}
                if any(type(value) is not int or not 0 <= value <= 9223372036854775807
                       for key, value in command.items() if key in integers):
                    raise ValueError
                if ('expected_base' in command and command['expected_base'] is not None and
                        (not isinstance(command['expected_base'],str) or not command['expected_base'].strip())):
                    raise ValueError
                if 'provenance' in command and type(command['provenance']) is not bool:
                    raise ValueError
                if 'explicit' in command and command['explicit'] is not True:
                    raise ValueError
                if any(not isinstance(command[key],list) for key in ('definitions','associations') if key in command):
                    raise ValueError
                if 'scenario_review' in command and not isinstance(command['scenario_review'],dict):
                    raise ValueError
                if 'equipment_review' in command and not isinstance(command['equipment_review'],dict):
                    raise ValueError
                nullable = {'expected_cursor','next_cursor'} if operation == 'discovery_stage' else set()
                if any(command[key] is not None and (not isinstance(command[key], str) or not command[key].strip()) for key in nullable):
                    raise ValueError
                if any(not isinstance(value, str) or not value.strip()
                       for key, value in command.items() if key not in integers | nullable | {'members','expected_base','explicit','definitions','associations','scenario_review','equipment_review','provenance'}):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                self.reply(400, refused())
                return
            if configuration.command_guard is not None:
                denial = configuration.command_guard(operation, command)
                if denial is not None:
                    self.reply(200, denial)
                    return
            if operation.startswith('drive_'):
                operator = configuration.source_operator
                if operator is not None:
                    result = getattr(operator, operation)(**command)
                else:
                    from nilaris.drive155_journey import disabled
                    result = disabled()
            elif operation == 'execute_fixture_v1':
                result = execution.execute_fixture_v1(**command)
            elif operation.startswith('review_'):
                operation = {'review_incoming':'read_incoming'}.get(operation,operation.removeprefix('review_'))
                result = getattr(review,operation)(**command)
            else:
                result = getattr(ingestion, operation)(**command)
            self.reply(200, result)

    class Server(HTTPServer):
        request_queue_size = 1

        def handle_error(self, request, client_address):
            # Dependency/request exceptions must never expose credentials or input.
            pass

    # Constructor binds atomically; no preflight can reserve or broaden the address.
    with Server(ADDRESS, Handler) as server:
        server.serve_forever(poll_interval=0.2)


def main():
    class CommandParser(argparse.ArgumentParser):
        def error(self, message):
            print(json.dumps(refused()), file=sys.stderr)
            raise SystemExit(2)

    parser = CommandParser()
    parser.add_argument('action', choices=('serve', 'client'))
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        configuration = Configuration.load(args.config)
    except ValueError:
        print(json.dumps(refused('configuration_unavailable')), file=sys.stderr)
        raise SystemExit(1) from None
    try:
        if args.action == 'serve':
            serve(configuration)
        else:
            try:
                raw = sys.stdin.buffer.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError
            except (OSError, ValueError, TypeError, RecursionError):
                result = refused()
            else:
                result = request(configuration, command)
            print(json.dumps(result, allow_nan=False))
    except (OSError, ValueError, TypeError):
        print(json.dumps(dict(known=True, category='listener_unavailable', affected={},
                              coverage='unknown', reason='Command listener is unavailable.',
                              next_action='restore_authorized_loopback_listener')), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
