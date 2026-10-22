#!/usr/bin/env python3
"""telegram-quota: the /quota command for the Telegram bridge pack.

A handler of the bridge's [[command]] contract: one JSON object on stdin, one
`{"actions": [...]}` object on stdout, exit 0 always. The reply is one <pre>
table of the usage windows read from aiquokka (one call per provider) and
from reading files in the shape README.md documents, with the time left until
each window resets.

    quota_command.py --config <absolute path of the TOML config>

Python 3.11 or later, standard library only. Nothing here runs a shell; every
child gets an argv list, closed stdin, a capped stdout, a deadline and its own
process group, killed whole when the read ends. The only file ever written is
the optional cache in `cache_dir`.
"""

import concurrent.futures
import datetime
import json
import math
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
import tomllib
import zoneinfo

FIXED_PATH = '/usr/local/bin:/usr/bin:/bin'
INPUT_MAX_BYTES = 64 * 1024
CONFIG_MAX_BYTES = 64 * 1024
READING_MAX_BYTES = 64 * 1024
PROVIDER_OUTPUT_MAX_BYTES = 64 * 1024
PROVIDER_STDERR_MAX_BYTES = 4096
KEY_MAX_BYTES = 4096
CACHE_MAX_BYTES = 256 * 1024
CACHE_NAME = 'quota-cache.json'
# The bridge allows the handler `timeout = 20`; every provider and reading
# file read ends by this many seconds after start, which leaves the reply well
# inside 15 s.
RUN_BUDGET_SECONDS = 13.0
READING_WORKERS = 8
READING_MAX_AGE = 30 * 60
READING_MAX_AHEAD = 5 * 60
CACHE_MAX_AGE = 24 * 3600
TEXT_MAX_UNITS = 4096
HTML_SOURCE_MAX_UNITS = 16384
REASON_MAX = 60
NAME_MAX = 32
MAX_GROUPS_PER_READING = 32
MAX_WINDOWS = 16

DEFAULT_PROVIDERS = ('claude', 'codex', 'muse', 'antigravity', 'grok', 'deepseek')
DEFAULT_TIMEOUT = 8
CONFIG_KEYS = {'aiquokka', 'providers', 'provider_timeout_seconds', 'deepseek_key_file',
               'path_prepend', 'readings', 'cache_dir', 'labels'}
PROVIDER_RE = re.compile(r'[a-z0-9-]{1,32}\Z')
KEY_NAME_RE = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')
DISPLAY_NAMES = {'claude': 'Claude', 'codex': 'Codex', 'muse': 'Muse', 'antigravity': 'Antigravity',
                 'grok': 'Grok', 'deepseek': 'DeepSeek'}
SHORT_LABELS = {'5h': '5h', 'Weekly': 'week', 'Weekly Fable': 'Fable wk'}
# '<group> - <span>' labels (one provider's several model groups) become
# '<group> <span>' with both parts short.
SHORT_GROUPS = {'Gemini Models': 'Gem', 'Claude and GPT models': 'C+GPT', 'Gemini 4 Argon': 'Argon'}
SHORT_SPANS = {'Weekly': 'wk', '5h': '5h'}
LABEL_SUFFIXES = (' Remaining', ' Limit')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
LABEL_WIDTH = 8
WHOLE_LABEL_MAX = 32
PERCENT_WIDTH = 4
DASH = '–'
ELLIPSIS = '…'
SEPARATOR = ' · '
WHITE, GREEN, YELLOW, RED = '⚪', '\U0001f7e2', '\U0001f7e1', '\U0001f534'

READING_KEYS = {'observed_at', 'groups'}
GROUP_KEYS = {'provider', 'account', 'windows', 'error'}
WINDOW_KEYS = {'label', 'used_percent', 'resets_at'}


class Unavailable(Exception):
    """A reason the whole reply is the one unavailable row (never a path)."""


# --- strict input --------------------------------------------------------

def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('duplicate key')
        seen[key] = value
    return seen


def _no_constant(name):
    raise ValueError(f'{name} is not JSON')


def _has_surrogate(text):
    return any('\ud800' <= c <= '\udfff' for c in text)


