"""Order conversation messages by native observations rather than receipt time.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table in ("session_inputs", "session_responses"):
        op.add_column(table, sa.Column("conversation_order", sa.BigInteger(), nullable=True))
        op.create_index(f"ix_{table}_conversation_order", table, ["session_id", "conversation_order", "seq"])

    # Existing output already records native response boundaries and Claude's
    # exact consumed-input receipts. Use those facts once during the upgrade;
    # never reconstruct native order from independent wall clocks.
    op.execute("""
        WITH first_output AS (
            SELECT session_id, response_id, MIN(seq) * 2 AS position
            FROM session_output WHERE response_id IS NOT NULL
              AND part->>'type' NOT IN ('start', 'message-metadata', 'data-input-consumed')
            GROUP BY session_id, response_id
        )
        UPDATE session_responses AS response SET conversation_order = output.position
        FROM first_output AS output
        WHERE response.session_id = output.session_id AND response.response_id = output.response_id
    """)
    op.execute("""
        WITH consumed AS (
            SELECT session_id, part->'data'->>'inputId' AS input_id, MIN(seq) * 2 AS position
            FROM session_output WHERE part->>'type' = 'data-input-consumed'
            GROUP BY session_id, part->'data'->>'inputId'
        )
        UPDATE session_inputs AS input SET conversation_order = consumed.position
        FROM consumed WHERE input.session_id = consumed.session_id AND input.input_id = consumed.input_id
    """)
    # Other adapters declared consumed inputs on their native response boundary.
    # A queued input with no native response/receipt remains unpositioned.
    op.execute("""
        WITH answered AS (
            SELECT link.session_id, link.input_id, MIN(response.conversation_order) - 1 AS position
            FROM session_response_inputs AS link JOIN session_responses AS response
              ON response.session_id = link.session_id AND response.response_id = link.response_id
            WHERE response.conversation_order IS NOT NULL
            GROUP BY link.session_id, link.input_id
        )
        UPDATE session_inputs AS input SET conversation_order = answered.position
        FROM answered WHERE input.session_id = answered.session_id AND input.input_id = answered.input_id
          AND input.conversation_order IS NULL
    """)
    for table in ("session_inputs", "session_responses"):
        op.execute(f"""
            UPDATE {table} SET conversation_order = (import_record->>'position')::bigint
            WHERE import_record IS NOT NULL
        """)


def downgrade() -> None:
    for table in ("session_inputs", "session_responses"):
        op.drop_index(f"ix_{table}_conversation_order", table_name=table)
        op.drop_column(table, "conversation_order")
