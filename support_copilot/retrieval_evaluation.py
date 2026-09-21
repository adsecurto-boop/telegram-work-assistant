import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .database import create_db_engine, create_session_factory
from .knowledge_service import KnowledgeService
from .config import Settings
from .migration_runner import MigrationRunner
from .models import KnowledgeArticle


@dataclass
class EvaluationReport:
    total_cases: int
    recall_at_1: float
    recall_at_3: float
    mean_reciprocal_rank: float
    failed_case_ids: List[str]
    details: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_retrieval(dataset_path: str, db: Session) -> EvaluationReport:
    if not os.path.exists(dataset_path):
        raise ValueError(f"Evaluation dataset file not found: '{dataset_path}'")

    with open(dataset_path, "r", encoding="utf-8") as f:
        try:
            cases = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse evaluation dataset JSON: {exc}") from exc

    if not isinstance(cases, list) or len(cases) == 0:
        raise ValueError("Evaluation dataset is empty. At least one case is required.")

    # Cache article stable keys
    articles = db.query(KnowledgeArticle).all()
    article_key_map = {a.id: a.stable_key for a in articles}

    total = len(cases)
    hits_at_1 = 0
    hits_at_3 = 0
    reciprocal_ranks: List[float] = []
    failed_ids: List[str] = []
    details: List[Dict[str, Any]] = []

    seen_case_ids = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Every evaluation case must be a JSON object.")
        case_id = case.get("case_id", "unknown")
        query = case.get("query", "")
        product_scope = case.get("product_scope")
        issue_type = case.get("issue_type")
        expected_key = case.get("expected_stable_key", "")
        negative_keys = set(case.get("negative_keys", []))
        if not all(isinstance(item, str) and item.strip() for item in (case_id, query, expected_key)):
            raise ValueError("Each evaluation case requires non-empty case_id, query, and expected_stable_key values.")
        if case_id in seen_case_ids:
            raise ValueError(f"Duplicate evaluation case_id: '{case_id}'.")
        seen_case_ids.add(case_id)

        results = KnowledgeService.search_approved_knowledge(
            db=db,
            query_text=query,
            product_scope=product_scope,
            issue_type=issue_type,
            limit=10,
        )

        retrieved_keys = [article_key_map.get(r.article_id, "") for r in results]

        rank: Optional[int] = None
        for idx, key in enumerate(retrieved_keys):
            if key == expected_key:
                rank = idx + 1
                break

        rr = 1.0 / rank if rank is not None else 0.0
        reciprocal_ranks.append(rr)

        hit_1 = rank == 1
        hit_3 = rank is not None and rank <= 3

        if hit_1:
            hits_at_1 += 1
        if hit_3:
            hits_at_3 += 1
        # Check negative expectations
        negative_hits = [k for k in retrieved_keys[:3] if k in negative_keys]
        if not hit_3 or negative_hits:
            failed_ids.append(case_id)

        details.append(
            {
                "case_id": case_id,
                "expected_key": expected_key,
                "retrieved_keys": retrieved_keys[:5],
                "rank": rank,
                "reciprocal_rank": round(rr, 4),
                "hit_at_1": hit_1,
                "hit_at_3": hit_3,
                "negative_hits": negative_hits,
            }
        )

    report = EvaluationReport(
        total_cases=total,
        recall_at_1=round(hits_at_1 / total, 4),
        recall_at_3=round(hits_at_3 / total, 4),
        mean_reciprocal_rank=round(sum(reciprocal_ranks) / total, 4),
        failed_case_ids=failed_ids,
        details=details,
    )
    return report


def evaluate_synthetic_fixture(dataset_path: str, corpus_path: str) -> EvaluationReport:
    if not os.path.exists(corpus_path):
        raise ValueError(f"Synthetic corpus file not found: '{corpus_path}'")
    with open(corpus_path, "r", encoding="utf-8") as corpus_file:
        try:
            corpus = json.load(corpus_file)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse synthetic corpus JSON: {exc}") from exc
    if not isinstance(corpus, list) or not corpus:
        raise ValueError("Synthetic retrieval corpus is empty.")

    with tempfile.TemporaryDirectory(prefix="support-copilot-retrieval-eval-") as temp_dir:
        database_file = os.path.join(temp_dir, "evaluation.sqlite3")
        settings = Settings(data_dir=temp_dir, db_path=f"sqlite:///{database_file}")
        MigrationRunner(settings).run_upgrade("head")
        engine = create_db_engine(settings.db_path)
        session_factory = create_session_factory(engine)
        db = session_factory()
        try:
            for entry in corpus:
                required = ("stable_key", "title", "product_scope", "issue_type", "content")
                if not isinstance(entry, dict) or any(not str(entry.get(field, "")).strip() for field in required):
                    raise ValueError("Each synthetic corpus article requires stable_key, title, product_scope, issue_type, and content.")
                article = KnowledgeService.create_article(
                    db=db,
                    stable_key=entry["stable_key"],
                    title=entry["title"],
                    product_scope=entry["product_scope"],
                    issue_type=entry["issue_type"],
                    client_scope=entry.get("client_scope"),
                    initial_content=entry["content"],
                    created_by="retrieval-evaluation-fixture",
                    required_facts=entry.get("required_facts", []),
                    prohibited_claims=entry.get("prohibited_claims", []),
                )
                KnowledgeService.approve_version(db, article.id, 1, "retrieval-evaluation-fixture")
            return evaluate_retrieval(dataset_path, db)
        finally:
            db.close()
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Support Copilot Knowledge Retrieval Evaluator")
    parser.add_argument("--dataset", required=True, help="Path to retrieval evaluation JSON dataset")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", help="Existing database path or connection URL (sqlite:///...)")
    source.add_argument(
        "--fixture-corpus",
        help="Synthetic knowledge corpus used to build and evaluate a temporary migrated database",
    )
    parser.add_argument("--threshold", type=float, default=0.7, help="Minimum Recall@3 quality threshold (default: 0.7)")
    args = parser.parse_args()

    try:
        if args.fixture_corpus:
            report = evaluate_synthetic_fixture(args.dataset, args.fixture_corpus)
        else:
            raw_database = args.database
            database_file = raw_database[len("sqlite:///"):] if raw_database.startswith("sqlite:///") else raw_database
            if not os.path.isfile(database_file):
                raise ValueError(f"Evaluation database file not found: '{database_file}'")
            db_path = raw_database if raw_database.startswith("sqlite:///") else f"sqlite:///{os.path.abspath(raw_database)}"
            engine = create_db_engine(db_path)
            session_factory = create_session_factory(engine)
            db = session_factory()
            try:
                report = evaluate_retrieval(args.dataset, db)
            finally:
                db.close()
                engine.dispose()
        print("=" * 60)
        print("RETRIEVAL EVALUATION REPORT")
        print("=" * 60)
        print(f"Total Cases:            {report.total_cases}")
        print(f"Recall@1:               {report.recall_at_1:.2%}")
        print(f"Recall@3:               {report.recall_at_3:.2%}")
        print(f"Mean Reciprocal Rank:   {report.mean_reciprocal_rank:.4f}")
        print(f"Failed Cases ({len(report.failed_case_ids)}):     {report.failed_case_ids or 'None'}")
        print("=" * 60)

        if report.recall_at_3 < args.threshold:
            print(f"FAILED: Recall@3 ({report.recall_at_3:.2%}) is below quality threshold ({args.threshold:.2%})")
            sys.exit(1)
        print("SUCCESS: Retrieval quality threshold satisfied.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