def _check_strings(value):
    if isinstance(value, str):
        if _has_surrogate(value):
            raise ValueError('lone surrogate')
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_strings(key)
            _check_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_strings(item)


def strict_json(data):
    """Parse bytes as one strict JSON value: UTF-8, no duplicate keys, no NaN
    or Infinity, no string escaping a lone surrogate. ValueError otherwise
    (also for nesting too deep to parse)."""
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        _check_strings(value)
    except RecursionError:
        raise ValueError('nested too deeply') from None
    return value


def _number(value):
    """A finite JSON number (not a boolean), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and abs(value) < 1e6 else None
    except OverflowError:
        return None


# --- text shown in the reply --------------------------------------------

def clean(text, limit):
    """Printable characters only (no control, format or surrogate), stripped
    and cut to `limit` characters."""
    return ''.join(c for c in str(text) if c.isprintable()).strip()[:limit]


def reason(text):
    """A reason as the reply may show it: its first line, emails as <account>,
    paths as <path>, a leading 'aiquokka: ' dropped, 120 characters, then
    printable characters only and REASON_MAX characters."""
    line = (str(text).strip().splitlines() or ['unavailable'])[0]
    line = re.sub(r'\S+@\S+', '<account>', line)
    line = re.sub(r'(?:~|\$HOME)?/[^\s`\'"]+', '<path>', line)
    line = (line.replace('aiquokka: ', '', 1).strip() or 'unavailable')[:120]
    return clean(line, REASON_MAX) or 'unavailable'


def escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def utf16_units(text):
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


# --- config -------------------------------------------------------------

def _read_capped(fd, limit):
    chunks, size = [], 0
    while size <= limit:
        chunk = os.read(fd, limit + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise OverflowError('too large')
    return b''.join(chunks)


def _owner_only_bytes(path, limit, nofollow=False):
    """The bytes of an owner-only regular file of at most `limit` bytes.
    PermissionError when it is not one; OSError when it cannot be read."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError('not an owner-only regular file')
        return _read_capped(fd, limit)
    finally:
        os.close(fd)


def _absolute(key, value):
    if not isinstance(value, str) or not value or '\0' in value or not os.path.isabs(value):
        raise Unavailable(f'{key} must be an absolute path')
    return value


def _absolute_list(key, value, directories=False):
    if not isinstance(value, list):
        raise Unavailable(f'{key} must be a list of absolute paths')
    for item in value:
        _absolute(key, item)
        if directories and ':' in item:
            raise Unavailable(f'{key} entries must not contain a colon')
    return list(value)


