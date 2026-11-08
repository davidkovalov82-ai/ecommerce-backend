from sqlalchemy import Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    """SQLAlchemy ORM модел за таблицата с потребители."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="customer")
    display_name: Mapped[str | None] = mapped_column(
        String(80), nullable=True, default=None
    )
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
