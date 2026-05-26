import json
import logging
import os
from pathlib import Path
import psycopg2

logger = logging.getLogger(__name__)

# Postgres Connection Parameters
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "bidbot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")


def get_connection():
    """Establishes and returns a connection to the PostgreSQL database."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )


def create_tables():
    """Creates the normalized relational tables if they do not exist."""
    queries = [
        """
        CREATE TABLE IF NOT EXISTS tenders (
            id UUID PRIMARY KEY,
            title TEXT,
            description TEXT,
            reference_number TEXT,
            contract_nature TEXT,
            scraper_url TEXT,
            created_at TIMESTAMPTZ,
            publication_date TIMESTAMPTZ,
            submitting_offers_deadline TIMESTAMPTZ,
            industry TEXT,
            nuts3 TEXT[],
            cpv_codes TEXT[],
            llm_extracted BOOLEAN DEFAULT FALSE,
            llm_tytul TEXT,
            llm_zamawiajacy TEXT,
            llm_budzet TEXT,
            llm_deadline TEXT,
            llm_miejsce_realizacji TEXT,
            llm_wymagania_techniczne TEXT[],
            llm_kryteria_oceny TEXT[],
            llm_wymagane_dokumenty TEXT[],
            llm_ryzyka TEXT[]
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS issuers (
            id SERIAL PRIMARY KEY,
            title TEXT UNIQUE NOT NULL,
            street TEXT,
            city TEXT,
            postal_code TEXT,
            country TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS tender_issuers (
            tender_id UUID REFERENCES tenders(id) ON DELETE CASCADE,
            issuer_id INT REFERENCES issuers(id) ON DELETE CASCADE,
            PRIMARY KEY (tender_id, issuer_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            tender_id UUID REFERENCES tenders(id) ON DELETE CASCADE,
            url TEXT,
            filename TEXT,
            local_path TEXT,
            downloaded BOOLEAN,
            is_zip BOOLEAN,
            extracted_status TEXT,
            UNIQUE (tender_id, filename)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS tender_tags (
            tender_id UUID REFERENCES tenders(id) ON DELETE CASCADE,
            tag TEXT,
            PRIMARY KEY (tender_id, tag)
        );
        """
    ]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for query in queries:
                cur.execute(query)
        conn.commit()
    finally:
        conn.close()


def save_tender_json(data: dict):
    """
    Saves a parsed scraper tender JSON dictionary to the normalized PostgreSQL schema.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1. Upsert main tenders row
            tender_id = data["id"]
            title = data.get("title")
            description = data.get("description")
            reference_number = data.get("referenceNumber")
            contract_nature = data.get("contractNature")
            scraper_url = data.get("scraper_url")
            created_at = data.get("createdAt")
            publication_date = data.get("publicationDate")
            submitting_offers_deadline = data.get("submittingOffersDeadline")
            
            enrichment = data.get("enrichment", {}) or {}
            industry = enrichment.get("industry")
            nuts3 = enrichment.get("nuts3", [])
            cpv_codes = data.get("cpvCodes", [])

            cur.execute(
                """
                INSERT INTO tenders (
                    id, title, description, reference_number, contract_nature, scraper_url, 
                    created_at, publication_date, submitting_offers_deadline, industry, nuts3, cpv_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    reference_number = EXCLUDED.reference_number,
                    contract_nature = EXCLUDED.contract_nature,
                    scraper_url = EXCLUDED.scraper_url,
                    created_at = EXCLUDED.created_at,
                    publication_date = EXCLUDED.publication_date,
                    submitting_offers_deadline = EXCLUDED.submitting_offers_deadline,
                    industry = EXCLUDED.industry,
                    nuts3 = EXCLUDED.nuts3,
                    cpv_codes = EXCLUDED.cpv_codes;
                """,
                (tender_id, title, description, reference_number, contract_nature, scraper_url, 
                 created_at, publication_date, submitting_offers_deadline, industry, nuts3, cpv_codes)
            )

            # 2. Insert tags
            tags = enrichment.get("tags", [])
            if tags:
                for tag in tags:
                    cur.execute(
                        """
                        INSERT INTO tender_tags (tender_id, tag)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING;
                        """,
                        (tender_id, tag)
                    )

            # 3. Insert issuers and relationship
            issuers = data.get("issuers", [])
            if issuers:
                for issuer in issuers:
                    iss_title = issuer.get("title")
                    if not iss_title:
                        continue
                    
                    addr = issuer.get("address", {}) or {}
                    street = addr.get("street")
                    city = addr.get("city")
                    postal_code = addr.get("postalCode")
                    country = addr.get("country")

                    # Upsert issuer
                    cur.execute(
                        """
                        INSERT INTO issuers (title, street, city, postal_code, country)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (title) DO UPDATE SET
                            street = EXCLUDED.street,
                            city = EXCLUDED.city,
                            postal_code = EXCLUDED.postal_code,
                            country = EXCLUDED.country
                        RETURNING id;
                        """,
                        (iss_title, street, city, postal_code, country)
                    )
                    issuer_id = cur.fetchone()[0]

                    # Link with tender
                    cur.execute(
                        """
                        INSERT INTO tender_issuers (tender_id, issuer_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING;
                        """,
                        (tender_id, issuer_id)
                    )

            # 4. Insert scraper attachments
            attachments = data.get("scraper_attachments", [])
            if attachments:
                for att in attachments:
                    url = att.get("url")
                    filename = att.get("filename")
                    local_path = att.get("local_path")
                    downloaded = att.get("downloaded", False)
                    is_zip = att.get("is_zip", False)
                    extracted_status = att.get("extracted_status")

                    cur.execute(
                        """
                        INSERT INTO attachments (tender_id, url, filename, local_path, downloaded, is_zip, extracted_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tender_id, filename) DO UPDATE SET
                            url = EXCLUDED.url,
                            local_path = EXCLUDED.local_path,
                            downloaded = EXCLUDED.downloaded,
                            is_zip = EXCLUDED.is_zip,
                            extracted_status = EXCLUDED.extracted_status;
                        """,
                        (tender_id, url, filename, local_path, downloaded, is_zip, extracted_status)
                    )

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def save_llm_data(tender_id: str, tender_data) -> bool:
    """
    Updates a tender record in PostgreSQL with LLM-extracted structured metadata.
    Supports Pydantic BaseModel as well as raw dictionaries.
    """
    if hasattr(tender_data, "model_dump"):
        data = tender_data.model_dump()
    elif hasattr(tender_data, "dict"):
        data = tender_data.dict()
    else:
        data = tender_data

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tenders SET
                    llm_extracted = TRUE,
                    llm_tytul = %s,
                    llm_zamawiajacy = %s,
                    llm_budzet = %s,
                    llm_deadline = %s,
                    llm_miejsce_realizacji = %s,
                    llm_wymagania_techniczne = %s,
                    llm_kryteria_oceny = %s,
                    llm_wymagane_dokumenty = %s,
                    llm_ryzyka = %s
                WHERE id = %s;
                """,
                (
                    data.get("tytul"),
                    data.get("zamawiajacy"),
                    data.get("budzet"),
                    data.get("deadline"),
                    data.get("miejsce_realizacji"),
                    data.get("wymagania_techniczne", []),
                    data.get("kryteria_oceny", []),
                    data.get("wymagane_dokumenty", []),
                    data.get("ryzyka", []),
                    tender_id
                )
            )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving LLM data to postgres for tender {tender_id}: {e}")
        return False
    finally:
        conn.close()


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
