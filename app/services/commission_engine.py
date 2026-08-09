"""Commission rule engine (non-LLM).

This module is the only place where commission amounts are computed.
APIs must never accept a client-supplied `computed_amount`; they call
`compute_settlement` and store the result.
"""
from decimal import ROUND_HALF_UP, Decimal

from app.models import CommissionRuleType

MONEY_QUANT = Decimal("0.01")


def compute_settlement(settlement, rule, lease_amount) -> Decimal:
    """Compute the settlement amount from a rule and the lease amount.

    Args:
        settlement: the pending settlement being created (kept for API parity;
            not used in the calculation).
        rule: a CommissionRule with `rule_type` and `value`.
        lease_amount: the lease's monetary basis (e.g. monthly rent).

    Returns:
        Decimal rounded to 2 decimal places.
    """
    rule_type = CommissionRuleType(rule.rule_type)
    value = Decimal(str(rule.value))
    basis = Decimal(str(lease_amount))

    if rule_type == CommissionRuleType.percentage:
        computed = basis * value / Decimal("100")
    elif rule_type == CommissionRuleType.flat:
        computed = value
    else:  # pragma: no cover - enum guards this
        raise ValueError(f"unsupported rule_type: {rule_type}")

    return computed.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
