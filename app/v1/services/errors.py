"""Service-layer errors.

Routes map these to HTTPException:
- NotFoundError → 404
- ConflictError → 409
- ValidationError → 400
- CrossOrgAccessError → 403
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base service error."""


class NotFoundError(ServiceError):
    """Resource not found."""


class ConflictError(ServiceError):
    """Resource already exists / state conflict."""


class ValidationError(ServiceError):
    """Input validation failure (distinct from PermissionDenied)."""


class CrossOrgAccessError(ServiceError):
    """Cross-org access attempt (fail-closed)."""