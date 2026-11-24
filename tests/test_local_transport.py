"""Controlled transport proof uses a real receiver and a fresh reader."""
import time

from nilaris.local_transport import LocalReceiver, LocalTransport


CUSTOMER = 'a' * 32
OPERATION = '1' * 32


def wait_for_deliveries(receiver, count):
    deadline = time.monotonic() + 2
    while len(receiver.deliveries) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(receiver.deliveries) >= count


def test_original_operation_has_a_durable_receiver_receipt(tmp_path):
    with LocalReceiver(tmp_path, CUSTOMER) as receiver:
        transport = LocalTransport(tmp_path, CUSTOMER)
        assert transport.lookup(OPERATION) is None
        result = transport.start(OPERATION, b'abc').wait()
        assert result == {
            'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
            'bytes': 3,
        }
        assert receiver.deliveries == (OPERATION,)
    assert LocalTransport(tmp_path, CUSTOMER).lookup(OPERATION) == result


def serve_until_stopped(directory, ready):
    from threading import Event

    with LocalReceiver(directory, CUSTOMER):
        ready.set()
        Event().wait(10)


def test_crashed_receiver_can_reopen_with_the_same_durable_readback(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(target=serve_until_stopped, args=(tmp_path, ready))
    process.start()
    try:
        assert ready.wait(5)
        transport = LocalTransport(tmp_path, CUSTOMER)
        receipt = transport.start(OPERATION, b'abc').wait()
    finally:
        process.terminate()
        process.join(3)
        assert not process.is_alive()
    with LocalReceiver(tmp_path, CUSTOMER) as restarted:
        assert transport.lookup(OPERATION) == receipt
        assert restarted.deliveries == ()


def test_same_key_in_two_customers_and_lost_response_reconcile_independently(tmp_path):
    other_customer = 'b' * 32
    with (LocalReceiver(tmp_path, CUSTOMER) as first,
          LocalReceiver(tmp_path, other_customer) as second):
        original = LocalTransport(tmp_path, CUSTOMER)
        other = LocalTransport(tmp_path, other_customer)
        # A sender can lose the response object without losing the effect.
        original.start(OPERATION, b'abc')
        other_receipt = other.start(OPERATION, b'12345').wait()
        wait_for_deliveries(first, 1)
    assert original.lookup(OPERATION)['bytes'] == 3
    assert other.lookup(OPERATION) == other_receipt
    assert other_receipt['bytes'] == 5
    assert first.deliveries == second.deliveries == (OPERATION,)


def test_conflicting_body_cannot_replace_the_original_receipt(tmp_path):
    import pytest

    with LocalReceiver(tmp_path, CUSTOMER) as receiver:
        transport = LocalTransport(tmp_path, CUSTOMER)
        receipt = transport.start(OPERATION, b'abc').wait()
        with pytest.raises(ValueError, match='original operation payload differs'):
            transport.start(OPERATION, b'changed').wait()
        wait_for_deliveries(receiver, 2)
    assert transport.lookup(OPERATION) == receipt
    assert receiver.deliveries == (OPERATION, OPERATION)


def test_inactive_receiver_and_foreign_operation_reference_refuse_without_effect(tmp_path):
    import pytest

    transport = LocalTransport(tmp_path, CUSTOMER)
    with pytest.raises(OSError):
        transport.start(OPERATION, b'abc')
    with LocalReceiver(tmp_path, CUSTOMER) as receiver:
        with pytest.raises(BlockingIOError):
            with LocalReceiver(tmp_path, CUSTOMER):
                raise AssertionError('second receiver acquired the original endpoint')
        with pytest.raises(ValueError):
            transport.start('../elsewhere', b'abc')
        with pytest.raises(ValueError):
            LocalTransport(tmp_path, '../elsewhere')
        with pytest.raises(ValueError):
            transport.start(OPERATION, b'x' * 4097)
    assert receiver.deliveries == ()
    assert transport.lookup(OPERATION) is None


def test_shutdown_closes_the_endpoint_while_an_active_sender_keeps_it_readable(tmp_path):
    import errno
    import socket
    import threading

    transport = LocalTransport(tmp_path, CUSTOMER)
    ready = threading.Event()
    closed = threading.Event()
    stop = threading.Event()

    def keep_sending():
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            packet = OPERATION.encode() + b'abc'
            while not stop.is_set():
                try:
                    sender.sendto(packet, str(transport.endpoint))
                except OSError as error:
                    if error.errno in (errno.ENOENT, errno.ECONNREFUSED):
                        closed.set()
                        return
                    if error.errno not in (errno.EAGAIN, errno.ENOBUFS):
                        raise
                else:
                    ready.set()

    sender = threading.Thread(target=keep_sending)
    try:
        with LocalReceiver(tmp_path, CUSTOMER) as receiver:
            sender.start()
            assert ready.wait(2)
            wait_for_deliveries(receiver, 2)
        assert closed.wait(1)
    finally:
        stop.set()
        sender.join(2)
        assert not sender.is_alive()


def test_renamed_receipt_needs_durable_directory_before_confirming(tmp_path, monkeypatch):
    import os
    import stat
    import pytest

    fsync = os.fsync

    def lose_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory sync unavailable')
        fsync(fd)

    transport = LocalTransport(tmp_path, CUSTOMER)
    receiver = LocalReceiver(tmp_path, CUSTOMER)
    with monkeypatch.context() as fault:
        with pytest.raises(RuntimeError, match='receiver failed'):
            with receiver:
                fault.setattr(os, 'fsync', lose_directory_sync)
                transport.start(OPERATION, b'abc')
                receiver.wait(2)
        assert receiver.deliveries == (OPERATION,)
        with pytest.raises(OSError, match='directory sync unavailable'):
            transport.lookup(OPERATION)
    assert transport.lookup(OPERATION)['bytes'] == 3


def test_receiver_wait_reports_failed_publication_and_releases_endpoint(tmp_path, monkeypatch):
    import os
    import stat
    import pytest

    transport = LocalTransport(tmp_path, CUSTOMER)
    receiver = LocalReceiver(tmp_path, CUSTOMER)
    fsync = os.fsync

    def lose_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('receiver write failed')
        fsync(fd)

    monkeypatch.setattr(os, 'fsync', lose_file_sync)
    with pytest.raises(RuntimeError, match='receiver failed'):
        with receiver:
            transport.start(OPERATION, b'abc')
            receiver.wait(2)
            pytest.fail('receiver failure did not interrupt the wait')
    assert receiver.deliveries == (OPERATION,)
    assert not transport.endpoint.exists()
    assert transport.lookup(OPERATION) is None


def test_receiver_requires_durable_receipt_directory_even_after_partial_start(tmp_path, monkeypatch):
    import os
    import pytest

    transport = LocalTransport(tmp_path, CUSTOMER)
    fsync = os.fsync
    project = tmp_path.stat()

    def lose_parent_sync(fd):
        if os.path.samestat(os.fstat(fd), project):
            assert not transport.endpoint.exists()
            raise OSError('cannot persist receipt directory')
        fsync(fd)

    with monkeypatch.context() as fault:
        fault.setattr(os, 'fsync', lose_parent_sync)
        for _ in range(2):
            with pytest.raises(OSError, match='cannot persist receipt directory'):
                with LocalReceiver(tmp_path, CUSTOMER):
                    pytest.fail('receiver became ready before its receipt directory was durable')
            assert transport.receipts.is_dir()
            assert not transport.endpoint.exists()
    with LocalReceiver(tmp_path, CUSTOMER) as restarted:
        assert transport.start(OPERATION, b'abc').wait()['bytes'] == 3
    assert restarted.deliveries == (OPERATION,)
