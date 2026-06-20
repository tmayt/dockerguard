from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    Float,
    Boolean,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///data/guardian.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


class ContainerConfig(Base):
    __tablename__ = "container_configs"

    name = Column(String, primary_key=True)
    priority = Column(Integer, default=5)
    suspended = Column(Boolean, default=False)


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    cpu_threshold = Column(Float, default=80)
    ram_threshold = Column(Float, default=80)
    check_interval = Column(Integer, default=10)


Base.metadata.create_all(bind=engine)