def load_config(path):
    """The validated config as a dict, or Unavailable naming the key."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise Unavailable('config path must be absolute')
    try:
        raw = _owner_only_bytes(path, CONFIG_MAX_BYTES)
    except PermissionError:
        raise Unavailable('config must be an owner-only regular file') from None
    except OverflowError:
        raise Unavailable('config is too large') from None
    except OSError:
        raise Unavailable('config unreadable') from None
    try:
        data = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise Unavailable('config is not valid TOML') from None
    for key in data:
        if key not in CONFIG_KEYS:
            raise Unavailable(f'unknown key {key}' if KEY_NAME_RE.match(key) else 'unknown key')

    config = {}
    providers = data.get('providers', list(DEFAULT_PROVIDERS))
    if not isinstance(providers, list) or not all(isinstance(p, str) and PROVIDER_RE.match(p) for p in providers):
        raise Unavailable('providers must be a list of provider names')
    if len(set(providers)) != len(providers):
        raise Unavailable('providers must not repeat')
    config['providers'] = providers

    if 'aiquokka' in data:
        config['aiquokka'] = _absolute('aiquokka', data['aiquokka'])
    elif providers:
        raise Unavailable('aiquokka is required')

    timeout = data.get('provider_timeout_seconds', DEFAULT_TIMEOUT)
    if _number(timeout) is None or not 1 <= timeout <= 15:
        raise Unavailable('provider_timeout_seconds must be 1-15')
    config['provider_timeout_seconds'] = timeout

    config['deepseek_key_file'] = (_absolute('deepseek_key_file', data['deepseek_key_file'])
                                   if 'deepseek_key_file' in data else None)
    config['path_prepend'] = _absolute_list('path_prepend', data.get('path_prepend', []), directories=True)
    config['readings'] = _absolute_list('readings', data.get('readings', []))
    config['cache_dir'] = _absolute('cache_dir', data['cache_dir']) if 'cache_dir' in data else None

    labels = data.get('labels', {})
    if not isinstance(labels, dict):
        raise Unavailable('labels must be a table')
    for name, label in labels.items():
        if not PROVIDER_RE.match(name) or not isinstance(label, str) or not 1 <= len(label) <= NAME_MAX \
                or clean(label, NAME_MAX) != label:
            raise Unavailable('labels must map providers to labels of 1-32 characters')
    config['labels'] = dict(labels)
    return config


def deepseek_key(path):
    """The key file's text, read like any credential: an owner-only regular
    file holding one token. None when there is none or it is not safe."""
    if not path:
        return None
    try:
        value = _owner_only_bytes(path, KEY_MAX_BYTES).decode('utf-8').strip()
    except (OSError, OverflowError, UnicodeDecodeError):
        return None
    if not value or any(c.isspace() for c in value):
        return None
    return value


def zone(name):
    """The zone TZ names, else UTC."""
    if name:
        try:
            return zoneinfo.ZoneInfo(name.removeprefix(':'))
        except Exception:
            pass
    return datetime.timezone.utc


# --- aiquokka -----------------------------------------------------------

def _exited(pid):
    """True once the child has exited; it stays a zombie (WNOWAIT), so its
    process group id cannot be reused before the group is killed."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def run_capped(argv, env, deadline, monotonic=time.monotonic):
    """Run argv (no shell, stdin closed, own process group) until it exits or
    `deadline` (a monotonic time) passes. Returns (status, stdout, stderr,
    problem): problem is None, 'not_started', 'timeout' or 'too_large'. The
    child's whole process group is killed before this returns."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True, close_fds=True)
    except (OSError, ValueError):
        return None, b'', b'', 'not_started'
    out, err, problem = bytearray(), bytearray(), None
    selector = selectors.DefaultSelector()
    try:
        selector.register(proc.stdout, selectors.EVENT_READ, 'out')
        selector.register(proc.stderr, selectors.EVENT_READ, 'err')
        while True:
            left = deadline - monotonic()
            if left <= 0:
                problem = 'timeout'
                break
            if not selector.get_map():
                if _exited(proc.pid):
                    break
                time.sleep(min(left, 0.02))
                continue
            events = selector.select(min(left, 0.05))
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 'out':
                    out += chunk
                    if len(out) > PROVIDER_OUTPUT_MAX_BYTES:
                        problem = 'too_large'
                else:
                    err += chunk[:max(PROVIDER_STDERR_MAX_BYTES - len(err), 0)]
            if problem:
                break
            # A helper the child left behind may hold the pipes open: once the
            # child itself is gone and nothing more is waiting, the read ends.
            if not events and _exited(proc.pid):
                break
    finally:
        selector.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    return proc.returncode, bytes(out), bytes(err), problem


def _window(item):
    if not isinstance(item, dict):
        return None
    label = item.get('label')
    resets = item.get('resets_at')
    return {'label': label if isinstance(label, str) else '?',
            'used_percent': _number(item.get('used_percent')),
            'resets_at': resets if isinstance(resets, str) else None}


def parse_provider(stdout, provider):
    """{'name', 'windows'} from aiquokka's JSON for one provider; only the
    window labels, percents and resets are kept (never account or plan)."""
    try:
        payload = strict_json(stdout)
    except ValueError:
        return {'error': 'unreadable output'}
    if not isinstance(payload, dict) or not isinstance(payload.get('windows'), list):
        return {'error': 'unexpected output'}
    windows = [w for w in (_window(item) for item in payload['windows'][:MAX_WINDOWS]) if w]
    name = payload.get('provider')
    name = clean(name, NAME_MAX) if isinstance(name, str) else ''
    return {'name': name or DISPLAY_NAMES.get(provider, provider.capitalize()), 'windows': windows}


def provider_environment(environ, path_prepend):
    env = dict(environ)
    env['PATH'] = ':'.join(list(path_prepend) + [FIXED_PATH])
    env.pop('DEEPSEEK_API_KEY', None)
    return env


def query_provider(aiquokka, provider, env, deadline, monotonic=time.monotonic):
    status, out, err, problem = run_capped([aiquokka, provider, '--json', '--no-refresh'], env, deadline, monotonic)
    if problem == 'not_started':
        return {'error': 'aiquokka not available'}
    if problem == 'timeout':
        return {'error': 'timeout'}
    if problem == 'too_large':
        return {'error': 'output too large'}
    if status != 0:
        text = (err or out).decode('utf-8', 'replace').strip()
        return {'error': reason(text) if text else f'exit status {status}'}
    return parse_provider(out, provider)


def read_providers(config, environ, deadline, monotonic=time.monotonic):
    """{provider: {'name', 'windows'} | {'error'}} for every listed provider,
    read in parallel; one not done by `deadline` is a timeout."""
    providers = config['providers']
    if not providers:
        return {}
    base = provider_environment(environ, config['path_prepend'])
    key = deepseek_key(config['deepseek_key_file']) if 'deepseek' in providers else None
    envs = {}
    for provider in providers:
        envs[provider] = dict(base)
        if provider == 'deepseek' and key:
            envs[provider]['DEEPSEEK_API_KEY'] = key
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(providers))
    try:
        futures = {provider: pool.submit(query_provider, config['aiquokka'], provider, envs[provider],
                                         min(monotonic() + config['provider_timeout_seconds'], deadline), monotonic)
                   for provider in providers}
        concurrent.futures.wait(futures.values(), timeout=max(deadline - monotonic(), 0) + 1)
        results = {}
        for provider, future in futures.items():
            if not future.done():
                results[provider] = {'error': 'timeout'}
            elif future.exception() is not None:
                results[provider] = {'error': 'read failed'}
            else:
                results[provider] = future.result()
        return results
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# --- the cache ----------------------------------------------------------

def _hold_dir(path):
    """An O_NOFOLLOW descriptor of `path`, which must be its own canonical
    path; OSError otherwise."""
    if os.path.realpath(path) != os.path.normpath(path):
        raise OSError('cache directory is not its own canonical path')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        pinned, named = os.fstat(fd), os.lstat(path)
        if (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino):
            raise OSError('cache directory changed while opening')
    except BaseException:
        os.close(fd)
        raise
    return fd


def _parse_time(text):
    """An aware datetime from ISO-8601 text with a zone, else None."""
    if not isinstance(text, str):
        return None
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def load_cache(cache_dir):
    """{provider: {'at': datetime, 'name', 'windows'}} from the cache file;
    {} when there is none or it is not the cache's own shape."""
    try:
        dfd = _hold_dir(cache_dir)
    except OSError:
        return {}
    try:
        fd = os.open(CACHE_NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                return {}
            data = strict_json(_read_capped(fd, CACHE_MAX_BYTES))
        finally:
            os.close(fd)
    except (OSError, OverflowError, ValueError):
        return {}
    finally:
        os.close(dfd)
    entries = data.get('providers') if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    cache = {}
    for provider, entry in entries.items():
        if not isinstance(entry, dict) or not isinstance(entry.get('windows'), list):
            continue
        at = _parse_time(entry.get('at'))
        name = entry.get('name')
        if at is None or not isinstance(name, str):
            continue
        windows = [w for w in (_window(item) for item in entry['windows'][:MAX_WINDOWS]) if w]
        cache[provider] = {'at': at, 'name': clean(name, NAME_MAX) or provider, 'windows': windows}
    return cache


def save_cache(cache_dir, cache):
    """Write the cache atomically: an unpredictable temp name created with
    O_EXCL|O_NOFOLLOW in the held directory, owner-only, fsynced, renamed
    over the cache. A failure leaves the old file and costs one stderr line."""
    body = json.dumps({'providers': {
        provider: {'at': entry['at'].isoformat(timespec='seconds'), 'name': entry['name'],
                   'windows': entry['windows']}
        for provider, entry in sorted(cache.items())}}, sort_keys=True, ensure_ascii=False).encode('utf-8')
    tmp = f'.quota-cache-{secrets.token_hex(16)}.tmp'
    try:
        dfd = _hold_dir(cache_dir)
    except OSError:
        print('quota cache not saved', file=sys.stderr)
        return
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        data = memoryview(body)
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, CACHE_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    except OSError:
        print('quota cache not saved', file=sys.stderr)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass  # renamed into place, or never created
        os.close(dfd)


# --- reading files ------------------------------------------------------

def _reading_group(item):
    """One group of a reading file, or ValueError when it is not the shape."""
    if not isinstance(item, dict) or not set(item) <= GROUP_KEYS or 'provider' not in item:
        raise ValueError('group shape')
    provider, account, error = item['provider'], item.get('account'), item.get('error')
    windows = item.get('windows', [])
    if not isinstance(provider, str) or not clean(provider, NAME_MAX):
        raise ValueError('provider')
    if account is not None and not isinstance(account, str):
        raise ValueError('account')
    if error is not None and not isinstance(error, str):
        raise ValueError('error')
    if not isinstance(windows, list):
        raise ValueError('windows')
    parsed = []
    for window in windows[:MAX_WINDOWS]:
        if not isinstance(window, dict) or not set(window) <= WINDOW_KEYS or not isinstance(window.get('label'), str):
            raise ValueError('window shape')
        used, resets = window.get('used_percent'), window.get('resets_at')
        if used is not None and _number(used) is None:
            raise ValueError('used_percent')
        if resets is not None and not isinstance(resets, str):
            raise ValueError('resets_at')
        parsed.append({'label': window['label'], 'used_percent': _number(used), 'resets_at': resets})
    name = clean(provider, NAME_MAX)
    return {'key': name.lower(), 'name': name, 'label': clean(account, NAME_MAX) if account else '',
            'windows': parsed, 'error': reason(error) if error is not None else None, 'as_of': None}


def failed_reading(path, why):
    """One unavailable group named by the reading file's basename."""
    base = clean(os.path.basename(path), NAME_MAX) or 'reading'
    return [{'key': base.lower(), 'name': base, 'label': '', 'windows': [], 'error': why, 'as_of': None}]


def read_reading(path, now):
    """The groups of one reading file; a file that cannot be used is one group
    named by its basename with the reason, a stale one shows each group as
    unavailable."""

    def failed(why):
        return failed_reading(path, why)

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return failed('no reading')
    except OSError:
        return failed('unreadable reading')
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return failed('not a regular file')
        raw = _read_capped(fd, READING_MAX_BYTES)
    except OverflowError:
        return failed('reading too large')
    except OSError:
        return failed('unreadable reading')
    finally:
        os.close(fd)
    try:
        data = strict_json(raw)
        if not isinstance(data, dict) or set(data) != READING_KEYS or not isinstance(data['groups'], list) \
                or len(data['groups']) > MAX_GROUPS_PER_READING:
            raise ValueError('reading shape')
        observed = _parse_time(data['observed_at'])
        if observed is None:
            raise ValueError('observed_at')
        groups = [_reading_group(item) for item in data['groups']]
    except ValueError:
        return failed('malformed reading')
    age = (now - observed).total_seconds()
    if age > READING_MAX_AGE or age < -READING_MAX_AHEAD:
        for group in groups:
            group['error'] = 'stale reading'
    return groups


class Readings:
    """The reading files, read in a few threads from the moment it is made, so
    they overlap the provider reads; `groups` waits until `deadline` at most
    and shows a file not read by then as a timeout. A thread cannot be
    stopped, so a read stuck in the kernel keeps its thread: `main` exits
    without joining it."""

    def __init__(self, paths, now):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=READING_WORKERS) if paths else None
        self._futures = [(path, self._pool.submit(read_reading, path, now)) for path in paths]

    def groups(self, deadline, monotonic=time.monotonic):
        """Every group of the readings, in file order."""
        if self._pool is None:
            return []
        concurrent.futures.wait([future for _, future in self._futures], timeout=max(deadline - monotonic(), 0))
        groups = []
        for path, future in self._futures:
            if not future.done():
                groups.extend(failed_reading(path, 'timeout'))
            elif future.exception() is not None:
                groups.extend(failed_reading(path, 'unreadable reading'))
            else:
                groups.extend(future.result())
        return groups

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


