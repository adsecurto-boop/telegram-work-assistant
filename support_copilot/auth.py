import secrets
from typing import Callable, List, Optional
from fastapi import Depends, Header, HTTPException, status
from .config import Settings, get_settings

class AuthenticatedCaller:
    def __init__(self, token_name: str, capabilities: List[str]):
        self.token_name = token_name
        self.capabilities = capabilities

def require_capability(required_capability: str) -> Callable:
    def dependency(
        authorization: Optional[str] = Header(None, alias="Authorization"),
        settings: Settings = Depends(get_settings),
    ) -> AuthenticatedCaller:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authorization credential.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization scheme. Bearer token required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided_token = parts[1]

        matched_scopes: Optional[List[str]] = None
        matched_token_id: Optional[str] = None

        for idx, (configured_token, scopes) in enumerate(settings.api_tokens.items()):
            if secrets.compare_digest(provided_token, configured_token):
                matched_scopes = scopes
                matched_token_id = f"token_{idx}"
                break

        if matched_scopes is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if required_capability not in matched_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required capability: '{required_capability}'.",
            )

        return AuthenticatedCaller(token_name=matched_token_id, capabilities=matched_scopes)

    return dependency
