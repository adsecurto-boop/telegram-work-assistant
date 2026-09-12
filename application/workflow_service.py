"""Application service for managing workflow templates, stages, and stage transitions."""
from database import Database
from application.task_service import MutationResult


class WorkflowService:
    def __init__(self, db: Database):
        self.db = db

    def list_templates(self) -> list[dict]:
        return self.db.get_workflow_templates()

    def get_template(self, template_id: int) -> dict | None:
        return self.db.get_workflow_template(template_id)

    def create_template(self, name: str, description: str | None = None,
                        work_type: str = 'requirement', is_default: bool = False,
                        stages: list[dict] | None = None) -> MutationResult:
        try:
            template_id = self.db.create_workflow_template(
                name=name, description=description, work_type=work_type,
                is_default=is_default, stages=stages
            )
            return MutationResult(
                success=True,
                entity_type='workflow_template',
                entity_id=template_id,
                summary=f"Created workflow template '{name}' (ID: {template_id})"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to create workflow template: {e}"
            )

    def transition_stage(self, entity_type: str, entity_id: int, new_stage_id: int,
                         actor_member_id: int | None = None, note: str | None = None) -> MutationResult:
        try:
            success = self.db.transition_stage(
                entity_type=entity_type, entity_id=entity_id,
                new_stage_id=new_stage_id, actor_member_id=actor_member_id,
                note=note
            )
            return MutationResult(
                success=success,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Transitioned {entity_type} #{entity_id} to stage #{new_stage_id}"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=f"Failed to transition stage: {e}"
            )

    def get_stage_history(self, entity_type: str, entity_id: int) -> list[dict]:
        return self.db.get_stage_history(entity_type, entity_id)