# --- the table ----------------------------------------------------------

def _ascii(text):
    return ''.join(c for c in text if ' ' <= c <= '~')


def _strip(label):
    text = str(label)
    for suffix in LABEL_SUFFIXES:
        text = text.removesuffix(suffix)
    return text


def short_label(label):
    text = _strip(label)
    group, dash, span = text.rpartition(' - ')
    if dash and group and span:
        span = _ascii(SHORT_SPANS.get(span, span)).strip()[:LABEL_WIDTH].rstrip()
        room = LABEL_WIDTH - len(span) - 1
        group = _ascii(SHORT_GROUPS.get(group, (group.split() or [''])[0])).strip()[:max(room, 0)]
        return f'{group} {span}' if group else span
    return _ascii(SHORT_LABELS.get(text, text)).strip()[:LABEL_WIDTH].rstrip()


def row_labels(windows):
    """The label column of one group: the short labels padded to 8, or,
    when two short labels would be equal, the whole labels (32 characters
    at most) padded to the longest of them, so no two rows read the same."""
    labels = [short_label(window['label']) for window in windows]
    if len(set(labels)) < len(labels):
        labels = [_ascii(_strip(window['label'])).strip()[:WHOLE_LABEL_MAX] for window in windows]
    width = max([LABEL_WIDTH] + [len(label) for label in labels])
    return [label.ljust(width) for label in labels]


