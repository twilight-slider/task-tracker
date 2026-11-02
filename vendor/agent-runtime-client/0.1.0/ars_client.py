"""Agent Runtime client source candidate 0.1.0. Standard library; no Docker access."""
import ctypes
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
import threading
import uuid

__version__ = '0.1.0'
MAX_REQUEST, MAX_RESPONSE = 16384, 262144
ROUTE = re.compile(r'^/v1/(?:health|readiness|images/deploy|(?:jobs|builds)(?:/[a-f0-9]{32}(?:/(?:result|stop|events|logs|commands|interrupt)(?:\?after_seq=\d{1,12})?)?)?)$')


class ArsError(Exception):
    def __init__(self, code, phase='client', request_id=None, retryable=False, status=None):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) else 'ARS_REQUEST_FAILED'
        self.phase, self.request_id, self.retryable, self.status = phase, request_id, bool(retryable), status
        super().__init__(self.code)


def powershell(script, values, timeout=15):
    """Fixed trusted script through stdin; no credential values in argv or output."""
    import base64
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    try:
        result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            input=json.dumps(values), capture_output=True, text=True, timeout=timeout,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        if result.returncode or len(result.stdout.encode()) > 16384:
            raise ArsError('WINDOWS_CLIENT_UNAVAILABLE')
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ArsError('WINDOWS_CLIENT_UNAVAILABLE') from None


def windows_token(path, helper):
    if os.name != 'nt':
        raise ArsError('WINDOWS_REQUIRED')
    path, helper = Path(path).absolute(), Path(helper).absolute()
    script = """$ErrorActionPreference='Stop'
$c=[Console]::In.ReadToEnd()|ConvertFrom-Json
. $c.helper
Assert-ArsCredentialRoot ([IO.Path]::GetDirectoryName($c.path))
if ((Get-Item -LiteralPath $c.path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'LINK'}
"""
    powershell(script, {'path': str(path), 'helper': str(helper)})
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    try:
        raw = path.read_bytes()
        if not 1 <= len(raw) <= 65536:
            raise ArsError('INVALID_CREDENTIAL')
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        incoming, outgoing = Blob(len(raw), buffer), Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        if not crypt.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
            raise ArsError('CREDENTIAL_UNAVAILABLE')
        try:
            return ctypes.string_at(outgoing.data, outgoing.size).decode('ascii')
        finally:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))
    except (OSError, UnicodeError):
        raise ArsError('CREDENTIAL_UNAVAILABLE') from None


def private_token(path):
    path = Path(path)
    try:
        if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ArsError('INVALID_CREDENTIAL')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o077 or info.st_size != 64:
            raise ArsError('INVALID_CREDENTIAL')
        return path.read_text(encoding='ascii')
    except (OSError, UnicodeError):
        raise ArsError('CREDENTIAL_UNAVAILABLE') from None


