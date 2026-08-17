from decimal import Decimal

API = "/api/v1"


def _income(lease_id, amount="12000.00", status="pending"):
    return {
        "lease_id": lease_id,
        "amount": amount,
        "received_date": "2026-02-01",
        "payment_method": "cash",
        "status": status,
        "description": "rent Feb",
    }


def _expense(status="pending"):
    return {
        "expense_date": "2026-02-05",
        "category": "repair",
        "amount": "5000.00",
        "payee": "Fix-It Co",
        "description": "AC repair",
        "status": status,
    }


def test_income_requires_status(client, admin_headers, lease_id):
    payload = _income(lease_id)
    payload.pop("status")
    resp = client.post(f"{API}/incomes", json=payload, headers=admin_headers)
    assert resp.status_code == 422


def test_income_confirm_and_double_confirm_idempotent(client, admin_headers, lease_id):
    resp = client.post(f"{API}/incomes", json=_income(lease_id), headers=admin_headers)
    income_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"

    resp = client.post(f"{API}/incomes/{income_id}/confirm", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    assert resp.json()["confirmed_by"] is not None

    resp = client.post(f"{API}/incomes/{income_id}/confirm", headers=admin_headers)
    # Financial-safety V1.1: an idempotent replay of confirm returns the
    # current confirmed state instead of a conflict.
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_income_reverse_flow(client, admin_headers, lease_id):
    resp = client.post(f"{API}/incomes", json=_income(lease_id, status="confirmed"), headers=admin_headers)
    income_id = resp.json()["id"]
    assert resp.json()["status"] == "confirmed"

    resp = client.post(f"{API}/incomes/{income_id}/reverse", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "reversed"

    resp = client.post(f"{API}/incomes/{income_id}/reverse", headers=admin_headers)
    # Financial-safety V1.1: replay of reverse returns the current reversed
    # state instead of a conflict.
    assert resp.status_code == 200
    assert resp.json()["status"] == "reversed"


def test_income_no_delete_endpoint(client, admin_headers, lease_id):
    resp = client.post(f"{API}/incomes", json=_income(lease_id), headers=admin_headers)
    income_id = resp.json()["id"]
    resp = client.delete(f"{API}/incomes/{income_id}", headers=admin_headers)
    assert resp.status_code == 405


def test_income_amount_locked_after_confirm(client, admin_headers, lease_id):
    resp = client.post(f"{API}/incomes", json=_income(lease_id), headers=admin_headers)
    income_id = resp.json()["id"]
    client.post(f"{API}/incomes/{income_id}/confirm", headers=admin_headers)

    resp = client.patch(
        f"{API}/incomes/{income_id}", json={"amount": "9999.00"}, headers=admin_headers
    )
    assert resp.status_code == 409


def test_expense_requires_status(client, admin_headers):
    payload = _expense()
    payload.pop("status")
    resp = client.post(f"{API}/expenses", json=payload, headers=admin_headers)
    assert resp.status_code == 422


def test_expense_approve_pay_reverse_flow(client, admin_headers, manager_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    assert resp.status_code == 201
    expense_id = resp.json()["id"]

    resp = client.post(f"{API}/expenses/{expense_id}/approve", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["approved_by"] is not None

    resp = client.post(f"{API}/expenses/{expense_id}/pay", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "paid"

    resp = client.post(f"{API}/expenses/{expense_id}/reverse", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "reversed"


def test_expense_admin_can_approve_own_created(client, admin_headers, manager_headers):
    # admin creates an expense, then approves their own -> allowed
    resp = client.post(f"{API}/expenses", json=_expense(), headers=admin_headers)
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/approve", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["approved_by"] is not None


def test_expense_manager_cannot_approve_own_created(client, admin_headers, manager_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/approve", headers=manager_headers)
    assert resp.status_code == 403


def test_expense_admin_can_reject_own_created(client, admin_headers):
    # Owner records an expense themselves, then rejects it -> allowed (the
    # Owner is the final authority; this is the V1 Owner-records flow).
    resp = client.post(f"{API}/expenses", json=_expense(), headers=admin_headers)
    assert resp.status_code == 201
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/reject", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_expense_manager_cannot_reject_own_created(client, admin_headers, manager_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/reject", headers=manager_headers)
    assert resp.status_code == 403


def test_expense_reject_flow(client, admin_headers, manager_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/reject", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_expense_amount_locked_after_approve(client, admin_headers, manager_headers):
    """003B §9: a critical financial field (amount) changed after approval must
    INVALIDATE the old approval and return the expense to PENDING for Owner
    re-review — it must NOT keep showing a stale APPROVED with the new amount."""
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    expense_id = resp.json()["id"]
    client.post(f"{API}/expenses/{expense_id}/approve", headers=admin_headers)

    resp = client.patch(
        f"{API}/expenses/{expense_id}", json={"amount": "35000.00"}, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"  # re-approval required, never stale APPROVED
    assert body["amount"] == "35000.00"
    assert body["approved_by"] is None
    assert body["reapproval_reason"] is not None


def test_expense_pay_requires_approved(client, admin_headers, manager_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=manager_headers)
    expense_id = resp.json()["id"]
    resp = client.post(f"{API}/expenses/{expense_id}/pay", headers=admin_headers)
    assert resp.status_code == 409


def test_expense_no_delete_endpoint(client, admin_headers):
    resp = client.post(f"{API}/expenses", json=_expense(), headers=admin_headers)
    expense_id = resp.json()["id"]
    resp = client.delete(f"{API}/expenses/{expense_id}", headers=admin_headers)
    assert resp.status_code == 405


def test_attachment_upload_download(client, admin_headers):
    files = {"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")}
    resp = client.post(f"{API}/attachments", files=files, headers=admin_headers)
    assert resp.status_code == 201
    attachment_id = resp.json()["id"]
    assert resp.json()["original_filename"] == "receipt.pdf"

    resp = client.get(f"{API}/attachments/{attachment_id}/download", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-1.4")


def test_money_precision_never_float(client, admin_headers, lease_id):
    # Decimal serialized as string, never as a float
    resp = client.post(f"{API}/incomes", json=_income(lease_id), headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["amount"] == "12000.00"

    # amounts are limited to Numeric(14,2): too many digits -> 422
    resp = client.post(
        f"{API}/incomes",
        json=_income(lease_id, amount="99999999999999.99"),
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_money_rejects_float_input(client, admin_headers, lease_id):
    resp = client.post(
        f"{API}/incomes",
        json=_income(lease_id, amount=12000.556),  # float with >2 decimals
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_expense_due_date_and_unit(client, admin_headers, unit_id):
    payload = _expense()
    payload.update({"due_date": "2026-08-18", "unit_id": unit_id})
    resp = client.post(f"{API}/expenses", json=payload, headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["due_date"] == "2026-08-18"
    assert resp.json()["unit_id"] == unit_id


def test_expense_unknown_unit_404(client, admin_headers):
    payload = _expense()
    payload["unit_id"] = 999999
    resp = client.post(f"{API}/expenses", json=payload, headers=admin_headers)
    assert resp.status_code == 404
