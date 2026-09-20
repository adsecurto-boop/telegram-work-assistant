import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .database import create_db_engine, create_session_factory
from .knowledge_service import KnowledgeService
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

    for case in cases:
        case_id = case.get("case_id", "unknown")
        query = case.get("query", "")
        product_scope = case.get("product_scope")
        issue_type = case.get("issue_type")
        expected_key = case.get("expected_stable_key", "")
        negative_keys = set(case.get("negative_keys", []))

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
        else:
            failed_ids.append(case_id)

        # Check negative expectations
        negative_hits = [k for k in retrieved_keys[:3] if k in negative_keys]

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Support Copilot Knowledge Retrieval Evaluator")
    parser.add_argument("--dataset", required=True, help="Path to retrieval evaluation JSON dataset")
    parser.add_argument("--database", required=True, help="Database path or connection URL (sqlite:///...)")
    parser.add_argument("--threshold", type=float, default=0.7, help="Minimum Recall@3 quality threshold (default: 0.7)")
    args = parser.parse_args()

    db_path = args.database
    if not db_path.startswith("sqlite:///"):
        db_path = f"sqlite:///{os.path.abspath(db_path)}"

    engine = create_db_engine(db_path)
    session_factory = create_session_factory(engine)
    db = session_factory()
    try:
        report = evaluate_retrieval(args.dataset, db)
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
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
