"""Application service providing a unified Work Item layer across Tasks, Cases, and Requirements."""
from database import Database
from application.task_service import MutationResult
from gemini_test_generator import GeminiTestConditionGenerator


class WorkItemService:
    def __init__(self, db: Database):
        self.db = db
        self.gemini_test_generator = GeminiTestConditionGenerator(db)

    def list_work_items(self) -> list[dict]:
        return self.db.get_all_work_items()

    def get_work_item(self, entity_type: str, entity_id: int) -> dict | None:
        return self.get_work_item_detail(entity_type, entity_id)

    def get_work_item_detail(self, entity_type: str, entity_id: int) -> dict | None:
        return self.db.get_work_item_detail(entity_type, entity_id)

    def update_work_item(self, entity_type: str, entity_id: int, **kwargs) -> MutationResult:
        try:
            success = self.db.update_work_item(entity_type, entity_id, **kwargs)
            return MutationResult(
                success=success,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Updated {entity_type} #{entity_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Failed to update {entity_type}: {e}"
            )

    def delete_work_item(self, entity_type: str, entity_id: int) -> MutationResult:
        try:
            success = self.db.delete_work_item(entity_type, entity_id)
            return MutationResult(
                success=success,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Deleted {entity_type} #{entity_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Failed to delete {entity_type}: {e}"
            )

    def snooze_followup(self, followup_id: int, days: int = 1) -> MutationResult:
        try:
            success = self.db.snooze_followup(followup_id, days=days)
            return MutationResult(
                success=success,
                entity_type='followup',
                entity_id=followup_id,
                summary=f"Snoozed follow-up #{followup_id} by {days} day(s)"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to snooze follow-up: {e}"
            )


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

    async def generate_draft_test_conditions(self, requirement_id: int,
                                            focus_area: str = 'comprehensive',
                                            model_name: str | None = None) -> dict:
        return await self.gemini_test_generator.generate_draft_conditions(
            requirement_id=requirement_id,
            focus_area=focus_area,
            model_name=model_name
        )

    def approve_draft_test_conditions(self, requirement_id: int,
                                      approved_conditions: list[dict],
                                      status: str = 'approved') -> dict:
        return self.gemini_test_generator.approve_draft_conditions(
            requirement_id=requirement_id,
            approved_conditions=approved_conditions,
            status=status
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

    def get_test_case(self, test_case_id: int) -> dict | None:
        return self.db.get_test_case(test_case_id)

    def update_test_case(self, test_case_id: int, **kwargs) -> MutationResult:
        try:
            success = self.db.update_test_case(test_case_id, **kwargs)
            return MutationResult(
                success=success,
                entity_type='test_case',
                entity_id=test_case_id,
                summary=f"Updated test case TC-{test_case_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to update test case: {e}")

    def delete_test_case(self, test_case_id: int) -> MutationResult:
        try:
            success = self.db.delete_test_case(test_case_id)
            return MutationResult(
                success=success,
                entity_type='test_case',
                entity_id=test_case_id,
                summary=f"Deleted test case TC-{test_case_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to delete test case: {e}")

    def update_test_condition(self, condition_id: int, **kwargs) -> MutationResult:
        try:
            success = self.db.update_test_condition(condition_id, **kwargs)
            return MutationResult(
                success=success,
                entity_type='test_condition',
                entity_id=condition_id,
                summary=f"Updated test condition #{condition_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to update test condition: {e}")

    def delete_test_condition(self, condition_id: int) -> MutationResult:
        try:
            success = self.db.delete_test_condition(condition_id)
            return MutationResult(
                success=success,
                entity_type='test_condition',
                entity_id=condition_id,
                summary=f"Deleted test condition #{condition_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to delete test condition: {e}")

    def list_test_conditions(self, requirement_id: int | None = None, status: str | None = None) -> list[dict]:
        return self.db.list_test_conditions(requirement_id=requirement_id, status=status)

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

    def list_test_executions(self, test_case_id: int | None = None, limit: int = 50) -> list[dict]:
        return self.db.list_test_executions(test_case_id=test_case_id, limit=limit)

    # Defect Operations
    def create_defect(self, title: str, description: str | None = None,
                      severity: str = 'major', priority: int = 2, status: str = 'new',
                      requirement_id: int | None = None, test_condition_id: int | None = None,
                      test_case_id: int | None = None, execution_id: int | None = None,
                      assigned_member_id: int | None = None, client: str | None = None,
                      product: str | None = None, ticket: str | None = None,
                      steps_to_reproduce: str | None = None, expected_result: str | None = None,
                      actual_result: str | None = None, build_found: str | None = None,
                      environment: str | None = None) -> MutationResult:
        try:
            defect_id = self.db.create_defect(
                title=title, description=description, severity=severity, priority=priority,
                status=status, requirement_id=requirement_id, test_condition_id=test_condition_id,
                test_case_id=test_case_id, execution_id=execution_id, assigned_member_id=assigned_member_id,
                client=client, product=product, ticket=ticket, steps_to_reproduce=steps_to_reproduce,
                expected_result=expected_result, actual_result=actual_result, build_found=build_found,
                environment=environment
            )
            return MutationResult(
                success=True,
                entity_type='defect',
                entity_id=defect_id,
                summary=f"Created defect DEF-{defect_id}: {title}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to create defect: {e}")

    def get_defect(self, defect_id: int) -> dict | None:
        return self.db.get_defect(defect_id)

    def list_defects(self, requirement_id: int | None = None, status: str | None = None,
                     assigned_member_id: int | None = None, limit: int = 100) -> list[dict]:
        return self.db.list_defects(requirement_id=requirement_id, status=status,
                                    assigned_member_id=assigned_member_id, limit=limit)

    def update_defect(self, defect_id: int, **kwargs) -> MutationResult:
        try:
            success = self.db.update_defect(defect_id, **kwargs)
            return MutationResult(
                success=success,
                entity_type='defect',
                entity_id=defect_id,
                summary=f"Updated defect DEF-{defect_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to update defect: {e}")

    def retest_defect(self, defect_id: int, result: str, build_fixed: str | None = None,
                      retest_notes: str | None = None, tester_member_id: int | None = None) -> MutationResult:
        try:
            success = self.db.retest_defect(
                defect_id=defect_id, result=result, build_fixed=build_fixed,
                retest_notes=retest_notes, tester_member_id=tester_member_id
            )
            return MutationResult(
                success=success,
                entity_type='defect',
                entity_id=defect_id,
                summary=f"Recorded retest ({result.upper()}) for DEF-{defect_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to retest defect: {e}")

    def reopen_defect(self, defect_id: int, reason: str | None = None) -> MutationResult:
        try:
            success = self.db.reopen_defect(defect_id, reason=reason)
            return MutationResult(
                success=success,
                entity_type='defect',
                entity_id=defect_id,
                summary=f"Reopened defect DEF-{defect_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to reopen defect: {e}")

    # Artifacts & Timeline & Posture
    def add_artifact(self, entity_type: str, entity_id: int, artifact_type: str,
                     name: str, path: str | None = None, url: str | None = None,
                     details: dict | None = None) -> MutationResult:
        try:
            art_id = self.db.add_artifact(
                entity_type=entity_type, entity_id=entity_id,
                artifact_type=artifact_type, name=name, path=path,
                url=url, details=details
            )
            return MutationResult(
                success=True,
                entity_type='artifact',
                entity_id=art_id,
                summary=f"Linked artifact '{name}' to {entity_type} #{entity_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to add artifact: {e}")

    def list_artifacts(self, entity_type: str | None = None, entity_id: int | None = None) -> list[dict]:
        return self.db.list_artifacts(entity_type=entity_type, entity_id=entity_id)

    def delete_artifact(self, artifact_id: int) -> MutationResult:
        try:
            success = self.db.delete_artifact(artifact_id)
            return MutationResult(
                success=success,
                entity_type='artifact',
                entity_id=artifact_id,
                summary=f"Deleted artifact #{artifact_id}"
            )
        except Exception as e:
            return MutationResult(success=False, summary=f"Failed to delete artifact: {e}")

    def get_timeline(self, entity_type: str, entity_id: int) -> list[dict]:
        return self.db.get_timeline(entity_type=entity_type, entity_id=entity_id)

    def get_testing_posture(self, requirement_id: int) -> dict:
        return self.db.get_testing_posture(requirement_id)

    def generate_completion_report(self, requirement_id: int) -> dict:
        return self.db.generate_completion_report(requirement_id)

    def propose_test_conditions(self, requirement_id: int) -> list[dict]:
        """
        AI-assisted test condition generator: Gemini drafts test conditions from user stories & criteria.
        Proposals are returned as draft candidates for explicit human review and approval.
        """
        req = self.db.get_requirement(requirement_id)
        if not req:
            return []

        title = req.get('title') or ''
        story = req.get('user_story') or req.get('requirement_text') or ''
        criteria = req.get('acceptance_criteria') or ''

        # Deterministic domain proposals
        proposals = [
            {
                'title': f"Verify happy-path acceptance criteria for {title}",
                'description': f"Ensure standard functional flow executes as specified in: {criteria[:120]}...",
                'category': 'functional',
                'risk_level': 'high'
            },
            {
                'title': f"Validate boundary and edge inputs on {title}",
                'description': "Test empty inputs, maximum string lengths, null fields, and numeric limits.",
                'category': 'boundary',
                'risk_level': 'medium'
            },
            {
                'title': f"Check permission and error handling for unauthorized actions",
                'description': "Verify appropriate 403 / validation messages when caller lacks necessary roles or inputs are malformed.",
                'category': 'negative',
                'risk_level': 'medium'
            },
            {
                'title': f"Regression validation on affected dependent workflows",
                'description': "Ensure existing client flows and report generation remain unaffected by changes.",
                'category': 'regression',
                'risk_level': 'low'
            }
        ]
        return proposals
