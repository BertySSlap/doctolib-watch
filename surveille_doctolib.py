# -*- coding: utf-8 -*-
"""
Veille Doctolib — alerte ntfy quand un créneau se libère chez un praticien.

Deux modes :
 - local  : lancé par la tâche planifiée Windows "VeilleDoctolib"
            (désactivée par défaut, le cloud prend le relais)
 - cloud  : lancé par GitHub Actions toutes les 10 min (env VEILLE_CLOUD=1),
            logs anonymisés (les logs Actions d'un dépôt public sont visibles
            par tous) et état sans donnée identifiante (clés hachées)

Une passe par exécution :
 1. lit config.json (liste des praticiens à surveiller)
 2. pour chaque praticien actif : récupère les créneaux visibles sur Doctolib
 3. compare avec l'état précédent (etat.json) et notifie les NOUVEAUX créneaux
    sur ntfy.sh (topic dans config.json)

Important anti-403 : Doctolib bloque l'endpoint availabilities.json pour les
requêtes "nues" depuis les datacenters. Il faut visiter la page HTML du
praticien d'abord (cookies Cloudflare) et envoyer des en-têtes complets de
navigateur (Sec-Fetch-*, sec-ch-ua, X-Requested-With, Referer).

Aucune dépendance externe (urllib uniquement). Python 3.10+.
Usage manuel : py surveille_doctolib.py --verbose
"""

import hashlib
import json
import os
import sys
import time
import gzip
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from datetime import datetime, date

DOSSIER = os.path.dirname(os.path.abspath(__file__))
FICHIER_CONFIG = os.path.join(DOSSIER, "config.json")
FICHIER_ETAT = os.path.join(DOSSIER, "etat.json")
FICHIER_LOG = os.path.join(DOSSIER, "veille.log")
FICHIER_COOKIES = os.path.join(DOSSIER, "cookies.lwp")

NTFY_URL = "https://ntfy.sh/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TTL_INFO_CACHE = 24 * 3600          # re-résolution des IDs motif/agenda : 1x/jour
SEUIL_PANNE = 30                    # échecs consécutifs avant alerte (≈5 h à 10 min)
JOURS_FR = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

CLOUD = bool(os.environ.get("VEILLE_CLOUD"))
VERBOSE = "--verbose" in sys.argv or CLOUD
MASQUES = []   # [(texte sensible, remplacement)] appliqué aux logs en cloud


# ---------------------------------------------------------------- utilitaires

