#!/usr/bin/env python3
"""
Creation en masse de projets Asana depuis un CSV export, ajout au
portfolio cible, puis alimentation des champs personnalises.

Le CSV (une ligne = un projet) est un export Asana : colonnes natives
(NAME, START DATE, DUE DATE, TEAM, URL...) et colonnes de champs
personnalises. Les projets crees sont ajoutes au portfolio pour
recevoir ses champs, puis remplis depuis le CSV selon mapping.json.

Deroulement par ligne :
1. rapprochement avec les projets deja dans le portfolio (GID extrait
   de la colonne URL, puis PRJ_CODE, puis nom) : deja present = pas de
   creation, champs mis a jour seulement ;
2. creation : sans modele par defaut, ou depuis un modele de projet
   (colonne du modele renseignee : nom ou GID, instanciation asynchrone
   via un job Asana) ;
3. ajout au portfolio : les champs personnalises du portfolio se
   rattachent au projet ;
4. ecriture des champs personnalises (mapping) : nombres, textes,
   dates, enum (option retrouvee par son nom), people (email utilisateur
   resolu en GID), multi-enum (valeurs separees par , ou ;).

Securite : dry-run par defaut (--apply pour ecrire), --limit pour
tester sur quelques lignes, --sans-champs pour creer sans remplir,
reprise possible (le script est idempotent).

Usage :
    # lister les modeles de projet du workspace (lecture seule)
    python3 creer-projets.py --list-templates

    # inspecter le portfolio : projets + champs personnalises (lecture seule)
    python3 creer-projets.py --scan-portfolio --portfolio <gid|nom>

    # simuler le passage complet (lectures API, aucune ecriture)
    python3 creer-projets.py --input export.csv --portfolio <gid|nom> --team <gid|nom> --fields mapping.json

    # executer reellement
    python3 creer-projets.py --input export.csv --portfolio <gid|nom> --team <gid|nom> --fields mapping.json --apply

Options principales :
    --input <chemin>        CSV source (separateur "," par defaut, export Asana)
    --portfolio <gid|nom>   Portfolio cible (GID ou nom exact)
    --team <gid|nom>        Equipe par defaut si la colonne TEAM est vide
    --privacy <valeur>      public_to_workspace (defaut), private_to_team, private
    --fields <mapping.json> Mapping colonne CSV -> champ Asana (nom ou GID)
    --col-nom <nom>         Colonne du nom de projet (defaut "NAME")
    --col-code <nom>        Colonne du code projet (defaut "PRJ_CODE")
    --col-modele <nom>      Colonne du modele de projet (defaut "Modèle", vide = sans modele)
    --col-debut <nom>       Colonne date de debut (defaut "START DATE")
    --col-fin <nom>         Colonne date d'echeance (defaut "DUE DATE")
    --col-team <nom>        Colonne equipe (defaut "TEAM")
    --limit <n>             Traiter les n premieres lignes
    --pause <s>             Pause entre deux ecritures (defaut 0.5)
    --job-timeout <s>       Attente max d'une instanciation de modele (defaut 900)
    --sans-creation         Ne rien creer : traiter seulement les projets existants
    --sans-champs           Creer sans ecrire les champs personnalises
    --rapport <chemin>      Rapport JSON du run (statistiques + statut par ligne)
    --apply                 Effectuer les ecritures (defaut : dry-run)
    --ipv4                  Forcer IPv4 pour les appels API (liste blanche IP)
"""

import argparse
import csv
import json
import os
import re
import socket
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from asana_config import add_config_args, get_config

API = "https://app.asana.com/api/1.0"

# Colonnes natives d'un export Asana : jamais ecrites dans les champs.
NATIVE_COLS = {
    "NAME", "TYPE", "TEAM", "URL", "DESCRIPTION", "OWNER", "STATUS",
    "LATEST STATUS UPDATE TITLE", "LATEST STATUS UPDATE", "POSTED BY",
    "POSTED ON", "OVERDUE TASKS", "COMPLETED TASKS", "INCOMPLETE TASKS",
    "PROGRESS", "START DATE", "DUE DATE", "CONNECTED GOALS", "NOTES",
    "PERMALINK URL", "CREATED ON", "COMPLETED ON", "MODIFIED ON",
}

URL_PROJECT_RE = re.compile(r"/project/(\d+)")


def parse_number(raw):
    if raw is None:
        return None
    text = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return None
    return float(text.replace(",", "."))


def to_iso_date(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9%]+", " ", text.lower()).strip()


def extract_project_gid(url):
    """GID du projet extrait d'une URL Asana, sinon None."""
    if not url:
        return None
    match = URL_PROJECT_RE.search(str(url))
    return match.group(1) if match else None


def valid_dates(start, due):
    """(start, due) retenus : start_on exige due_on et doit le preceder."""
    if start and not due:
        return None, due, "date de debut ignoree : Asana exige une date d'echeance avec une date de debut"
    if start and due and start > due:
        return None, None, "dates ignorees : debut apres echeance"
    return start, due, None


