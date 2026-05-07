"""Tests for runbook_exec/models.py.

Covers:
- needs_approval truth table: all 12 combinations of auto_approve_level × risk_level
- AuditEntry round-trips through JSON without losing data
- RiskLevel serializes to its string value in JSON
"""

import json

import pytest

from runbook_exec.models import (
    ActionType,
    AuditEntry,
    Config,
    RiskLevel,
    Step,
    needs_approval,
)


# ---------------------------------------------------------------------------
# needs_approval truth table
# ---------------------------------------------------------------------------

# All 12 combinations: 4 auto_approve_level values × 3 risk_level values
# auto_approve_level: None, READ_ONLY, MODIFYING, DESTRUCTIVE
# risk_level:         READ_ONLY, MODIFYING, DESTRUCTIVE
#
# Rule: approval required iff risk_level is STRICTLY HIGHER than auto_approve_level.
# None is treated as "auto-approve only read_only" (equivalent to auto_approve_level=READ_ONLY
# for the purpose of read_only steps, but modifying/destructive still need approval).

NEEDS_APPROVAL_CASES = [
    # (auto_approve_level, risk_level, expected_needs_approval)
    # --- auto_approve_level = None (default) ---
    (None,                    RiskLevel.READ_ONLY,   False),  # read_only auto-runs
    (None,                    RiskLevel.MODIFYING,   True),   # modifying needs approval
    (None,                    RiskLevel.DESTRUCTIVE, True),   # destructive needs approval
    # --- auto_approve_level = READ_ONLY ---
    (RiskLevel.READ_ONLY,     RiskLevel.READ_ONLY,   False),  # at ceiling → auto-run
    (RiskLevel.READ_ONLY,     RiskLevel.MODIFYING,   True),   # above ceiling → approval
    (RiskLevel.READ_ONLY,     RiskLevel.DESTRUCTIVE, True),   # above ceiling → approval
    # --- auto_approve_level = MODIFYING ---
    (RiskLevel.MODIFYING,     RiskLevel.READ_ONLY,   False),  # below ceiling → auto-run
    (RiskLevel.MODIFYING,     RiskLevel.MODIFYING,   False),  # at ceiling → auto-run
    (RiskLevel.MODIFYING,     RiskLevel.DESTRUCTIVE, True),   # above ceiling → approval
    # --- auto_approve_level = DESTRUCTIVE ---
    (RiskLevel.DESTRUCTIVE,   RiskLevel.READ_ONLY,   False),  # below ceiling → auto-run
    (RiskLevel.DESTRUCTIVE,   RiskLevel.MODIFYING,   False),  # below ceiling → auto-run
    (RiskLevel.DESTRUCTIVE,   RiskLevel.DESTRUCTIVE, False),  # at ceiling → auto-run
]


@pytest.mark.parametrize(
    "auto_approve_level, risk_level, expected",
    NEEDS_APPROVAL_CASES,
    ids=[
        f"auto={str(a).split('.')[-1] if a else 'None'}_risk={r.name}"
        for a, r, _ in NEEDS_APPROVAL_CASES
    ],
)
def test_needs_approval_truth_table(
    make_step, make_config, auto_approve_level, risk_level, expected
):
    """All 12 combinations of auto_approve_level × risk_level produce the correct result."""
    step = make_step(risk_level=risk_level)
    config = make_config(auto_approve_level=auto_approve_level)
    assert needs_approval(step, config) is expected


def test_needs_approval_unclassified_step_requires_approval(make_step, make_config):
    """A step with no risk_level (not yet classified) requires approval as a safety default."""
    step = make_step(risk_level=None)
    config = make_config(auto_approve_level=RiskLevel.DESTRUCTIVE)  # most permissive config
    assert needs_approval(step, config) is True


# ---------------------------------------------------------------------------
# AuditEntry JSON round-trip
# ---------------------------------------------------------------------------

def _make_audit_entry(**overrides) -> AuditEntry:
    """Build a fully-populated AuditEntry for round-trip tests."""
    defaults = dict(
        seq=1,
        action=ActionType.EXECUTE,
        timestamp="2024-01-15T03:00:00Z",
        step_index=2,
        step_text="Check disk usage",
        command="df -h /",
        risk_level=RiskLevel.READ_ONLY,
        output="Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        50G   20G   28G  42% /",
        stdout="Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        50G   20G   28G  42% /",
        stderr="",
        exit_code=0,
        duration_seconds=0.123,
        approver_slack_id=None,
        reasoning=None,
        mode="live",
        prev_hash="abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
        hash="def456abc123def456abc123def456abc123def456abc123def456abc123def4",
    )
    defaults.update(overrides)
    return AuditEntry(**defaults)


def test_audit_entry_round_trips_through_json():
    """AuditEntry serializes to JSON and deserializes back without losing any data."""
    original = _make_audit_entry()

    json_str = original.model_dump_json()
    restored = AuditEntry.model_validate_json(json_str)

    assert restored == original