def stamp(moment, tz):
    local = moment.astimezone(tz)
    return f'{DAYS[local.weekday()]} {local:%H:%M}'


def time_left(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes}m'
    if minutes < 24 * 60:
        return f'{minutes // 60}h{minutes % 60:02d}m'
    return f'{minutes // (24 * 60)}d{minutes // 60 % 24}h'


def window_row(window, tz, now, label=None):
    label = short_label(window['label']).ljust(LABEL_WIDTH) if label is None else label
    resets = window['resets_at']
    if resets is None or resets.startswith('0001-01-01'):
        return f'{WHITE} {label}{DASH:>{PERCENT_WIDTH}}  no window'
    used = window['used_percent']
    percent = None if used is None else math.floor(used + 0.5)
    if percent is None:
        dot = WHITE
    elif percent >= 90:
        dot = RED
    elif percent >= 70:
        dot = YELLOW
    else:
        dot = GREEN
    shown = DASH if percent is None else f'{percent}%'
    moment = _parse_time(resets)
    if moment is None:
        return f'{dot} {label}{shown:>{PERCENT_WIDTH}}  unknown'
    left = (moment - now).total_seconds()
    return f'{dot} {label}{shown:>{PERCENT_WIDTH}}  {stamp(moment, tz)}  {time_left(left) if left > 0 else DASH}'


