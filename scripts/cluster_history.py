"""
CLI tool for clustering review inbox candidates into suggested cases.
Usage:
    python scripts/cluster_history.py --dry-run
    python scripts/cluster_history.py --import-id 2 --dry-run
    python scripts/cluster_history.py --apply-suggestions
"""
import argparse
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from database import Database
from clustering import HistoricalClusterEngine


def main():
    parser = argparse.ArgumentParser(description='Historical message clustering tool.')
    parser.add_argument('--dry-run', action='store_true', help='Preview cluster suggestions without saving.')
    parser.add_argument('--apply-suggestions', action='store_true', help='Persist cluster suggestions as pending.')
    parser.add_argument('--import-id', type=int, default=None, help='Filter candidates by import batch ID.')
    parser.add_argument('--start-date', type=str, default=None, help='Filter by start date (YYYY-MM-DD).')
    parser.add_argument('--end-date', type=str, default=None, help='Filter by end date (YYYY-MM-DD).')
    args = parser.parse_args()

    if not args.dry_run and not args.apply_suggestions:
        print("Specify either --dry-run or --apply-suggestions.")
        sys.exit(1)

    db = Database(config.DB_PATH)
    engine = HistoricalClusterEngine(db)

    print(f"Running clustering on database: {config.DB_PATH} ...")
    clusters, metrics = engine.cluster(
        import_id=args.import_id,
        start_date=args.start_date,
        end_date=args.end_date
    )

    print("\n" + "=" * 50)
    print("HISTORICAL MESSAGE CLUSTERING REPORT")
    print("=" * 50)
    print(f"Candidate messages processed : {metrics['candidate_messages_processed']}")
    print(f"Suggested clusters           : {metrics['suggested_clusters']}")
    print(f"Standalone items             : {metrics['standalone_items']}")
    print(f"High confidence (>= 0.8)     : {metrics['high_confidence_count']}")
    print(f"Medium confidence (0.5-0.79) : {metrics['medium_confidence_count']}")
    print(f"Low confidence (< 0.5)       : {metrics['low_confidence_count']}")
    print(f"Existing-case matches        : {metrics['existing_case_matches']}")
    print(f"Possible duplicate messages  : {metrics['possible_duplicates']}")
    print(f"AI calls / failures          : {metrics['ai_calls']} / {metrics['ai_failures']}")
    print("=" * 50)

    if clusters:
        print("\nTop 5 Cluster Previews:")
        for i, c in enumerate(clusters[:5], 1):
            print(f"\n{i}. [{c.confidence:.2f}] {c.title}")
            print(f"   Reason  : {c.reason}")
            print(f"   Client  : {c.suggested_client or 'Unknown'}")
            print(f"   Product : {c.suggested_product or 'General'}")
            print(f"   Messages: {len(c.messages)} item(s)")
            for m in c.messages[:2]:
                text_preview = (m.get('text') or '').strip().replace('\n', ' ')[:65]
                print(f"     • #{m['id']} [{m.get('occurred_at')[:16]}]: {text_preview}")

    if args.apply_suggestions:
        res = engine.apply_suggestions(clusters)
        saved, already = res if isinstance(res, tuple) else (res, 0)
        print(f"\nSuccessfully saved {saved} new cluster suggestion(s) ({already} already existed with pending status).")
        print("Review and manage them in the localhost dashboard or via Telegram.")
    else:
        print("\nDry-run complete. No changes written to database.")


if __name__ == '__main__':
    main()
