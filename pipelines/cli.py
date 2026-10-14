"""Point d'entrée unique : python -m pipelines.cli <commande>

  init-db                    Crée / met à jour le schéma (idempotent)
  backfill [--from] [--to]   Charge l'historique (défaut : 2016-17 -> saison courante)
  incremental                Met à jour la saison en cours
  derive                     Recalcule xG proxy + vues matérialisées
  skills list [--category]   Liste les compétences disponibles
  skills run NAME [--args]   Exécute une compétence (args JSON)
"""
from __future__ import annotations

import argparse
import json
import sys

from core.logging_utils import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pl", description="Premier League Data Platform")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    bf = sub.add_parser("backfill")
    bf.add_argument("--from", dest="start", help="ex. 2016-17")
    bf.add_argument("--to", dest="end", help="ex. 2025-26")
    bf.add_argument("--force", action="store_true", help="re-télécharge et recharge même si inchangé")
    inc = sub.add_parser("incremental")
    inc.add_argument("--force", action="store_true")
    sub.add_parser("derive")

    sk = sub.add_parser("skills")
    sk_sub = sk.add_subparsers(dest="skills_cmd", required=True)
    ls = sk_sub.add_parser("list")
    ls.add_argument("--category", choices=["retrieval", "transform", "load", "analysis", "design"])
    run = sk_sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("--args", default="{}", help='kwargs JSON, ex. \'{"force": true}\'')

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    from pipelines import etl  # import tardif : `--help` reste instantané

    if args.cmd == "init-db":
        from core.db import apply_sql_files
        apply_sql_files()
    elif args.cmd == "backfill":
        etl.backfill(args.start, args.end, args.force)
    elif args.cmd == "incremental":
        etl.incremental(args.force)
    elif args.cmd == "derive":
        etl.run_derived()
    elif args.cmd == "skills":
        from skills.registry import get_skill, list_skills
        if args.skills_cmd == "list":
            for s in list_skills(args.category):
                print(f"{s.category:<10} {s.name:<34} {s.summary}")
        else:
            result = get_skill(args.name)(**json.loads(args.args))
            print(result if result is not None else "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