def read_rows(path, sep):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=sep)
        return list(reader), reader.fieldnames or []


def source_column(row, col):
    """Tolere espaces/underscores, casse, accents et emojis dans les en-tetes."""
    if col in row:
        return col
    matches = [key for key in row if normalize(key) == normalize(col)]
    if len(matches) > 1:
        raise RuntimeError(f"Colonne CSV ambigue pour {col} : {', '.join(matches)}")
    return matches[0] if matches else None


def build_opener():
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def force_ipv4():
    """Utilise l'adresse IPv4 autorisee par le filtrage reseau."""
    original = socket.getaddrinfo

    def ipv4_addresses(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_addresses


OPENER = None


def api_fetch(path, pat, method="GET", body=None):
    global OPENER
    if OPENER is None:
        OPENER = build_opener()
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": "Bearer " + pat,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    for attempt in range(1, 7):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with OPENER.open(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                retry_after = int(exc.headers.get("Retry-After") or min(2 ** attempt, 30))
                sys.stderr.write(f"  ... HTTP {exc.code}, nouvelle tentative dans {retry_after}s\n")
                time.sleep(retry_after)
                continue
            payload = {}
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                pass
            msg = "; ".join(e.get("message", "") for e in payload.get("errors", [])) or str(exc)
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            if attempt < 6:
                sys.stderr.write(f"  ... erreur reseau ({exc}), nouvelle tentative\n")
                time.sleep(min(2 ** attempt, 30))
                continue
            raise RuntimeError(f"{method} {path} -> erreur reseau : {exc}")
    raise RuntimeError(f"{method} {path} -> echecs repetes (429/5xx)")


def get_all(path, pat):
    base, _, query = path.partition("?")
    params = dict(urllib.parse.parse_qs(query))
    out = []
    offset = None
    while True:
        params["offset"] = [offset] if offset else []
        qs = urllib.parse.urlencode(params, doseq=True)
        payload = api_fetch(f"{base}?{qs}", pat)
        out.extend(payload.get("data") or [])
        nxt = payload.get("next_page")
        offset = nxt.get("offset") if nxt else None
        if not offset:
            break
    return out


DETAIL_FIELDS = (
    "name,permalink_url,custom_fields,custom_fields.name,custom_fields.gid,"
    "custom_fields.resource_subtype,custom_fields.display_value,"
    "custom_fields.text_value,custom_fields.number_value,"
    "custom_fields.date_value,custom_fields.enum_value,"
    "custom_fields.enum_value.gid,custom_fields.enum_value.name,"
    "custom_fields.multi_enum_values,custom_fields.multi_enum_values.gid,"
    "custom_fields.multi_enum_values.name,"
    "custom_fields.people_value,custom_fields.people_value.gid,"
    "custom_fields.people_value.name,"
    "custom_fields.enum_options,custom_fields.enum_options.gid,"
    "custom_fields.enum_options.name,custom_fields.enum_options.enabled"
)


def portfolio_items(pat, gid):
    return get_all(
        f"/portfolios/{gid}/items?limit=100&opt_fields=name,permalink_url",
        pat,
    )


def project_details(pat, project_gid):
    payload = api_fetch(
        f"/projects/{project_gid}?opt_fields={DETAIL_FIELDS}",
        pat,
    )
    return payload["data"]


def add_to_portfolio(pat, portfolio_gid, project_gid):
    api_fetch(
        f"/portfolios/{portfolio_gid}/addItem",
        pat,
        method="POST",
        body={"data": {"item": project_gid}},
    )


def collect_field_defs(details_list):
    """Definitions des champs vus sur les projets, options d'enum incluses."""
    defs = {}
    for details in details_list:
        for cf in details.get("custom_fields") or []:
            current = defs.get(cf["gid"]) or {
                "gid": cf["gid"],
                "name": cf.get("name") or "?",
                "subtype": cf.get("resource_subtype") or "text",
                "options": {},
            }
            for option in cf.get("enum_options") or []:
                current["options"][normalize(option.get("name") or "")] = option["gid"]
                if str(option.get("gid")):
                    current["options"][str(option["gid"])] = option["gid"]
            defs[cf["gid"]] = current
    return list(defs.values())


def find_field(field_defs, gid):
    for field in field_defs:
        if field["gid"] == gid:
            return field
    return None


def cf_value(cf):
    if cf.get("number_value") is not None:
        return cf["number_value"]
    text = cf.get("text_value")
    if text is not None:
        return text
    enum_value = cf.get("enum_value")
    if isinstance(enum_value, dict):
        return enum_value.get("name") or enum_value.get("gid")
    values = cf.get("multi_enum_values")
    if isinstance(values, list) and values:
        return ", ".join(v.get("name") or v.get("gid") or "" for v in values if isinstance(v, dict))
    people = cf.get("people_value")
    if isinstance(people, list) and people:
        return ", ".join(v.get("name") or v.get("gid") or "" for v in people if isinstance(v, dict))
    return cf.get("display_value")


class UserCache:
    """Resolution email -> GID utilisateur du workspace, chargee une fois."""

    def __init__(self, pat, workspace):
        self.pat = pat
        self.workspace = workspace
        self._by_email = None

    def _load(self):
        if self._by_email is not None:
            return
        users = get_all(
            f"/workspaces/{self.workspace}/users?limit=100&opt_fields=name,email",
            self.pat,
        )
        self._by_email = {}
        for user in users:
            email = (user.get("email") or "").strip().casefold()
            if email:
                self._by_email[email] = user["gid"]

    def gid_for_email(self, email):
        self._load()
        return self._by_email.get(str(email).strip().casefold())


def build_payload(row, mapping, field_defs, user_cache):
    """Payload custom_fields depuis une ligne CSV et le mapping resolu.

    Retourne (payload, alertes) : un champ impossible a remplir est
    signale dans alertes et ignore, sans bloquer les autres.
    """
    payload = {}
    alertes = []
    for col, gid in mapping.items():
        source = source_column(row or {}, col)
        if source is None:
            # colonne absente du CSV : signalee une seule fois au niveau du run
            continue
        raw = row.get(source)
        if raw is None or str(raw).strip() == "":
            continue
        field = find_field(field_defs, gid) or {"subtype": "text", "options": {}}
        subtype = field.get("subtype") or "text"
        if subtype == "number":
            value = parse_number(raw)
            if value is None:
                alertes.append(f"{col} : valeur non numerique {raw!r}, ignoree")
                continue
            payload[gid] = round(value, 4)
        elif subtype == "date":
            iso = to_iso_date(raw)
            if not iso:
                alertes.append(f"{col} : date illisible {raw!r}, ignoree")
                continue
            payload[gid] = {"date": iso}
        elif subtype == "enum":
            option_gid = field.get("options", {}).get(normalize(str(raw))) or (
                str(raw).strip() if str(raw).strip().isdigit() else None
            )
            if not option_gid:
                alertes.append(f"{col} : option {raw!r} introuvable, ignoree")
                continue
            payload[gid] = str(option_gid)
        elif subtype == "multi_enum":
            gids = []
            for part in re.split(r"[;,]", str(raw)):
                part = part.strip()
                if not part:
                    continue
                option_gid = field.get("options", {}).get(normalize(part)) or (
                    part if part.isdigit() else None
                )
                if option_gid:
                    gids.append(str(option_gid))
                else:
                    alertes.append(f"{col} : option {part!r} introuvable, ignoree")
            if gids:
                payload[gid] = gids
        elif subtype == "people":
            gids = []
            for part in re.split(r"[;,]", str(raw)):
                part = part.strip()
                if not part:
                    continue
                user_gid = user_cache.gid_for_email(part) if "@" in part else None
                if user_gid:
                    gids.append(user_gid)
                elif part.isdigit():
                    gids.append(part)
                else:
                    alertes.append(f"{col} : utilisateur {part!r} introuvable, ignore")
            if gids:
                payload[gid] = gids
        else:
            payload[gid] = str(raw).strip()
    return payload, alertes


def load_mapping(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_mapping(mapping, field_defs):
    """Accepte les GID existants et les noms de champs dans un mapping portable."""
    resolved = {}
    used_fields = {}
    for col, selector in mapping.items():
        target = str(selector or col).strip()
        matches = [field for field in field_defs if field["gid"] == target]
        if not matches:
            matches = [field for field in field_defs
                       if normalize(field["name"]) == normalize(target)]
        if len(matches) > 1:
            raise RuntimeError(f"Champ ambigue pour {col} : {target}. Renseigner son GID dans le mapping.")
        if not matches:
            print(f"  {col} -> champ {target!r} absent du portfolio, ignore")
            continue
        field = matches[0]
        if field["gid"] in used_fields:
            raise RuntimeError(
                f"Mapping ambigue : {used_fields[field['gid']]} et {col} ciblent le meme champ "
                f"{field['name']} ({field['gid']}). Corriger le mapping avant l'import."
            )
        resolved[col] = field["gid"]
        used_fields[field["gid"]] = col
        print(f"  {col} -> {field['gid']} ({field['name']})")
    return resolved


def resolve_portfolio(pat, workspace, selector):
    selector = str(selector or "").strip()
    if not selector:
        raise RuntimeError("Portfolio manquant : passer --portfolio <gid|nom>.")
    if selector.isdigit():
        return selector
    portfolios = get_all(
        f"/workspaces/{workspace}/portfolios?limit=100&opt_fields=name",
        pat,
    )
    matches = [p for p in portfolios if normalize(p.get("name")) == normalize(selector)]
    if len(matches) > 1:
        noms = ", ".join(f"{p['name']} ({p['gid']})" for p in matches)
        raise RuntimeError(f"Plusieurs portfolios portent ce nom : {noms}. Passer le GID.")
    if not matches:
        raise RuntimeError(
            f"Portfolio {selector!r} introuvable dans le workspace. Passer son GID avec --portfolio."
        )
    return matches[0]["gid"]


def resolve_team(pat, workspace, selector):
    """GID d'equipe depuis un GID ou un nom exact (cache gere par l'appelant)."""
    selector = str(selector or "").strip()
    if not selector:
        return None
    if selector.isdigit():
        return selector
    teams = get_all(
        f"/organizations/{workspace}/teams?limit=100&opt_fields=name",
        pat,
    )
    if not teams:
        me = api_fetch("/users/me?opt_fields=gid", pat)["data"]
        teams = get_all(
            f"/users/{me['gid']}/teams?organization={workspace}&limit=100&opt_fields=name",
            pat,
        )
    matches = [t for t in teams if normalize(t.get("name")) == normalize(selector)]
    if len(matches) > 1:
        noms = ", ".join(f"{t['name']} ({t['gid']})" for t in matches)
        raise RuntimeError(f"Plusieurs equipes portent ce nom : {noms}. Passer le GID.")
    if not matches:
        raise RuntimeError(f"Equipe {selector!r} introuvable. Passer son GID avec --team.")
    return matches[0]["gid"]


def list_templates(pat, workspace):
    """Modeles de projet visibles : par workspace, ou equipe par equipe
    pour une organisation (le parametre workspace y est refuse)."""
    try:
        return get_all(
            f"/project_templates?workspace={workspace}&limit=100&opt_fields=name",
            pat,
        )
    except RuntimeError as exc:
        if "organization" not in str(exc):
            raise
    templates = {}
    for team in get_all(
        f"/organizations/{workspace}/teams?limit=100&opt_fields=name",
        pat,
    ):
        try:
            for template in get_all(
                f"/project_templates?team={team['gid']}&limit=100&opt_fields=name",
                pat,
            ):
                templates[template["gid"]] = template
        except RuntimeError as exc:
            sys.stderr.write(f"  ... modeles de l'equipe {team.get('name')!r} inaccessibles ({exc})\n")
    return list(templates.values())


def resolve_template(selector, templates):
    """Retrouve un modele de projet par GID ou nom exact parmi la liste."""
    selector = str(selector or "").strip()
    if not selector:
        return None
    if selector.isdigit():
        for template in templates:
            if template["gid"] == selector:
                return template
        return {"gid": selector, "name": selector}
    matches = [t for t in templates if normalize(t.get("name")) == normalize(selector)]
    if len(matches) > 1:
        noms = ", ".join(f"{t['name']} ({t['gid']})" for t in matches)
        raise RuntimeError(f"Plusieurs modeles portent ce nom : {noms}. Passer le GID.")
    if not matches:
        raise RuntimeError(
            f"Modele {selector!r} introuvable dans le workspace. "
            "Relancer avec --list-templates pour voir les modeles et leurs GID."
        )
    return matches[0]


def template_date_variables(pat, template_gid):
    """Variables de dates du modele : [{gid, name}], liste vide si aucune."""
    payload = api_fetch(
        f"/project_templates/{template_gid}?opt_fields=name,requested_dates",
        pat,
    )
    variables = []
    for var in payload["data"].get("requested_dates") or []:
        variables.append({"gid": str(var.get("gid")), "name": var.get("name") or var.get("gid")})
    return variables


def build_requested_dates(variables, row, col_start, col_due):
    """requested_dates pour instantiateProject depuis une ligne CSV.

    Gid "1" = debut du projet, "2" = echeance ; les autres variables du
    modele sont retrouvees par leur nom dans une colonne du CSV. Les
    variables non remplies gardent la valeur par defaut du modele.
    """
    dates = []
    alertes = []
    seen = set()
    start = to_iso_date(row.get(source_column(row, col_start))) if source_column(row, col_start) else None
    due = to_iso_date(row.get(source_column(row, col_due))) if source_column(row, col_due) else None
    for var in variables:
        gid = var["gid"]
        if gid in seen:
            continue
        if gid == "1" and start:
            dates.append({"gid": gid, "value": start})
            seen.add(gid)
        elif gid == "2" and due:
            dates.append({"gid": gid, "value": due})
            seen.add(gid)
        else:
            source = source_column(row, var["name"])
            iso = to_iso_date(row.get(source)) if source else None
            if iso:
                dates.append({"gid": gid, "value": iso})
                seen.add(gid)
            else:
                alertes.append(f"variable de date {var['name']!r} sans valeur : defaut du modele")
    return dates, alertes


def find_project_by_name(pat, workspace_gid, name, exclude_gids, timeout, label):
    """Repli quand /jobs est illisible : retrouver le projet cree en listant
    les projets du workspace (le typeahead ne renvoie rien avec certains
    comptes de service, et la liste par equipe rate les projets sans equipe)."""
    deadline = time.time() + timeout
    delay = 10
    while time.time() < deadline:
        try:
            projets = get_all(
                f"/projects?workspace={workspace_gid}&limit=100&opt_fields=name,created_at",
                pat,
            )
        except RuntimeError as exc:
            sys.stderr.write(f"  ... liste du workspace indisponible ({exc}), nouvelle tentative\n")
            projets = []
        matches = [
            p for p in projets
            if (p.get("name") or "").casefold() == name.casefold()
            and p.get("gid") not in exclude_gids
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(
                f"{label} : plusieurs projets portent deja ce nom, le projet cree ne peut pas etre identifie"
            )
        sys.stderr.write(f"  ... projet pas encore visible, nouvelle recherche dans {delay}s\n")
        time.sleep(delay)
    raise RuntimeError(f"{label} : projet introuvable apres {timeout}s de recherche")


def wait_for_job(pat, job_gid, timeout, label, fallback=None):
    """Interroge un job Asana jusqu'a succeeded/failed. Retourne new_project.

    /jobs est illisible avec certains comptes de service (404) : dans ce
    cas, si un repli est fourni, le projet cree est retrouve par recherche.
    """
    deadline = time.time() + timeout
    delay = 2
    while time.time() < deadline:
        try:
            payload = api_fetch(f"/jobs/{job_gid}?opt_fields=status,new_project", pat)
        except RuntimeError as exc:
            if fallback is not None and ("404" in str(exc) or "403" in str(exc)):
                sys.stderr.write(f"  ... /jobs illisible ({exc}), repli par recherche\n")
                return fallback()
            raise
        job = payload["data"]
        status = job.get("status")
        if status == "succeeded":
            project = job.get("new_project") or {}
            if not project.get("gid"):
                raise RuntimeError(f"{label} : job termine sans projet (new_project absent)")
            return project
        if status == "failed":
            raise RuntimeError(f"{label} : job Asana en echec")
        sys.stderr.write(f"  ... instanciation {status or 'en cours'}, attente {delay}s\n")
        time.sleep(delay)
        delay = min(delay * 2, 15)
    raise RuntimeError(f"{label} : instanciation non terminee apres {timeout}s")


def wait_for_fields(pat, project_gid, wanted_gids, pauses=(2, 5, 10)):
    """Relit le projet jusqu'a voir les champs mappes (rattachement portfolio)."""
    details = project_details(pat, project_gid)
    for pause in pauses:
        present = {cf.get("gid") for cf in details.get("custom_fields") or []}
        missing = {gid for gid in wanted_gids if gid not in present}
        if not missing:
            return details
        sys.stderr.write(f"  ... champs non encore rattaches ({len(missing)}), nouvelle lecture dans {pause}s\n")
        time.sleep(pause)
        details = project_details(pat, project_gid)
    return details


def index_items(items, details_by_gid, col_code):
    """Index des projets du portfolio : GID, code projet, nom."""
    index = {}
    for item in items:
        details = details_by_gid.get(item["gid"], {})
        keys = [str(item["gid"])]
        if col_code:
            for cf in details.get("custom_fields") or []:
                if (cf.get("name") or "").casefold() == col_code.casefold():
                    value = cf_value(cf)
                    if value is not None:
                        keys.append(str(value).strip().casefold())
                    break
        keys.append((item.get("name") or "").casefold())
        for key in keys:
            if key and key not in index:
                index[key] = item
    return index


def print_field_defs(field_defs, details_by_gid):
    usage = {}
    for details in details_by_gid.values():
        for cf in details.get("custom_fields") or []:
            if cf_value(cf) is not None:
                usage[cf["gid"]] = usage.get(cf["gid"], 0) + 1
    print(f"Champs personnalises vus sur les projets ({len(field_defs)}) :")
    for field in sorted(field_defs, key=lambda d: d["name"].lower()):
        count = usage.get(field["gid"], 0)
        print(f"  - {field['name']} | {field['subtype']} | {field['gid']} | renseigne sur {count} projet(s)")
    print()


def scan_portfolio(args, pat, portfolio_gid):
    print(f"Portfolio {portfolio_gid} : recuperation des projets...")
    items = portfolio_items(pat, portfolio_gid)
    details_by_gid = {}
    for item in items:
        details_by_gid[item["gid"]] = project_details(pat, item["gid"])
        time.sleep(0.05)
    field_defs = collect_field_defs(details_by_gid.values())
    print(f"{len(items)} projet(s) dans le portfolio\n")
    print_field_defs(field_defs, details_by_gid)


def run(args, pat, cfg, portfolio_gid):
    col_nom = args.col_nom
    col_code = args.col_code
    col_modele = args.col_modele
    col_start = args.col_debut
    col_due = args.col_fin
    col_team = args.col_team
    rows = args.rows

    mapping = {}
    if args.fields:
        mapping = load_mapping(args.fields)
        if not mapping:
            sys.stderr.write("Erreur fatale : mapping vide.\n")
            sys.exit(1)

    print("Recuperation des projets du portfolio...")
    items = portfolio_items(pat, portfolio_gid)
    details_by_gid = {}
    for item in items:
        details_by_gid[item["gid"]] = project_details(pat, item["gid"])
        time.sleep(0.05)
    field_defs = collect_field_defs(details_by_gid.values())
    index = index_items(items, details_by_gid, col_code)
    connus = {item["gid"] for item in items}
    print(f"Projets dans le portfolio : {len(items)}")

    resolved_mapping = {}
    if mapping:
        print(f"Mapping ({len(mapping)} champ(s)) :")
        resolved_mapping = resolve_mapping(mapping, field_defs)
        for col in resolved_mapping:
            if not any(source_column(row, col) is not None for row in rows):
                print(f"  ! {col} : colonne absente du CSV, aucune valeur importee pour ce champ")
        print()

    templates = list_templates(pat, cfg["workspace_gid"])
    print(f"Modeles de projet visibles : {len(templates)}\n")

    user_cache = UserCache(pat, cfg["workspace_gid"])
    team_cache = {}

    def team_gid_for(row):
        selector = ""
        source = source_column(row, col_team) if row else None
        if source:
            selector = (row.get(source) or "").strip()
        if not selector:
            selector = args.team or ""
        if not selector:
            return None, "equipe manquante (colonne TEAM vide et pas de --team)"
        if selector.isdigit():
            return selector, None
        if selector in team_cache:
            return team_cache[selector], None
        try:
            gid = resolve_team(pat, cfg["workspace_gid"], selector)
        except RuntimeError as exc:
            return None, str(exc)
        team_cache[selector] = gid
        return gid, None

    stats = {"crees": 0, "existants": 0, "absents": 0, "erreurs": 0, "champs": 0}
    lignes = []

    for numero, row in enumerate(rows, start=2):
        nom = (row.get(col_nom) or "").strip() if col_nom in row else ""
        if not nom and col_nom not in row:
            alt = source_column(row, col_nom)
            nom = (row.get(alt) or "").strip() if alt else ""
        code = ""
        source = source_column(row, col_code)
        if source:
            code = (row.get(source) or "").strip()
        url_gid = extract_project_gid(row.get("URL") or row.get("url"))
        label = f"{code} {nom}".strip() or f"ligne {numero}"
        entree = {"ligne": numero, "code": code, "nom": nom, "action": "", "projet": None, "alertes": []}

        if not nom:
            entree["action"] = "ignoree"
            entree["alertes"].append("nom de projet absent")
            stats["erreurs"] += 1
            print(f"- {label} : ! nom absent, ligne ignoree")
            lignes.append(entree)
            continue

        # 1. rapprochement : GID d'URL, code projet, puis nom
        item = None
        for key in (url_gid, code.casefold(), nom.casefold()):
            if key and key in index:
                item = index[key]
                break

        project_gid = item["gid"] if item else None
        modele = ""
        source = source_column(row, col_modele)
        if source:
            modele = (row.get(source) or "").strip()

        if item:
            entree["action"] = "existant"
            entree["projet"] = project_gid
            stats["existants"] += 1
            print(f"- {label} -> deja dans le portfolio ({project_gid})")
        elif args.sans_creation:
            entree["action"] = "absente"
            entree["alertes"].append("creation desactivee (--sans-creation)")
            stats["absents"] += 1
            print(f"- {label} : absent (creation desactivee)")
            lignes.append(entree)
            continue
        else:
            team_gid, erreur = team_gid_for(row)
            if erreur:
                entree["action"] = "erreur"
                entree["alertes"].append(erreur)
                stats["erreurs"] += 1
                print(f"- {label} : ! {erreur}")
                lignes.append(entree)
                continue
            start = to_iso_date(row.get(source_column(row, col_start))) if source_column(row, col_start) else None
            due = to_iso_date(row.get(source_column(row, col_due))) if source_column(row, col_due) else None
            start, due, alerte = valid_dates(start, due)
            if alerte:
                entree["alertes"].append(alerte)
                print(f"  ! {alerte}")

            if modele:
                try:
                    template = resolve_template(modele, templates)
                except RuntimeError as exc:
                    entree["action"] = "erreur"
                    entree["alertes"].append(str(exc))
                    stats["erreurs"] += 1
                    print(f"- {label} : ! {exc}")
                    lignes.append(entree)
                    continue
                print(f"- {label} : creation depuis le modele {template['name']} ({template['gid']})")
                entree["action"] = f"creation (modele {template['name']})"
                if args.apply:
                    try:
                        variables = template_date_variables(pat, template["gid"])
                        dates, alertes_dates = build_requested_dates(variables, row, col_start, col_due)
                        for message in alertes_dates:
                            entree["alertes"].append(message)
                            print(f"  ! {message}")
                        body = {
                            "data": {
                                "name": nom,
                                "privacy_setting": args.privacy,
                            }
                        }
                        if team_gid:
                            body["data"]["team"] = team_gid
                        if dates:
                            body["data"]["requested_dates"] = dates
                        job = api_fetch(
                            f"/project_templates/{template['gid']}/instantiateProject",
                            pat,
                            method="POST",
                            body=body,
                        )["data"]

                        def repli():
                            return find_project_by_name(
                                pat, cfg["workspace_gid"], nom,
                                connus | {e["projet"] for e in lignes if e.get("projet")},
                                args.job_timeout, f"{label} (modele {template['name']})",
                            )

                        project = wait_for_job(
                            pat, job["gid"], args.job_timeout,
                            f"{label} (modele {template['name']})",
                            fallback=repli,
                        )
                        project_gid = project["gid"]
                        connus.add(project_gid)
                        entree["projet"] = project_gid
                        # dates du CSV non couvertes par les variables du
                        # modele : posees juste apres la creation
                        updates = {}
                        gids_vars = {v["gid"] for v in variables}
                        if due and "2" not in gids_vars:
                            updates["due_on"] = due
                        if start and "1" not in gids_vars:
                            updates["start_on"] = start
                        if updates:
                            api_fetch(
                                f"/projects/{project_gid}",
                                pat,
                                method="PUT",
                                body={"data": updates},
                            )
                            print(f"  dates posees : {', '.join(f'{k}={v}' for k, v in updates.items())}")
                        time.sleep(args.pause)
                    except RuntimeError as exc:
                        entree["action"] = "erreur"
                        entree["alertes"].append(str(exc))
                        stats["erreurs"] += 1
                        print(f"  ! echec : {exc}")
                        lignes.append(entree)
                        continue
            else:
                print(f"- {label} : creation sans modele")
                entree["action"] = "creation"
                if args.apply:
                    try:
                        body = {"data": {"name": nom, "team": team_gid, "privacy_setting": args.privacy}}
                        if due:
                            body["data"]["due_on"] = due
                        if start:
                            body["data"]["start_on"] = start
                        project = api_fetch("/projects", pat, method="POST", body=body)["data"]
                        project_gid = project["gid"]
                        entree["projet"] = project_gid
                        time.sleep(args.pause)
                    except RuntimeError as exc:
                        entree["action"] = "erreur"
                        entree["alertes"].append(str(exc))
                        stats["erreurs"] += 1
                        print(f"  ! echec : {exc}")
                        lignes.append(entree)
                        continue

            if args.apply and project_gid:
                try:
                    add_to_portfolio(pat, portfolio_gid, project_gid)
                    index[str(project_gid)] = {"gid": project_gid, "name": nom}
                    time.sleep(args.pause)
                except RuntimeError as exc:
                    entree["alertes"].append(f"ajout au portfolio : {exc}")
                    print(f"  ! ajout au portfolio : {exc}")
            stats["crees"] += 1

        # 2. champs personnalises
        if not resolved_mapping or args.sans_champs:
            if not item and args.sans_champs:
                print("  champs non remplis (--sans-champs)")
            lignes.append(entree)
            continue

        if not args.apply:
            # apercu des valeurs prevues, champs vus sur le portfolio
            payload, alertes_champs = build_payload(row, resolved_mapping, field_defs, user_cache)
            for message in alertes_champs:
                entree["alertes"].append(message)
                print(f"  ! {message}")
            valeurs = ", ".join(
                f"{next((f['name'] for f in field_defs if f['gid'] == g), g)}={v}"
                for g, v in payload.items()
            )
            print(f"  champs prevus : {valeurs or '(aucun)'}")
            lignes.append(entree)
            continue

        if not project_gid:
            lignes.append(entree)
            continue
        wanted = set(resolved_mapping.values())
        try:
            details = wait_for_fields(pat, project_gid, wanted)
        except RuntimeError as exc:
            entree["alertes"].append(f"lecture projet : {exc}")
            stats["erreurs"] += 1
            print(f"  ! lecture projet : {exc}")
            lignes.append(entree)
            continue
        project_field_defs = collect_field_defs([details])
        payload, alertes_champs = build_payload(row, resolved_mapping, project_field_defs, user_cache)
        for message in alertes_champs:
            entree["alertes"].append(message)
            print(f"  ! {message}")
        present = {cf.get("gid") for cf in details.get("custom_fields") or []}
        absents = [g for g in resolved_mapping.values() if g not in present]
        for gid in absents:
            payload.pop(gid, None)
            nom_champ = next((col for col, g in resolved_mapping.items() if g == gid), gid)
            message = f"champ {nom_champ} non rattache au projet, valeur non ecrite"
            entree["alertes"].append(message)
            print(f"  ! {message}")
        if payload:
            valeurs = ", ".join(
                f"{next((f['name'] for f in project_field_defs if f['gid'] == g), g)}={v}"
                for g, v in payload.items()
            )
            print(f"  champs : {valeurs}")
            try:
                api_fetch(
                    f"/projects/{project_gid}",
                    pat,
                    method="PUT",
                    body={"data": {"custom_fields": payload}},
                )
                stats["champs"] += 1
                time.sleep(args.pause)
            except RuntimeError as exc:
                entree["alertes"].append(f"ecriture champs : {exc}")
                stats["erreurs"] += 1
                print(f"  ! ecriture champs : {exc}")
        lignes.append(entree)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"\n{mode} termine : {stats['crees']} cree(s), {stats['existants']} existant(s), "
        f"{stats['absents']} absent(s), {stats['champs']} projet(s) avec champs ecrits, "
        f"{stats['erreurs']} erreur(s)."
    )
    if not args.apply:
        print("Aucune modification effectuee. Relancer avec --apply pour executer.")
    return {"stats": stats, "lignes": lignes}


def main():
    parser = argparse.ArgumentParser(description="Creation en masse de projets Asana + portfolio + champs.")
    add_config_args(parser)
    parser.add_argument("--workspace", default=None, help="GID du workspace (surcharge le config.env)")
    parser.add_argument("--input", default=None)
    parser.add_argument("--sep", default=",", help="Separateur du CSV (defaut , pour un export Asana)")
    parser.add_argument("--portfolio", default=None)
    parser.add_argument("--team", default=None, help="Equipe par defaut si la colonne TEAM est vide")
    parser.add_argument("--privacy", default="public_to_workspace",
                        choices=("public_to_workspace", "private_to_team", "private"))
    parser.add_argument("--fields", default=None)
    parser.add_argument("--col-nom", default="NAME")
    parser.add_argument("--col-code", default="PRJ_CODE")
    parser.add_argument("--col-modele", default="Modèle")
    parser.add_argument("--col-debut", default="START DATE")
    parser.add_argument("--col-fin", default="DUE DATE")
    parser.add_argument("--col-team", default="TEAM")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--job-timeout", type=int, default=900)
    parser.add_argument("--scan-portfolio", action="store_true")
    parser.add_argument("--list-templates", action="store_true")
    parser.add_argument("--sans-creation", action="store_true")
    parser.add_argument("--sans-champs", action="store_true")
    parser.add_argument("--rapport", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ipv4", action="store_true", help="Utiliser IPv4 (liste blanche IP)")
    args = parser.parse_args()

    if args.ipv4:
        force_ipv4()

    rows, fieldnames = [], []
    if args.input:
        rows, fieldnames = read_rows(args.input, args.sep)
        if args.limit:
            rows = rows[: args.limit]

    if not args.list_templates and not args.scan_portfolio and not rows:
        sys.stderr.write("Erreur fatale : donner --input <csv>, ou --list-templates, ou --scan-portfolio.\n")
        sys.exit(1)

    if args.list_templates and not args.input and not args.scan_portfolio:
        cfg = get_config(args)
        pat = cfg["token"]
        me = api_fetch("/users/me?opt_fields=name", pat)["data"]
        print(f"Environnement : {cfg['env'].upper()} | compte : {me.get('name')}\n")
        templates = list_templates(pat, cfg["workspace_gid"])
        print(f"Modeles de projet du workspace ({len(templates)}) :")
        for template in sorted(templates, key=lambda t: (t.get("name") or "").lower()):
            print(f"  - {template.get('name')} | {template['gid']}")
        print("\nUn modele s'utilise par son nom ou son GID dans la colonne du modele du CSV.")
        return

    cfg = get_config(args)
    pat = cfg["token"]
    me = api_fetch("/users/me?opt_fields=name", pat)["data"]
    print(f"Environnement : {cfg['env'].upper()} | compte : {me.get('name')}\n")

    if args.scan_portfolio and not args.input:
        portfolio_gid = resolve_portfolio(pat, cfg["workspace_gid"], args.portfolio)
        scan_portfolio(args, pat, portfolio_gid)
        return

    portfolio_gid = resolve_portfolio(pat, cfg["workspace_gid"], args.portfolio)
    args.rows = rows
    resultat = run(args, pat, cfg, portfolio_gid)

    if args.rapport:
        rapport = {
            "date": datetime.now().isoformat(timespec="seconds"),
            "mode": "APPLY" if args.apply else "DRY-RUN",
            "environnement": cfg["env"],
            "portfolio": portfolio_gid,
            "input": args.input,
            "stats": resultat["stats"],
            "lignes": resultat["lignes"],
        }
        with open(args.rapport, "w", encoding="utf-8") as fh:
            json.dump(rapport, fh, ensure_ascii=False, indent=2)
        print(f"Rapport JSON ecrit : {args.rapport}")

    if resultat["stats"]["erreurs"]:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, FileNotFoundError) as exc:
        sys.stderr.write(f"\nErreur fatale : {exc}\n")
        sys.exit(1)
