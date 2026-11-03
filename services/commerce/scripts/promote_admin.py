"""CLI скрипт за повишаване на съществуващ потребител до роля 'admin'."""

import argparse
import sys
from pathlib import Path

# Добавяне на корена на commerce към sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from app.db import db
from app.modules.accounts.repository import UserRepository


def promote_user(session: Session, email: str) -> bool:
    """Повишава потребителя с подадения имейл до роля 'admin'.

    Връща True при настъпила промяна, False ако потребителят вече е admin.
    Повдига ValueError при празен имейл или ако потребителят липсва.
    """
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("Имейл адресът не може да бъде празен.")

    user_repo = UserRepository(session)
    user = user_repo.get_by_email(normalized_email)
    if user is None:
        raise ValueError(f"Потребител с имейл '{normalized_email}' не съществува.")

    if user.role == "admin":
        return False

    user.role = "admin"
    session.commit()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Повишаване на съществуващ потребител до роля 'admin'."
    )
    parser.add_argument(
        "email",
        help="Имейл адрес на потребителя, който да бъде повишен до admin",
    )
    args = parser.parse_args()

    with db.session() as session:
        try:
            changed = promote_user(session, args.email)
            normalized_email = args.email.strip().lower()
            if changed:
                print(
                    f"Потребителят '{normalized_email}' беше успешно повишен до роля 'admin'."
                )
            else:
                print(f"Потребителят '{normalized_email}' вече има роля 'admin'.")
        except ValueError as err:
            print(f"Грешка: {err}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

