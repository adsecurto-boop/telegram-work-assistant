"""Application service providing a unified Work Item layer across Tasks, Cases, and Requirements."""
from database import Database
from application.task_service import MutationResult


class WorkItemService:
    def __init__(self, db: Database):
        self.db = db

    def list_work_items(self) -> list[dict]:
        return self.db.get_all_work_items()

    def get_work_item(self, entity_type: str, entity_id: int) -> dict | None:
        meta = self.db.get_work_item_meta(entity_type, entity_id)
        if not meta:
            # Check if underlying entity exists
            if entity_type == 'task':
                t = self.db.get_task(entity_id)
                if not t:
                    return None
            elif entity_type == 'case':
                c = self.db.get_case(entity_id)
                if not c:
                    return None
            elif entity_type == 'requirement':
                r = self.db.get_requirement(entity_id)
                if not r:
                    return None
        # Find item in all_work_items
        for item in self.db.get_all_work_items():
            if item['entity_type'] == entity_type and item['entity_id'] == entity_id:
                item['meta'] = meta
                item['blockers'] = self.db.get_blockers(entity_type, entity_id, status='active')
                item['stage_history'] = self.db.get_stage_history(entity_type, entity_id)
                return item
        return None

    def update_work_item_meta(self, entity_type: str, entity_id: int, **kwargs) -> MutationResult:
        try:
            res = self.db.upsert_work_item_meta(entity_type, entity_id, **kwargs)
            return MutationResult(
                success=True,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Updated metadata for {entity_type} #{entity_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Failed to update metadata: {e}"
            )

    def add_blocker(self, entity_type: str, entity_id: int, dependency_type: str,
                    description: str, waiting_on_member_id: int | None = None,
                    waiting_on_role: str | None = None,
                    expected_resolution_date: str | None = None) -> MutationResult:
        try:
            blocker_id = self.db.add_blocker_dependency(
                entity_type=entity_type, entity_id=entity_id,
                dependency_type=dependency_type, description=description,
                waiting_on_member_id=waiting_on_member_id, waiting_on_role=waiting_on_role,
                expected_resolution_date=expected_resolution_date
            )
            return MutationResult(
                success=True,
                entity_type='blocker',
                entity_id=blocker_id,
                summary=f"Logged blocker #{blocker_id} on {entity_type} #{entity_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to add blocker: {e}"
            )

    def resolve_blocker(self, blocker_id: int, resolution_notes: str | None = None) -> MutationResult:
        try:
            success = self.db.resolve_blocker(blocker_id, resolution_notes=resolution_notes)
            return MutationResult(
                success=success,
                entity_type='blocker',
                entity_id=blocker_id,
                summary=f"Resolved blocker #{blocker_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to resolve blocker: {e}"
            )

    def get_blockers(self, entity_type: str | None = None, entity_id: int | None = None,
                     status: str | None = 'active') -> list[dict]:
        return self.db.get_blockers(entity_type=entity_type, entity_id=entity_id, status=status)

    # Requirement & Testing Helpers
    def create_requirement(self, title: str, description: str | None = None,
                           requirement_text: str | None = None, user_story: str | None = None,
                           acceptance_criteria: str | None = None, client: str | None = None,
                           product: str | None = None, ticket: str | None = None,
                           priority: int = 1, due_date: str | None = None,
                           owner_member_id: int | None = None, workflow_template_id: int | None = None) -> MutationResult:
        try:
            req_id = self.db.create_requirement(
                title=title, description=description, requirement_text=requirement_text,
                user_story=user_story, acceptance_criteria=acceptance_criteria, client=client,
                product=product, ticket=ticket, priority=priority, due_date=due_date,
                owner_member_id=owner_member_id, workflow_template_id=workflow_template_id
            )
            return MutationResult(
                success=True,
                entity_type='requirement',
                entity_id=req_id,
                summary=f"Created requirement REQ-{req_id}: {title}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to create requirement: {e}"
            )

    def get_requirement(self, req_id: int) -> dict | None:
        return self.db.get_requirement(req_id)

    def list_requirements(self, status: str | None = None) -> list[dict]:
        return self.db.list_requirements(status=status)

    def add_test_condition(self, requirement_id: int, title: str, description: str | None = None,
                           category: str = 'functional', risk_level: str = 'medium',
                           status: str = 'draft', notes: str | None = None) -> MutationResult:
        try:
            cond_id = self.db.add_test_condition(
                requirement_id=requirement_id, title=title, description=description,
                category=category, risk_level=risk_level, status=status, notes=notes
            )
            return MutationResult(
                success=True,
                entity_type='test_condition',
                entity_id=cond_id,
                summary=f"Created test condition #{cond_id} on requirement REQ-{requirement_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to add test condition: {e}"
            )

    def add_test_case(self, title: str, requirement_id: int | None = None,
                      test_condition_id: int | None = None, objective: str | None = None,
                      preconditions: str | None = None, steps: str | None = None,
                      test_data: str | None = None, expected_result: str | None = None,
                      priority: int = 2, automation_status: str = 'manual') -> MutationResult:
        try:
            case_id = self.db.add_test_case(
                title=title, requirement_id=requirement_id,
                test_condition_id=test_condition_id, objective=objective,
                preconditions=preconditions, steps=steps, test_data=test_data,
                expected_result=expected_result, priority=priority,
                automation_status=automation_status
            )
            return MutationResult(
                success=True,
                entity_type='test_case',
                entity_id=case_id,
                summary=f"Created test case TC-{case_id}: {title}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to add test case: {e}"
            )

    def list_test_cases(self, requirement_id: int | None = None) -> list[dict]:
        return self.db.list_test_cases(requirement_id=requirement_id)

    def record_test_execution(self, test_case_id: int, result: str, build: str | None = None,
                              environment: str | None = None, actual_result: str | None = None,
                              defect_id: int | None = None, executed_by_member_id: int | None = None,
                              notes: str | None = None, session_id: int | None = None) -> MutationResult:
        try:
            exec_id = self.db.record_test_execution(
                test_case_id=test_case_id, result=result, build=build,
                environment=environment, actual_result=actual_result,
                defect_id=defect_id, executed_by_member_id=executed_by_member_id,
                notes=notes, session_id=session_id
            )
            return MutationResult(
                success=True,
                entity_type='test_execution',
                entity_id=exec_id,
                summary=f"Recorded {result.upper()} execution for TC-{test_case_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to record test execution: {e}"
            )
