"""agent_runs: user_id, input_text, prompt_version, needs_clarification status

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = ("pending_confirmation", "confirmed", "executed", "rejected", "failed")
NEW = ("pending_confirmation", "needs_clarification", "confirmed", "executed", "rejected", "failed")


def _check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE agent_runs SET user_id = vm.user_id FROM voice_messages vm "
        "WHERE agent_runs.voice_message_id = vm.id"
    )
    op.execute("DELETE FROM agent_runs WHERE user_id IS NULL")  # unreachable rows from before this column
    op.alter_column("agent_runs", "user_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_agent_runs_user_id_users"), "agent_runs", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(op.f("ix_agent_runs_user_id"), "agent_runs", ["user_id"])
    op.add_column("agent_runs", sa.Column("input_text", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("prompt_version", sa.String(length=20), nullable=True))

    op.drop_constraint(op.f("ck_agent_runs_run_status"), "agent_runs", type_="check")
    op.create_check_constraint(op.f("ck_agent_runs_run_status"), "agent_runs", _check(NEW))


def downgrade() -> None:
    op.execute("UPDATE agent_runs SET status = 'rejected' WHERE status = 'needs_clarification'")
    op.drop_constraint(op.f("ck_agent_runs_run_status"), "agent_runs", type_="check")
    op.create_check_constraint(op.f("ck_agent_runs_run_status"), "agent_runs", _check(OLD))
    op.drop_column("agent_runs", "prompt_version")
    op.drop_column("agent_runs", "input_text")
    op.drop_index(op.f("ix_agent_runs_user_id"), table_name="agent_runs")
    op.drop_constraint(op.f("fk_agent_runs_user_id_users"), "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "user_id")
