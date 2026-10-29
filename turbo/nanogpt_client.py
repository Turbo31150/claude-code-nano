#!/usr/bin/env python3
"""nanogpt_client — connecteur NanoGPT UNIQUE de Turbo OS (29/10/2026, missions OMEGA « intégration NanoGPT »,
« NanoGPT full backend » et « cockpit fournisseur de capacités »). Bibliothèque standard seulement.

Utilisé par : greffon cockpit v4 (modules/nanogpt.py), assistant :8650, turbo-auto (moteur nanogpt), claude-nano (profils),
CLI (`python3 ~/jarvis/turbo/nanogpt_client.py <opération> '<json>'`).

- Clé : lue à chaque appel dans ~/.config/nanogpt/credentials.env (600), jamais journalisée ni renvoyée.
- Politique : ~/jarvis/config/nanogpt-politique.json (interrupteur « actif », appelants, profils, plafonds, payant).
- Reprises : 429 → attente Retry-After (requête non traitée) ; 5xx et réseau → reprise exponentielle + gigue
  UNIQUEMENT pour les requêtes idempotentes (lectures, embeddings) ; jamais pour une génération au résultat incertain.
- Payant à l'acte (vidéo, voix, transcription, recherche web, lots, modèles hors abonnement) : refusé sauf
  politique « rubriques_payantes_autorisees » + confirme=True + plafond USD/jour local non atteint.
- Journal : ~/jarvis/data/nanogpt/usage.jsonl (appelant, opération, modèle, code, durée, jetons, coût déclaré ; jamais
  de contenu). Catalogues datés : ~/jarvis/data/nanogpt/catalogue/. Registre : capacites.json (+ historique daté).
- Artefacts (images, voix) : ~/jarvis/data/cockpit-v4/nanogpt/ (dossier existant du greffon cockpit).
"""
import base64
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

H = Path.home()
CREDS = H / ".config/nanogpt/credentials.env"
POLITIQUE = H / "jarvis/config/nanogpt-politique.json"
DONNEES = H / "jarvis/data/nanogpt"
ARTEFACTS = H / "jarvis/data/cockpit-v4/nanogpt"
API = "https://api.nano-gpt.com"
SITE = "https://nano-gpt.com"
CATALOGUES = {"texte": "/api/v1/models?detailed=true", "image": "/api/v1/image-models?detailed=true",
              "video": "/api/v1/video-models?detailed=true", "audio": "/api/v1/audio-models?detailed=true",
              "3d": "/api/v1/3d-models", "embeddings": "/api/v1/embedding-models", "rerank": "/api/v1/rerank-models"}
ABONNEMENT = {"texte": "models", "image": "image-models", "embeddings": "embedding-models"}   # listes publiées (vidéo/audio/3D : 404)
RX_MODELE = re.compile(r"^[A-Za-z0-9._:/+@-]{1,160}$")
RX_CLE = re.compile(r"sk-nano-[A-Za-z0-9_-]+")
IMAGE_MAX_OCTETS = 10 * 1024 * 1024
_POL = {"mtime": None, "donnees": None}
_SEM = {"n": None, "sem": None}
_VERROU = threading.Lock()


class ErreurNanoGPT(Exception):
    """type : politique, entree, auth, solde, quota, serveur, reseau, reponse, non_supporte."""

    def __init__(self, type_, message, http=None):
        super().__init__(message)
        self.type, self.http = type_, http

    def dict(self):
        return {"ok": False, "erreur": str(self), "type": self.type, "http": self.http, "solution": SOLUTIONS.get(self.type, "")}


SOLUTIONS = {"politique": "Voir ~/jarvis/config/nanogpt-politique.json (actif, appelants, payant, plafond) ou confirmer explicitement.",
             "solde": "Solde NanoGPT insuffisant pour du payant à l'acte : rester sur les modèles de l'abonnement ou recharger (Franck).",
             "quota": "Limite NanoGPT atteinte : attendre la remise à zéro (Retry-After) ou réduire la cadence.",
             "auth": "Clé NanoGPT refusée : vérifier ~/.config/nanogpt (Franck seul peut la renouveler).",
             "entree": "Paramètres invalides : vérifier le modèle (discover_models) et la taille des entrées.",
             "serveur": "Incident chez NanoGPT : réessayer plus tard ou basculer sur un modèle local (:1234).",
             "reseau": "NanoGPT injoignable : vérifier la connexion ; repli local possible.",
             "non_supporte": "Opération non disponible : voir discover_capabilities pour les alternatives."}


# ───────────────────────────── socle ─────────────────────────────
def politique():
    p = Path(POLITIQUE)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        raise ErreurNanoGPT("politique", f"politique absente : {p}")
    with _VERROU:
        if _POL["mtime"] != mtime:
            _POL.update(mtime=mtime, donnees=json.loads(p.read_text(encoding="utf-8")))
        return _POL["donnees"]


