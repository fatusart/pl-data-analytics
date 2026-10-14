"""Extraction de l'API officielle FPL (saison en cours).

Stratégie incrémentale :
  1. `bootstrap-static` + `fixtures` à chaque exécution (légers, ~2 requêtes) ;
     un snapshot daté de bootstrap alimente fact_fpl_player_snapshot (prix, forme, % sélection).
  2. `element-summary/{id}` (≈ 700+ requêtes) UNIQUEMENT si une nouvelle gameweek s'est
     terminée depuis le dernier chargement (watermark), ou si force=True.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from config.settings import get_settings
from core.http import HttpError, client_for
from core.seasons import current_season
from extractors.base import raw_path, read_json, write_json
from skills.registry import skill

log = logging.getLogger(__name__)
SOURCE = "fpl_api"


def last_finished_gameweek(bootstrap: dict) -> int:
    finished = [e["id"] for e in bootstrap.get("events", []) if e.get("finished") and e.get("data_checked")]
    return max(finished, default=0)


@skill("retrieval.fpl_api", "retrieval",
       "API FPL officielle : bootstrap, fixtures, historiques joueurs (incrémental par GW)",
       tags=("incremental", "current-season"))
def extract_fpl_current(last_loaded_gw: int | None = None, force: bool = False,
                        max_workers: int = 4) -> dict[str, Path | int | None]:
    eps = get_settings().sources[SOURCE]["endpoints"]
    client = client_for(SOURCE)
    season = current_season()

    bootstrap = client.get_json(eps["bootstrap"])
    bootstrap_path = write_json(raw_path(SOURCE, season, "bootstrap_static.json"), bootstrap)
    write_json(raw_path(SOURCE, season, f"snapshots/bootstrap_{date.today():%Y%m%d}.json"), bootstrap)
    fixtures_path = write_json(raw_path(SOURCE, season, "fixtures.json"), client.get_json(eps["fixtures"]))

    finished_gw = last_finished_gameweek(bootstrap)
    log.info("FPL %s : dernière GW terminée = %s (dernière chargée = %s)", season, finished_gw, last_loaded_gw)

    summaries_dir = raw_path(SOURCE, season, "element_summary")
    needs_refresh = force or last_loaded_gw is None or finished_gw > last_loaded_gw
    if needs_refresh and finished_gw > 0:
        element_ids = [p["id"] for p in bootstrap["elements"]]
        failures = _fetch_element_summaries(client, eps["element_summary"], element_ids, summaries_dir, max_workers)
        if failures:
            # On n'avance pas le watermark si des joueurs manquent : la prochaine exécution réessaiera.
            log.error("%d element-summary en échec : %s…", len(failures), failures[:10])
            finished_gw = last_loaded_gw or 0
    else:
        log.info("Aucune nouvelle GW terminée : historiques joueurs non re-téléchargés")

    return {"bootstrap": bootstrap_path, "fixtures": fixtures_path,
            "element_summary_dir": summaries_dir, "finished_gw": finished_gw}


def _fetch_element_summaries(client, template: str, element_ids: list[int], dest_dir: Path,
                             max_workers: int) -> list[int]:
    failures: list[int] = []

    def fetch(eid: int) -> int:
        payload = client.get_json(template.format(element_id=eid))
        # On ne garde que l'historique de la saison (history_past = agrégats des saisons passées)
        write_json(dest_dir / f"{eid}.json", {"element": eid, "history": payload.get("history", []),
                                              "fixtures": payload.get("fixtures", [])})
        return eid

    # Le TokenBucket est partagé : le parallélisme masque la latence sans dépasser le débit.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch, eid): eid for eid in element_ids}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                fut.result()
            except HttpError as exc:
                failures.append(futures[fut])
                log.warning("element-summary %s : %s", futures[fut], exc)
            if i % 100 == 0:
                log.info("element-summary : %d/%d", i, len(element_ids))
    return failures


def load_bootstrap(path: Path | None = None) -> dict:
    return read_json(path or raw_path(SOURCE, current_season(), "bootstrap_static.json"))
