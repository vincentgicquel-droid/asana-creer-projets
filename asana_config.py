#!/usr/bin/env python3
"""Configuration Asana commune aux scripts : token, workspace, proxy.

Un seul fichier centralise la configuration : config.env. Le module le
cherche dans cet ordre :

1. les variables d'environnement de la session (prioritaires) ;
2. un fichier .env ou config.env a cote du script, puis en remontant
   les dossiers parents (projet, racine de tous les projets...) ;
3. ~/.asana/config.env.

Format du fichier, lignes cle=valeur :

    ASANA_TOKEN=<token compte de service>
    ASANA_WORKSPACE_SANDBOX=<gid sandbox>
    ASANA_WORKSPACE_PROD=<gid prod>
    ASANA_ENV=sandbox            # environnement par defaut : sandbox ou prod
    HTTPS_PROXY=                 # optionnel, proxy d'entreprise

Dans un script :

    from asana_config import add_config_args, get_config

    parser = argparse.ArgumentParser()
    add_config_args(parser)          # --env sandbox|prod
    parser.add_argument("--workspace", default=None)  # surcharge ponctuelle
    args = parser.parse_args()

    cfg = get_config(args)
    cfg["token"]           # token (ASANA_TOKEN ou ASANA_PAT)
    cfg["env"]             # "sandbox" ou "prod"
    cfg["workspace_gid"]   # GID selon l'environnement choisi

Verification rapide sans appel reseau :
    python3 asana_config.py --env prod
    python3 asana_config.py --show-sources
"""

import argparse
import inspect
import os
import sys
from pathlib import Path

ENV_KEYS = ("ASANA_TOKEN", "ASANA_PAT")
CONFIG_NAMES = (".env", "config.env")


def _read_env_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
    except OSError:
        pass
    return values


def _candidate_files(start_dir):
    seen = set()
    start = Path(start_dir).absolute()
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            path = directory / name
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                yield path
    home = Path.home() / ".asana" / "config.env"
    if home.is_file():
        yield home


def merged_values(start_dir):
    merged = {}
    for path in _candidate_files(start_dir):
        for key, value in _read_env_file(path).items():
            if key not in merged and value:
                merged[key] = value
    return merged


def _caller_dir():
    frame = inspect.currentframe()
    try:
        while frame:
            path = frame.f_globals.get("__file__")
            if path and Path(path).name != Path(__file__).name:
                return os.path.dirname(os.path.abspath(path))
            frame = frame.f_back
    finally:
        del frame
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _pick(name, env, merged):
    suffix = name + "_" + env.upper()
    return os.environ.get(suffix) or merged.get(suffix) or os.environ.get(name) or merged.get(name)


def resolve(env=None, script_dir=None, sources=None):
    if script_dir is None:
        script_dir = _caller_dir()
    merged = merged_values(script_dir)

    env = env or os.environ.get("ASANA_ENV") or merged.get("ASANA_ENV") or "sandbox"
    env = env.strip().lower()
    if env not in ("sandbox", "prod"):
        raise ValueError(f"Environnement invalide : {env} (attendu : sandbox ou prod)")

    token = None
    for key in ENV_KEYS:
        token = os.environ.get(key) or merged.get(key)
        if token:
            break

    workspace_gid = _pick("ASANA_WORKSPACE", env, merged)
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or merged.get("HTTPS_PROXY")
        or merged.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or merged.get("HTTP_PROXY")
        or merged.get("http_proxy")
    )

    if sources is not None:
        sources["files"] = [str(p) for p in _candidate_files(script_dir)]

    if proxy and not os.environ.get("HTTPS_PROXY"):
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy

    return {"env": env, "token": token, "workspace_gid": workspace_gid, "proxy": proxy}


def add_config_args(parser, default_env=None):
    parser.add_argument(
        "--env",
        choices=["sandbox", "prod"],
        default=default_env,
        help="Environnement Asana : sandbox ou prod (defaut : ASANA_ENV du config.env, sinon sandbox)",
    )
    return parser


def get_config(args=None, script_dir=None, require_workspace=True):
    env = getattr(args, "env", None) if args is not None else None
    cfg = resolve(env=env, script_dir=script_dir)
    if not cfg["token"]:
        sys.stderr.write(
            "Erreur fatale : token manquant.\n"
            "Renseigner ASANA_TOKEN dans config.env (a cote du script, dans un dossier\n"
            "parent, ou ~/.asana/config.env), ou exporter ASANA_TOKEN=xxx.\n"
        )
        sys.exit(1)
    workspace = getattr(args, "workspace", None) if args is not None else None
    if workspace:
        cfg["workspace_gid"] = workspace
    elif require_workspace and not cfg["workspace_gid"]:
        sys.stderr.write(
            f"Erreur fatale : GID de workspace {cfg['env']} manquant.\n"
            f"Renseigner ASANA_WORKSPACE_{cfg['env'].upper()} dans config.env,\n"
            "ou passer --workspace <gid>.\n"
        )
        sys.exit(1)
    return cfg


def mask(token):
    if not token:
        return "(absent)"
    return token[:4] + "..." + token[-4:] if len(token) > 12 else "***"


def main():
    parser = argparse.ArgumentParser(description="Affiche la configuration Asana resolue.")
    add_config_args(parser)
    parser.add_argument("--show-sources", action="store_true", help="Lister les fichiers de config consultes")
    args = parser.parse_args()
    sources = {} if args.show_sources else None
    cfg = resolve(env=args.env, sources=sources)
    print(f"Environnement    : {cfg['env']}")
    print(f"Token            : {mask(cfg['token'])}")
    print(f"Workspace {cfg['env']:<7}: {cfg['workspace_gid'] or '(absent)'}")
    print(f"Proxy            : {cfg['proxy'] or '(aucun)'}")
    if sources:
        print("Fichiers consultes (du plus proche au plus loin) :")
        for path in sources["files"]:
            print(f"  - {path}")


if __name__ == "__main__":
    main()
