# -*- coding: utf-8 -*-
"""Sonde diagnostic : teste chaque étape (page HTML, info.json,
availabilities.json) sur un profil témoin aléatoire puis sur les profils
passés via la variable d'environnement PROBE_SLUGS (jamais affichés).
N'affiche que des codes de statut."""
import json
import gzip
import os
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import datetime

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

COMMUNS = {
    "User-Agent": UA,
    "Accept-Language": "fr-FR,fr;q=0.9",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", '
                 '"Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class Client:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _open(self, url, headers):
        req = urllib.request.Request(url, headers={**COMMUNS, **headers})
        r = self.op.open(req, timeout=30)
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return r.status, body

    def get_html(self, url):
        return self._open(url, {
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })

    def get_json(self, url, referer):
        st, body = self._open(url, {
            "Accept": "application/json", "Referer": referer,
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        })
        return st, json.loads(body)


def teste(nom_affiche, slug, chemin_page=None):
    """Déroule page -> info -> availabilities, affiche les statuts."""
    c = Client()
    page = "https://www.doctolib.fr" + (
        chemin_page or "/profil/" + slug)  # chemin réel si connu
    try:
        st, _ = c.get_html(page)
        print(f"{nom_affiche} page: {st}")
    except urllib.error.HTTPError as e:
        print(f"{nom_affiche} page: HTTP {e.code}")
    try:
        st, d = c.get_json(
            "https://www.doctolib.fr/online_booking/api/"
            "slot_selection_funnel/v1/info.json?profile_slug="
            + urllib.parse.quote(slug) + "&locale=fr", page)
        data = d.get("data", {})
        motifs = data.get("visit_motives", [])
        agendas = [a for a in data.get("agendas", [])
                   if not a.get("booking_disabled")]
        print(f"{nom_affiche} info: {st} | motifs {len(motifs)} "
              f"| agendas {len(agendas)}")
    except urllib.error.HTTPError as e:
        print(f"{nom_affiche} info: HTTP {e.code} (stop)")
        return False
    if not motifs or not agendas:
        print(f"{nom_affiche} : pas de motif/agenda (stop)")
        return False
    q = urllib.parse.urlencode({
        "ignore_current_draft": "true",
        "start_date": str(datetime.date.today()),
        "visit_motive_ids": "-".join(str(m["id"]) for m in motifs),
        "agenda_ids": "-".join(str(a["id"]) for a in agendas),
        "practice_ids": "-".join(sorted({str(a.get("practice_id"))
                                         for a in agendas
                                         if a.get("practice_id")})),
        "limit": "15",
    })
    try:
        st, av = c.get_json(
            "https://www.doctolib.fr/availabilities.json?" + q, page)
        print(f"{nom_affiche} availabilities: {st} "
              f"| total {av.get('total')} | next_slot "
              f"{'oui' if av.get('next_slot') else 'non'}")
        return True
    except urllib.error.HTTPError as e:
        print(f"{nom_affiche} availabilities: HTTP {e.code}")
        return False


def main():
    # 1) témoin : un praticien aléatoire trouvé par autocomplete
    c = Client()
    temoin = None
    for recherche in ("martin", "dupont", "bernard"):
        try:
            st, d = c.get_json(
                "https://www.doctolib.fr/api/searchbar/autocomplete.json"
                "?search=" + recherche, "https://www.doctolib.fr/")
        except urllib.error.HTTPError as e:
            print("autocomplete: HTTP", e.code)
            continue
        for p in d.get("profiles", []):
            if p.get("owner_type") != "Organization" and p.get("link"):
                temoin = p["link"]
                break
        if temoin:
            break
    if temoin:
        slug = temoin.rstrip("/").rsplit("/", 1)[-1]
        teste("temoin", slug, temoin)
    else:
        print("pas de temoin trouve")

    # 2) cibles secrètes (jamais affichées) : "slug|/chemin/page,slug|/chemin"
    cibles = os.environ.get("PROBE_SLUGS", "")
    for i, entree in enumerate([x for x in cibles.split(",") if x.strip()], 1):
        entree = entree.strip()
        slug, _, chemin = entree.partition("|")
        teste(f"cible{i}", slug, chemin or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
