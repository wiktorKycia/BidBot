import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    create_engine,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "bidbot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

DATABASE_URL = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Tender(Base):
    __tablename__ = "tenders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    title: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    reference_number: Mapped[str | None] = mapped_column(String)
    contract_nature: Mapped[str | None] = mapped_column(String)
    scraper_url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitting_offers_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    industry: Mapped[str | None] = mapped_column(String)
    nuts3: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    cpv_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    llm_extracted: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_tytul: Mapped[str | None] = mapped_column(String)
    llm_zamawiajacy: Mapped[str | None] = mapped_column(String)
    llm_budzet: Mapped[str | None] = mapped_column(String)
    llm_deadline: Mapped[str | None] = mapped_column(String)
    llm_miejsce_realizacji: Mapped[str | None] = mapped_column(String)
    llm_wymagania_techniczne: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    llm_kryteria_oceny: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    llm_wymagane_dokumenty: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    llm_ryzyka: Mapped[list[str] | None] = mapped_column(ARRAY(String))


class Issuer(Base):
    __tablename__ = "issuers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    street: Mapped[str | None] = mapped_column(String)
    city: Mapped[str | None] = mapped_column(String)
    postal_code: Mapped[str | None] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String)


class TenderIssuer(Base):
    __tablename__ = "tender_issuers"

    tender_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), primary_key=True)
    issuer_id: Mapped[int] = mapped_column(Integer, ForeignKey("issuers.id", ondelete="CASCADE"), primary_key=True)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("tender_id", "filename", name="unique_tender_filename"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tender_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str | None] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    local_path: Mapped[str | None] = mapped_column(String)
    downloaded: Mapped[bool | None] = mapped_column(Boolean)
    is_zip: Mapped[bool | None] = mapped_column(Boolean)
    extracted_status: Mapped[str | None] = mapped_column(String)


class TenderTag(Base):
    __tablename__ = "tender_tags"

    tender_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True)


def create_tables():
    Base.metadata.create_all(bind=engine)


