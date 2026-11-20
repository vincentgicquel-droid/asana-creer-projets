# Creation de projets en masse

Creer des projets Asana depuis un CSV export, les ajouter a un portfolio pour leur rattacher ses champs personnalises, puis renseigner ces champs depuis le CSV. Python 3 suffit, sans dependance externe.

## CSV attendu

Le CSV source est un export Asana (separateur `,`, encodage UTF-8), une ligne par projet a creer. Colonnes utilisees par defaut (toutes renommables en option) :

| Colonne | Role |
|---|---|
| `NAME` | Nom du projet a creer |
| `PRJ_CODE` | Code projet, cle de rapprochement avec l'existant |
| `START DATE` | Date de debut du projet (optionnelle) |
| `DUE DATE` | Date d'echeance du projet (optionnelle) |
| `TEAM` | Equipe de creation (si vide : option `--team`) |
| `URL` | Si remplie (projet deja existant), le GID y est lu : pas de creation |
| `Modèle` | Nom ou GID d'un modele de projet ; vide = creation sans modele |

Les autres colonnes natives de l'export (TYPE, STATUS, PROGRESS...) sont ignorees. Les colonnes de champs personnalises sont lues selon `mapping.json`.

Un fichier pret a remplir est fourni : `modele-import.csv` (structure complete d'export Asana, deux lignes d'exemple a remplacer). Il reste local (exclu du depot) car il porte les valeurs reelles.

## Deroulement par ligne

1. Rapprochement avec les projets deja presents dans le portfolio : GID lu dans `URL`, puis `PRJ_CODE`, puis nom. Deja present : pas de creation, champs mis a jour seulement.
2. Creation : sans modele par defaut (`POST /projects`), ou depuis un modele de projet (instanciation asynchrone via un job Asana, puis attente de sa fin).
3. Ajout au portfolio : les champs personnalises du portfolio se rattachent au projet (le script attend le rattachement avant d'ecrire).
4. Ecriture des champs personnalises selon le mapping.

Le script est idempotent : le relancer apres une interruption ne recree pas les projets deja traites.

## Mapping des champs

Fichier local `mapping.json` (exclu du depot) : copier `mapping.example.json` et adapter. Format :

```json
{
  "PRJ_CODE": "PRJ_CODE",
  "CDP": "CDP",
  "Budget Initial": "Budget Initial"
}
```

- Cle : nom de la colonne CSV ; valeur : nom du champ Asana (ou son GID).
- Casse, accents, espaces/underscores et emojis sont tolérés dans les deux noms.
- Deux colonnes ciblant le meme champ, ou un nom ambigue : arret avec message.
- Un champ absent du portfolio est signale et ignore.

Sous-types geres a l'ecriture :

| Type | Valeur attendue dans le CSV |
|---|---|
| Nombre | `5000`, `63,9` |
| Texte | libre |
| Date | `JJ/MM/AAAA` ou `AAAA-MM-JJ` |
| Enum | nom exact de l'option (ou GID d'option) |
| Multi-enum | options separees par `,` ou `;` |
| People | email du membre du workspace (ou GID) |

Une option ou un email introuvable est signale et ignore, sans bloquer la ligne.

## Modele de projet

Un modele de projet s'identifie par son nom ou son GID. Lister les modeles visibles dans le workspace :

```bash
python creer-projets.py --list-templates
```

La colonne du modele accepte le nom exact (par exemple le modele de la methode projet standard) ou le GID. Les deux se remplissent ligne par ligne : un meme CSV peut melanger des lignes avec le modele Tempo tech, un autre modele, ou sans modele. Le GID est recommande (sans ambiguite) ; le nom est tolerant casse/accents/espaces mais doit rester non ambigu. Les GID etant propres a chaque environnement, lister les modeles de l'environnement cible avant de remplir la colonne :

```bash
python creer-projets.py --list-templates --env sandbox
python creer-projets.py --list-templates --env prod
```

L'instanciation lit toujours l'objet modele Asana (template), jamais un projet qui servirait de modele : la structure, les champs et les vues proviennent du modele vise dans l'environnement ou le script tourne. Les dates de debut/echeance du CSV alimentent les variables de date du modele (`1` = debut, `2` = echeance) ; les autres variables du modele sont retrouvees par leur nom dans une colonne du CSV, sinon elles gardent la valeur par defaut du modele (signalé en log). Les dates du CSV non couvertes par des variables du modele sont posees juste apres la creation. Un modele peut exiger une variable de date (constate sur un modele avec date de debut obligatoire) : remplir `START DATE` pour les lignes qui l'utilisent, sinon l'instanciation est refusee et la ligne est journalisee en erreur.

