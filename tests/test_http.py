"""Real submission-entrypoint HTTP checks plus isolated deadline challenges.

Run: PYTHONPATH=src python3 -m unittest discover -s tests -p test_http.py -v
"""
import concurrent.futures
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from unittest.mock import Mock, patch

from agent import server, runtime
from dataclasses import replace
from agent.brain import decide, empty_response

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / '../../docs/request.txt'
SECRET = 'private-request-marker-81e2d7-绝不记录'


def stalled_compute(payload, snapshot, deadline, pipe):
    """Picklable worker used to challenge the actual spawn/terminate path."""
    time.sleep(30)


class HTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = json.loads(SAMPLE.read_text())
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            cls.port = probe.getsockname()[1]
        cls.logs = tempfile.TemporaryFile()
        launch_root = Path(os.environ.get('CORE_GEEK_TEST_ROOT', str(ROOT)))
        launch = ['bash', 'run.sh', str(cls.port)] if launch_root == ROOT else [sys.executable, 'main3.py', str(cls.port)]
        cls.process = subprocess.Popen(launch, cwd=launch_root,
                                       env=dict(os.environ, PYTHON=sys.executable, CORE_GEEK_DEBUG_LOG='off'),
                                       stdout=cls.logs, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 8
        last_error = None
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.logs.seek(0)
                raise RuntimeError('Submission server exited: ' + cls.logs.read().decode())
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.1):
                    return
            except OSError as exc:
                last_error = repr(exc)
                time.sleep(0.025)
        diagnostic = subprocess.run(['lsof', '-nP', '-a', '-p', str(cls.process.pid), '-i'], capture_output=True, text=True).stdout
        cls.process.terminate()
        cls.process.wait(timeout=3)
        cls.logs.seek(0)
        details = cls.logs.read().decode()
        cls.logs.close()
        raise RuntimeError(f'Submission server did not start: {last_error}; logs={details}; sockets={diagnostic}')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=3)
        cls.logs.close()

    def post(self, payload):
        payload = copy.deepcopy(payload)
        payload['teamOur']['teamId'] = str(payload['teamOur'].get('teamId','')) + self._testMethodName
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4.5)
        started = time.monotonic()
        try:
            conn.request('POST', '/', json.dumps(payload).encode(),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            body = response.read()
            elapsed = time.monotonic() - started
            self.assertEqual(response.status, 200)
            self.assertLess(elapsed, 5)
            self.assertEqual(int(response.getheader('Content-Length')), len(body))
            self.assertIn('application/json', response.getheader('Content-Type'))
            value = json.loads(body)
            self.check_schema(value)
            return value
        finally:
            conn.close()

    def check_schema(self, value):
        self.assertEqual(set(value), {'roleCommandMap', 'prompt', 'executeCmd'})
        self.assertIsInstance(value['roleCommandMap'], dict)
        self.assertIsInstance(value['prompt'], str)
        self.assertIsInstance(value['executeCmd'], str)

    def raw_post(self, headers, body=b''):
        started = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.port), timeout=4.5) as conn:
            conn.sendall(b'POST / HTTP/1.1\r\nHost: localhost\r\n' +
                         headers + b'\r\n\r\n' + body)
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                chunks.append(data)
        self.assertLess(time.monotonic() - started, 5)
        header, body = b''.join(chunks).split(b'\r\n\r\n', 1)
        self.assertIn(b'200', header.split(b'\r\n')[0])
        value = json.loads(body)
        self.check_schema(value)
        return value

    def test_official_sample_and_repeated_determinism(self):
        expected = decide(copy.deepcopy(self.sample))
        for _ in range(3):
            self.assertEqual(self.post(self.sample), expected)

    def test_concurrent_rounds_do_not_share_state(self):
        payloads = []
        for index, round_no in enumerate([1, 70, 71, 130]):
            payload = copy.deepcopy(self.sample)
            payload['roundNo'] = round_no
            payload['teamOur']['teamId'] = f'concurrent-{index}'
            payload['teamOur']['goldNum'] = 20 + index * 100
            payload['llmResp'] = SECRET + str(index)
            payloads.append(payload)
        expected = [decide(copy.deepcopy(payload)) for payload in payloads]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(self.post, payloads))
        self.assertEqual(actual, expected)
        for payload, result in zip(reversed(payloads), reversed(expected)):
            self.assertEqual(self.post(payload), result)

    def test_malformed_framing_returns_safe_response(self):
        cases = [b'', b'Content-Length: nope', b'Content-Length: -1',
                 b'Content-Length: 0', b'Content-Length: 1.5',
                 b'Content-Length: 2097153',
                 b'Content-Length: 2\r\nContent-Length: 2',
                 b'Content-Length: 2\r\nTransfer-Encoding: chunked',
                 b'Content-Length: 100']
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_post(headers, b'{}'), empty_response())
        self.assertEqual(self.post(self.sample), decide(self.sample))

    def test_malformed_json_returns_safe_response(self):
        for body in [b'{', b'null', b'[]', b'"text"', b'\xff\xfe',
                     ('{"secret":"' + SECRET).encode()]:
            with self.subTest(body=body):
                self.assertEqual(self.raw_post(
                    b'Content-Length: ' + str(len(body)).encode(), body), empty_response())

    def test_stateful_task_roundtrip_and_retry(self):
        payload = copy.deepcopy(self.sample)
        payload['roundNo'] = 10
        payload['phaseTask'] = 'fixture task'
        payload['llmResp'] = ''
        first = self.post(payload)
        self.assertEqual(first, self.post(payload))
        context = json.loads(first['prompt'].split('\n', 1)[1])
        payload['roundNo'] = 11
        payload['llmResp'] = json.dumps({**{k: context[k] for k in ('taskKey', 'requestId', 'roundNo')}, 'executeCmd': 'printf 42'})
        command = self.post(payload)
        self.assertEqual(command['executeCmd'], 'printf 42')
        self.assertEqual(command, self.post(payload))
        payload['roundNo'] = 12
        payload['llmResp'] = ''
        payload['lastCmdResult'] = '[exitCode:0]\n42\n'
        continued = self.post(payload)
        self.assertIn('[exitCode:0]', continued['prompt'])
        context = json.loads(continued['prompt'].split('\n', 1)[1])
        payload['roundNo'] = 13
        payload['llmResp'] = json.dumps({**{k: context[k] for k in ('taskKey', 'requestId', 'roundNo')}, 'taskAnswer': '42'})
        answer = self.post(payload)
        self.assertTrue(any(c.get('taskAnswer') == '42' for c in answer['roleCommandMap'].values()))
        self.assertEqual(answer['prompt'], '')

    def test_sensitive_text_is_not_written_to_stdout_or_stderr(self):
        payload = copy.deepcopy(self.sample)
        payload['llmResp'] = SECRET
        payload['phaseTask'] = SECRET
        payload['worldNews'] = {'officialNews': SECRET, 'folkLegends': SECRET}
        payload['lastCmdResult'] = SECRET
        self.post(payload)
        body = ('{"secret":"' + SECRET).encode()
        self.raw_post(b'Content-Length: ' + str(len(body)).encode(), body)
        self.logs.seek(0)
        self.assertNotIn(SECRET.encode(), self.logs.read())


