# Copyright 2017 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test problem submission against hard-coded replies with unittest.mock."""

from __future__ import division, absolute_import, print_function, unicode_literals

import time
import json
import unittest
import itertools
import threading
import collections

from datetime import datetime, timedelta
from dateutil.tz import UTC
from dateutil.parser import parse as parse_datetime
from requests.structures import CaseInsensitiveDict
from requests.exceptions import HTTPError
from concurrent.futures import TimeoutError

try:
    import dimod
except ImportError:
    dimod = None

from dwave.cloud.utils import evaluate_ising, generate_const_ising_problem
from dwave.cloud.client import Client, Solver
from dwave.cloud.computation import Future
from dwave.cloud.exceptions import SolverFailureError, CanceledFutureError
from dwave.cloud.testing import mock


def test_problem(solver):
    """The problem answered by mocked replies below."""
    return generate_const_ising_problem(solver, h=1, j=-1)


def solver_data(id_, incomplete=False):
    """Return data for a solver."""
    obj = {
        "properties": {
            "supported_problem_types": ["qubo", "ising"],
            "qubits": [0, 1, 2, 3, 4],
            "couplers": list(itertools.combinations(range(5), 2)),
            "num_qubits": 5,
            "parameters": {"num_reads": "Number of samples to return."}
        },
        "id": id_,
        "description": "A test solver"
    }

    if incomplete:
        del obj['properties']['parameters']

    return obj


def complete_reply(id_, solver_name):
    """Reply with solutions for the test problem."""
    return json.dumps({
        "status": "COMPLETED",
        "solved_on": "2013-01-18T10:26:00.020954",
        "solver": solver_name,
        "submitted_on": "2013-01-18T10:25:59.941674",
        "answer": {
            'format': 'qp',
            "num_variables": 5,
            "energies": 'AAAAAAAALsA=',
            "num_occurrences": 'ZAAAAA==',
            "active_variables": 'AAAAAAEAAAACAAAAAwAAAAQAAAA=',
            "solutions": 'AAAAAA==',
            "timing": {}
        },
        "type": "ising",
        "id": id_
    })


def complete_no_answer_reply(id_, solver_name):
    """A reply saying a problem is finished without providing the results."""
    return json.dumps({
        "status": "COMPLETED",
        "solved_on": "2012-12-05T19:15:07+00:00",
        "solver": solver_name,
        "submitted_on": "2012-12-05T19:06:57+00:00",
        "type": "ising",
        "id": id_
    })


def error_reply(id_, solver_name, error):
    """A reply saying an error has occurred."""
    return json.dumps({
        "status": "FAILED",
        "solved_on": "2013-01-18T10:26:00.020954",
        "solver": solver_name,
        "submitted_on": "2013-01-18T10:25:59.941674",
        "type": "ising",
        "id": id_,
        "error_message": error
    })


def immediate_error_reply(code, msg):
    """A reply saying an error has occurred (before scheduling for execution)."""
    return json.dumps({
        "error_code": code,
        "error_msg": msg
    })


def cancel_reply(id_, solver_name):
    """A reply saying a problem was canceled."""
    return json.dumps({
        "status": "CANCELLED",
        "solved_on": "2013-01-18T10:26:00.020954",
        "solver": solver_name,
        "submitted_on": "2013-01-18T10:25:59.941674",
        "type": "ising",
        "id": id_
    })


def datetime_in_future(seconds=0):
    now = datetime.utcnow().replace(tzinfo=UTC)
    return now + timedelta(seconds=seconds)


def continue_reply(id_, solver_name, now=None, eta_min=None, eta_max=None):
    """A reply saying a problem is still in the queue."""

    if not now:
        now = datetime_in_future(0)

    resp = {
        "status": "PENDING",
        "solved_on": None,
        "solver": solver_name,
        "submitted_on": now.isoformat(),
        "type": "ising",
        "id": id_
    }
    if eta_min:
        resp.update({
            "earliest_estimated_completion": eta_min.isoformat(),
        })
    if eta_max:
        resp.update({
            "latest_estimated_completion": eta_max.isoformat(),
        })
    return json.dumps(resp)