def _semaphore():
    n = max(1, int(politique().get("concurrence", 4)))
    with _VERROU:
        if _SEM["n"] != n:
            _SEM.update(n=n, sem=threading.BoundedSemaphore(n))
        return _SEM["sem"]


def _cle():
    for ligne in open(CREDS, encoding="utf-8"):
        m = re.match(r"\s*NANOGPT_API_KEY\s*=\s*(\S+)", ligne)
        if m:
            return m.group(1)
    raise ErreurNanoGPT("auth", "clé NanoGPT absente du fichier d'identifiants")


def _masquer(texte):
    return RX_CLE.sub("sk-nano-***", str(texte))


def _dossier(p):
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _journal(**entree):
    ligne = {"t": time.strftime("%F %T"), **entree}
    with _VERROU:
        with open(_dossier(Path(DONNEES)) / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")


def _type_http(code):
    if code == 402:
        return "solde"
    if code == 429:
        return "quota"
    if code in (401, 403):
        return "auth"
    return "serveur" if code >= 500 else "entree"


def _retry_after(entetes):
    v = (entetes or {}).get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def _gigue(essai):
    return min(30.0, 1.5 * 2 ** (essai - 1)) * (0.5 + random.random() / 2)


def _cout(d):
    if not isinstance(d, dict):
        return None
    for v in (d.get("cost"), (d.get("nanoGPT") or {}).get("cost") if isinstance(d.get("nanoGPT"), dict) else None,
              (d.get("usage") or {}).get("cost") if isinstance(d.get("usage"), dict) else None):
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _requete(operation, url, corps=None, *, appelant, modele=None, idempotent=False, delai=None, binaire=False, methode=None):
    """Appel HTTP unique et gouverné. Renvoie (données, méta). Lève ErreurNanoGPT."""
    pol = politique()
    if not pol.get("actif"):
        raise ErreurNanoGPT("politique", "NanoGPT désactivé par la politique (actif=false)")
    if appelant not in pol.get("appelants", []):
        raise ErreurNanoGPT("politique", f"appelant non autorisé par la politique : {appelant}")
    delai = delai or pol.get("delai_s", 120)
    reprises, attente_max = int(pol.get("reprises_max", 3)), float(pol.get("attente_retry_after_max_s", 60))
    k = _cle()
    donnees = json.dumps(corps).encode() if corps is not None else None
    methode = methode or ("POST" if donnees is not None else "GET")
    t0, essai = time.time(), 0

    def fin(ok, http, erreur=None, d=None):
        u = (d or {}).get("usage") if isinstance(d, dict) else None
        _journal(appelant=appelant, operation=operation, modele=modele, http=http, ok=ok, essais=essai,
                 duree_s=round(time.time() - t0, 2), jetons_entree=(u or {}).get("prompt_tokens") or (u or {}).get("input_tokens"),
                 jetons_sortie=(u or {}).get("completion_tokens") or (u or {}).get("output_tokens"),
                 usd=_cout(d), erreur=erreur)

    with _semaphore():
        while True:
            essai += 1
            req = urllib.request.Request(url, data=donnees, method=methode, headers={
                "Authorization": f"Bearer {k}", "x-api-key": k, "Content-Type": "application/json",
                "User-Agent": "turbo-os-nanogpt-client/1"})
            try:
                with urllib.request.urlopen(req, timeout=delai) as r:
                    octets, http = r.read(), r.status
                break
            except urllib.error.HTTPError as e:
                texte = _masquer(e.read()[:500].decode("utf-8", "replace"))
                attente = None
                if essai <= reprises:
                    if e.code == 429:                                   # non traitée : reprise sûre
                        ra = _retry_after(e.headers)
                        attente = _gigue(essai) if ra is None else (ra if ra <= attente_max else None)
                    elif e.code in (500, 502, 503, 504) and idempotent:
                        attente = _gigue(essai)
                if attente is None:
                    fin(False, e.code, _type_http(e.code))
                    raise ErreurNanoGPT(_type_http(e.code), f"HTTP {e.code} : {texte}", http=e.code)
                time.sleep(attente)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if not idempotent or essai > reprises:
                    fin(False, None, "reseau")
                    raise ErreurNanoGPT("reseau", _masquer(f"{type(e).__name__} : {e}")[:300])
                time.sleep(_gigue(essai))
    if binaire:
        fin(True, http)
        return octets, {"duree_s": round(time.time() - t0, 2), "essais": essai, "http": http}
    try:
        d = json.loads(octets)
    except ValueError:
        fin(False, http, "reponse")
        raise ErreurNanoGPT("reponse", "réponse NanoGPT non JSON", http=http)
    fin(True, http, d=d)
    return d, {"duree_s": round(time.time() - t0, 2), "essais": essai, "http": http}


# ───────────────────────────── catalogue et garde-fous ─────────────────────────────
def _liste(d):
    L = d.get("data") if isinstance(d, dict) else d
    return list(L.values()) if isinstance(L, dict) else (L or [])


def discover_models(rubrique="texte", rafraichir=False, appelant="cli"):
    """Catalogue d'une rubrique, mis en cache par jour (historique conservé) : ~/jarvis/data/nanogpt/catalogue/."""
    if rubrique not in CATALOGUES:
        raise ErreurNanoGPT("entree", f"rubrique inconnue : {rubrique} ({', '.join(CATALOGUES)})")
    f = _dossier(Path(DONNEES) / "catalogue") / f"{rubrique}-{time.strftime('%Y%m%d')}.json"
    if f.is_file() and not rafraichir:
        return json.loads(f.read_text(encoding="utf-8"))
    brut, _ = _requete("discover_models", SITE + CATALOGUES[rubrique], appelant=appelant, idempotent=True, delai=60)
    inclus = None
    if rubrique in ABONNEMENT:
        d, _ = _requete("discover_models", SITE + f"/api/subscription/v1/{ABONNEMENT[rubrique]}", appelant=appelant, idempotent=True, delai=60)
        inclus = {m.get("id") for m in _liste(d)}
    modeles = []
    for m in _liste(brut):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        modeles.append({"id": m["id"], "nom": m.get("name") or m["id"], "inclus": (m["id"] in inclus) if inclus is not None else None,
                        "contexte": m.get("context_length"), "sortie_max": m.get("max_output_tokens"),
                        "capacites": sorted(k for k, v in (m.get("capabilities") or {}).items() if v is True),
                        "prix": m.get("pricing"), "description": (m.get("description") or "")[:240]})
    sortie = {"rubrique": rubrique, "date": time.strftime("%F %T"), "total": len(modeles),
              "inclus": sum(1 for m in modeles if m["inclus"]), "modeles": modeles}
    f.write_text(json.dumps(sortie, ensure_ascii=False), encoding="utf-8")
    return sortie


GROUPES = {"profil": "★ Profils", "abonnement": "Abonnement — avec outils", "abonnement-sans-outils": "Abonnement — ⚠ sans outils",
           "payant": "💲 Payants (solde requis) — avec outils", "payant-sans-outils": "💲 Payants (solde requis) — ⚠ sans outils"}


def liste_modeles(appelant="cli", payants=True, rafraichir=False):
    """TOUS les modèles texte, MÊME méthode partout (claude-nano, /model, assistant du cockpit, OpenClaw) — 29/10/2026.
    Ordre : profils de la politique, abonnement avec outils, abonnement sans outils, payants avec outils, payants sans outils ;
    modèles interdits par la politique exclus. « libelle » = étiquettes communes (abonnement / 💲, contexte, ⚠, ∥, raisonnement, vision)."""
    pol = politique()
    profils = {v: k for k, v in (pol.get("profils") or {}).items() if v}
    interdits = tuple(pol.get("modeles_interdits_prefixes") or ())
    ordre = {g: i for i, g in enumerate(GROUPES)}
    sortie = []
    for m in discover_models("texte", rafraichir=rafraichir, appelant=appelant)["modeles"]:
        if m["id"].lower().startswith(interdits) or (not payants and not m["inclus"]):
            continue
        c, ctx = m["capacites"], m.get("contexte")
        outils = "tool_calling" in c
        groupe = "profil" if m["id"] in profils else ("abonnement" if m["inclus"] else "payant") + ("" if outils else "-sans-outils")
        etiq = (["profil " + profils[m["id"]]] if m["id"] in profils else []) + (["abonnement"] if m["inclus"] else ["💲 payant (solde)"])
        etiq += [f"{ctx // 1000} k" if isinstance(ctx, int) else "? k"] + ([] if outils else ["⚠ sans outils"])
        etiq += (["∥ parallèle"] if "parallel_tool_calls" in c else []) + (["raisonnement"] if "reasoning" in c else [])
        etiq += ["vision"] if "vision" in c else []
        sortie.append({"id": m["id"], "nom": m["nom"] or m["id"], "groupe": groupe, "profil": profils.get(m["id"]),
                       "inclus": bool(m["inclus"]), "outils": outils, "capacites": c, "contexte": ctx, "sortie_max": m.get("sortie_max"),
                       "prix": m.get("prix"), "libelle": " · ".join(etiq)})
    return sorted(sortie, key=lambda m: (ordre[m["groupe"]], m["id"].lower()))


def _modele(rubrique, profil, modele, capacite=None, appelant="cli"):
    pol = politique()
    ident = modele or (pol.get("profils") or {}).get(profil or "")
    if not ident:
        raise ErreurNanoGPT("entree", f"aucun modèle : ni « modele » ni profil « {profil} » dans la politique")
    if not RX_MODELE.match(ident):
        raise ErreurNanoGPT("entree", "identifiant de modèle invalide")
    if any(ident.lower().startswith(p) for p in pol.get("modeles_interdits_prefixes", [])):
        raise ErreurNanoGPT("politique", f"modèle interdit par la politique (préfixe) : {ident}")
    cat = {m["id"]: m for m in discover_models(rubrique, appelant=appelant)["modeles"]}
    m = cat.get(ident)
    if m is None:
        alt = (pol.get("profils") or {}).get(profil or "") or next((i for i, x in cat.items() if x["inclus"]), None)
        raise ErreurNanoGPT("entree", f"modèle absent du catalogue {rubrique} : {ident}" + (f" (alternative : {alt})" if alt else ""))
    if capacite and capacite not in m["capacites"]:
        raise ErreurNanoGPT("entree", f"le modèle {ident} ne déclare pas la capacité « {capacite} »")
    return m


def depense_jour():
    f = Path(DONNEES) / "usage.jsonl"
    if not f.is_file():
        return 0.0
    jour, total = time.strftime("%F"), 0.0
    for ligne in open(f, encoding="utf-8"):
        try:
            e = json.loads(ligne)
        except ValueError:
            continue
        if e.get("t", "").startswith(jour) and isinstance(e.get("usd"), (int, float)):
            total += e["usd"]
    return round(total, 4)


def _garde_payant(payant, confirme, operation):
    if not payant:
        return
    pol = politique()
    if not pol.get("rubriques_payantes_autorisees"):
        raise ErreurNanoGPT("politique", f"{operation} : payant à l'acte, interdit par la politique (rubriques_payantes_autorisees=false)")
    if confirme is not True:
        raise ErreurNanoGPT("politique", f"{operation} : payant à l'acte, confirmation explicite requise (confirme=true)")
    plafond, dep = float(pol.get("plafond_usd_jour", 0)), depense_jour()
    if dep >= plafond:
        raise ErreurNanoGPT("politique", f"{operation} : plafond local atteint ({dep} $ dépensés sur {plafond} $ aujourd'hui)")


def _taille(texte):
    if len(texte) > int(politique().get("taille_entree_max_caracteres", 60000)):
        raise ErreurNanoGPT("entree", "entrée trop longue pour la politique (taille_entree_max_caracteres)")


def _image_url(image):
    """Chemin local (image seulement, ≤ 10 Mo, signature vérifiée), URL https ou data URL."""
    s = str(image or "")
    if s.startswith("data:image/") or s.startswith("https://"):
        return s
    p = Path(s).expanduser()
    if not p.is_file():
        raise ErreurNanoGPT("entree", "image introuvable (chemin local, https:// ou data:image/…)")
    octets = p.read_bytes()
    if len(octets) > IMAGE_MAX_OCTETS:
        raise ErreurNanoGPT("entree", "image trop lourde (> 10 Mo)")
    signatures = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif", b"RIFF": "webp"}
    mime = next((v for k, v in signatures.items() if octets.startswith(k)), None)
    if not mime:
        raise ErreurNanoGPT("entree", "fichier non reconnu comme image (PNG, JPEG, GIF, WEBP)")
    return f"data:image/{mime};base64," + base64.b64encode(octets).decode()


def _artefact(octets, extension, operation):
    nom = f"{time.strftime('%Y%m%d-%H%M%S')}-{operation}{extension}"
    (_dossier(Path(ARTEFACTS)) / nom).write_bytes(octets)
    return nom


def _taches(maj=None):
    f = _dossier(Path(DONNEES)) / "taches.json"
    with _VERROU:
        t = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        if maj:
            t.setdefault(maj["id"], {}).update(maj)
            f.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
        return t


def _image_reponse(d, operation):
    img = (_liste(d) or [{}])[0]
    if img.get("b64_json"):
        octets = base64.b64decode(img["b64_json"])
    elif img.get("url"):
        octets = urllib.request.urlopen(img["url"], timeout=120).read()
    else:
        raise ErreurNanoGPT("reponse", "aucune image renvoyée")
    return _artefact(octets, ".png", operation)


# ───────────────────────────── opérations ─────────────────────────────
def generate_text(consigne=None, messages=None, profil="auto", modele=None, systeme=None, max_tokens=None, appelant="cli", confirme=False,
                  _operation="generate_text"):
    m = _modele("texte", profil, modele, appelant=appelant)
    _garde_payant(not m["inclus"], confirme, "generate_text")
    if not messages:
        if not consigne:
            raise ErreurNanoGPT("entree", "consigne vide")
        messages = ([{"role": "system", "content": systeme}] if systeme else []) + [{"role": "user", "content": consigne}]
    _taille("".join(c if isinstance(c, str) else "".join(x.get("text", "") for x in c if isinstance(x, dict))
                    for c in (msg.get("content") for msg in messages)))
    plafond = int(politique().get("jetons_sortie_max", 4000))
    d, meta = _requete(_operation, API + "/api/v1/chat/completions",
                       {"model": m["id"], "messages": messages, "max_tokens": min(int(max_tokens or plafond), plafond)},
                       appelant=appelant, modele=m["id"])
    try:
        texte = d["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise ErreurNanoGPT("reponse", "réponse de chat sans « choices »")
    u = d.get("usage") or {}
    return {"ok": True, "operation": "generate_text", "modele": d.get("model") or m["id"], "inclus": m["inclus"], "texte": texte,
            "jetons": [u.get("prompt_tokens"), u.get("completion_tokens")], **meta}


def analyze_image(image, question="Décris cette image en français.", profil="vision", modele=None, max_tokens=800, appelant="cli", confirme=False):
    m = _modele("texte", profil, modele, capacite="vision", appelant=appelant)
    _garde_payant(not m["inclus"], confirme, "analyze_image")
    contenu = [{"type": "text", "text": str(question)[:4000]}, {"type": "image_url", "image_url": {"url": _image_url(image)}}]
    r = generate_text(messages=[{"role": "user", "content": contenu}], modele=m["id"], max_tokens=max_tokens, appelant=appelant,
                      confirme=confirme, _operation="analyze_image")
    return {**r, "operation": "analyze_image"}


def generate_image(prompt, profil="image", modele=None, taille="1024x1024", appelant="cli", confirme=False):
    m = _modele("image", profil, modele, capacite="image_generation", appelant=appelant)
    _garde_payant(not m["inclus"], confirme, "generate_image")
    if not str(prompt or "").strip() or len(prompt) > 4000:
        raise ErreurNanoGPT("entree", "consigne d'image vide ou trop longue")
    d, meta = _requete("generate_image", API + "/v1/images/generations",
                       {"model": m["id"], "prompt": prompt, "n": 1, "size": taille, "response_format": "b64_json"},
                       appelant=appelant, modele=m["id"], delai=300)
    return {"ok": True, "operation": "generate_image", "modele": m["id"], "inclus": m["inclus"], "fichier": _image_reponse(d, "image"), **meta}


def edit_image(image, prompt, profil="edition_image", modele=None, taille=None, appelant="cli", confirme=False):
    m = _modele("image", profil, modele, capacite="image_to_image", appelant=appelant)
    _garde_payant(not m["inclus"], confirme, "edit_image")
    if not str(prompt or "").strip() or len(prompt) > 4000:
        raise ErreurNanoGPT("entree", "consigne d'édition vide ou trop longue")
    corps = {"model": m["id"], "prompt": prompt, "image": _image_url(image), "response_format": "b64_json", **({"size": taille} if taille else {})}
    d, meta = _requete("edit_image", API + "/api/v1/images/edit", corps, appelant=appelant, modele=m["id"], delai=300)
    return {"ok": True, "operation": "edit_image", "modele": m["id"], "inclus": m["inclus"], "fichier": _image_reponse(d, "edition"), **meta}


def generate_video(prompt, modele=None, duree=None, resolution=None, appelant="cli", confirme=False):
    _garde_payant(True, confirme, "generate_video")                      # aucune vidéo dans l'abonnement (catalogue)
    m = _modele("video", "video", modele, appelant=appelant)
    corps = {"model": m["id"], "prompt": str(prompt)[:4000], **({"duration": duree} if duree else {}), **({"resolution": resolution} if resolution else {})}
    d, meta = _requete("generate_video", API + "/api/generate-video", corps, appelant=appelant, modele=m["id"], delai=120)
    ident = d.get("runId") or d.get("requestId") or d.get("id")
    _taches({"id": ident, "type": "video", "modele": m["id"], "cree": time.strftime("%F %T"), "statut": d.get("status") or "soumis"})
    return {"ok": True, "operation": "generate_video", "modele": m["id"], "tache": ident, **meta}


def transcribe_audio(audio_url, modele=None, langue="auto", appelant="cli", confirme=False):
    if not str(audio_url or "").startswith("https://"):
        raise ErreurNanoGPT("entree", "audio_url https:// requise (fichier local non pris en charge par ce connecteur)")
    _garde_payant(True, confirme, "transcribe_audio")
    m = _modele("audio", "transcription", modele, appelant=appelant)
    d, meta = _requete("transcribe_audio", API + "/api/transcribe", {"audioUrl": audio_url, "model": m["id"], "language": langue},
                       appelant=appelant, modele=m["id"], delai=300)
    if d.get("runId") and not d.get("transcription"):
        _taches({"id": d["runId"], "type": "transcription", "modele": m["id"], "cree": time.strftime("%F %T"), "statut": d.get("status") or "soumis"})
        return {"ok": True, "operation": "transcribe_audio", "tache": d["runId"], **meta}
    return {"ok": True, "operation": "transcribe_audio", "modele": m["id"], "texte": d.get("transcription") or d.get("text") or "", **meta}


def synthesize_speech(texte, modele=None, voix=None, appelant="cli", confirme=False):
    if not str(texte or "").strip() or len(texte) > 4000:
        raise ErreurNanoGPT("entree", "texte vide ou trop long (4 000 caractères)")
    _garde_payant(True, confirme, "synthesize_speech")
    m = _modele("audio", "voix", modele, appelant=appelant)
    octets, meta = _requete("synthesize_speech", API + "/api/v1/audio/speech",
                            {"model": m["id"], "input": texte, "response_format": "mp3", **({"voice": voix} if voix else {})},
                            appelant=appelant, modele=m["id"], delai=180, binaire=True)
    return {"ok": True, "operation": "synthesize_speech", "modele": m["id"], "fichier": _artefact(octets, ".mp3", "voix"), **meta}


def create_embeddings(textes, profil="embeddings", modele=None, appelant="cli", confirme=False):
    textes = [textes] if isinstance(textes, str) else list(textes or [])
    if not textes or len(textes) > 256 or any(not isinstance(t, str) or not t or len(t) > 8000 for t in textes):
        raise ErreurNanoGPT("entree", "1 à 256 textes non vides de 8 000 caractères au plus")
    m = _modele("embeddings", profil, modele, appelant=appelant)
    _garde_payant(True, confirme, "create_embeddings")     # listés « abonnement » mais facturés : 402 constaté le 29/10 (0,0001 $ requis)
    d, meta = _requete("create_embeddings", API + "/api/v1/embeddings", {"model": m["id"], "input": textes},
                       appelant=appelant, modele=m["id"], idempotent=True)
    vecteurs = [e.get("embedding") for e in sorted(_liste(d), key=lambda e: e.get("index", 0))]
    if len(vecteurs) != len(textes) or not all(isinstance(v, list) for v in vecteurs):
        raise ErreurNanoGPT("reponse", "nombre de vecteurs incohérent")
    return {"ok": True, "operation": "create_embeddings", "modele": m["id"], "nombre": len(vecteurs),
            "dimensions": len(vecteurs[0]), "vecteurs": vecteurs, **meta}


def rerank_documents(*_, **__):
    raise ErreurNanoGPT("non_supporte", "reranking : NanoGPT publie un catalogue (/api/v1/rerank-models, 1 modèle) mais aucun endpoint "
                                        "documenté (vérifié le 29/10/2026) ; alternative : create_embeddings + similarité cosinus")


def search_web(requete, profondeur="standard", appelant="cli", confirme=False):
    if not str(requete or "").strip() or len(requete) > 500 or profondeur not in ("standard", "deep"):
        raise ErreurNanoGPT("entree", "requête vide/trop longue ou profondeur invalide (standard, deep)")
    _garde_payant(True, confirme, "search_web")                          # 0,006 $ par recherche (doc)
    d, meta = _requete("search_web", API + "/api/web", {"query": requete, "depth": profondeur}, appelant=appelant, delai=120)
    return {"ok": True, "operation": "search_web", "resultats": d.get("data") or d.get("results") or d, "usd": _cout(d), **meta}


def submit_batch(requetes, modele=None, profil="fast", max_tokens=512, appelant="cli", confirme=False):
    """Lot natif (format Messages) : facturé sur le solde, jamais sur l'abonnement (doc Batch API)."""
    if not requetes or len(requetes) > 1000:
        raise ErreurNanoGPT("entree", "1 à 1 000 requêtes {custom_id, consigne}")
    _garde_payant(True, confirme, "submit_batch")
    m = _modele("texte", profil, modele, capacite="batch_api", appelant=appelant)
    corps = {"requests": [{"custom_id": str(r.get("custom_id") or i), "params": {"model": m["id"], "max_tokens": int(max_tokens),
             "messages": [{"role": "user", "content": str(r.get("consigne") or "")[:20000]}]}} for i, r in enumerate(requetes)]}
    d, meta = _requete("submit_batch", API + "/api/v1/messages/batches", corps, appelant=appelant, modele=m["id"])
    _taches({"id": d.get("id"), "type": "lot", "modele": m["id"], "cree": time.strftime("%F %T"), "statut": d.get("processing_status") or "soumis"})
    return {"ok": True, "operation": "submit_batch", "tache": d.get("id"), **meta}


def check_job_status(id, type=None, appelant="cli"):
    t = _taches().get(id) or {}
    genre = type or t.get("type")
    q = urllib.parse.quote(str(id), safe="")
    if genre == "video":
        d, _ = _requete("check_job_status", API + f"/api/video/status?requestId={q}", appelant=appelant, idempotent=True)
        d = d.get("data") or d
        url = (d.get("output") or {}).get("url") if isinstance(d.get("output"), dict) else None
        statut = d.get("status")
    elif genre == "transcription":
        d, _ = _requete("check_job_status", API + "/api/transcribe/status", {"runId": id}, appelant=appelant, idempotent=True)
        statut, url = d.get("status"), None
    elif genre == "lot":
        d, _ = _requete("check_job_status", API + f"/api/v1/messages/batches/{q}", appelant=appelant, idempotent=True)
        statut, url = d.get("processing_status"), d.get("results_url")
    else:
        raise ErreurNanoGPT("entree", "type de tâche inconnu (video, transcription, lot)")
    _taches({"id": id, "type": genre, "statut": statut, "url": url, "maj": time.strftime("%F %T")})
    return {"ok": True, "operation": "check_job_status", "tache": id, "type": genre, "statut": statut, "url": url}


# ───────────────────────────── registre des capacités ─────────────────────────────
OPERATIONS = {
    "discover_models": (discover_models, "catalogue d'une rubrique (texte, image, video, audio, 3d, embeddings, rerank)", "GET /api/v1/*-models", None, "gratuit"),
    "discover_capabilities": (None, "registre des capacités et de leur statut de validation", "local", None, "gratuit"),
    "generate_text": (generate_text, "texte, raisonnement, code (profils coding, reasoning, fast, research, auto)", "POST /api/v1/chat/completions", "texte", "abonnement"),
    "analyze_image": (analyze_image, "analyse d'une image par un modèle vision", "POST /api/v1/chat/completions (image_url)", "texte", "abonnement"),
    "generate_image": (generate_image, "génération d'image (100/jour dans l'abonnement)", "POST /v1/images/generations", "image", "abonnement"),
    "edit_image": (edit_image, "édition d'image (image_to_image)", "POST /api/v1/images/edit", "image", "abonnement"),
    "generate_video": (generate_video, "génération vidéo asynchrone", "POST /api/generate-video", "video", "payant"),
    "transcribe_audio": (transcribe_audio, "transcription d'un audio https://", "POST /api/transcribe", "audio", "payant"),
    "synthesize_speech": (synthesize_speech, "synthèse vocale (mp3)", "POST /api/v1/audio/speech", "audio", "payant"),
    "create_embeddings": (create_embeddings, "vecteurs d'embeddings (listés « abonnement » mais facturés : 402 constaté le 29/10)", "POST /api/v1/embeddings", "embeddings", "payant"),
    "rerank_documents": (rerank_documents, "reranking", "non documenté", "rerank", "inconnu"),
    "search_web": (search_web, "recherche web directe", "POST /api/web", None, "payant"),
    "submit_batch": (submit_batch, "lot asynchrone (format Messages), solde seulement", "POST /api/v1/messages/batches", "texte", "payant"),
    "check_job_status": (check_job_status, "suivi des tâches asynchrones (vidéo, transcription, lot)", "GET/POST */status", None, "gratuit"),
}


def _validations():
    f = Path(DONNEES) / "validations.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def noter_validation(operation, statut, preuve):
    """statut : valide, echec, bloque_solde, bloque_politique, non_supporte, non_teste."""
    v = _validations()
    v[operation] = {"statut": statut, "preuve": _masquer(preuve)[:300], "date": time.strftime("%F %T")}
    (_dossier(Path(DONNEES)) / "validations.json").write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")


def discover_capabilities(rafraichir=False, appelant="cli"):
    pol, val = politique(), _validations()
    caps = []
    for op, (_, desc, endpoint, rubrique, cout) in OPERATIONS.items():
        compat = None
        if rubrique:
            try:
                cat = discover_models(rubrique, rafraichir=rafraichir, appelant=appelant)
                compat = {"total": cat["total"], "inclus": cat["inclus"]}
            except ErreurNanoGPT as e:
                compat = {"erreur": str(e)[:120]}
        payant = cout == "payant"
        bloque = payant and (not pol.get("rubriques_payantes_autorisees") or float(pol.get("plafond_usd_jour", 0)) <= 0)
        v = val.get(op) or {"statut": "non_teste"}
        caps.append({"operation": op, "description": desc, "endpoint": endpoint, "cout": cout, "modeles": compat,
                     "profil_defaut": {"generate_text": pol["profils"].get("auto"), "analyze_image": pol["profils"].get("vision"),
                                       "generate_image": pol["profils"].get("image"), "edit_image": pol["profils"].get("edition_image"),
                                       "create_embeddings": pol["profils"].get("embeddings"), "synthesize_speech": pol["profils"].get("voix"),
                                       "transcribe_audio": pol["profils"].get("transcription")}.get(op),
                     "politique": "bloqué (payant, plafond 0 $)" if bloque else ("désactivé" if not pol.get("actif") else "autorisé"),
                     "validation": v, "disponible": v.get("statut") == "valide" and not bloque and bool(pol.get("actif"))})
    registre = {"date": time.strftime("%F %T"), "politique_version": pol.get("version"), "actif": pol.get("actif"), "capacites": caps}
    d = _dossier(Path(DONNEES))
    (d / "capacites.json").write_text(json.dumps(registre, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / f"capacites-{time.strftime('%Y%m%d')}.json").write_text(json.dumps(registre, ensure_ascii=False, indent=1), encoding="utf-8")
    return registre


def compte(appelant="cli"):
    solde, _ = _requete("compte", SITE + "/api/check-balance", {}, appelant=appelant, idempotent=True, delai=30)
    ab, _ = _requete("compte", API + "/api/v1/subscription/usage", appelant=appelant, idempotent=True, delai=30)
    return {"source": "NanoGPT", "solde_usd": solde.get("usd_balance"), "abonnement_actif": ab.get("active"),
            "limites": ab.get("limits"), "jetons_semaine": ab.get("weeklyInputTokens"), "images_jour": ab.get("dailyImages")}


def usage(jours=1):
    """Métriques CALCULÉES LOCALEMENT depuis le journal (distinctes des compteurs NanoGPT de compte())."""
    f = Path(DONNEES) / "usage.jsonl"
    seuil = time.strftime("%F", time.localtime(time.time() - 86400 * (jours - 1)))
    lignes = []
    if f.is_file():
        for l in open(f, encoding="utf-8"):
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if e.get("t", "")[:10] >= seuil:
                lignes.append(e)
    durees = sorted(e["duree_s"] for e in lignes if e.get("ok") and isinstance(e.get("duree_s"), (int, float)))
    q = lambda p: durees[min(len(durees) - 1, int(p * len(durees)))] if durees else None
    par = {}
    for e in lignes:
        for cle in ("appelant", "operation", "modele"):
            g = par.setdefault(cle, {}).setdefault(str(e.get(cle)), {"n": 0, "echecs": 0, "jetons_entree": 0, "jetons_sortie": 0, "usd": 0.0})
            g["n"] += 1
            g["echecs"] += 0 if e.get("ok") else 1
            g["jetons_entree"] += e.get("jetons_entree") or 0
            g["jetons_sortie"] += e.get("jetons_sortie") or 0
            g["usd"] = round(g["usd"] + (e.get("usd") or 0), 4)
    erreurs = {}
    for e in lignes:
        if not e.get("ok"):
            erreurs[e.get("erreur") or "?"] = erreurs.get(e.get("erreur") or "?", 0) + 1
    return {"source": "journal local", "depuis": seuil, "requetes": len(lignes), "reussies": sum(1 for e in lignes if e.get("ok")),
            "echecs_par_type": erreurs, "latence_p50_s": q(0.5), "latence_p95_s": q(0.95), "par": par, "usd_declares": depense_jour()}


def executer(operation, parametres=None, appelant="cli"):
    """Point d'entrée unique (cockpit, assistant) : renvoie toujours un dict, jamais d'exception."""
    parametres = dict(parametres or {})
    parametres.pop("appelant", None)
    try:
        if operation == "discover_capabilities":
            return {"ok": True, **discover_capabilities(bool(parametres.get("rafraichir")), appelant=appelant)}
        if operation not in OPERATIONS or OPERATIONS[operation][0] is None:
            raise ErreurNanoGPT("entree", f"opération inconnue : {operation}")
        r = OPERATIONS[operation][0](**parametres, appelant=appelant)
        return r if isinstance(r, dict) and "ok" in r else {"ok": True, **(r if isinstance(r, dict) else {"resultat": r})}
    except ErreurNanoGPT as e:
        return {**e.dict(), "operation": operation}
    except TypeError as e:
        return {"ok": False, "type": "entree", "operation": operation, "erreur": f"paramètres invalides : {e}"[:300]}


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__ + "\nOpérations : " + ", ".join(OPERATIONS) + ", compte, usage")
        sys.exit(0)
    op, params = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    if op == "compte":
        r = compte()
    elif op == "usage":
        r = usage(int(params.get("jours", 1)))
    elif op == "modeles":
        L = liste_modeles(payants=params.get("payants", True))
        r = {"ok": True, "total": len(L), "par_groupe": {GROUPES[g]: sum(1 for m in L if m["groupe"] == g) for g in GROUPES},
             "premiers": [f"{m['id']} — {m['libelle']}" for m in L[:8]]}
    else:
        r = executer(op, params)
        if op == "create_embeddings" and r.get("ok"):
            r["vecteurs"] = f"{r['nombre']} vecteurs de dimension {r['dimensions']} (masqués)"
        if op == "discover_models" and r.get("ok"):
            r["modeles"] = [m["id"] for m in r.get("modeles", [])][:40]
    print(json.dumps(r, ensure_ascii=False, indent=1))
    sys.exit(0 if r.get("ok", True) else 1)
