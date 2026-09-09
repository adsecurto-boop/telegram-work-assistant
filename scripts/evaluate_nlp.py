"""Offline adaptive-NLP evaluation. No Telegram or Gemini calls are made."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from nlp import DeterministicParser
from nlp_policy import evaluate_action_policy


def evaluate(cases: list[dict], reference: datetime) -> tuple[dict, list[str]]:
    intent_hits = entity_hits = entity_total = false_actions = unknown_count = 0
    failures: list[str] = []
    by_intent: dict[str, dict[str, int]] = {}
    for case in cases:
        parsed = DeterministicParser.parse(case['input'], reference)
        actual_intent = parsed.intent.value if parsed else 'unknown'
        unknown_count += int(actual_intent == 'unknown')
        intent_ok = actual_intent == case['intent']
        intent_hits += int(intent_ok)
        bucket = by_intent.setdefault(case['intent'], {'total': 0, 'correct': 0})
        bucket['total'] += 1
        bucket['correct'] += int(intent_ok)

        mismatches = []
        expected_entities = case.get('entities', {})
        for key, expected in expected_entities.items():
            entity_total += 1
            actual = getattr(parsed.entities, key, None) if parsed else None
            if actual == expected:
                entity_hits += 1
            else:
                mismatches.append(f'{key}={actual!r}, expected {expected!r}')

        decision = 'reject'
        mutates = False
        if parsed:
            policy, mutates, _ = evaluate_action_policy(parsed)
            decision = policy.value
        expected_decision = case.get('decision')
        decision_ok = expected_decision is None or decision == expected_decision
        if case.get('allow_mutation') is False and mutates:
            false_actions += 1
        if not intent_ok or mismatches or not decision_ok:
            details = [f'intent={actual_intent!r} expected={case["intent"]!r}']
            details.extend(mismatches)
            if not decision_ok:
                details.append(f'decision={decision!r} expected={expected_decision!r}')
            failures.append(f'{case["id"]}: ' + '; '.join(details))

    return {
        'total': len(cases),
        'intent_accuracy': intent_hits / len(cases) if cases else 0.0,
        'entity_accuracy': entity_hits / entity_total if entity_total else 1.0,
        'unknown_rate': unknown_count / len(cases) if cases else 0.0,
        'false_actions': false_actions,
        'by_intent': by_intent,
    }, failures


def main() -> int:
    parser = argparse.ArgumentParser(description='Evaluate deterministic natural-language recognition offline.')
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests' / 'fixtures' / 'nl_eval_cases.json')
    parser.add_argument('--reference', default='2026-09-10T12:00:00+05:30')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    reference = datetime.fromisoformat(args.reference).astimezone(ZoneInfo(config.TIMEZONE))
    metrics, failures = evaluate(cases, reference)
    print(f"Parser: {__import__('nlp').NL_PARSER_VERSION}")
    print(f"Cases: {metrics['total']}")
    print(f"Intent accuracy: {metrics['intent_accuracy']:.1%}")
    print(f"Entity accuracy: {metrics['entity_accuracy']:.1%}")
    print(f"Unknown rate: {metrics['unknown_rate']:.1%}")
    print(f"False actions: {metrics['false_actions']}")
    print('Per intent:')
    for intent, values in sorted(metrics['by_intent'].items()):
        print(f"  {intent}: {values['correct']}/{values['total']}")
    if failures:
        print('Failed cases:')
        for failure in failures:
            print(f'  {failure}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
