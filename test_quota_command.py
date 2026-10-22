"""Tests of quota_command.py: fakes only (a fake aiquokka executable, reading
files in temporary directories), a fixed clock and a fixed time zone."""

import datetime
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import quota_command as qc

HERE = Path(__file__).resolve().parent
COMMAND = HERE / 'quota_command.py'
UTC = datetime.timezone.utc
# Sat 2026-10-17 15:59 at UTC+5 (Etc/GMT-5 is UTC+5 in POSIX's inverted sign).
NOW = datetime.datetime(2026, 10, 17, 10, 59, tzinfo=UTC)
TZ = 'Etc/GMT-5'
REQUEST = {'kind': 'command', 'command': 'quota', 'args': '', 'update_id': 40, 'message_id': 1040,
           'callback_data': None, 'callback_message_id': None, 'received_at': '2026-10-17T10:59:00+00:00',
           'user_display_name': 'Alex'}

# The layout: one monospace block, the colour dot first on each row.
SAMPLE = '\n'.join([
    'Claude \u00b7 personal',
    '\U0001f7e2 5h        5%  Sat 19:59  4h00m',
    '\U0001f7e2 week     56%  Wed 10:59  3d19h',
    '\U0001f7e2 Fable wk  9%  Wed 10:59  3d19h',
    'Codex \u00b7 work',
    '\u26aa 5h         \u2013  no window',
    '\U0001f7e2 week      7%  Thu 11:24  4d19h',
])

FAKE_AIQUOKKA = '''\
import json, os, subprocess, sys, time
FAKE = {fake!r}
provider = sys.argv[1]
with open(os.path.join(FAKE, 'calls.log'), 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
with open(os.path.join(FAKE, provider + '.env.json'), 'w') as out:
    json.dump(dict(os.environ), out)
try:
    with open(os.path.join(FAKE, provider + '.json')) as spec_file:
        spec = json.load(spec_file)
except FileNotFoundError:
    sys.stderr.write('aiquokka: no such provider\\n')
    sys.exit(2)
if spec.get('child'):
    child = subprocess.Popen(['sleep', '60'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    with open(os.path.join(FAKE, provider + '.child'), 'w') as out:
        out.write(str(child.pid))
time.sleep(spec.get('sleep', 0))
if spec.get('via'):
    sys.stdout.write(subprocess.run([spec['via']], capture_output=True, text=True).stdout)
if spec.get('big'):
    sys.stdout.write('x' * (70 * 1024))
sys.stdout.write(spec.get('stdout', ''))
sys.stderr.write(spec.get('stderr', ''))
sys.exit(spec.get('exit', 0))
'''


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    raise TypeError(value)


def iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def window(label, used, resets):
    return {'label': label, 'used_percent': used, 'resets_at': iso(resets) if resets else None}


def claude_payload():
    return {'provider': 'Claude', 'plan': 'Max 20x', 'account': {'email': 'someone@example.org'},
            'windows': [
                dict(window('5h', 5, NOW + datetime.timedelta(hours=4)), id='session'),
                dict(window('Weekly', 56, datetime.datetime(2026, 10, 21, 5, 59, tzinfo=UTC)), id='weekly_all'),
                dict(window('Weekly Fable', 9, datetime.datetime(2026, 10, 21, 5, 59, tzinfo=UTC)), id='weekly_scoped'),
            ]}


def codex_reading_group(account='work'):
    return {'provider': 'Codex', 'account': account, 'error': None, 'windows': [
        {'label': '5h', 'used_percent': None, 'resets_at': None},
        window('Weekly', 7, datetime.datetime(2026, 10, 22, 6, 24, tzinfo=UTC)),
    ]}


