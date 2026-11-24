import json

import pytest

from nilaris.rfq_runtime import PumpReviewConfig


def test_review_config_only_permits_the_private_loopback_database(tmp_path):
    path = tmp_path / 'review.json'
    monitor = tmp_path / 'service-monitor.json'
    database = dict(host='127.0.0.1', port=55627, user='nilaris_app', dbname='demo', password='dummy-reviewer-password')
    for db, mode, valid in [
        (database, 0o600, True),
        (database, 0o644, False),
        (dict(database, host='example.invalid'), 0o600, False),
        (dict(database, hostaddr='192.0.2.1'), 0o600, False),
        (dict(database, port=True), 0o600, False),
        (dict(database, dbname='host=example.invalid'), 0o600, False),
    ]:
        path.write_text(json.dumps(dict(database=db, monitor_path=str(monitor))))
        path.chmod(mode)
        if valid:
            config = PumpReviewConfig.load(path)
            assert config.database['host'] == '127.0.0.1' and config.database['port'] == 55627
            assert config.monitor_path == monitor
            assert database['password'] not in repr(config)
        else:
            with pytest.raises(ValueError, match='private loopback review configuration'):
                PumpReviewConfig.load(path)