def group_lines(group, tz, now):
    header = group['name'] + (SEPARATOR + group['label'] if group['label'] else '')
    if group['as_of'] is not None:
        header += f' (as of {stamp(group["as_of"], tz)})'
    if group['error'] is not None:
        return [header, f'{WHITE} unavailable: {group["error"]}']
    if not group['windows']:
        return [header, f'{WHITE} unavailable: no windows']
    labels = row_labels(group['windows'])
    return [header] + [window_row(window, tz, now, label) for window, label in zip(group['windows'], labels)]


def _fits(lines):
    text = '\n'.join(lines)
    return utf16_units(text) <= TEXT_MAX_UNITS and utf16_units(f'<pre>{escape(text)}</pre>') <= HTML_SOURCE_MAX_UNITS


def fit(blocks):
    """The lines of as many leading groups as fit the message, a last line
    '…' when any were dropped. A first group too long alone is cut by line."""
    for keep in range(len(blocks), 0, -1):
        lines = [line for block in blocks[:keep] for line in block] + ([ELLIPSIS] if keep < len(blocks) else [])
        if _fits(lines):
            return lines
    first = blocks[0] if blocks else []
    for keep in range(len(first) - 1, 0, -1):
        if _fits(first[:keep] + [ELLIPSIS]):
            return first[:keep] + [ELLIPSIS]
    return [f'{WHITE} quota unavailable: reply too long']


