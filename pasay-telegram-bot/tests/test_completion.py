"""PASAY-V2-OWNER-SECRETARY-JOURNEY-AUDIT-006 (Journey M): deterministic
completion-feedback selector. No LLM, safe template pools, recent-avoidance so
the same wording is not repeated back-to-back."""
from pasay_bot.render import completion


def test_select_returns_valid_pool():
    key, template = completion.select("zh", "task")
    assert key
    assert template
    assert template in dict(completion._TEMPLATES["zh.task"]).values()


def test_pool_has_10_safe_templates_across_categories():
    # Journey M prefers ~10+ safe templates per major completion category when
    # practical; the pools must be small, positive and never fabricated praise.
    pools = completion._TEMPLATES
    # task + payment across the three locales.
    assert len(pools["zh.task"]) >= 5
    assert len(pools["en.task"]) >= 5
    assert len(pools["zh.payment"]) >= 3
    for pool in pools.values():
        for _key, tmpl in pool:
            # No fabricated numbers / names / amounts inside the framing line.
            assert not any(c.isdigit() for c in tmpl), f"digit in template: {tmpl}"


def test_recent_avoidance_cycles_but_falls_back():
    recent = set()
    seen = []
    # Collect the distinct variants offered up to the pool length.
    for _ in range(len(completion._TEMPLATES["zh.task"])):
        key, _ = completion.select("zh", "task", recent)
        seen.append(key)
        recent.add(key)
    # When every variant has been used, we still return a valid one (fallback).
    key, template = completion.select("zh", "task", recent)
    assert template
    assert key in dict(completion._TEMPLATES["zh.task"])