class Fixture:
    """A temporary directory with a fake aiquokka, its per-provider answers,
    reading files and the config."""

    def __init__(self, case):
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fake = self.root / 'fake'
        self.bin = self.root / 'bin'
        self.fake.mkdir()
        self.bin.mkdir()
        self.aiquokka = self.bin / 'aiquokka'
        self.aiquokka.write_text(f'#!{sys.executable}\n' + FAKE_AIQUOKKA.format(fake=str(self.fake)))
        self.aiquokka.chmod(0o755)

    def provider(self, name, payload=None, **spec):
        if payload is not None:
            spec['stdout'] = json.dumps(payload)
        (self.fake / f'{name}.json').write_text(json.dumps(spec))

    def helper(self, directory, name, text):
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({text!r})\n')
        path.chmod(0o755)
        return path

    def reading(self, name, data, observed=NOW):
        path = self.root / name
        if isinstance(data, list):
            data = {'observed_at': iso(observed), 'groups': data}
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return str(path)

    def config(self, mode=0o600, labels=None, raw=None, **keys):
        keys.setdefault('aiquokka', str(self.aiquokka))
        lines = [f'{key} = {toml_value(value)}' for key, value in keys.items()]
        if labels:
            lines.append('[labels]')
            lines += [f'{key} = {toml_value(value)}' for key, value in labels.items()]
        path = self.root / 'quota.toml'
        path.write_text(raw if raw is not None else '\n'.join(lines) + '\n')
        path.chmod(mode)
        return str(path)

    def env_of(self, provider):
        return json.loads((self.fake / f'{provider}.env.json').read_text())

    def calls(self):
        log = self.fake / 'calls.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def answer(config, request=REQUEST, now=NOW, tz=TZ, environ=None, **kwargs):
    env = {'PATH': qc.FIXED_PATH, 'HOME': '/nonexistent', 'LANG': 'C.UTF-8', 'TZ': tz,
           'TELEGRAM_BRIDGE_COMMAND': 'quota'} if environ is None else environ
    stdin = request if isinstance(request, bytes) else json.dumps(request).encode()
    return qc.run(['--config', config], stdin, env, now=now, **kwargs)


def table(result):
    """The visible text of a send answer, checked against the handler contract."""
    actions = result['actions']
    assert len(actions) == 1 and actions[0]['type'] == 'send' and actions[0]['format'] == 'html', result
    text = actions[0]['text']
    assert text.startswith('<pre>') and text.endswith('</pre>'), text
    body = text[len('<pre>'):-len('</pre>')]
    assert '<' not in body and '>' not in body, body
    return body.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


