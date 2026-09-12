"""
Automated quality checks and validation for generated reports against structured database records.
Detects factual discrepancies, count hallucinations, task status mismatches, missing priority items,
unverified testing/resolution claims, duplicate clients, and out-of-bounds records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class ReportWarning:
    code: str
    message: str
    severity: Literal['warning', 'error', 'info'] = 'warning'
    record_ref: str | None = None


@dataclass
class ReportValidationResult:
    is_valid: bool
    warnings: list[ReportWarning] = field(default_factory=list)
    verified_metrics: dict[str, Any] = field(default_factory=dict)
    provenance_links: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(w.severity == 'error' for w in self.warnings)


class ReportValidator:
    """Validates generated report text against the source structured records scoped to shift/date."""

    @classmethod
    def validate(
        cls,
        report_kind: str,
        report_text: str,
        shift: dict,
        activities: list[dict],
        tasks: list[Any],
        cases: list[dict] | None = None,
        test_sessions: list[dict] | None = None,
        followups: list[dict] | None = None
    ) -> ReportValidationResult:
        warnings: list[ReportWarning] = []
        provenance: list[dict[str, Any]] = []

        cases = cases or []
        test_sessions = test_sessions or []
        followups = followups or []
        from models import Task
        tasks = [Task.from_row(t) if isinstance(t, dict) else t for t in (tasks or [])]
        lowered_report = report_text.casefold()

        shift_start = shift.get('start') or ''
        shift_end = shift.get('end') or ''
        shift_date = shift_start[:10] if shift_start else datetime.now().date().isoformat()

        # 1. Structured Metrics Calculation
        support_activities = [a for a in activities if a.get('category') == 'support']
        client_map = {}
        for a in support_activities:
            raw = (a.get('client') or '').strip()
            if raw:
                client_map[raw.casefold()] = raw
        for c in cases:
            raw = (c.get('client') or '').strip()
            if raw:
                client_map[raw.casefold()] = raw
        all_unique_clients = {v for k, v in client_map.items() if k not in ('general', 'none', 'unknown', 'n/a', '')}
        lowered_clients = {k: v for k, v in client_map.items() if k not in ('general', 'none', 'unknown', 'n/a', '')}

        # Tasks scoped to this shift/date
        completed_tasks = [t for t in tasks if getattr(t, 'status', None) and t.status.value == 'completed']
        pending_tasks = [t for t in tasks if getattr(t, 'status', None) and t.status.value in ('pending', 'in_progress', 'blocked')]

        passed_tests = [t for t in test_sessions if t.get('result') == 'passed']
        failed_tests = [t for t in test_sessions if t.get('result') == 'failed']

        known_tickets = set()
        for c in cases:
            if c.get('ticket'):
                known_tickets.add(c['ticket'].casefold())
        for t in tasks:
            if getattr(t, 'ticket', None):
                known_tickets.add(t.ticket.casefold())

        metrics = {
            'unique_clients_count': len(all_unique_clients),
            'unique_clients': sorted(list(all_unique_clients)),
            'support_interactions_count': len(support_activities),
            'tasks_completed_count': len(completed_tasks),
            'tasks_pending_count': len(pending_tasks),
            'test_sessions_count': len(test_sessions),
            'passed_tests_count': len(passed_tests),
            'failed_tests_count': len(failed_tests),
            'shift_date': shift_date,
        }

        # 2. Check Client Counts (Rule 1)
        client_count_matches = re.findall(r'\b(?:named\s+clients|handled|clients\s+handled)[:\s]+(\d+)\b', report_text, re.I)
        for count_str in client_count_matches:
            claimed_count = int(count_str)
            if claimed_count != len(all_unique_clients) and claimed_count != len(support_activities):
                warnings.append(ReportWarning(
                    code='UNSUPPORTED_CLIENT_COUNT',
                    message=f"Report states {claimed_count} clients, but structured data has {len(all_unique_clients)} unique client(s) ({len(support_activities)} interaction(s)).",
                    severity='error'
                ))

        # Check Interaction Counts (Rule 2)
        interaction_count_matches = re.findall(r'\b(?:interactions|queries|support\s+queries)[:\s]+(\d+)\b', report_text, re.I)
        for count_str in interaction_count_matches:
            claimed_interactions = int(count_str)
            if claimed_interactions != len(support_activities):
                warnings.append(ReportWarning(
                    code='UNSUPPORTED_INTERACTION_COUNT',
                    message=f"Report states {claimed_interactions} interactions, but structured records show {len(support_activities)}.",
                    severity='warning'
                ))

        # 3. Check for Duplicate Clients (Rule 3)
        client_tokens = re.findall(r'\b(?:clients(?:\s+handled)?|handled|customer)[:\s]+([^\n]+)', report_text, re.I)
        for c_text in client_tokens:
            cleaned_line = re.sub(r'^(?:clients|handled|customer)[:\s]*', '', c_text, flags=re.I)
            cleaned_line = re.sub(r'^\s*\d+[\.\)]\s*', '', cleaned_line).rstrip('. \t')
            items = [x.strip().casefold() for x in re.split(r'[,;/]| and ', cleaned_line) if len(x.strip()) > 2]
            seen_clients = set()
            for it in items:
                if it in seen_clients:
                    warnings.append(ReportWarning(
                        code='DUPLICATE_CLIENT_ENTRY',
                        message=f"Duplicate client entry '{it}' in report client summary.",
                        severity='warning'
                    ))
                seen_clients.add(it)

        for c_lower, c_orig in lowered_clients.items():
            # Check for multiple bullet entries mentioning this client
            bullet_matches = len(re.findall(rf'^\s*[•\-\*]\s*.*?\b{re.escape(c_lower)}\b', lowered_report, re.MULTILINE))
            if bullet_matches > 1:
                warnings.append(ReportWarning(
                    code='DUPLICATE_CLIENT_ENTRY',
                    message=f"Client '{c_orig}' appears {bullet_matches} times in the report's bullet items.",
                    severity='warning'
                ))

        # 4. Check Task Status Consistency (Rules 4 & 5)
        completed_section = ''
        pending_section = ''
        if 'completed' in lowered_report or 'accomplishments' in lowered_report:
            parts = re.split(r'\b(?:pending|remaining|tomorrow|priorities)\b', lowered_report, maxsplit=1)
            completed_section = parts[0]
            if len(parts) > 1:
                pending_section = parts[1]

        for t in completed_tasks:
            t_title = t.title.strip()
            provenance.append({
                'section_name': 'completed_tasks',
                'record_type': 'task',
                'record_id': t.id,
                'detail': t_title
            })
            if pending_section and t_title.casefold() in pending_section:
                warnings.append(ReportWarning(
                    code='TASK_STATUS_MISMATCH',
                    message=f"Completed Task #{t.id} ('{t_title}') appears in the pending/remaining section.",
                    severity='warning',
                    record_ref=f"task:{t.id}"
                ))

        for t in pending_tasks:
            t_title = t.title.strip()
            provenance.append({
                'section_name': 'pending_tasks',
                'record_type': 'task',
                'record_id': t.id,
                'detail': t_title
            })
            if completed_section and t_title.casefold() in completed_section:
                warnings.append(ReportWarning(
                    code='TASK_STATUS_MISMATCH',
                    message=f"Pending Task #{t.id} ('{t_title}') appears in the completed section.",
                    severity='warning',
                    record_ref=f"task:{t.id}"
                ))

        # 5. Missing Open High-Priority Cases (Rule 6)
        high_priority_open_cases = [
            c for c in cases
            if c.get('priority', 1) >= 2 and c.get('status') not in ('resolved', 'closed')
        ]
        for c in high_priority_open_cases:
            c_title = c.get('title', '').casefold()
            c_id_str = f"case-{c['id']}"
            provenance.append({
                'section_name': 'high_priority_cases',
                'record_type': 'work_case',
                'record_id': c['id'],
                'detail': c.get('title')
            })
            if c_title and (c_title not in lowered_report and c_id_str not in lowered_report):
                warnings.append(ReportWarning(
                    code='OMITTED_HIGH_PRIORITY_CASE',
                    message=f"Open high-priority CASE-{c['id']} ('{c.get('title')}') is not mentioned in the report.",
                    severity='warning',
                    record_ref=f"case:{c['id']}"
                ))

        # 6. Overdue Follow-ups Check (Rule 7)
        for f in followups:
            if f.get('status') == 'pending':
                f_due = f.get('due_at') or ''
                if shift_start and shift_end and f_due and shift_start <= f_due <= shift_end:
                    f_note = (f.get('note') or '').casefold()
                    if f_note and f_note not in lowered_report and f"case-{f.get('case_id')}" not in lowered_report:
                        warnings.append(ReportWarning(
                            code='OVERDUE_FOLLOWUP_OMITTED',
                            message=f"Pending follow-up #{f.get('id')} for CASE-{f.get('case_id')} due at {f_due[:16]} is not mentioned.",
                            severity='info',
                            record_ref=f"followup:{f.get('id')}"
                        ))

        # 7. Resolution claims without supporting case state or event (Rule 8)
        resolution_matches = re.finditer(r'\b(?:resolved|fix(?:ed)?)\s+(?:the\s+)?([a-zA-Z0-9_\- ]+?)\s+(?:issue|case|problem|bug)\b', lowered_report)
        for m in resolution_matches:
            target_topic = m.group(1).strip()
            # Verify if an actual resolved case exists matching this topic
            matching_resolved = [
                c for c in cases
                if c.get('status') in ('resolved', 'closed') and (target_topic in c.get('title', '').casefold() or c.get('client', '').casefold() in target_topic)
            ]
            if not matching_resolved and len(cases) > 0:
                warnings.append(ReportWarning(
                    code='UNSUPPORTED_RESOLUTION_CLAIM',
                    message=f"Report claims resolution for '{target_topic}', but no corresponding case is marked resolved.",
                    severity='warning'
                ))

        # 8. Testing Conclusions without Test Records (Rule 9)
        # A section heading alone is not a claim that testing happened.
        claim_text = '\n'.join(line for line in lowered_report.splitlines()
                               if line.strip().strip('#*: ') != 'testing')
        claims_testing = bool(re.search(r'\b(?:tested|testing|test\s+session|reproduced)\b', claim_text))
        has_records = bool(test_sessions) or any(a.get('category') == 'testing' for a in activities)
        if claims_testing and not has_records:
            warnings.append(ReportWarning(
                code='UNVERIFIED_TEST_CLAIM',
                message="Report mentions testing or reproduction outcomes, but no test session or testing activity was recorded.",
                severity='warning'
            ))

        # 9. Conflicting Test Results (Rule 10)
        claims_passed = bool(re.search(r'\b(?:tests?\s+passed|all\s+tests?\s+passed|verification\s+passed)\b', lowered_report))
        claims_failed = bool(re.search(r'\b(?:tests?\s+failed|tests?\s+broken)\b', lowered_report))
        if claims_passed and failed_tests and not passed_tests:
            warnings.append(ReportWarning(
                code='CONFLICTING_TEST_RESULT',
                message="Report claims tests passed, but recorded test sessions only contain failures.",
                severity='error'
            ))
        elif claims_failed and passed_tests and not failed_tests:
            warnings.append(ReportWarning(
                code='CONFLICTING_TEST_RESULT',
                message="Report claims tests failed, but recorded test sessions only contain passes.",
                severity='error'
            ))

        # 10. Hallucinated Ticket Detection (Rule 11)
        ticket_mentions = re.findall(r'\b([A-Z]{2,8}-\d{1,6})\b', report_text)
        for t_token in ticket_mentions:
            if t_token.casefold() not in known_tickets:
                warnings.append(ReportWarning(
                    code='HALLUCINATED_TICKET',
                    message=f"Ticket '{t_token}' in report does not match any known case or task ticket for this shift.",
                    severity='warning'
                ))

        # 11. Hallucinated Client Detection (Rule 12)
        client_line_matches = re.findall(r'(?:client|handled|customer)[:\s]+([A-Za-z0-9_\-]+(?:\s+\d+)?)', report_text, re.I)
        for_client_matches = re.findall(r'\bfor\s+([A-Z][A-Za-z0-9_\-]+(?:\s+\d+)?)\b', report_text)
        for cl_token in client_line_matches + for_client_matches:
            cl_token_clean = cl_token.strip().casefold()
            if cl_token_clean not in ('none', 'unspecified', 'general', 'support', 'various', 'the', 'this', 'a', 'an', 'query', 'queries', 'task', 'case'):
                # Ignore digits, masked clients ("client 1", "client 10", "client-a"), redacted tokens
                if cl_token_clean.isdigit() or re.match(r'^client[\s_-]?(?:\d+|[a-z])$', cl_token_clean) or cl_token_clean in ('client', '[client_redacted]', '[client]'):
                    continue
                if cl_token_clean not in lowered_clients:
                    warnings.append(ReportWarning(
                        code='HALLUCINATED_CLIENT',
                        message=f"Client '{cl_token}' mentioned in report is not recorded in shift activities or cases.",
                        severity='warning'
                    ))

        # 12. Record Outside Shift (Rule 13)
        if shift_start and shift_end:
            start_dt = datetime.fromisoformat(shift_start)
            end_dt = datetime.fromisoformat(shift_end)
            for a in activities:
                occurred = a.get('occurred_at') or a.get('created_at') or ''
                if not occurred:
                    continue
                occurred_dt = datetime.fromisoformat(occurred)
                # Legacy timestamps without offsets are interpreted in shift-local time.
                if occurred_dt.tzinfo is None:
                    occurred_dt = occurred_dt.replace(tzinfo=start_dt.tzinfo)
                occurred_aligned = occurred_dt.astimezone(start_dt.tzinfo) if start_dt.tzinfo else occurred_dt
                start_aligned = start_dt
                end_aligned = end_dt.astimezone(start_dt.tzinfo) if start_dt.tzinfo else end_dt
                if not (start_aligned <= occurred_aligned <= end_aligned):
                    warnings.append(ReportWarning(
                        code='RECORD_OUTSIDE_SHIFT',
                        message=f"Activity #{a.get('id')} occurred at {occurred_aligned.isoformat()}, outside the shift window ({start_dt} to {end_dt}).",
                        severity='info',
                        record_ref=f"activity:{a.get('id')}"
                    ))
                    break

        # 13. Tomorrow Item Incorrectly Completed Today (Rule 14)
        if completed_section:
            for t in tasks:
                due = getattr(t, 'due_date', None) or ''
                if due and due > shift_date:
                    if t.title.casefold() in completed_section:
                        warnings.append(ReportWarning(
                            code='TOMORROW_ITEM_COMPLETED_TODAY',
                            message=f"Task #{t.id} ('{t.title}') is scheduled for {due}, but appears in today's completed section.",
                            severity='warning',
                            record_ref=f"task:{t.id}"
                        ))

        for ts in test_sessions:
            provenance.append({
                'section_name': 'testing',
                'record_type': 'test_session',
                'record_id': ts.get('id'),
                'detail': ts.get('scenario')
            })

        is_valid = len([w for w in warnings if w.severity == 'error']) == 0

        return ReportValidationResult(
            is_valid=is_valid,
            warnings=warnings,
            verified_metrics=metrics,
            provenance_links=provenance
        )