def log(msg):
    if CLOUD:
        for s, r in MASQUES:
            msg = msg.replace(s, r)
    ligne = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    if VERBOSE and sys.stdout:   # sys.stdout est None sous pythonw
        try:
            print(ligne, flush=True)
        except UnicodeEncodeError:
            print(ligne.encode("ascii", "replace").decode(), flush=True)
    try:
        # plafonne le log à ~500 Ko (garde la fin)
        if os.path.exists(FICHIER_LOG) and os.path.getsize(FICHIER_LOG) > 500_000:
            with open(FICHIER_LOG, encoding="utf-8", errors="replace") as f:
                contenu = f.read()
            with open(FICHIER_LOG, "w", encoding="utf-8") as f:
                f.write(contenu[-200_000:])
        with open(FICHIER_LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except OSError:
        pass


def normalise(s):
    """minuscules + sans accents, pour comparer les noms de motifs"""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.casefold().strip()


def formate_creneau(iso):
    try:
        d = datetime.fromisoformat(iso)
        if d.hour or d.minute:
            return f"{JOURS_FR[d.weekday()]} {d:%d/%m/%Y} à {d:%H:%M}"
        return f"{JOURS_FR[d.weekday()]} {d:%d/%m/%Y}"
    except ValueError:
        return iso


def date_de(iso):
    try:
        return datetime.fromisoformat(iso).date()
    except (ValueError, TypeError):
        return None


def slug_depuis_url(url):
    chemin = urllib.parse.urlparse(url).path
    return chemin.rstrip("/").rsplit("/", 1)[-1]


def cle_etat(slug, topic):
    """En cloud, l'état est public (commité) : clé hachée, non identifiante."""
    if CLOUD:
        return hashlib.sha256((slug + topic).encode()).hexdigest()[:12]
    return slug


# ------------------------------------------------------------------ HTTP

EN_TETES_COMMUNS = {
    "User-Agent": UA,
    "Accept-Language": "fr-FR,fr;q=0.9",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", '
                 '"Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class Client:
    def __init__(self):
        self.cj = http.cookiejar.LWPCookieJar(FICHIER_COOKIES)
        try:
            if os.path.exists(FICHIER_COOKIES):
                self.cj.load(ignore_discard=True)
        except (OSError, http.cookiejar.LoadError):
            pass
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _open(self, url, en_tetes):
        req = urllib.request.Request(
            url, headers={**EN_TETES_COMMUNS, **en_tetes})
        r = self.op.open(req, timeout=30)
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body

    def visite_page(self, url):
        """Échauffement anti-403 : cookies Cloudflare de la page praticien."""
        self._open(url, {
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })

    def get_json(self, url, referer):
        body = self._open(url, {
            "Accept": "application/json", "Referer": referer,
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        })
        return json.loads(body)

    def sauve_cookies(self):
        try:
            self.cj.save(ignore_discard=True)
        except OSError:
            pass


def notifie(topic, titre, message, priorite=4, tags=None, url_clic=None):
    corps = {
        "topic": topic,
        "title": titre,
        "message": message,
        "priority": priorite,
        "tags": tags or ["calendar"],
    }
    if url_clic:
        corps["click"] = url_clic
        corps["actions"] = [
            {"action": "view", "label": "Ouvrir Doctolib", "url": url_clic}]
    req = urllib.request.Request(
        NTFY_URL,
        data=json.dumps(corps).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20)
    log(f"ntfy -> {titre}")


# ------------------------------------------------------------------ Doctolib

def recupere_info(client, slug, referer):
    """IDs des motifs / agendas / cabinets d'un praticien (endpoint 2026)."""
    url = ("https://www.doctolib.fr/online_booking/api/slot_selection_funnel/"
           f"v1/info.json?profile_slug={urllib.parse.quote(slug)}&locale=fr")
    d = client.get_json(url, referer)["data"]
    return {
        "motifs": [{"id": m["id"], "nom": m["name"]}
                   for m in d.get("visit_motives", [])],
        "agendas": [{"id": a["id"],
                     "practice_id": a.get("practice_id"),
                     "motif_ids": a.get("visit_motive_ids") or []}
                    for a in d.get("agendas", [])
                    if not a.get("booking_disabled")],
    }


def recupere_creneaux(client, info, filtres_motifs, jours, referer):
    """Liste triée des créneaux ISO visibles dans la fenêtre + next_slot."""
    motifs = info["motifs"]
    if filtres_motifs:
        f = [normalise(x) for x in filtres_motifs]
        motifs = [m for m in motifs if any(x in normalise(m["nom"]) for x in f)]
        if not motifs:
            raise ValueError(
                "aucun motif ne correspond au filtre "
                f"{filtres_motifs} (motifs dispo : "
                + "; ".join(m['nom'] for m in info['motifs']) + ")")
    if not motifs:
        raise ValueError("aucun motif réservable en ligne pour ce praticien")
    ids_motifs = [m["id"] for m in motifs]
    agendas = [a for a in info["agendas"]
               if not a["motif_ids"] or set(a["motif_ids"]) & set(ids_motifs)]
    if not agendas:
        agendas = info["agendas"]
    if not agendas:
        raise ValueError("aucun agenda réservable en ligne pour ce praticien")

    q = urllib.parse.urlencode({
        "ignore_current_draft": "true",
        "start_date": str(date.today()),
        "visit_motive_ids": "-".join(str(i) for i in ids_motifs),
        "agenda_ids": "-".join(str(a["id"]) for a in agendas),
        "practice_ids": "-".join(sorted({str(a["practice_id"]) for a in agendas
                                         if a["practice_id"]})),
        "limit": str(jours),
    })
    av = client.get_json(
        "https://www.doctolib.fr/availabilities.json?" + q, referer)

    creneaux = []
    for jour in av.get("availabilities", []):
        for s in jour.get("slots") or []:
            iso = s if isinstance(s, str) else s.get("start_date")
            if iso:
                creneaux.append(iso)
    noms_motifs = [m["nom"] for m in motifs]
    return sorted(set(creneaux)), av.get("next_slot"), noms_motifs


# ------------------------------------------------------------------ cœur

def traite_praticien(client, praticien, etat_p, topic, jours):
    nom = praticien.get("nom") or slug_depuis_url(praticien["url"])
    url = praticien["url"]
    slug = slug_depuis_url(url)

    # échauffement anti-403 (best effort)
    try:
        client.visite_page(url)
    except (urllib.error.URLError, OSError):
        pass

    # cache des IDs (24 h) — jamais persisté en cloud (état public)
    maintenant = time.time()
    if (not etat_p.get("info_cache")
            or maintenant - etat_p.get("info_ts", 0) > TTL_INFO_CACHE):
        etat_p["info_cache"] = recupere_info(client, slug, url)
        etat_p["info_ts"] = maintenant

    try:
        creneaux, next_slot, noms_motifs = recupere_creneaux(
            client, etat_p["info_cache"], praticien.get("motifs"), jours, url)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # IDs probablement périmés -> on invalide le cache pour la
            # prochaine passe
            etat_p["info_cache"] = None
            etat_p["info_ts"] = 0
        raise

    # filtre optionnel "avant_le" (créneaux strictement avant cette date)
    avant_le = praticien.get("avant_le")
    if avant_le:
        limite = date.fromisoformat(avant_le)
        creneaux = [c for c in creneaux
                    if datetime.fromisoformat(c).date() < limite]
        if next_slot and (date_de(next_slot) is None
                          or date_de(next_slot) >= limite):
            next_slot = None

    # meilleur créneau connu, dans OU au-delà de la fenêtre de 15 jours
    # (15 = plafond de l'API) — c'est lui qui permet d'avancer un RDV lointain
    candidats = [c for c in creneaux[:1] + ([next_slot] if next_slot else [])
                 if date_de(c)]
    meilleur = min(candidats, key=date_de) if candidats else None

    vus = etat_p.setdefault("vus", {})

    # purge des créneaux passés
    aujourdhui = datetime.now().astimezone()
    for iso in list(vus):
        try:
            if datetime.fromisoformat(iso) < aujourdhui:
                del vus[iso]
        except ValueError:
            del vus[iso]

    nouveaux = [c for c in creneaux if c not in vus]
    for c in nouveaux:
        vus[c] = int(maintenant)

    if not etat_p.get("init"):
        # première passe : on prend l'existant comme référence, sans alerter
        etat_p["init"] = True
        etat_p["meilleur"] = meilleur
        detail = (f"{len(creneaux)} créneau(x) actuellement visibles "
                  f"(1er : {formate_creneau(creneaux[0])})" if creneaux
                  else "aucun créneau visible sous 15 jours")
        if meilleur and not creneaux:
            detail += f" — prochain créneau connu : {formate_creneau(meilleur)}"
        notifie(topic, f"Veille activée : {nom}",
                detail + "\nTu seras alerté(e) dès qu'un nouveau créneau apparaît.",
                priorite=2, tags=["white_check_mark"], url_clic=url)
        log(f"{nom} : baseline posée ({len(creneaux)} créneaux)")
        return

    if nouveaux:
        lignes = [f"• {formate_creneau(c)}" for c in nouveaux[:6]]
        if len(nouveaux) > 6:
            lignes.append(f"… +{len(nouveaux) - 6} autres")
        msg = "\n".join(lignes)
        if praticien.get("motifs"):
            msg += "\nMotif(s) : " + " / ".join(noms_motifs)
        titre = (f"RDV dispo : {nom}" if len(nouveaux) == 1
                 else f"{len(nouveaux)} RDV dispo : {nom}")
        notifie(topic, titre, msg, priorite=4, url_clic=url)
        log(f"{nom} : {len(nouveaux)} nouveau(x) créneau(x) notifié(s)")
    else:
        log(f"{nom} : rien de neuf ({len(creneaux)} créneaux connus)")

    # suivi permanent du meilleur créneau connu : alerte dès qu'il AVANCE
    # (désistement au-delà de la fenêtre de 15 jours compris)
    ref = etat_p.get("meilleur") or etat_p.pop("next_slot", None)  # migration
    etat_p.pop("next_slot", None)
    if meilleur:
        if (ref and date_de(ref) and date_de(meilleur) < date_de(ref)
                and not nouveaux):
            # (si "nouveaux" a déjà alerté cette passe, inutile de doubler)
            notifie(topic, f"RDV plus tôt : {nom}",
                    f"Le meilleur créneau connu est avancé au "
                    f"{formate_creneau(meilleur)} "
                    f"(avant : {formate_creneau(ref)}).",
                    priorite=4, url_clic=url)
            log(f"{nom} : meilleur créneau avancé {ref} -> {meilleur}")
        elif not ref:
            log(f"{nom} : meilleur créneau initialisé -> {meilleur}")
        # s'il recule (créneau pris), on suit silencieusement : la prochaine
        # amélioration sera comparée à la nouvelle réalité
        etat_p["meilleur"] = meilleur


def main():
    try:
        with open(FICHIER_CONFIG, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"ERREUR config.json illisible : {e}")
        return 1

    topic = cfg.get("ntfy_topic")
    if not topic:
        log("ERREUR : pas de ntfy_topic dans config.json")
        return 1
    jours = int(cfg.get("jours_a_scanner", 15))

    # anonymisation des logs publics : nom / slug / url -> P1, P2, ...
    for i, p in enumerate(cfg.get("praticiens", []), 1):
        for s in {p.get("nom"), slug_depuis_url(p["url"]), p["url"]}:
            if s:
                MASQUES.append((s, f"P{i}"))
    MASQUES.sort(key=lambda x: -len(x[0]))

    etat = {}
    if os.path.exists(FICHIER_ETAT):
        try:
            with open(FICHIER_ETAT, encoding="utf-8") as f:
                etat = json.load(f)
        except (OSError, json.JSONDecodeError):
            etat = {}
    etats_p = etat.setdefault("praticiens", {})

    client = Client()
    actifs = [p for p in cfg.get("praticiens", []) if p.get("actif", True)]
    if not actifs:
        log("aucun praticien actif dans config.json")

    for praticien in actifs:
        cle = cle_etat(slug_depuis_url(praticien["url"]), topic)
        etat_p = etats_p.setdefault(cle, {})
        nom = praticien.get("nom", "?")
        try:
            traite_praticien(client, praticien, etat_p, topic, jours)
            etat_p["echecs"] = 0
            etat_p["panne_notifiee"] = False
        except Exception as e:  # un praticien en échec ne bloque pas les autres
            if (CLOUD and isinstance(e, urllib.error.HTTPError)
                    and e.code == 403):
                # loterie d'IP datacenter : bruit attendu, pas une panne —
                # les passes locales (PC allumé) détectent les vraies casses
                log(f"{nom} : IP GitHub refusée (403), prochaine passe")
                continue
            etat_p["echecs"] = etat_p.get("echecs", 0) + 1
            log(f"ECHEC {nom} ({etat_p['echecs']}x) : {type(e).__name__}: {e}")
            if (etat_p["echecs"] >= SEUIL_PANNE
                    and not etat_p.get("panne_notifiee")):
                etat_p["panne_notifiee"] = True
                try:
                    notifie(topic, f"Veille Doctolib en panne : {nom}",
                            f"Impossible de consulter la page depuis "
                            f"{etat_p['echecs']} passages "
                            f"({type(e).__name__}). Dernière erreur : {e}",
                            priorite=2, tags=["warning"],
                            url_clic=praticien["url"])
                except Exception:
                    pass
        time.sleep(1.5)  # politesse entre praticiens

    client.sauve_cookies()

    if CLOUD:
        # l'état est commité dans un dépôt public : on n'y garde rien
        # d'identifiant (pas de cache d'IDs, clés déjà hachées)
        for e_p in etats_p.values():
            e_p.pop("info_cache", None)
            e_p.pop("info_ts", None)
        # au moins un commit par mois pour que GitHub ne coupe pas le cron
        etat["keepalive"] = f"{date.today():%Y-%m}"

    try:
        with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
            json.dump(etat, f, ensure_ascii=False, indent=1)
    except OSError as e:
        log(f"ERREUR écriture etat.json : {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
