"""Private database configuration for the local pump Reviewer."""
import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re

from nilaris.drive155_private import read_private_json


@dataclass(frozen=True)
class PumpReviewConfig:
    database: dict = field(repr=False)
    monitor_path: Path

    @classmethod
    def load(cls, path):
        try:
            data = read_private_json(path, Path(__file__).resolve().parents[2])
            db = data['database']
            monitor_path = Path(data['monitor_path'])
            if (set(data) != {'database', 'monitor_path'} or not monitor_path.is_absolute()
                    or set(db) != {'host', 'port', 'user', 'dbname', 'password'}
                    or db['host'] != '127.0.0.1' or type(db['port']) is not int
                    or not 1 <= db['port'] <= 65535
                    or not all(re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]{0,62}', db[k])
                               for k in ('user', 'dbname'))
                    or not isinstance(db['password'], str) or not db['password']):
                raise ValueError
            return cls(dict(db, hostaddr='127.0.0.1', connect_timeout=3), monitor_path)
        except (ValueError, KeyError, OSError, RuntimeError, TypeError):
            pass
        raise ValueError('A private loopback review configuration is required.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Print the one local database port for the Reviewer sandbox.')
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    try:
        print(PumpReviewConfig.load(args.config).database['port'])
    except ValueError as error:
        parser.exit(2, str(error) + '\n')
