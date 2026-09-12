"""Application service for managing members, roles, and assignments."""
from database import Database
from application.task_service import MutationResult


class MemberService:
    def __init__(self, db: Database):
        self.db = db

    def add_member(self, name: str, email: str | None = None,
                   telegram_handle: str | None = None, notes: str | None = None,
                   roles: list[str] | None = None) -> MutationResult:
        try:
            member_id = self.db.add_member(
                name=name, email=email, telegram_handle=telegram_handle,
                notes=notes, roles=roles
            )
            return MutationResult(
                success=True,
                entity_type='member',
                entity_id=member_id,
                summary=f"Added team member {name} (ID: {member_id})"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to add member: {e}"
            )

    def get_members(self) -> list[dict]:
        return self.db.get_members()

    def get_member(self, member_id: int) -> dict | None:
        return self.db.get_member(member_id)

    def get_roles(self) -> list[dict]:
        return self.db.get_roles()

    def add_role(self, name: str, description: str | None = None) -> MutationResult:
        try:
            role_id = self.db.add_role(name=name, description=description)
            return MutationResult(
                success=True,
                entity_type='role',
                entity_id=role_id,
                summary=f"Created role '{name}' (ID: {role_id})"
            )
        except Exception as e:
            return MutationResult(
                success=False,
                summary=f"Failed to add role: {e}"
            )
