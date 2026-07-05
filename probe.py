# -*- coding: utf-8 -*-
"""Sonde : vérifie que les endpoints Doctolib répondent depuis cet hôte.
N'affiche que des codes de statut et des compteurs (pas de données)."""
import json
import gzip
import urllib.request
import urllib.parse
import urllib.error
import datetime

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json",
           "Accept-Language": "fr-FR,fr;q=0.9",
           "Referer": "https://www.doctolib.fr/"}


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    r = urllib.request.urlopen(req, timeout=30)
    body = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return r.status, json.loads(body)


def main():
    profils = []
    for recherche in ("martin", "dupont", "bernard"):
        st, d = get_json("https://www.doctolib.fr/api/searchbar/"
                         "autocomplete.json?search=" + recherche)
        print("autocomplete:", st)
        profils += [p for p in d.get("profiles", [])
                    if p.get("owner_type") != "Organization" and p.get("link")]
    print("candidats:", len(profils))
    ok = False
    for p in profils[:10]:
        slug = p["link"].rstrip("/").rsplit("/", 1)[-1]
        try:
            st, d = get_json(
                "https://www.doctolib.fr/online_booking/api/"
                "slot_selection_funnel/v1/info.json?profile_slug="
                + urllib.parse.quote(slug) + "&locale=fr")
        except urllib.error.HTTPError as e:
            print("info: HTTP", e.code, "(profil suivant)")
            continue
        data = d.get("data", {})
        motifs = data.get("visit_motives", [])
        agendas = [a for a in data.get("agendas", [])
                   if not a.get("booking_disabled")]
        print("info:", st, "| motifs:", len(motifs), "| agendas:", len(agendas))
        if not motifs or not agendas:
            continue
        q = urllib.parse.urlencode({
            "ignore_current_draft": "true",
            "start_date": str(datetime.date.today()),
            "visit_motive_ids": str(motifs[0]["id"]),
            "agenda_ids": "-".join(str(a["id"]) for a in agendas),
            "practice_ids": "-".join(sorted({str(a.get("practice_id"))
                                             for a in agendas
                                             if a.get("practice_id")})),
            "limit": "14",
        })
        st, av = get_json("https://www.doctolib.fr/availabilities.json?" + q)
        print("availabilities:", st, "| total:", av.get("total"))
        ok = True
        break
    print("RESULTAT:", "OK" if ok else "ECHEC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
