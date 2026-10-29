"""Tests hors réseau du connecteur NanoGPT (~/jarvis/turbo/nanogpt_client.py) : faux serveur HTTP sur 127.0.0.1.
Lancer seul : timeout 120 ~/jarvis/.venv/bin/pytest tests/test_nanogpt_client.py -q -p no:cacheprovider
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "turbo"))
import nanogpt_client as nc  # noqa: E402

CLE_FACTICE = "sk-nano-" + "f" * 32
SCENARIO = {"chat": [], "appels": []}


class Faux(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, corps, entetes=None):
        b = json.dumps(corps).encode()
        self.send_response(code)
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        SCENARIO["appels"].append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/api/v1/models"):
            return self._json(200, {"data": [{"id": "m/inclus", "capabilities": {"vision": True, "tool_calling": True}},
                                             {"id": "m/payant", "capabilities": {}}]})
        if self.path.startswith("/api/subscription/v1/models"):
            return self._json(200, {"data": [{"id": "m/inclus"}]})
        if self.path.startswith("/api/v1/embedding-models"):
            return self._json(200, {"data": [{"id": "e/inclus"}]})
        if self.path.startswith("/api/subscription/v1/embedding-models"):
            return self._json(200, {"data": [{"id": "e/inclus"}]})
        self._json(404, {"error": "inconnu"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        corps = json.loads(self.rfile.read(n) or b"{}")
        SCENARIO["appels"].append(("POST", self.path, corps))
        if self.path == "/api/v1/chat/completions":
            code, entetes = SCENARIO["chat"].pop(0) if SCENARIO["chat"] else (200, {})
            if code == "lent":
                time.sleep(2)
                code = 200
            if code != 200:
                return self._json(code, {"error": f"erreur {code} avec {CLE_FACTICE}"}, entetes)
            return self._json(200, {"model": corps["model"], "choices": [{"message": {"content": "BONJOUR"}}],
                                    "usage": {"prompt_tokens": 7, "completion_tokens": 1}, "cost": 0})
        if self.path == "/api/v1/embeddings":
            code, entetes = SCENARIO["chat"].pop(0) if SCENARIO["chat"] else (200, {})
            if code != 200:
                return self._json(code, {"error": "temporaire"}, entetes)
            return self._json(200, {"data": [{"index": i, "embedding": [0.1, 0.2, 0.3]} for i, _ in enumerate(corps["input"])]})
        self._json(404, {"error": "inconnu"})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Faux)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    creds = tmp_path / "credentials.env"
    creds.write_text(f"NANOGPT_API_KEY={CLE_FACTICE}\n")
    pol = {"version": 1, "actif": True, "concurrence": 2, "delai_s": 1, "reprises_max": 2, "attente_retry_after_max_s": 1,
           "jetons_sortie_max": 64, "taille_entree_max_caracteres": 1000, "rubriques_payantes_autorisees": False,
           "plafond_usd_jour": 0.0, "modeles_interdits_prefixes": ["anthropic/"],
           "profils": {"auto": "m/inclus", "vision": "m/inclus", "embeddings": "e/inclus"}, "appelants": ["tests"]}
    fpol = tmp_path / "politique.json"
    fpol.write_text(json.dumps(pol))
    for k, v in {"API": base, "SITE": base, "CREDS": creds, "POLITIQUE": fpol, "DONNEES": tmp_path / "donnees",
                 "ARTEFACTS": tmp_path / "artefacts"}.items():
        monkeypatch.setattr(nc, k, v)
    monkeypatch.setattr(nc, "_gigue", lambda essai: 0.01)
    nc._POL.update(mtime=None, donnees=None)
    SCENARIO["chat"].clear()
    SCENARIO["appels"].clear()
    yield {"pol": fpol, "tmp": tmp_path}
    srv.shutdown()


def _ecrire_pol(env, **maj):
    p = json.loads(env["pol"].read_text())
    p.update(maj)
    env["pol"].write_text(json.dumps(p))
    nc._POL.update(mtime=None)


def _journal(env):
    f = env["tmp"] / "donnees" / "usage.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def test_texte_ok_et_journal_sans_contenu(env):
    r = nc.executer("generate_text", {"consigne": "secret-du-cluster"}, appelant="tests")
    assert r["ok"] and r["texte"] == "BONJOUR" and r["jetons"] == [7, 1]
    j = _journal(env)
    brut = (env["tmp"] / "donnees" / "usage.jsonl").read_text()
    assert j[-1]["operation"] == "generate_text" and j[-1]["ok"] is True
    assert "secret-du-cluster" not in brut and "BONJOUR" not in brut and CLE_FACTICE not in brut


def test_plafond_jetons_sortie(env):
    nc.executer("generate_text", {"consigne": "x", "max_tokens": 99999}, appelant="tests")
    corps = [a[2] for a in SCENARIO["appels"] if a[1] == "/api/v1/chat/completions"][-1]
    assert corps["max_tokens"] == 64


def test_429_retry_after_puis_succes(env):
    SCENARIO["chat"][:] = [(429, {"Retry-After": "0"}), (200, {})]
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert r["ok"] and r["essais"] == 2


def test_429_retry_after_trop_long_echoue_sans_attendre(env):
    SCENARIO["chat"][:] = [(429, {"Retry-After": "3600"})]
    t0 = time.time()
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert not r["ok"] and r["type"] == "quota" and time.time() - t0 < 1


def test_500_pas_de_reprise_pour_generation(env):
    SCENARIO["chat"][:] = [(500, {}), (200, {})]
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert not r["ok"] and r["type"] == "serveur"
    assert sum(1 for a in SCENARIO["appels"] if a[1] == "/api/v1/chat/completions") == 1


def test_503_reprise_pour_embeddings_idempotents(env):
    _ecrire_pol(env, rubriques_payantes_autorisees=True, plafond_usd_jour=1.0)
    SCENARIO["chat"][:] = [(503, {}), (200, {})]
    r = nc.executer("create_embeddings", {"textes": ["a", "b"], "confirme": True}, appelant="tests")
    assert r["ok"] and r["nombre"] == 2 and r["dimensions"] == 3 and r["essais"] == 2


def test_402_solde_type(env):
    SCENARIO["chat"][:] = [(402, {})]
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert r["type"] == "solde" and CLE_FACTICE not in r["erreur"] and "sk-nano-***" in r["erreur"]


def test_delai_depasse_type_reseau_sans_reprise(env):
    SCENARIO["chat"][:] = [("lent", {})]
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert not r["ok"] and r["type"] == "reseau"


def test_interrupteur_actif_false(env):
    _ecrire_pol(env, actif=False)
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="tests")
    assert r["type"] == "politique" and not SCENARIO["appels"]


def test_appelant_non_autorise(env):
    r = nc.executer("generate_text", {"consigne": "x"}, appelant="inconnu")
    assert r["type"] == "politique"


def test_modele_payant_refuse_par_politique(env):
    r = nc.executer("generate_text", {"consigne": "x", "modele": "m/payant"}, appelant="tests")
    assert r["type"] == "politique" and "payant" in r["erreur"]
    assert not any(a[1] == "/api/v1/chat/completions" for a in SCENARIO["appels"])


def test_payant_autorise_mais_sans_confirmation(env):
    _ecrire_pol(env, rubriques_payantes_autorisees=True, plafond_usd_jour=1.0)
    r = nc.executer("generate_text", {"consigne": "x", "modele": "m/payant"}, appelant="tests")
    assert r["type"] == "politique" and "confirmation" in r["erreur"]
    r = nc.executer("generate_text", {"consigne": "x", "modele": "m/payant", "confirme": True}, appelant="tests")
    assert r["ok"]


def test_video_bloquee_sans_appel(env):
    r = nc.executer("search_web", {"requete": "x"}, appelant="tests")
    assert r["type"] == "politique" and not any(a[1] == "/api/web" for a in SCENARIO["appels"])


def test_modele_interdit_anthropic(env):
    r = nc.executer("generate_text", {"consigne": "x", "modele": "anthropic/claude-opus-5.5"}, appelant="tests")
    assert r["type"] == "politique"


def test_modele_absent_propose_alternative(env):
    r = nc.executer("generate_text", {"consigne": "x", "modele": "m/inexistant"}, appelant="tests")
    assert r["type"] == "entree" and "alternative" in r["erreur"]


def test_entree_trop_longue(env):
    r = nc.executer("generate_text", {"consigne": "x" * 5000}, appelant="tests")
    assert r["type"] == "entree"


def test_vision_refuse_un_fichier_non_image(env, tmp_path):
    f = tmp_path / "pas-une-image.txt"
    f.write_text("données privées")
    r = nc.executer("analyze_image", {"image": str(f)}, appelant="tests")
    assert r["type"] == "entree" and "image" in r["erreur"]


def test_rerank_non_supporte(env):
    assert nc.executer("rerank_documents", {}, appelant="tests")["type"] == "non_supporte"


def test_catalogue_cache_date(env):
    a = nc.discover_models("texte", appelant="tests")
    n = len(SCENARIO["appels"])
    b = nc.discover_models("texte", appelant="tests")
    assert a == b and len(SCENARIO["appels"]) == n and a["inclus"] == 1
    assert list((env["tmp"] / "donnees" / "catalogue").glob("texte-*.json"))


def test_registre_capacites_ne_declare_rien_disponible_sans_validation(env):
    reg = nc.executer("discover_capabilities", {}, appelant="tests")
    assert reg["ok"] and all(not c["disponible"] for c in reg["capacites"])
    nc.noter_validation("generate_text", "valide", "test")
    reg = nc.discover_capabilities(appelant="tests")
    dispo = {c["operation"] for c in reg["capacites"] if c["disponible"]}
    assert dispo == {"generate_text"}


def test_concurrence_bornee(env):
    SCENARIO["chat"][:] = [("lent", {})] * 0
    actifs, maxi = [0], [0]
    orig = nc.urllib.request.urlopen

    def compte(*a, **k):
        actifs[0] += 1
        maxi[0] = max(maxi[0], actifs[0])
        time.sleep(0.2)
        try:
            return orig(*a, **k)
        finally:
            actifs[0] -= 1
    nc.urllib.request.urlopen = compte
    try:
        nc.discover_models("texte", appelant="tests")
        ts = [threading.Thread(target=nc.executer, args=("generate_text", {"consigne": "x"}), kwargs={"appelant": "tests"}) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        nc.urllib.request.urlopen = orig
    assert maxi[0] <= 2


def test_payant_refuse_avant_toute_verification_de_modele(env):
    r = nc.executer("generate_video", {"prompt": "x", "modele": "inexistant"}, appelant="tests")
    assert r["type"] == "politique" and not SCENARIO["appels"]


def test_taille_ignore_les_images_base64(env, tmp_path):
    f = tmp_path / "i.png"
    f.write_bytes(b"\x89PNG" + b"0" * 4000)
    r = nc.executer("analyze_image", {"image": str(f), "question": "x"}, appelant="tests")
    assert r["ok"], r