def send(lines):
    return {'actions': [{'type': 'send', 'format': 'html', 'text': f'<pre>{escape(chr(10).join(lines))}</pre>'}]}


def unavailable(why=None):
    return send([f'{WHITE} quota unavailable' + (f': {reason(why)}' if why else '')])


# --- the run ------------------------------------------------------------

def provider_entries(config, environ, now, deadline, monotonic=time.monotonic):
    """One group per provider, in the order of `providers`: its live answer,
    else its cached one, else its error."""
    live = read_providers(config, environ, deadline, monotonic)
    cache_dir = config['cache_dir']
    cache = load_cache(cache_dir) if cache_dir and live else {}
    saved = {p: e for p, e in cache.items() if p in config['providers']}
    changed = False
    entries = []
    for provider in config['providers']:
        result = live[provider]
        label = config['labels'].get(provider, '')
        if 'error' not in result:
            group = {'name': result['name'], 'windows': result['windows'], 'error': None, 'as_of': None}
            if cache_dir:
                saved[provider] = {'at': now, 'name': result['name'], 'windows': result['windows']}
                changed = True
        else:
            cached = saved.get(provider)
            age = (now - cached['at']).total_seconds() if cached else None
            if cached and -READING_MAX_AHEAD <= age <= CACHE_MAX_AGE:
                group = {'name': cached['name'], 'windows': cached['windows'], 'error': None, 'as_of': cached['at']}
            else:
                group = {'name': DISPLAY_NAMES.get(provider, provider.capitalize()), 'windows': [],
                         'error': result['error'], 'as_of': None}
        entries.append(dict(group, key=provider, label=label))
    if changed:
        save_cache(cache_dir, saved)
    return entries


def collect(config, environ, now, deadline, monotonic=time.monotonic):
    """Every group of the reply, in order: the providers in the order of
    `providers`, then those only the readings name; within a provider the
    aiquokka group first, then the readings in file order. The readings are
    read while the providers are, and both end by `deadline`."""
    readings = Readings(config['readings'], now)
    try:
        entries = provider_entries(config, environ, now, deadline, monotonic)
        entries.extend(readings.groups(deadline, monotonic))
    finally:
        readings.close()

    order = list(config['providers'])
    for entry in entries:
        if entry['key'] not in order:
            order.append(entry['key'])
    # Python's sort is stable: aiquokka groups come first in `entries`, then
    # the readings in file order.
    return sorted(entries, key=lambda entry: order.index(entry['key']))


def parse_args(argv):
    if len(argv) != 2 or argv[0] != '--config':
        raise Unavailable('usage: quota_command.py --config <path>')
    return argv[1]


def run(argv, stdin_bytes, environ, now=None, budget=RUN_BUDGET_SECONDS, monotonic=time.monotonic):
    """The handler's answer as a dict; never raises."""
    start = monotonic()
    try:
        if stdin_bytes is None or len(stdin_bytes) > INPUT_MAX_BYTES:
            raise ValueError('input size')
        request = strict_json(stdin_bytes)
        if not isinstance(request, dict):
            raise ValueError('input shape')
    except ValueError:
        return unavailable('unreadable input')
    if request.get('kind') == 'callback':
        return {'actions': [{'type': 'answer_callback'}]}
    try:
        config = load_config(parse_args(argv))
        tz = zone(environ.get('TZ'))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        groups = collect(config, environ, now, start + budget, monotonic)
        if not groups:
            return unavailable('no sources configured')
        return send(fit([group_lines(group, tz, now) for group in groups]))
    except Unavailable as error:
        return unavailable(str(error))
    except Exception:
        return unavailable('internal error')


def main():
    try:
        stdin_bytes = sys.stdin.buffer.read(INPUT_MAX_BYTES + 1)
    except Exception:
        stdin_bytes = None
    try:
        answer = run(sys.argv[1:], stdin_bytes, dict(os.environ))
    except BaseException:
        answer = unavailable('internal error')
    sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.flush()
    # Every child is dead by now; exit without joining the reader threads.
    os._exit(0)


if __name__ == '__main__':
    main()
