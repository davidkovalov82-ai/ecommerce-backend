"""add_check_constraints_to_products

Revision ID: a0edab4d3e86
Revises: 4f6d091ef23b
Create Date: 2026-10-29 11:42:10.995752

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0edab4d3e86"
down_revision: str | Sequence[str] | None = "4f6d091ef23b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_check_constraint(
        "ck_products_price_minor_non_negative",
        "products",
        "price_minor >= 0",
    )
    op.create_check_constraint(
        "ck_products_stock_non_negative",
        "products",
        "stock >= 0",
    )
    op.create_check_constraint(
        "ck_products_currency_eur",
        "products",
        "currency = 'EUR'",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_products_currency_eur", "products", type_="check")
    op.drop_constraint("ck_products_stock_non_negative", "products", type_="check")
    op.drop_constraint("ck_products_price_minor_non_negative", "products", type_="check")
