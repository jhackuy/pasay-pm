"""Read-only rent-payment matching endpoint (V1.3 Slice 2, Entry B).

POST /payments/match resolves a natural-language payment statement to the
most likely open receivable with a confidence grade. It never writes
financial records; confirmation keeps flowing through the existing
Income create + Owner-only confirm chain.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import manager_or_admin
from app.database import get_db
from app.models.user import User
from app.schemas.payment_match import PaymentMatchRequest, PaymentMatchResponse
from app.services import payment_match as payment_match_service
from app.services.organization_scope import list_active_org_ids_for_user

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/match", response_model=PaymentMatchResponse)
def match_rent_payment(
    payload: PaymentMatchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(manager_or_admin),
):
    # Fail-closed: caller active memberships only. Empty membership -> no
    # leases are loaded (no cross-org candidate is ever reachable).
    org_ids = list_active_org_ids_for_user(db, user.id)
    result = payment_match_service.match_payment(
        db, payload.text, amount=payload.amount, org_ids=org_ids
    )
    return {
        "received_date": result.received_date,
        "candidates": [
            {
                "kind": cand.kind.value,
                "confidence": cand.confidence.value,
                "lease_id": cand.lease_id,
                "unit_id": cand.unit_id,
                "unit_number": cand.unit_number,
                "property_id": cand.property_id,
                "property_name": cand.property_name,
                "tenant_id": cand.tenant_id,
                "tenant_name": cand.tenant_name,
                "period": cand.period,
                "due_date": cand.due_date,
                "amount": str(cand.amount),
                "open_count": cand.open_count,
                "due_amount": str(cand.due_amount),
                "paid_amount": str(cand.paid_amount),
                "remaining_balance": str(cand.remaining_balance),
                "income_id": cand.income_id,
                "income_status": cand.income_status,
            }
            for cand in result.candidates
        ],
    }
