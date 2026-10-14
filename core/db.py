"""Accès PostgreSQL : moteur, exécution de scripts, upsert idempotent, journal ETL, watermarks."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Integer, MetaData, Table, create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from config.settings import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)
CHUNK_SIZE = 5_000


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True, future=True)


@lru_cache
def _table(schema: str, name: str) -> Table:
    return Table(name, MetaData(), schema=schema, autoload_with=get_engine())


def apply_sql_files(directory: Path = PROJECT_ROOT / "sql") -> None:
    """Applique les scripts DDL (idempotents) dans l'ordre alphabétique."""
    with get_engine().begin() as conn:
        for path in sorted(directory.glob("*.sql")):
            log.info("Exécution de %s", path.name)
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> dicts compatibles SQL (NaN/NaT -> None, numpy -> natif)."""
    clean = df.astype(object).where(pd.notna(df), None)
    out = clean.to_dict(orient="records")
    for row in out:
        for k, v in row.items():
            if isinstance(v, np.generic):
                row[k] = v.item()
    return out


def upsert_dataframe(
    df: pd.DataFrame,
    table: str,
    conflict_cols: Sequence[str],
    schema: str = "pl",
    update_cols: Sequence[str] | None = None,
    hash_col: str | None = "record_hash",
    conn: Connection | None = None,
) -> int:
    """INSERT … ON CONFLICT DO UPDATE par lots.

    - Idempotent : rejouer le même lot ne change rien.
    - Si `hash_col` est présent, la mise à jour n'a lieu que si le contenu a changé
      (évite les écritures inutiles et garde `updated_at` significatif).
    """
    if df.empty:
        return 0
    tbl = _table(schema, table)
    cols = [c for c in df.columns if c in tbl.c]
    dropped = set(df.columns) - set(cols)
    if dropped:
        log.debug("Colonnes ignorées pour %s.%s : %s", schema, table, sorted(dropped))
    # [] = "ne jamais mettre à jour" (DO NOTHING) ; None = toutes les colonnes hors clé
    update_cols = (list(update_cols) if update_cols is not None
                   else [c for c in cols if c not in conflict_cols])

    # Les clés mappées via dict contiennent des NaN => float ; on revient à des entiers nullables
    frame = df[cols].copy()
    for col in cols:
        if isinstance(tbl.c[col].type, Integer) and frame[col].dtype.kind == "f":
            frame[col] = frame[col].round().astype("Int64")

    total = 0

    def _run(c: Connection) -> None:
        nonlocal total
        records = _records(frame)
        # PostgreSQL limite une requête à 65 535 paramètres : lot = f(nombre de colonnes)
        chunk = max(1, min(CHUNK_SIZE, 60_000 // max(1, len(cols))))
        for i in range(0, len(records), chunk):
            stmt = pg_insert(tbl).values(records[i:i + chunk])
            set_ = {col: stmt.excluded[col] for col in update_cols}
            if set_ and "updated_at" in tbl.c and "updated_at" not in set_:
                set_["updated_at"] = text("now()")
            where = None
            if hash_col and hash_col in tbl.c and hash_col in cols:
                where = tbl.c[hash_col].is_distinct_from(stmt.excluded[hash_col])
            if set_:
                stmt = stmt.on_conflict_do_update(index_elements=list(conflict_cols), set_=set_, where=where)
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
            # RETURNING ne renvoie que les lignes réellement insérées/modifiées (rowcount = -1 ici)
            stmt = stmt.returning(tbl.c[conflict_cols[0]])
            total += len(c.execute(stmt).all())

    if conn is not None:
        _run(conn)
    else:
        with get_engine().begin() as c:
            _run(c)
    log.info("Upsert %s.%s : %d lignes insérées/modifiées sur %d", schema, table, total, len(df))
    return total


def read_sql(query: str, params: dict | None = None) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def refresh_materialized_views() -> None:
    with get_engine().begin() as conn:
        for mv in ("pl.mv_league_table", "pl.mv_referee_profile"):
            conn.execute(text(f"REFRESH MATERIALIZED VIEW {mv}"))
    log.info("Vues matérialisées rafraîchies")


# --------------------------------------------------------------------------- meta
@contextmanager
def etl_run(pipeline: str, mode: str, season_id: int | None = None) -> Iterator[dict]:
    """Trace chaque exécution dans meta.etl_runs (succès / échec / compteurs)."""
    with get_engine().begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO meta.etl_runs (pipeline, mode, season_id) VALUES (:p, :m, :s) RETURNING run_id"),
            {"p": pipeline, "m": mode, "s": season_id},
        ).scalar_one()
    stats: dict = {"run_id": run_id, "rows_in": 0, "rows_upserted": 0, "rows_rejected": 0, "status": "success"}
    try:
        yield stats
    except Exception as exc:
        stats["status"], stats["message"] = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        with get_engine().begin() as conn:
            conn.execute(
                text("""UPDATE meta.etl_runs SET finished_at = now(), status = :status,
                        rows_in = :rows_in, rows_upserted = :rows_upserted,
                        rows_rejected = :rows_rejected, message = :message WHERE run_id = :run_id"""),
                {**stats, "message": stats.get("message")},
            )


def save_rejects(run_id: int, source: str, season_id: int | None, rejects: list[dict]) -> None:
    if not rejects:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text("""INSERT INTO meta.rejected_rows (run_id, source, season_id, payload, errors)
                    VALUES (:run_id, :source, :season_id, CAST(:payload AS jsonb), CAST(:errors AS jsonb))"""),
            [{"run_id": run_id, "source": source, "season_id": season_id,
              "payload": json.dumps(r["payload"], default=str), "errors": json.dumps(r["errors"], default=str)}
             for r in rejects],
        )
    log.warning("%d lignes mises en quarantaine (meta.rejected_rows) pour %s", len(rejects), source)


def get_watermark(source: str, season_id: int) -> dict | None:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM meta.etl_watermarks WHERE source = :s AND season_id = :y"),
            {"s": source, "y": season_id},
        ).mappings().first()
    return dict(row) if row else None


def set_watermark(source: str, season_id: int, last_gameweek: int | None = None,
                  last_match_date=None, checksum: str | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("""INSERT INTO meta.etl_watermarks (source, season_id, last_gameweek, last_match_date,
                                                     content_checksum, last_success_at)
                    VALUES (:s, :y, :gw, :d, :c, now())
                    ON CONFLICT (source, season_id) DO UPDATE SET
                        last_gameweek = COALESCE(EXCLUDED.last_gameweek, meta.etl_watermarks.last_gameweek),
                        last_match_date = COALESCE(EXCLUDED.last_match_date, meta.etl_watermarks.last_match_date),
                        content_checksum = COALESCE(EXCLUDED.content_checksum, meta.etl_watermarks.content_checksum),
                        last_success_at = now()"""),
            {"s": source, "y": season_id, "gw": last_gameweek, "d": last_match_date, "c": checksum},
        )