class Client:
    def __init__(self, *, token=None, credential_path=None, powershell_helper=None, token_file=None,
                 host='127.0.0.1', port=48758, timeout=20, startup=None, expected_version='0.3.0'):
        if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ArsError('INVALID_CLIENT_CONFIG')
        try:
            address = ipaddress.IPv4Address(host)
        except (ValueError, TypeError):
            raise ArsError('INVALID_CLIENT_CONFIG') from None
        if host != '127.0.0.1' and (token_file is None or not address.is_private or address.is_unspecified or address.is_multicast):
            raise ArsError('INVALID_CLIENT_CONFIG')
        if startup is not None and (not isinstance(startup, dict) or set(startup) != {'task_name', 'agent_sid'} or
                not isinstance(startup['task_name'], str) or not re.fullmatch(r'AgentRuntime-Restart-[A-Za-z0-9_-]+', startup['task_name']) or
                not isinstance(startup['agent_sid'], str) or not re.fullmatch(r'S-1-5-21-[0-9-]+', startup['agent_sid']) or host != '127.0.0.1'):
            raise ArsError('INVALID_STARTUP_CONFIG')
        if sum(value is not None for value in (token, credential_path, token_file)) != 1:
            raise ArsError('INVALID_CREDENTIAL')
        if credential_path is not None:
            if powershell_helper is None:
                raise ArsError('POWERSHELL_HELPER_REQUIRED')
            token = windows_token(credential_path, powershell_helper)
        elif token_file is not None:
            token = private_token(token_file)
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
            raise ArsError('INVALID_CREDENTIAL')
        if not isinstance(expected_version, str) or not re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?', expected_version):
            raise ArsError('INVALID_CLIENT_CONFIG')
        self.expected_version = expected_version
        self.token, self.host, self.port = token, host, port
        self.timeout, self.startup = timeout, startup

    def decode(self, status, raw, content_type, health=False):
        try:
            if len(raw) > MAX_RESPONSE:
                raise ArsError('CLIENT_RESPONSE_TOO_LARGE')
            if content_type == 'application/x-ndjson' and status < 400:
                result = [json.loads(line) for line in raw.splitlines() if line]
                if len(result) > 256 or any(not isinstance(row, dict) for row in result):
                    raise ArsError('INVALID_RESPONSE')
                previous = None
                for row in result:
                    seq = row.get('seq')
                    if type(seq) is not int or (previous is not None and seq <= previous):
                        raise ArsError('INVALID_RESPONSE')
                    previous = seq
            elif content_type == 'application/json':
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ArsError('INVALID_RESPONSE')
            else:
                raise ArsError('INVALID_RESPONSE')
            records = result if isinstance(result, list) else [result]
            if not (health and status < 400):
                for record in records:
                    if record.get('api_version') != '1':
                        raise ArsError('UNSUPPORTED_VERSION')
                    correlation = record.get('stream_request_id') if isinstance(result, list) else record.get('request_id')
                    try:
                        uuid.UUID(correlation)
                    except (ValueError, TypeError, AttributeError):
                        raise ArsError('INVALID_RESPONSE') from None
            if status >= 400:
                correlation = result.get('request_id')
                try:
                    uuid.UUID(correlation)
                except (ValueError, TypeError, AttributeError):
                    correlation = None
                raise ArsError(result.get('code', result.get('error')), request_id=correlation,
                    phase='server', retryable=result.get('retryable') is True, status=status)
            return result
        except (ValueError, UnicodeError):
            raise ArsError('INVALID_RESPONSE') from None

    def exchange(self, method, path, body):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        def abort():
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        timer = threading.Timer(self.timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.request(method, path, body, {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            return self.decode(response.status, raw, response.getheader('Content-Type', '').split(';')[0], health=path == '/v1/health')
        except (OSError, http.client.HTTPException):
            # A failed POST can already have executed. Caller retains and reconciles its saved key.
            raise ArsError('ARS_UNAVAILABLE', retryable=True) from None
        finally:
            timer.cancel()
            connection.close()

    def request(self, method, path, payload=None, *, key=None, request_id=None):
        if method not in ('GET', 'POST') or not isinstance(path, str) or not ROUTE.fullmatch(path):
            raise ArsError('UNSUPPORTED_OPERATION')
        raw = None
        if method == 'POST':
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
                raise ArsError('IDEMPOTENCY_KEY_REQUIRED')
            request_id = request_id or str(uuid.uuid4())
            try:
                uuid.UUID(request_id)
                raw = json.dumps({'api_version': '1', 'request_id': request_id, 'idempotency_key': key, 'payload': payload}, ensure_ascii=False).encode()
            except (ValueError, TypeError, AttributeError):
                raise ArsError('INVALID_REQUEST') from None
            if len(raw) > MAX_REQUEST:
                raise ArsError('CLIENT_REQUEST_TOO_LARGE')
        result = self.exchange(method, path, raw)
        if method == 'POST' and (not isinstance(result, dict) or result.get('request_id') != request_id):
            raise ArsError('CLIENT_CORRELATION_MISMATCH')
        return result

    def create(self, launch, key, request_id=None):
        return self.request('POST', '/v1/jobs', launch, key=key, request_id=request_id)

    def command(self, job_id, method, params, key, request_id=None):
        return self.request('POST', '/v1/jobs/' + job_id + '/commands',
            {'schema_version': 1, 'id': key, 'method': method, 'params': params}, key=key, request_id=request_id)

    def status(self, job_id):
        return self.request('GET', '/v1/jobs/' + job_id)

    def events(self, job_id, after_seq=0):
        if type(after_seq) is not int or not 0 <= after_seq <= 999999999999:
            raise ArsError('INVALID_REQUEST')
        return self.request('GET', '/v1/jobs/' + job_id + '/events?after_seq=' + str(after_seq))

    def stop(self, job_id, key, request_id=None):
        return self.request('POST', '/v1/jobs/' + job_id + '/stop', {}, key=key, request_id=request_id)

    def startup_status(self, trigger=False):
        if self.startup is None or os.name != 'nt':
            raise ArsError('STARTUP_NOT_CONFIGURED')
        script = r"""$ErrorActionPreference='Stop'
$c=[Console]::In.ReadToEnd()|ConvertFrom-Json
$s=New-Object -ComObject Schedule.Service;$s.Connect();$t=$s.GetFolder('\').GetTask($c.task_name)
$p=$t.Definition.Principal.UserId
$sid=if($p -match '^S-1-'){$p}else{([Security.Principal.NTAccount]::new($p)).Translate([Security.Principal.SecurityIdentifier]).Value}
if($sid -ne $c.agent_sid){@{code='IDENTITY_MISMATCH'}|ConvertTo-Json -Compress;exit 0}
if($c.trigger){$null=$t.Run($null)}
@{state=[int]$t.State;last_result=[int]$t.LastTaskResult;agent_sid=$sid}|ConvertTo-Json -Compress
"""
        result = powershell(script, {**self.startup, 'trigger': trigger})
        if isinstance(result, dict) and result.get('code') == 'IDENTITY_MISMATCH':
            raise ArsError('IDENTITY_MISMATCH', phase='startup')
        return result

    def ensure_available(self, seconds=60):
        if type(seconds) is not int or not 1 <= seconds <= 120:
            raise ArsError('INVALID_CLIENT_CONFIG')
        try:
            health = self.request('GET', '/v1/health')
            if health.get('alive') is True:
                if health.get('version') != self.expected_version:
                    raise ArsError('ARS_VERSION_MISMATCH', phase='startup')
                return health
        except ArsError as error:
            if error.code != 'ARS_UNAVAILABLE':
                raise
        self.startup_status(trigger=True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                health = self.request('GET', '/v1/health')
                if health.get('alive') is True:
                    if health.get('version') != self.expected_version:
                        raise ArsError('ARS_VERSION_MISMATCH', phase='startup')
                    return health
            except ArsError as error:
                if error.code != 'ARS_UNAVAILABLE':
                    raise
            time.sleep(min(.25, max(0, deadline - time.monotonic())))
        raise ArsError('STARTUP_TIMEOUT', phase='startup', retryable=True)