def choose_reply(path, replies, statuses=None, date=None):
    """Choose the right response based on the path and make a mock response."""

    if statuses is None:
        statuses = collections.defaultdict(lambda: iter([200]))

    if date is None:
        date = datetime_in_future(0)

    if path in replies:
        response = mock.Mock(['json', 'raise_for_status', 'headers'])
        response.status_code = next(statuses[path])
        response.json.side_effect = lambda: json.loads(replies[path])
        response.headers = CaseInsensitiveDict({'Date': date.isoformat()})
        def raise_for_status():
            if not 200 <= response.status_code < 300:
                raise HTTPError(response.status_code)
        response.raise_for_status = raise_for_status
        return response
    else:
        raise NotImplementedError(path)


class _QueryTest(unittest.TestCase):
    def _check(self, results, linear, quad, num):
        # Did we get the right number of samples?
        self.assertTrue(100 == sum(results.occurrences))

        # Make sure the number of occurrences and energies are all correct
        for energy, state in zip(results.energies, results.samples):
            self.assertTrue(energy == evaluate_ising(linear, quad, state))


@mock.patch('time.sleep', lambda *x: None)
class MockSubmission(_QueryTest):
    """Test connecting and some related failure modes."""

    def test_submit_null_reply(self):
        """Get an error when the server's response is incomplete."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': ''})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                with self.assertRaises(ValueError):
                    results.samples

    def test_submit_ok_reply(self):
        """Handle a normal query and response."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % complete_no_answer_reply(
                    '123', 'abc123')})
            session.get = lambda a: choose_reply(a, {
                'problems/123/': complete_reply(
                    '123', 'abc123')})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                self._check(results, linear, quadratic, 100)

    def test_submit_error_reply(self):
        """Handle an error on problem submission."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % error_reply(
                    '123', 'abc123', 'An error message')})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                with self.assertRaises(SolverFailureError):
                    results.samples

    def test_submit_immediate_error_reply(self):
        """Handle an (obvious) error on problem submission."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % immediate_error_reply(
                    400, "Missing parameter 'num_reads' in problem JSON")})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic)

                with self.assertRaises(SolverFailureError):
                    results.samples

    def test_submit_cancel_reply(self):
        """Handle a response for a canceled job."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % cancel_reply('123', 'abc123')})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                with self.assertRaises(CanceledFutureError):
                    results.samples

    def test_submit_continue_then_ok_reply(self):
        """Handle polling for a complete problem."""

        now = datetime_in_future(0)
        eta_min, eta_max = datetime_in_future(10), datetime_in_future(30)

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % continue_reply(
                    '123', 'abc123', eta_min=eta_min, eta_max=eta_max, now=now)
            }, date=now)
            session.get = lambda a: choose_reply(a, {
                'problems/?id=123': '[%s]' % complete_no_answer_reply(
                    '123', 'abc123'),
                'problems/123/': complete_reply(
                    '123', 'abc123')
            }, date=now)
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                self._check(results, linear, quadratic, 100)

                # test future has eta_min and eta_max parsed correctly
                self.assertEqual(results.eta_min, eta_min)
                self.assertEqual(results.eta_max, eta_max)

    def test_submit_continue_then_error_reply(self):
        """Handle polling for an error message."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            session.post = lambda a, _: choose_reply(a, {
                'problems/': '[%s]' % continue_reply(
                    '123', 'abc123')})
            session.get = lambda a: choose_reply(a, {
                'problems/?id=123': '[%s]' % error_reply(
                    '123', 'abc123', "error message")})
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)
                results = solver.sample_ising(linear, quadratic, num_reads=100)

                with self.assertRaises(SolverFailureError):
                    self._check(results, linear, quadratic, 100)

    def test_submit_continue_then_ok_and_error_reply(self):
        """Handle polling for the status of multiple problems."""

        # we need a "global session", because mocked responses are stateful
        def global_mock_session():
            session = mock.Mock()

            # on first status poll, return pending for both problems
            # on second status poll, return error for first problem and complete for second
            def continue_then_complete(path, state={'count': 0}):
                state['count'] += 1
                if state['count'] < 2:
                    return choose_reply(path, {
                        'problems/?id=1': '[{}]'.format(continue_reply('1', 'abc123')),
                        'problems/?id=2': '[{}]'.format(continue_reply('2', 'abc123')),
                        'problems/1/': continue_reply('1', 'abc123'),
                        'problems/2/': continue_reply('2', 'abc123'),
                        'problems/?id=1,2': '[{},{}]'.format(continue_reply('1', 'abc123'),
                                                                      continue_reply('2', 'abc123')),
                        'problems/?id=2,1': '[{},{}]'.format(continue_reply('2', 'abc123'),
                                                                      continue_reply('1', 'abc123'))
                    })
                else:
                    return choose_reply(path, {
                        'problems/?id=1': '[{}]'.format(error_reply('1', 'abc123', 'error')),
                        'problems/?id=2': '[{}]'.format(complete_no_answer_reply('2', 'abc123')),
                        'problems/1/': error_reply('1', 'abc123', 'error'),
                        'problems/2/': complete_reply('2', 'abc123'),
                        'problems/?id=1,2': '[{},{}]'.format(error_reply('1', 'abc123', 'error'),
                                                                      complete_no_answer_reply('2', 'abc123')),
                        'problems/?id=2,1': '[{},{}]'.format(complete_no_answer_reply('2', 'abc123'),
                                                                      error_reply('1', 'abc123', 'error'))
                    })

            def accept_problems_with_continue_reply(path, body, ids=iter('12')):
                problems = json.loads(body)
                return choose_reply(path, {
                    'problems/': json.dumps(
                        [json.loads(continue_reply(next(ids), 'abc123')) for _ in problems])
                })

            session.get = continue_then_complete
            session.post = accept_problems_with_continue_reply

            return session

        session = global_mock_session()

        with mock.patch.object(Client, 'create_session', lambda self: session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)

                results1 = solver.sample_ising(linear, quadratic, num_reads=100)
                results2 = solver.sample_ising(linear, quadratic, num_reads=100)

                with self.assertRaises(SolverFailureError):
                    self._check(results1, linear, quadratic, 100)
                self._check(results2, linear, quadratic, 100)

    def test_exponential_backoff_polling(self):
        "After each poll, back-off should double"

        # we need a "global session", because mocked responses are stateful
        def global_mock_session():
            session = mock.Mock()

            # on submit, return status pending
            session.post = lambda path, _: choose_reply(path, {
                'problems/': '[%s]' % continue_reply('123', 'abc123')
            })

            # on first and second status poll, return pending
            # on third status poll, return completed
            def continue_then_complete(path, state={'count': 0}):
                state['count'] += 1
                if state['count'] < 3:
                    return choose_reply(path, {
                        'problems/?id=123': '[%s]' % continue_reply('123', 'abc123'),
                        'problems/123/': continue_reply('123', 'abc123')
                    })
                else:
                    return choose_reply(path, {
                        'problems/?id=123': '[%s]' % complete_no_answer_reply('123', 'abc123'),
                        'problems/123/': complete_reply('123', 'abc123')
                    })

            session.get = continue_then_complete

            return session

        session = global_mock_session()

        with mock.patch.object(Client, 'create_session', lambda self: session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                future = solver.sample_qubo({})
                future.result()

                # after third poll, back-off interval should be 4 x initial back-off
                self.assertEqual(future._poll_backoff, Client._POLL_BACKOFF_MIN * 2**2)

    def test_eta_min_is_ignored_on_first_poll(self):
        "eta_min/earliest_estimated_completion should not be used anymore"

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            now = datetime_in_future(0)
            eta_min, eta_max = datetime_in_future(10), datetime_in_future(30)
            session = mock.Mock()
            session.post = lambda path, _: choose_reply(path, {
                'problems/': '[%s]' % continue_reply(
                    '1', 'abc123', eta_min=eta_min, eta_max=eta_max, now=now)
            }, date=now)
            session.get = lambda path: choose_reply(path, {
                'problems/?id=1': '[%s]' % complete_no_answer_reply(
                    '1', 'abc123'),
                'problems/1/': complete_reply(
                    '1', 'abc123')
            }, date=now)
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                def assert_no_delay(s):
                    s and self.assertTrue(
                        abs(s - client._POLL_BACKOFF_MIN) < client._POLL_BACKOFF_MIN / 10.0)

                with mock.patch('time.sleep', assert_no_delay):
                    future = solver.sample_qubo({})
                    future.result()

    def test_immediate_polling_without_eta_min(self):
        "First poll happens with minimal delay if eta_min missing"

        # each thread can have its instance of a session because
        # responses are stateless
        def create_mock_session(client):
            now = datetime_in_future(0)
            session = mock.Mock()
            session.post = lambda path, _: choose_reply(path, {
                'problems/': '[%s]' % continue_reply('1', 'abc123')
            }, date=now)
            session.get = lambda path: choose_reply(path, {
                'problems/?id=1': '[%s]' % complete_no_answer_reply('1', 'abc123'),
                'problems/1/': complete_reply('1', 'abc123')
            }, date=now)
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                def assert_no_delay(s):
                    s and self.assertTrue(
                        abs(s - client._POLL_BACKOFF_MIN) < client._POLL_BACKOFF_MIN / 10.0)

                with mock.patch('time.sleep', assert_no_delay):
                    future = solver.sample_qubo({})
                    future.result()

    def test_immediate_polling_with_local_clock_unsynced(self):
        """First poll happens with minimal delay if local clock is way off from
        the remote/server clock."""

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            badnow = datetime_in_future(100)
            session = mock.Mock()
            session.post = lambda path, _: choose_reply(path, {
                'problems/': '[%s]' % continue_reply('1', 'abc123')
            }, date=badnow)
            session.get = lambda path: choose_reply(path, {
                'problems/?id=1': '[%s]' % complete_no_answer_reply('1', 'abc123'),
                'problems/1/': complete_reply('1', 'abc123')
            }, date=badnow)
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                def assert_no_delay(s):
                    s and self.assertTrue(
                        abs(s - client._POLL_BACKOFF_MIN) < client._POLL_BACKOFF_MIN / 10.0)

                with mock.patch('time.sleep', assert_no_delay):
                    future = solver.sample_qubo({})
                    future.result()

    def test_polling_recovery_after_5xx(self):
        "Polling shouldn't be aborted on 5xx responses."

        # we need a "global session", because mocked responses are stateful
        def global_mock_session():
            session = mock.Mock()

            # on submit, return status pending
            session.post = lambda path, _: choose_reply(path, {
                'problems/': '[%s]' % continue_reply('123', 'abc123')
            })

            # on first and second status poll, fail with 503 and 504
            # on third status poll, return completed
            statuses = iter([503, 504])
            def continue_then_complete(path, state={'count': 0}):
                state['count'] += 1
                if state['count'] < 3:
                    return choose_reply(path, replies={
                        'problems/?id=123': '[%s]' % continue_reply('123', 'abc123'),
                        'problems/123/': continue_reply('123', 'abc123')
                    }, statuses={
                        'problems/?id=123': statuses,
                        'problems/123/': statuses
                    })
                else:
                    return choose_reply(path, {
                        'problems/?id=123': '[%s]' % complete_no_answer_reply('123', 'abc123'),
                        'problems/123/': complete_reply('123', 'abc123')
                    })

            session.get = continue_then_complete

            return session

        session = global_mock_session()

        with mock.patch.object(Client, 'create_session', lambda self: session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                future = solver.sample_qubo({})
                future.result()

                # after third poll, back-off interval should be 4 x initial back-off
                self.assertEqual(future._poll_backoff, Client._POLL_BACKOFF_MIN * 2**2)


class DeleteEvent(Exception):
    """Throws exception when mocked client submits an HTTP DELETE request."""

    def __init__(self, url, body):
        """Return the URL of the request with the exception for test verification."""
        self.url = url
        self.body = body

    @staticmethod
    def handle(path, **kwargs):
        """Callback useable to mock a delete request."""
        raise DeleteEvent(path, json.dumps(kwargs['json']))


@mock.patch('time.sleep', lambda *x: None)
class MockCancel(unittest.TestCase):
    """Make sure cancel works at the two points in the process where it should."""

    def test_cancel_with_id(self):
        """Make sure the cancel method submits to the right endpoint.

        When cancel is called after the submission is finished.
        """
        submission_id = 'test-id'

        # each thread can have its instance of a session because
        # the mocked responses are stateless
        def create_mock_session(client):
            session = mock.Mock()
            reply_body = '[%s]' % continue_reply(submission_id, 'solver')
            session.get = lambda a: choose_reply(a, {
                'problems/?id={}'.format(submission_id): reply_body})
            session.delete = DeleteEvent.handle
            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))
                future = solver._retrieve_problem(submission_id)
                future.cancel()

                try:
                    self.assertTrue(future.id is not None)
                    future.samples
                    self.fail()
                except DeleteEvent as event:
                    if event.url == 'problems/':
                        self.assertEqual(event.body, '["{}"]'.format(submission_id))
                    else:
                        self.assertEqual(event.url, 'problems/{}/'.format(submission_id))

    def test_cancel_without_id(self):
        """Make sure the cancel method submits to the right endpoint.

        When cancel is called before the submission has returned the problem id.
        """
        submission_id = 'test-id'
        release_reply = threading.Event()

        # each thread can have its instance of a session because
        # we use a global lock (event) in the mocked responses
        def create_mock_session(client):
            reply_body = '[%s]' % continue_reply(submission_id, 'solver')

            session = mock.Mock()
            session.get = lambda a: choose_reply(a, {
                'problems/?id={}'.format(submission_id): reply_body})

            def post(a, _):
                release_reply.wait()
                return choose_reply(a, {'problems/': reply_body})

            session.post = post
            session.delete = DeleteEvent.handle

            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data('abc123'))

                linear, quadratic = test_problem(solver)

                future = solver.sample_ising(linear, quadratic)
                future.cancel()

                try:
                    release_reply.set()
                    future.samples
                    self.fail()
                except DeleteEvent as event:
                    if event.url == 'problems/':
                        self.assertEqual(event.body, '["{}"]'.format(submission_id))
                    else:
                        self.assertEqual(event.url, 'problems/{}/'.format(submission_id))


