"""Idempotent seed: the owner plus two sample contacts."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Contact, User
from app.db.session import SessionLocal

SAMPLE_CONTACTS = [
    {"name": "علی رضایی", "aliases": ["علی", "Ali", "Ali Rezaei"], "email": "ali.example@example.com"},
    {"name": "سارا محمدی", "aliases": ["سارا", "Sara", "Sara Mohammadi"], "email": "sara.example@example.com"},
]


def seed(session: Session) -> None:
    settings = get_settings()
    owner_ids = sorted(settings.allowed_ids)
    if not owner_ids:
        raise SystemExit("ALLOWED_USER_IDS is empty; cannot seed the owner.")

    owner = session.scalar(select(User).where(User.telegram_user_id == owner_ids[0]))
    if owner is None:
        owner = User(
            telegram_user_id=owner_ids[0],
            name=settings.owner_name,
            email=settings.owner_email or None,
            timezone=settings.default_timezone,
        )
        session.add(owner)
        session.flush()

    for data in SAMPLE_CONTACTS:
        exists = session.scalar(
            select(Contact).where(Contact.user_id == owner.id, Contact.name == data["name"])
        )
        if exists is None:
            session.add(Contact(user_id=owner.id, **data))
    session.commit()


def main() -> None:
    with SessionLocal() as session:
        seed(session)
    print("Seeded owner and sample contacts.")


if __name__ == "__main__":
    main()