class TableTest(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture(self)

    def test_sample_table(self):
        self.fx.provider('claude', claude_payload())
        reading = self.fx.reading('work.json', [codex_reading_group()])
        config = self.fx.config(providers=['claude'], readings=[reading], labels={'claude': 'personal'})
        text = table(answer(config))
        print('\n' + text)
        self.assertEqual(text, SAMPLE)

    def test_account_and_plan_never_shown(self):
        self.fx.provider('claude', claude_payload())
        text = table(answer(self.fx.config(providers=['claude'])))
        self.assertNotIn('someone', text)
        self.assertNotIn('Max 20x', text)
        self.assertEqual(text.splitlines()[0], 'Claude')

    def test_aiquokka_called_with_argv_and_no_refresh(self):
        for provider in ('claude', 'codex'):
            self.fx.provider(provider, claude_payload())
        answer(self.fx.config(providers=['claude', 'codex']))
        self.assertEqual(sorted(self.fx.calls()), [['claude', '--json', '--no-refresh'],
                                                   ['codex', '--json', '--no-refresh']])

    def test_rows(self):
        cases = [
            (window('5h', 69.4, NOW + datetime.timedelta(minutes=45, seconds=30)), '\U0001f7e2 5h       69%  Sat 16:44  45m'),
            (window('5h', 69.5, NOW + datetime.timedelta(minutes=45)), '\U0001f7e1 5h       70%  Sat 16:44  45m'),
            (window('Weekly', 89, NOW + datetime.timedelta(hours=23, minutes=59)), '\U0001f7e1 week     89%  Sun 15:58  23h59m'),
            (window('Weekly', 90, NOW + datetime.timedelta(days=6)), '\U0001f534 week     90%  Fri 15:59  6d0h'),
            (window('Weekly', 100, NOW - datetime.timedelta(minutes=1)), '\U0001f534 week    100%  Sat 15:58  \u2013'),
            (window('Weekly', None, NOW + datetime.timedelta(hours=1)), '\u26aa week       \u2013  Sat 16:59  1h00m'),
            (window('Weekly', 3, None), '\u26aa week       \u2013  no window'),
            ({'label': 'Weekly', 'used_percent': 3, 'resets_at': '0001-01-01T00:00:00Z'}, '\u26aa week       \u2013  no window'),
            ({'label': 'Weekly', 'used_percent': 3, 'resets_at': 'soon'}, '\U0001f7e2 week      3%  unknown'),
        ]
        tz = qc.zone(TZ)
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(qc.window_row(item, tz, NOW), expected)

    def test_labels(self):
        cases = {'5h': '5h', 'Weekly': 'week', 'Weekly Fable': 'Fable wk', 'Weekly Limit Remaining': 'week',
                 'Five Hour Limit': 'Five Hou', 'Balance': 'Balance', 'Wöchentlich': 'Wchentli'}
        for label, short in cases.items():
            with self.subTest(label=label):
                self.assertEqual(qc.short_label(label), short)

    def test_group_and_span_labels(self):
        cases = {'Gemini Models - Weekly': 'Gem wk', 'Gemini Models - 5h': 'Gem 5h',
                 'Claude and GPT models - Weekly': 'C+GPT wk', 'Claude and GPT models - 5h': 'C+GPT 5h',
                 'Gemini 4 Argon - Weekly': 'Argon wk', 'Gemini 4 Argon - Weekly Limit': 'Argon wk',
                 'Grok Heavy - Daily': 'Gr Daily', 'Big Models - Monthly cap': 'Monthly', ' - 5h': '- 5h'}
        for label, short in cases.items():
            with self.subTest(label=label):
                self.assertEqual(qc.short_label(label), short)

    def test_antigravity_rows_are_distinct(self):
        resets = NOW + datetime.timedelta(hours=4)
        labels = ['Gemini Models - Weekly', 'Gemini Models - 5h', 'Claude and GPT models - Weekly',
                  'Claude and GPT models - 5h', 'Gemini 4 Argon - Weekly']
        self.fx.provider('antigravity', {'provider': 'Antigravity', 'account': 'someone@example.com',
                                         'windows': [window(label, 12, resets) for label in labels]})
        text = table(answer(self.fx.config(providers=['antigravity'])))
        self.assertEqual(text.splitlines(), [
            'Antigravity',
            '\U0001f7e2 Gem wk   12%  Sat 19:59  4h00m',
            '\U0001f7e2 Gem 5h   12%  Sat 19:59  4h00m',
            '\U0001f7e2 C+GPT wk 12%  Sat 19:59  4h00m',
            '\U0001f7e2 C+GPT 5h 12%  Sat 19:59  4h00m',
            '\U0001f7e2 Argon wk 12%  Sat 19:59  4h00m',
        ])

    def test_equal_short_labels_show_whole_labels(self):
        windows = [{'label': label} for label in ('Weekly', 'Weekly Fable', 'Pro Models - Weekly', 'Pro Max - Weekly')]
        self.assertEqual(qc.row_labels(windows), ['Weekly             ', 'Weekly Fable       ',
                                                  'Pro Models - Weekly', 'Pro Max - Weekly   '])
        self.assertEqual(qc.row_labels(windows[:2]), ['week    ', 'Fable wk'])
        long = [{'label': 'Pro ' + 'x' * 40 + ' - Weekly'}, {'label': 'Pro Max - Weekly'}]
        self.assertEqual([len(label) for label in qc.row_labels(long)], [32, 32])

    def test_time_zone_from_environment_else_utc(self):
        self.assertEqual(qc.zone('Etc/GMT-5').utcoffset(NOW), datetime.timedelta(hours=5))
        for name in (None, '', 'No/Such_Zone', '/etc/passwd', '../../x'):
            with self.subTest(name=name):
                self.assertIs(qc.zone(name), datetime.timezone.utc)
        reading = self.fx.reading('r.json', [codex_reading_group()])
        text = table(answer(self.fx.config(providers=[], readings=[reading]), tz='Not/AZone'))
        self.assertIn('Thu 06:24', text)

    def test_group_order(self):
        for provider in ('claude', 'codex'):
            self.fx.provider(provider, {'provider': provider.capitalize(), 'windows': []})
        first = self.fx.reading('a.json', [
            {'provider': 'Grok', 'windows': []},
            {'provider': 'Codex', 'account': 'work', 'windows': []},
            {'provider': 'Claude', 'account': 'work', 'windows': []},
        ])
        second = self.fx.reading('b.json', [{'provider': 'Claude', 'account': 'lab', 'windows': []}])
        config = self.fx.config(providers=['claude', 'codex'], readings=[first, second],
                                labels={'claude': 'personal', 'codex': 'personal'})
        headers = [line for line in table(answer(config)).splitlines() if not line.startswith('\u26aa')]
        self.assertEqual(headers, ['Claude \u00b7 personal', 'Claude \u00b7 work', 'Claude \u00b7 lab',
                                   'Codex \u00b7 personal', 'Codex \u00b7 work', 'Grok'])

    def test_text_is_escaped(self):
        reading = self.fx.reading('r.json', [{'provider': '<b>A&B</b>', 'account': 'x<y',
                                              'windows': [window('<i>', 5, NOW + datetime.timedelta(hours=1))]}])
        result = answer(self.fx.config(providers=[], readings=[reading]))
        raw = result['actions'][0]['text']
        self.assertIn('&lt;b&gt;A&amp;B&lt;/b&gt; \u00b7 x&lt;y', raw)
        self.assertEqual(table(result).splitlines()[1][:6], '\U0001f7e2 <i> ')

    def test_long_reply_drops_whole_groups(self):
        groups = [{'provider': f'P{i:02d}', 'account': 'a', 'windows': [
            window(f'w{j}', j, NOW + datetime.timedelta(hours=j + 1)) for j in range(16)]} for i in range(32)]
        readings = [self.fx.reading(f'r{n}.json', groups) for n in range(2)]
        result = answer(self.fx.config(providers=[], readings=readings))
        text = table(result)
        lines = text.splitlines()
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertEqual(lines[-1], '\u2026')
        body = lines[:-1]
        self.assertEqual(len(body) % 17, 0)
        for start in range(0, len(body), 17):
            self.assertTrue(body[start].startswith('P'), body[start])
            self.assertTrue(all(not line.startswith('P') for line in body[start + 1:start + 17]))
        self.assertLess(len(body) // 17, 64)

    def test_fit_cuts_a_lone_long_group_by_line(self):
        block = ['Huge'] + ['\U0001f7e2 ' + 'x' * 40] * 200
        lines = qc.fit([block])
        self.assertEqual(lines[-1], '\u2026')
        self.assertTrue(qc._fits(lines))
        self.assertGreater(len(lines), 50)


class InputAndConfigTest(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture(self)
        self.fx.provider('claude', claude_payload())

    def assertUnavailable(self, result, why):
        self.assertEqual(table(result), f'\u26aa quota unavailable: {why}')

    def test_callback_answer(self):
        request = dict(REQUEST, kind='callback', args=None, message_id=None, callback_data='quota:x')
        self.assertEqual(answer('/nonexistent/config.toml', request), {'actions': [{'type': 'answer_callback'}]})
        self.assertEqual(self.fx.calls(), [])

    def test_unreadable_stdin(self):
        for stdin in (b'', b'not json', b'[1]', b'{"kind": "command", "kind": "callback"}', b'{"a": NaN}',
                      b'{"a": "\\ud800"}', b'\xff\xfe', b'[' * 20000 + b']' * 20000, b'{"a": "' + b'x' * (64 * 1024) + b'"}'):
            with self.subTest(stdin=stdin[:40]):
                self.assertUnavailable(answer(self.fx.config(), stdin), 'unreadable input')
        self.assertUnavailable(qc.run(['--config', self.fx.config()], None, {}), 'unreadable input')

    def test_config_refusals_name_the_key_never_the_path(self):
        cases = [
            ({'colour': 'red'}, 'unknown key colour'),
            ({'raw': '"/odd key" = 1\n'}, 'unknown key'),
            ({'raw': 'providers = [\n'}, 'config is not valid TOML'),
            ({'providers': ['Claude']}, 'providers must be a list of provider names'),
            ({'providers': ['claude', 'claude']}, 'providers must not repeat'),
            ({'provider_timeout_seconds': 16}, 'provider_timeout_seconds must be 1-15'),
            ({'provider_timeout_seconds': True}, 'provider_timeout_seconds must be 1-15'),
            ({'readings': ['relative.json']}, 'readings must be an absolute path'),
            ({'readings': '/a.json'}, 'readings must be a list of absolute paths'),
            ({'path_prepend': ['/a:/b']}, 'path_prepend entries must not contain a colon'),
            ({'cache_dir': 'cache'}, 'cache_dir must be an absolute path'),
            ({'deepseek_key_file': 3}, 'deepseek_key_file must be an absolute path'),
            ({'aiquokka': 'aiquokka'}, 'aiquokka must be an absolute path'),
            ({'labels': {'claude': ''}}, 'labels must map providers to labels of 1-32 characters'),
            ({'mode': 0o644}, 'config must be an owner-only regular file'),
        ]
        for keys, why in cases:
            with self.subTest(keys=keys):
                keys = dict(keys)
                config = self.fx.config(**keys)
                result = answer(config)
                self.assertUnavailable(result, why)
                self.assertNotIn(str(self.fx.root), json.dumps(result))

    def test_example_config_loads(self):
        path = self.fx.root / 'example.toml'
        path.write_text((HERE / 'quota.example.toml').read_text())
        path.chmod(0o600)
        config = qc.load_config(str(path))
        self.assertEqual(config['providers'], list(qc.DEFAULT_PROVIDERS))
        self.assertEqual(config['provider_timeout_seconds'], qc.DEFAULT_TIMEOUT)

    def test_aiquokka_required_with_providers(self):
        path = self.fx.root / 'quota.toml'
        path.write_text('providers = ["claude"]\n')
        path.chmod(0o600)
        self.assertUnavailable(answer(str(path)), 'aiquokka is required')
        path.write_text('')
        self.assertUnavailable(answer(str(path)), 'aiquokka is required')

    def test_config_path_and_arguments(self):
        self.assertUnavailable(answer('relative.toml'), 'config path must be absolute')
        self.assertUnavailable(answer(str(self.fx.root / 'missing.toml')), 'config unreadable')
        stdin = json.dumps(REQUEST).encode()
        self.assertUnavailable(qc.run([], stdin, {}), 'usage: quota_command.py --config <path>')

    def test_no_sources(self):
        self.assertUnavailable(answer(self.fx.config(providers=[])), 'no sources configured')

    def test_internal_error_is_still_an_answer(self):
        original = qc.collect
        qc.collect = lambda *args, **kwargs: 1 / 0
        try:
            self.assertUnavailable(answer(self.fx.config()), 'internal error')
        finally:
            qc.collect = original


class ProviderTest(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture(self)

    def rows(self, config, **kwargs):
        return table(answer(config, **kwargs)).splitlines()

    def test_failures_show_one_row_each(self):
        self.fx.provider('claude', stdout='{"windows": [}')
        self.fx.provider('codex', stdout='[1, 2]')
        self.fx.provider('grok', exit=3)
        self.fx.provider('muse', stderr='aiquokka: token for someone@example.org at /srv/someone/.muse/auth.json expired\nsecond line\n', exit=4)
        self.fx.provider('deepseek', big=True)
        config = self.fx.config(providers=['claude', 'codex', 'grok', 'muse', 'deepseek', 'antigravity'])
        self.assertEqual(self.rows(config), [
            'Claude', '\u26aa unavailable: unreadable output',
            'Codex', '\u26aa unavailable: unexpected output',
            'Grok', '\u26aa unavailable: exit status 3',
            'Muse', '\u26aa unavailable: token for <account> at <path> expired',
            'DeepSeek', '\u26aa unavailable: output too large',
            'Antigravity', '\u26aa unavailable: no such provider',
        ])

    def test_missing_aiquokka(self):
        config = self.fx.config(providers=['claude'], aiquokka=str(self.fx.root / 'none'))
        self.assertEqual(self.rows(config), ['Claude', '\u26aa unavailable: aiquokka not available'])

    def test_slow_provider_times_out_and_its_group_is_killed(self):
        self.fx.provider('claude', claude_payload(), sleep=30, child=True)
        self.fx.provider('codex', {'provider': 'Codex', 'windows': [window('Weekly', 7, NOW + datetime.timedelta(days=1))]})
        started = time.monotonic()
        rows = self.rows(self.fx.config(providers=['claude', 'codex'], provider_timeout_seconds=1))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(rows[:2], ['Claude', '\u26aa unavailable: timeout'])
        self.assertEqual(rows[2:], ['Codex', '\U0001f7e2 week      7%  Sun 15:59  1d0h'])
        self.assertProcessGone(int((self.fx.fake / 'claude.child').read_text()))

    def test_the_whole_run_has_one_budget(self):
        self.fx.provider('claude', sleep=30)
        started = time.monotonic()
        rows = self.rows(self.fx.config(providers=['claude'], provider_timeout_seconds=15), budget=1.0)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(rows, ['Claude', '\u26aa unavailable: timeout'])

    def assertProcessGone(self, pid):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state in ('Z', 'X'):
                return
            time.sleep(0.05)
        self.fail(f'process {pid} still running')

    def test_path_prepend_reaches_the_provider(self):
        tools = self.fx.root / 'tools'
        self.fx.helper(tools, 'fake-muse', json.dumps({'provider': 'Muse', 'windows': [
            window('5h', 12, NOW + datetime.timedelta(minutes=30))]}))
        self.fx.provider('muse', via='fake-muse')
        rows = self.rows(self.fx.config(providers=['muse'], path_prepend=[str(tools)]))
        self.assertEqual(rows, ['Muse', '\U0001f7e2 5h       12%  Sat 16:29  30m'])
        self.assertEqual(self.fx.env_of('muse')['PATH'], f'{tools}:{qc.FIXED_PATH}')

    def test_deepseek_key_reaches_deepseek_only(self):
        key = self.fx.root / 'deepseek.key'
        key.write_text('sk-test-0123\n')
        key.chmod(0o600)
        for provider in ('deepseek', 'grok'):
            self.fx.provider(provider, {'windows': []})
        environ = {'PATH': qc.FIXED_PATH, 'HOME': '/nonexistent', 'TZ': TZ, 'DEEPSEEK_API_KEY': 'inherited'}
        result = answer(self.fx.config(providers=['deepseek', 'grok'], deepseek_key_file=str(key)), environ=environ)
        self.assertEqual(self.fx.env_of('deepseek')['DEEPSEEK_API_KEY'], 'sk-test-0123')
        self.assertNotIn('DEEPSEEK_API_KEY', self.fx.env_of('grok'))
        self.assertNotIn('sk-test', json.dumps(result))

    def test_unsafe_key_file_means_no_key(self):
        key = self.fx.root / 'deepseek.key'
        key.write_text('sk-test-0123\n')
        key.chmod(0o644)
        self.fx.provider('deepseek', {'windows': []})
        answer(self.fx.config(providers=['deepseek'], deepseek_key_file=str(key)))
        self.assertNotIn('DEEPSEEK_API_KEY', self.fx.env_of('deepseek'))
        answer(self.fx.config(providers=['deepseek'], deepseek_key_file=str(self.fx.root / 'missing.key')))
        self.assertNotIn('DEEPSEEK_API_KEY', self.fx.env_of('deepseek'))


class ReadingTest(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture(self)

    def rows(self, *readings):
        return table(answer(self.fx.config(providers=[], readings=list(readings)))).splitlines()

    def test_good_reading_with_an_error_group(self):
        path = self.fx.reading('work.json', [
            codex_reading_group(),
            {'provider': 'Claude', 'account': 'work', 'windows': [],
             'error': 'login for someone@example.org failed: see /var/lib/x/log ' + 'y' * 80},
        ])
        rows = self.rows(path)
        self.assertEqual(rows[-2:], ['Claude \u00b7 work',
                                     '\u26aa unavailable: login for <account> failed: see <path> ' + 'y' * 21])
        self.assertEqual(len(rows[-1]) - len('\u26aa unavailable: '), 60)

    def test_stale_and_future_readings(self):
        for name, observed in (('old.json', NOW - datetime.timedelta(minutes=31)),
                               ('ahead.json', NOW + datetime.timedelta(minutes=6))):
            with self.subTest(name=name):
                path = self.fx.reading(name, [codex_reading_group(), {'provider': 'Claude', 'windows': []}], observed)
                self.assertEqual(self.rows(path), ['Codex \u00b7 work', '\u26aa unavailable: stale reading',
                                                   'Claude', '\u26aa unavailable: stale reading'])
        fresh = self.fx.reading('fresh.json', [codex_reading_group()], NOW - datetime.timedelta(minutes=29))
        self.assertEqual(self.rows(fresh)[0], 'Codex \u00b7 work')

    def test_unusable_files_are_one_group_named_by_the_file(self):
        directory = self.fx.root / 'dir.json'
        directory.mkdir()
        link = self.fx.root / 'link.json'
        link.symlink_to(self.fx.reading('target.json', [codex_reading_group()]))
        cases = [
            (str(self.fx.root / 'missing.json'), 'no reading'),
            (self.fx.reading('broken.json', '{"observed_at": '), 'malformed reading'),
            (self.fx.reading('naive.json', {'observed_at': '2026-10-17T10:59:00', 'groups': []}), 'malformed reading'),
            (self.fx.reading('extra.json', {'observed_at': iso(NOW), 'groups': [], 'accounts': {}}), 'malformed reading'),
            (self.fx.reading('window.json', [{'provider': 'Claude', 'windows': [{'label': '5h', 'used_percent': '5'}]}]),
             'malformed reading'),
            (self.fx.reading('nested.json', '[' * 20000 + ']' * 20000), 'malformed reading'),
            (self.fx.reading('big.json', 'x' * (64 * 1024 + 1)), 'reading too large'),
            (str(directory), 'not a regular file'),
            (str(link), 'unreadable reading'),
        ]
        for path, why in cases:
            with self.subTest(why=why, path=os.path.basename(path)):
                self.assertEqual(self.rows(path), [os.path.basename(path), f'\u26aa unavailable: {why}'])

    def readings(self, count):
        return [self.fx.reading(f'r{i:02}.json', [{'provider': f'P{i:02}', 'windows': [], 'error': 'read'}])
                for i in range(count)]

    def test_slow_readings_time_out_inside_the_run_budget(self):
        paths = self.readings(3 * qc.READING_WORKERS)
        slow = {paths[1], paths[5]}
        release = threading.Event()
        self.addCleanup(release.set)
        real = qc.read_reading

        def reader(path, now):
            if path in slow:
                release.wait(30)
            return real(path, now)

        with mock.patch.object(qc, 'read_reading', reader):
            started = time.monotonic()
            rows = table(answer(self.fx.config(providers=[], readings=paths), budget=1.0)).splitlines()
            self.assertLess(time.monotonic() - started, 4)
        expected = []
        for i, path in enumerate(paths):
            expected += ([f'r{i:02}.json', '\u26aa unavailable: timeout'] if path in slow
                         else [f'P{i:02}', '\u26aa unavailable: read'])
        self.assertEqual(rows, expected)

    def test_a_reading_that_raises_is_unreadable(self):
        paths = self.readings(2)
        real = qc.read_reading

        def reader(path, now):
            if path == paths[0]:
                raise RuntimeError('boom')
            return real(path, now)

        with mock.patch.object(qc, 'read_reading', reader):
            rows = self.rows(*paths)
        self.assertEqual(rows, ['r00.json', '\u26aa unavailable: unreadable reading',
                                'P01', '\u26aa unavailable: read'])


class CacheTest(unittest.TestCase):

    def setUp(self):
        self.fx = Fixture(self)
        self.cache = self.fx.root / 'cache'
        self.cache.mkdir(mode=0o700)

    def config(self):
        return self.fx.config(providers=['claude'], cache_dir=str(self.cache), labels={'claude': 'personal'})

    def test_last_good_answer_is_shown_with_its_time(self):
        self.fx.provider('claude', claude_payload())
        live = table(answer(self.config()))
        self.assertEqual(os.listdir(self.cache), ['quota-cache.json'])
        cached = self.cache / 'quota-cache.json'
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o600)
        self.assertNotIn('someone', cached.read_text())
        self.assertNotIn('Max 20x', cached.read_text())

        self.fx.provider('claude', exit=1, stderr='aiquokka: not logged in\n')
        later = NOW + datetime.timedelta(hours=1)
        rows = table(answer(self.config(), now=later)).splitlines()
        self.assertEqual(rows[0], 'Claude \u00b7 personal (as of Sat 15:59)')
        self.assertEqual(rows[1], '\U0001f7e2 5h        5%  Sat 19:59  3h00m')
        self.assertEqual(live.splitlines()[1:], [
            '\U0001f7e2 5h        5%  Sat 19:59  4h00m',
            '\U0001f7e2 week     56%  Wed 10:59  3d19h',
            '\U0001f7e2 Fable wk  9%  Wed 10:59  3d19h'])

        expired = NOW + datetime.timedelta(hours=24, minutes=1)
        self.assertEqual(table(answer(self.config(), now=expired)).splitlines(),
                         ['Claude \u00b7 personal', '\u26aa unavailable: not logged in'])
        self.assertEqual(os.listdir(self.cache), ['quota-cache.json'])

    def test_no_cache_dir_writes_nothing(self):
        self.fx.provider('claude', claude_payload())
        config = self.fx.config(providers=['claude'])
        before = sorted(p.name for p in self.fx.root.iterdir())
        answer(config)
        self.assertEqual(sorted(p.name for p in self.fx.root.iterdir()), before)

    def test_unsafe_cache_is_ignored(self):
        self.fx.provider('claude', exit=1)
        cached = self.cache / 'quota-cache.json'
        cached.write_text(json.dumps({'providers': {'claude': {
            'at': iso(NOW), 'name': 'Claude', 'windows': [window('5h', 5, NOW + datetime.timedelta(hours=1))]}}}))
        cached.chmod(0o644)
        self.assertEqual(table(answer(self.config())).splitlines()[1], '\u26aa unavailable: exit status 1')
        cached.chmod(0o600)
        self.assertEqual(table(answer(self.config())).splitlines()[0], 'Claude \u00b7 personal (as of Sat 15:59)')


class EndToEndTest(unittest.TestCase):

    def test_bridge_environment_and_handler_contract(self):
        fx = Fixture(self)
        tools = fx.root / 'tools'
        fx.helper(tools, 'fake-muse', json.dumps({'provider': 'Muse', 'windows': [
            window('5h', 12, datetime.datetime.now(UTC) + datetime.timedelta(hours=2))]}))
        fx.provider('muse', via='fake-muse')
        reading = fx.reading('work.json', [codex_reading_group()], datetime.datetime.now(UTC))
        config = fx.config(providers=['muse'], path_prepend=[str(tools)], readings=[reading])
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(fx.root), 'LANG': 'C.UTF-8', 'TZ': TZ,
               'TELEGRAM_BRIDGE_COMMAND': 'quota'}
        result = subprocess.run([str(COMMAND), '--config', config], input=json.dumps(REQUEST).encode(),
                                capture_output=True, env=env, cwd=str(HERE), timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 64 * 1024)
        answer_object = qc.strict_json(result.stdout)
        self.assertEqual(set(answer_object), {'actions'})
        self.assertEqual(set(answer_object['actions'][0]), {'type', 'format', 'text'})
        rows = table(answer_object).splitlines()
        self.assertEqual(rows[0], 'Muse')
        self.assertRegex(rows[1], r'^\U0001f7e2 5h       12%  \w{3} \d\d:\d\d  1h59m$')
        self.assertEqual(rows[2:4], ['Codex \u00b7 work', '\u26aa 5h         \u2013  no window'])
        self.assertEqual(fx.env_of('muse')['PATH'], f'{tools}:/usr/local/bin:/usr/bin:/bin')

    def test_unreadable_stdin_still_answers(self):
        result = subprocess.run([str(COMMAND), '--config', '/nonexistent.toml'], input=b'{', capture_output=True,
                                env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(table(qc.strict_json(result.stdout)), '\u26aa quota unavailable: unreadable input')


if __name__ == '__main__':
    unittest.main()
