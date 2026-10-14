"""Planificateur minimal (sans dépendance) pour le conteneur `scheduler`.

Cadence adaptative :
  - jour de match (une fixture FPL commence dans ±24 h) : toutes les 2 h ;
  - sinon : toutes les 12 h (capture prix / blessures / transferts).
En production, préférer un orchestrateur (cron, Airflow, Dagster, Prefect) appelant la même CLI.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from core.logging_utils import setup_logging

log = logging.getLogger("scheduler")
MATCHDAY_INTERVAL = timedelta(hours=2)
QUIET_INTERVAL = timedelta(hours=12)


def _is_matchday() -> bool:
    from core.db import read_sql
    now = datetime.now(timezone.utc)
    df = read_sql("""SELECT 1 FROM pl.fact_matches
                     WHERE kickoff_at BETWEEN :lo AND :hi LIMIT 1""",
                  {"lo": now - timedelta(hours=24), "hi": now + timedelta(hours=24)})
    return not df.empty


def main() -> None:
    setup_logging()
    from core.db import apply_sql_files
    from pipelines import etl

    apply_sql_files()
    while True:
        started = time.monotonic()
        try:
            etl.incremental()
        except Exception:  # le planificateur ne doit jamais mourir sur une erreur transitoire
            log.exception("Échec de la mise à jour incrémentale — nouvel essai au prochain cycle")
        try:
            interval = MATCHDAY_INTERVAL if _is_matchday() else QUIET_INTERVAL
        except Exception:
            interval = MATCHDAY_INTERVAL
        sleep_s = max(60.0, interval.total_seconds() - (time.monotonic() - started))
        log.info("Prochaine exécution dans %.0f min", sleep_s / 60)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