Volumetrie constatee : l'instanciation d'un modele riche (plusieurs centaines de taches) prend plusieurs minutes par projet (5 min mesurees). Monter `--job-timeout` (defaut 900 s) en consequence et prevoir un lancement en arriere-plan pour quelques centaines de projets. Le projet cree n'apparait dans Asana qu'a la fin de l'instanciation.

Deux comportements verifies en reel (18/11/2026, modele riche de la methode projet standard) :

- **Les vues du modele sont copiees automatiquement** dans chaque projet instancie : apercu, liste, tableau, chronologie, tableau de bord (widgets inclus), calendrier, messages, connaissances et gantt, avec leurs reglages (regroupement, colonnes affichees, zoom). Inutile de les recreer a la main. C'est aussi un argument pour instancier le modele plutot que dupliquer un projet existant (la duplication conserve aussi les vues, mais part d'un projet source a maintenir synchronise).
- **Le modele peut porter des residus** du projet dont il est issu : description non nettoye, dates fixes anciennes sur certaines taches, statuts ou liens pre-remplis, taches en double. Ces residus se retrouvent dans chaque projet instancie. Verifier et nettoyer le modele avant une montee en charge.

Note sur l'instanciation : elle passe par un job Asana, et l'endpoint `/jobs` peut etre illisible avec un compte de service (404). Le script bascule alors automatiquement sur la liste des projets du workspace pour retrouver le projet cree par son nom (le typeahead ne renvoie rien avec ces comptes) : eviter deux lignes du CSV avec le meme nom quand une colonne de modele est renseignee.

Pour une premiere passe a blanc, creer sans modele (`--sans-champs` pour ne pas remplir les champs), verifier le rendu dans Asana, puis ajouter le modele ligne par ligne.

## Configuration et lancement

Placer le `config.env` habituel a cote du script ou dans un dossier parent : token, workspace sandbox et prod, proxy. Demarrer le proxy local avant les appels a Asana si le reseau l'exige.

Ouvrir un terminal dans le dossier du package.

1. Lister les modeles (lecture seule) et repérer le GID du portfolio cible :

```bash
python creer-projets.py --list-templates
python creer-projets.py --scan-portfolio --portfolio <gid_portfolio>
```

2. Simuler sur trois lignes (lectures API, aucune ecriture) :

```bash
python creer-projets.py --input export.csv --portfolio <gid_portfolio> --team <gid_equipe> --fields mapping.json --limit 3
```

3. Verifier les logs : rapprochements, creations prevues, valeurs de champs prevues, alertes. Retirer `--limit` et ajouter `--apply` pour executer.

Options utiles :

- `--sans-creation` : ne rien creer, traiter seulement les projets existants ;
- `--sans-champs` : creer sans ecrire les champs (passe de verification) ;
- `--privacy public_to_workspace|private_to_team|private` (defaut : public au workspace) ;
- `--pause <s>` : pause entre deux ecritures (defaut 0,5) ;
- `--job-timeout <s>` : attente max d'une instanciation de modele (defaut 900 s) ;
- `--rapport <chemin>` : rapport JSON du run (statistiques + statut par ligne) ;
- `--ipv4` : forcer IPv4 lorsque l'adresse est autorisee par la liste blanche API ;
- `--env sandbox|prod` : environnement du `config.env` (defaut sandbox, prod toujours explicite).

Sur macOS, utiliser `python3` a la place de `python`.

## Volumetrie

La creation sans modele est rapide (moins d'une seconde par projet). L'instanciation depuis un modele est asynchrone cote Asana et peut durer plusieurs minutes par projet selon la richesse du modele : pour quelques centaines de projets avec modele, prevoir un lancement en arriere-plan et une reprise par relancements successifs (le script saute les projets deja crees).

## Limites connues

- La colonne `OWNER` (proprietaire du projet) n'est pas posee : le rapprochement par nom seul est ambigu. A ajuster dans Asana apres creation.
- La colonne `DESCRIPTION` n'est pas copiee dans la description du projet.
- Un champ en lecture seule (calcule) refuse l'ecriture : l'erreur est journalisee et la ligne continue.