class PackagedHTTPIntegration(HTTPIntegration):
    """Run the same transport contract against the actual extracted artifact."""
    @classmethod
    def setUpClass(cls):
        cls.extracted = tempfile.TemporaryDirectory(prefix='coregeek-http-package-')
        archive = ROOT / 'artifacts/coregeek-v0.2.tar.gz'
        # Always validate current source, never a stale package.
        subprocess.run([sys.executable, str(ROOT/'tools/package.py')], check=True)
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                if member.name.startswith('/') or '..' in Path(member.name).parts or not member.isfile():
                    raise ValueError('unsafe package member')
            package.extractall(cls.extracted.name)
        cls.previous_root = os.environ.get('CORE_GEEK_TEST_ROOT')
        os.environ['CORE_GEEK_TEST_ROOT'] = cls.extracted.name
        try:
            super().setUpClass()
        except Exception:
            cls.restore_root()
            cls.extracted.cleanup()
            raise

    @classmethod
    def restore_root(cls):
        if cls.previous_root is None:
            os.environ.pop('CORE_GEEK_TEST_ROOT', None)
        else:
            os.environ['CORE_GEEK_TEST_ROOT'] = cls.previous_root

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls.restore_root()
            cls.extracted.cleanup()


class BoundedDecisionChallenges(unittest.TestCase):
    def test_compute_exception_returns_empty_without_exception_text(self):
        pipe = Mock()
        with patch.object(runtime, 'compute', side_effect=RuntimeError(SECRET)):
            runtime._worker({'llmResp': SECRET}, {}, time.monotonic()+3, pipe)
        pipe.send.assert_called_once_with({'worker_error':'RuntimeError','message':SECRET})
        pipe.close.assert_called_once()

    def test_stalled_worker_is_terminated_and_next_decision_works(self):
        started = time.monotonic()
        with patch.object(runtime, '_worker', stalled_compute), \
                patch.object(runtime, 'DEFAULT', replace(runtime.DEFAULT, decision_seconds=0.15)):
            self.assertIsNone(runtime.execute({}, {}, time.monotonic()+3))
        self.assertLess(time.monotonic() - started, 2.5)
        sample = json.loads(SAMPLE.read_text())
        self.assertEqual(runtime.execute(sample, {"version":0}, time.monotonic()+3).response, decide(sample))


if __name__ == '__main__':
    unittest.main()
