from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.infrastructure.postgres import build_postgres_runtime_boundary, create_app_engine

database_boundary = build_postgres_runtime_boundary(settings)
engine = create_app_engine(database_boundary)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency: yields a database session."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