def save_tender_json(data: dict):
    session = SessionLocal()
    try:
        tender_id = data["id"]

        stmt = pg_insert(Tender).values(
            id=tender_id,
            title=data.get("title"),
            description=data.get("description"),
            reference_number=data.get("referenceNumber"),
            contract_nature=data.get("contractNature"),
            scraper_url=data.get("scraper_url"),
            created_at=data.get("createdAt"),
            publication_date=data.get("publicationDate"),
            submitting_offers_deadline=data.get("submittingOffersDeadline"),
            industry=data.get("enrichment", {}).get("industry") if data.get("enrichment") else None,
            nuts3=data.get("enrichment", {}).get("nuts3", []) if data.get("enrichment") else [],
            cpv_codes=data.get("cpvCodes", []),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Tender.id],
            set_={
                "title": stmt.excluded.title,
                "description": stmt.excluded.description,
                "reference_number": stmt.excluded.reference_number,
                "contract_nature": stmt.excluded.contract_nature,
                "scraper_url": stmt.excluded.scraper_url,
                "created_at": stmt.excluded.created_at,
                "publication_date": stmt.excluded.publication_date,
                "submitting_offers_deadline": stmt.excluded.submitting_offers_deadline,
                "industry": stmt.excluded.industry,
                "nuts3": stmt.excluded.nuts3,
                "cpv_codes": stmt.excluded.cpv_codes,
            },
        )
        session.execute(stmt)

        enrichment = data.get("enrichment", {}) or {}
        tags = enrichment.get("tags", [])
        if tags:
            for tag in tags:
                stmt_tag = pg_insert(TenderTag).values(tender_id=tender_id, tag=tag)
                stmt_tag = stmt_tag.on_conflict_do_nothing(index_elements=[TenderTag.tender_id, TenderTag.tag])
                session.execute(stmt_tag)

        issuers = data.get("issuers", [])
        if issuers:
            for issuer in issuers:
                iss_title = issuer.get("title")
                if not iss_title:
                    continue
                addr = issuer.get("address", {}) or {}
                stmt_iss = pg_insert(Issuer).values(
                    title=iss_title,
                    street=addr.get("street"),
                    city=addr.get("city"),
                    postal_code=addr.get("postalCode"),
                    country=addr.get("country"),
                )
                stmt_iss = stmt_iss.on_conflict_do_update(
                    index_elements=[Issuer.title],
                    set_={
                        "street": stmt_iss.excluded.street,
                        "city": stmt_iss.excluded.city,
                        "postal_code": stmt_iss.excluded.postal_code,
                        "country": stmt_iss.excluded.country,
                    },
                ).returning(Issuer.id)
                issuer_id = session.execute(stmt_iss).scalar_one()

                stmt_link = pg_insert(TenderIssuer).values(tender_id=tender_id, issuer_id=issuer_id)
                stmt_link = stmt_link.on_conflict_do_nothing(index_elements=[TenderIssuer.tender_id, TenderIssuer.issuer_id])
                session.execute(stmt_link)

        attachments = data.get("scraper_attachments", [])
        if attachments:
            for att in attachments:
                stmt_att = pg_insert(Attachment).values(
                    tender_id=tender_id,
                    url=att.get("url"),
                    filename=att.get("filename"),
                    local_path=att.get("local_path"),
                    downloaded=att.get("downloaded", False),
                    is_zip=att.get("is_zip", False),
                    extracted_status=att.get("extracted_status"),
                )
                stmt_att = stmt_att.on_conflict_do_update(
                    index_elements=[Attachment.tender_id, Attachment.filename],
                    set_={
                        "url": stmt_att.excluded.url,
                        "local_path": stmt_att.excluded.local_path,
                        "downloaded": stmt_att.excluded.downloaded,
                        "is_zip": stmt_att.excluded.is_zip,
                        "extracted_status": stmt_att.excluded.extracted_status,
                    },
                )
                session.execute(stmt_att)

        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def save_llm_data(tender_id: str, tender_data) -> bool:
    if hasattr(tender_data, "model_dump"):
        data = tender_data.model_dump()
    elif hasattr(tender_data, "dict"):
        data = tender_data.dict()
    else:
        data = tender_data

    session = SessionLocal()
    try:
        stmt = (
            update(Tender)
            .where(Tender.id == tender_id)
            .values(
                llm_extracted=True,
                llm_tytul=data.get("tytul"),
                llm_zamawiajacy=data.get("zamawiajacy"),
                llm_budzet=data.get("budzet"),
                llm_deadline=data.get("deadline"),
                llm_miejsce_realizacji=data.get("miejsce_realizacji"),
                llm_wymagania_techniczne=data.get("wymagania_techniczne", []),
                llm_kryteria_oceny=data.get("kryteria_oceny", []),
                llm_wymagane_dokumenty=data.get("wymagane_dokumenty", []),
                llm_ryzyka=data.get("ryzyka", []),
            )
        )
        session.execute(stmt)
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving LLM data to postgres for tender {tender_id}: {e}")
        return False
    finally:
        session.close()


def main():
    logging.basicConfig(level=logging.INFO)
    logger.info("Initializing PostgreSQL relational schema...")
    try:
        create_tables()
        logger.info("Tables created successfully.")
    except Exception as e:
        logger.error(f"Failed to create tables: {e}")
        return

    parsed_dir = Path(__file__).resolve().parent.parent / "data" / "parsed"
    if not parsed_dir.exists():
        logger.warning(f"Parsed directory {parsed_dir} does not exist. Skipping initial import.")
        return

    logger.info(f"Scanning parsed JSON files in {parsed_dir}...")
    json_files = list(parsed_dir.glob("*.json"))
    logger.info(f"Found {len(json_files)} files to import.")

    success_count = 0
    for file_path in json_files:
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            save_tender_json(data)
            success_count += 1
        except Exception as e:
            logger.error(f"Error importing {file_path.name}: {e}")

    logger.info(f"Successfully imported {success_count}/{len(json_files)} tender records into PostgreSQL.")


if __name__ == "__main__":
    main()