def test_audit_entry_round_trip_preserves_all_fields():
    """Every field on AuditEntry survives a JSON round-trip with its exact value."""
    original = _make_audit_entry(
        seq=42,
        action=ActionType.APPROVE,
        timestamp="2024-06-01T12:34:56Z",
        step_index=7,
        step_text="Restart the service",
        command="systemctl restart myapp",
        risk_level=RiskLevel.MODIFYING,
        output="Restarted myapp.service",
        stdout="Restarted myapp.service",
        stderr="",
        exit_code=0,
        duration_seconds=1.5,
        approver_slack_id="U012AB3CD",
        reasoning="Operator approved restart",
        mode="live",
        prev_hash="a" * 64,
        hash="b" * 64,
    )

    data = json.loads(original.model_dump_json())
    restored = AuditEntry(**data)

    assert restored.seq == 42
    assert restored.action == ActionType.APPROVE
    assert restored.timestamp == "2024-06-01T12:34:56Z"
    assert restored.step_index == 7
    assert restored.step_text == "Restart the service"
    assert restored.command == "systemctl restart myapp"
    assert restored.risk_level == RiskLevel.MODIFYING
    assert restored.output == "Restarted myapp.service"
    assert restored.stdout == "Restarted myapp.service"
    assert restored.stderr == ""
    assert restored.exit_code == 0
    assert restored.duration_seconds == 1.5
    assert restored.approver_slack_id == "U012AB3CD"
    assert restored.reasoning == "Operator approved restart"
    assert restored.mode == "live"
    assert restored.prev_hash == "a" * 64
    assert restored.hash == "b" * 64


def test_audit_entry_round_trip_with_none_fields():
    """AuditEntry with optional fields set to None round-trips correctly."""
    original = _make_audit_entry(
        step_index=None,
        step_text=None,
        command=None,
        risk_level=None,
        output=None,
        stdout=None,
        stderr=None,
        exit_code=None,
        duration_seconds=None,
        approver_slack_id=None,
        reasoning=None,
        prev_hash=None,
    )

    json_str = original.model_dump_json()
    restored = AuditEntry.model_validate_json(json_str)

    assert restored == original
    assert restored.step_index is None
    assert restored.prev_hash is None
    assert restored.risk_level is None


def test_audit_entry_round_trip_dry_run_mode():
    """AuditEntry with mode='dry_run' round-trips correctly."""
    original = _make_audit_entry(mode="dry_run")

    json_str = original.model_dump_json()
    restored = AuditEntry.model_validate_json(json_str)

    assert restored.mode == "dry_run"
    assert restored == original


def test_audit_entry_round_trip_all_action_types():
    """AuditEntry round-trips correctly for every ActionType value."""
    for action in ActionType:
        original = _make_audit_entry(action=action)
        json_str = original.model_dump_json()
        restored = AuditEntry.model_validate_json(json_str)
        assert restored.action == action


# ---------------------------------------------------------------------------
# RiskLevel JSON serialization
# ---------------------------------------------------------------------------

def test_risk_level_serializes_to_string_value_in_json():
    """RiskLevel enum values serialize to their string representation in JSON."""
    step = Step(
        index=1,
        text="Check disk space",
        command="df -h",
        risk_level=RiskLevel.READ_ONLY,
    )
    data = json.loads(step.model_dump_json())
    assert data["risk_level"] == "read_only"


def test_risk_level_modifying_serializes_to_string():
    """RiskLevel.MODIFYING serializes to 'modifying' in JSON."""
    step = Step(
        index=1,
        text="Restart service",
        command="systemctl restart myapp",
        risk_level=RiskLevel.MODIFYING,
    )
    data = json.loads(step.model_dump_json())
    assert data["risk_level"] == "modifying"


def test_risk_level_destructive_serializes_to_string():
    """RiskLevel.DESTRUCTIVE serializes to 'destructive' in JSON."""
    step = Step(
        index=1,
        text="Delete old logs",
        command="rm -rf /var/log/old",
        risk_level=RiskLevel.DESTRUCTIVE,
    )
    data = json.loads(step.model_dump_json())
    assert data["risk_level"] == "destructive"


def test_risk_level_in_audit_entry_serializes_to_string():
    """RiskLevel inside an AuditEntry serializes to its string value in JSON."""
    entry = _make_audit_entry(risk_level=RiskLevel.DESTRUCTIVE)
    data = json.loads(entry.model_dump_json())
    assert data["risk_level"] == "destructive"


def test_risk_level_string_value_matches_enum_name_lowercase():
    """Each RiskLevel's string value is the lowercase version of its name."""
    assert RiskLevel.READ_ONLY.value == "read_only"
    assert RiskLevel.MODIFYING.value == "modifying"
    assert RiskLevel.DESTRUCTIVE.value == "destructive"


def test_risk_level_deserializes_from_string():
    """RiskLevel can be deserialized from its string value."""
    step_data = {
        "index": 1,
        "text": "Check disk space",
        "command": "df -h",
        "risk_level": "destructive",
    }
    step = Step(**step_data)
    assert step.risk_level == RiskLevel.DESTRUCTIVE


def test_risk_level_is_str_subclass():
    """RiskLevel is a str enum, so instances compare equal to their string values."""
    assert RiskLevel.READ_ONLY == "read_only"
    assert RiskLevel.MODIFYING == "modifying"
    assert RiskLevel.DESTRUCTIVE == "destructive"
