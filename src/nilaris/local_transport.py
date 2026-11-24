"""Controlled, same-host transport for bounded synthetic items.

D is the kernel accepting a complete datagram on the receiver's socket. Only
the receiver publishes the durable observation. Waiting and readback never send.
"""
from collections import deque
from contextlib import ExitStack
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time


MAX_BYTES = 4096

# Only definite rejections from sendto on this local, atomic datagram.
# Darwin send(2) documents would-block, insufficient buffer/memory and
# impossible atomic size: none of these puts a packet in the receive queue.
_DATAGRAM_REFUSALS = frozenset((
    errno.ENOENT, errno.ENOTDIR, errno.ECONNREFUSED,
    errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS, errno.ENOMEM, errno.EMSGSIZE,
))


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('original scoped identity required')
    return value


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('private transport directory required')
    return info


class HandoffRefused(OSError):
    """The local kernel could not deliver the datagram to a receiver."""


class LocalTransport:
    def __init__(self, directory, customer):
        self.directory = Path(directory)
        self.customer = _identity(customer)
        self.receipts = self.directory / (self.customer + ".local-receipts")
        info = _private_directory(self.directory)
        # Unix socket names have a small platform limit independent of the
        # length of the provisioner's directory. Bind the name to its identity.
        scope = f'{os.getuid()}/{info.st_dev}/{info.st_ino}/{customer}'.encode()
        self.endpoint = Path('/tmp') / ('nilaris-' + sha256(scope).hexdigest()[:40]) / 'receive'

    def _receipt(self, operation_id):
        return self.receipts / (_identity(operation_id) + '.json')

    def lookup(self, operation_id):
        path = self._receipt(operation_id)
        with ExitStack() as handles:
            try:
                directory = os.open(self.receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            handles.callback(os.close, directory)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('private receipt directory required')
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            file = handles.enter_context(os.fdopen(fd, 'rb'))
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 512):
                raise ValueError('invalid private receipt')
            receipt = json.loads(file.read(513))
            # Rename is visible before the publisher's directory fsync. Finish
            # that durability boundary ourselves before reporting a known result.
            os.fsync(directory)
        if (not isinstance(receipt, dict) or receipt.get('customer') != self.customer or receipt.get('operation_id') != operation_id
                or not re.fullmatch('[0-9a-f]{64}', receipt.get('sha256', ''))
                or type(receipt.get('bytes')) is not int or not 0 < receipt['bytes'] <= MAX_BYTES):
            raise ValueError('invalid transport receipt')
        return {'sha256': receipt['sha256'], 'bytes': receipt['bytes']}

    def start(self, operation_id, payload):
        header = _identity(operation_id).encode('ascii')
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BYTES:
            raise ValueError('bounded synthetic bytes required')
        packet = header + payload
        with ExitStack() as handles:
            try:
                _private_directory(self.endpoint.parent)
                info = self.endpoint.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise PermissionError('original customer socket required')
                sender = handles.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
                sender.setblocking(False)
            except OSError as error:
                raise HandoffRefused('local receiver unavailable before handoff') from error
            try:
                sent = sender.sendto(packet, str(self.endpoint))
            except OSError as error:
                if error.errno in _DATAGRAM_REFUSALS:
                    raise HandoffRefused('local receiver refused datagram') from error
                raise
            if sent != len(packet):
                raise OSError('incomplete local handoff')
        return _Response(self, operation_id, {'sha256': sha256(payload).hexdigest(), 'bytes': len(payload)})


class _Response:
    def __init__(self, transport, operation_id, expected):
        self.transport = transport
        self.operation_id = operation_id
        self.expected = expected

    def wait(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            receipt = self.transport.lookup(self.operation_id)
            if receipt is not None:
                if receipt != self.expected:
                    raise ValueError('original operation payload differs')
                return receipt
            time.sleep(0.01)
        raise TimeoutError('local response unknown; inspect original operation')


class LocalReceiver:
    """One disposable receiver. Recent deliveries include duplicate handoffs."""
    def __init__(self, directory, customer):
        self.transport = LocalTransport(directory, customer)
        self._deliveries = deque(maxlen=1024)
        self._stop = threading.Event()
        self._failed = threading.Event()

    @property
    def deliveries(self):
        return tuple(self._deliveries)

    def __enter__(self):
        endpoint = self.transport.endpoint
        receipts = self.transport.receipts
        with ExitStack() as ownership:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(receipts.parent, flags)
            try:
                try:
                    os.mkdir(receipts.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                directory = os.open(receipts.name, flags, dir_fd=parent)
                ownership.callback(os.close, directory)
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PermissionError('private receipt directory required')
                # A prior startup may have died after mkdir, before this sync.
                os.fsync(parent)
            finally:
                os.close(parent)
            # Death releases ownership even if the old socket name remains.
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            endpoint.parent.mkdir(mode=0o700, exist_ok=True)
            _private_directory(endpoint.parent)
            try:
                stale = endpoint.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(stale.st_mode) or stale.st_uid != os.getuid():
                    raise PermissionError('unexpected object at customer endpoint')
                endpoint.unlink()
            ownership.callback(endpoint.parent.rmdir)
            self._socket = ownership.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
            self._socket.bind(str(endpoint))
            ownership.callback(endpoint.unlink)
            self._socket.settimeout(0.05)
            self._thread = threading.Thread(target=self._run)
            self._thread.start()
            self._ownership = ownership.pop_all()
        return self

    def wait(self, seconds):
        """Interrupt the monitor interval as soon as receive or publication fails."""
        if self._failed.wait(seconds):
            raise RuntimeError('local receiver failed; inspect original operation')

    def _run(self):
        try:
            self._receive()
        except Exception:
            # Propagate failure to the owning command without exposing payloads.
            self._failed.set()

    def _receive(self):
        while not self._stop.is_set():
            try:
                packet = self._socket.recv(33 + MAX_BYTES)
            except TimeoutError:
                continue
            if not 32 < len(packet) <= 32 + MAX_BYTES or not re.fullmatch(b'[0-9a-f]{32}', packet[:32]):
                continue
            operation_id = packet[:32].decode('ascii')
            self._deliveries.append(operation_id)
            if self.transport.lookup(operation_id) is None:
                self._publish(operation_id, packet[32:])

    def _publish(self, operation_id, payload):
        path = self.transport._receipt(operation_id)
        receipt = dict(customer=self.transport.customer, operation_id=operation_id,
                       sha256=sha256(payload).hexdigest(), bytes=len(payload))
        # The exclusive endpoint admits one receiver/writer per customer.
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(receipt, file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(3)
        if self._thread.is_alive():
            raise RuntimeError('local receiver shutdown unconfirmed')
        self._ownership.close()
        if exc[0] is None:
            self.wait(0)
