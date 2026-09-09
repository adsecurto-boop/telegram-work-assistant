"""Run one read-only connector sync into the review inbox."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from connectors import configured_connector, sync_connector
from database import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('connector', choices=('freshdesk','freshchat','csv'))
    parser.add_argument('--csv-path')
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    result = sync_connector(Database(config.DB_PATH),
                            configured_connector(args.connector, args.csv_path), args.limit)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