class TestComputationID(unittest.TestCase):

    def test_id_getter_setter(self):
        """Future.get_id/get_id works in isolation as expected."""

        f = Future(solver=None, id_=None)

        # f.id should be None
        self.assertIsNone(f.id)
        with self.assertRaises(TimeoutError):
            f.wait_id(timeout=1)

        # set it
        submission_id = 'test-id'
        f.id = submission_id

        # validate it's available
        self.assertEqual(f.wait_id(), submission_id)
        self.assertEqual(f.wait_id(timeout=1), submission_id)
        self.assertEqual(f.id, submission_id)

    def test_id_integration(self):
        """Problem ID getter blocks correctly when ID set by the client."""

        submission_id = 'test-id'
        solver_name = 'solver-id'
        release_reply = threading.Event()

        # each thread can have its instance of a session because
        # we use a global lock (event) in the mocked responses
        def create_mock_session(client):
            session = mock.Mock()

            # delayed submit; emulates waiting in queue
            def post(path, _):
                release_reply.wait()
                reply_body = complete_reply(submission_id, solver_name)
                return choose_reply(path, {'problems/': '[%s]' % reply_body})

            session.post = post

            return session

        with mock.patch.object(Client, 'create_session', create_mock_session):
            with Client('endpoint', 'token') as client:
                solver = Solver(client, solver_data(solver_name))

                linear, quadratic = test_problem(solver)

                future = solver.sample_ising(linear, quadratic)

                # initially, the id is not available
                with self.assertRaises(TimeoutError):
                    future.wait_id(timeout=1)

                # release the mocked sapi reply with the id
                release_reply.set()

                # verify the id is now available
                self.assertEqual(future.wait_id(), submission_id)

    @unittest.skipUnless(dimod, "dimod required for 'Solver.sample_bqm'")
    def test_sampleset_id(self):
        f = Future(solver=None, id_=None)

        # f.id should be None
        self.assertIsNone(f.id)
        with self.assertRaises(TimeoutError):
            f.sampleset.wait_id(timeout=1)

        # set it
        submission_id = 'test-id'
        f.id = submission_id

        # validate it's available
        self.assertEqual(f.sampleset.wait_id(), submission_id)
        self.assertEqual(f.sampleset.wait_id(timeout=1), submission_id)
