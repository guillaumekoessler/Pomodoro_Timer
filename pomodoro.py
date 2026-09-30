#!/usr/bin/env python3
"""
Générateur de minuteur Pomodoro sur fond vert.

Produit une vidéo contenant un compte à rebours entouré d'un cercle de
progression, sur un fond vert uni à incruster dans CapCut (Incrustation
chromatique / Chroma key), avec des bips aux changements de session.

Exemples :
    python pomodoro.py --apercu                        # images PNG pour régler le design
    python pomodoro.py --test                          # vidéo courte : minutes -> secondes
    python pomodoro.py                                 # vidéo complète selon config.toml
    python pomodoro.py --travail 50 --pause 10 --cycles 3
    python pomodoro.py --lister-styles Menlo.ttc       # styles contenus dans une police
"""

from __future__ import annotations

import argparse
import bisect
import math
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

RACINE = Path(__file__).resolve().parent
TAUX_AUDIO = 48_000
TAU = 2 * math.pi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAUTS = {
    "sessions": {
        "preparation": 0,
        "travail": 25,
        "pause": 5,
        "cycles": 4,
        "pause_longue": 0,
        "pause_longue_tous_les": 4,
        "finir_par_pause": False,
        "duree_fin": 5,
    },
    "video": {
        "largeur": 1920,
        "hauteur": 1080,
        "fond": "#00FF00",
        "fps": 30,
        "fps_rendu": 10,
        "codec": "h264",
    },
    "cercle": {
        "rayon": 300,
        "epaisseur": 22,
        "couleur_piste": "#2B2B2B",
        "couleur_travail": "#FFFFFF",
        "couleur_pause": "#FFB86B",
        "couleur_pause_longue": "#8BE9FD",
        "couleur_preparation": "#BD93F9",
        "sens": "vide",
        "bouts_arrondis": True,
    },
    "texte": {
        "police": "Menlo.ttc",
        "police_index": 0,
        "police_variante": "",
        "taille": 150,
        "couleur": "#FFFFFF",
        "gras": False,
        "chiffres_fixes": True,
        "police_libelle": "",
        "libelle_index": 0,
        "libelle_variante": "",
        "gras_libelle": False,
        "taille_libelle": 44,
        "couleur_libelle": "#FFFFFF",
        "ecart_libelle": 24,
        "libelle_preparation": "PRÉPARATION",
        "libelle_travail": "FOCUS {n}/{total}",
        "libelle_pause": "PAUSE",
        "libelle_pause_longue": "PAUSE LONGUE",
        "libelle_fin": "TERMINÉ",
    },
    "sons": {
        "volume": 0.6,
        "au_demarrage": True,
        "debut_preparation": "integre:preparation",
        "debut_travail": "integre:travail",
        "debut_pause": "integre:pause",
        "fin": "integre:fin",
    },
}


def charger_config(chemin: Path) -> dict:
    cfg = {section: dict(valeurs) for section, valeurs in DEFAUTS.items()}
    if not chemin.exists():
        print(f"ℹ️  {chemin} introuvable : valeurs par défaut utilisées.")
        return cfg
    with open(chemin, "rb") as f:
        lu = tomllib.load(f)
    for section, valeurs in lu.items():
        if section not in cfg:
            print(f"⚠️  Section inconnue ignorée : [{section}]")
            continue
        for cle, val in valeurs.items():
            if cle not in cfg[section]:
                print(f"⚠️  Clé inconnue ignorée : [{section}] {cle}")
                continue
            cfg[section][cle] = val
    return cfg


def couleur(hexa: str) -> np.ndarray:
    h = str(hexa).strip().lstrip("#")
    if len(h) != 6:
        sys.exit(f'❌ Couleur invalide : {hexa!r} (format attendu : "#RRGGBB")')
    return np.array([int(h[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


# ---------------------------------------------------------------------------
# Déroulé des sessions
# ---------------------------------------------------------------------------


@dataclass
class Session:
    type: str  # "preparation", "travail", "pause" ou "pause_longue"
    debut: int  # secondes
    duree: int  # secondes
    numero: int  # numéro de la session de travail concernée


def construire_sessions(s: dict, unite: int) -> list[Session]:
    sessions, t = [], 0
    cycles = int(s["cycles"])
    # Compte à rebours de départ : en secondes, jamais raccourci par --test
    prep = max(0, int(round(float(s["preparation"]))))
    if prep > 0:
        sessions.append(Session("preparation", t, prep, 1))
        t += prep
    for n in range(1, cycles + 1):
        d = int(round(s["travail"] * unite))
        sessions.append(Session("travail", t, d, n))
        t += d
        if n == cycles and not s["finir_par_pause"]:
            break
        longue = s["pause_longue"] > 0 and n % int(s["pause_longue_tous_les"]) == 0
        d = int(round((s["pause_longue"] if longue else s["pause"]) * unite))
        if d > 0:
            sessions.append(Session("pause_longue" if longue else "pause", t, d, n))
            t += d
    return sessions


def format_temps(secondes: int) -> str:
    h, reste = divmod(secondes, 3600)
    m, s = divmod(reste, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Polices
# ---------------------------------------------------------------------------


def resoudre_police(nom: str) -> str:
    p = Path(nom).expanduser()
    for candidat in ([RACINE / p] if not p.is_absolute() else []) + [p]:
        if candidat.exists():
            return str(candidat)
    # Pillow cherche aussi le nom de fichier dans les dossiers de polices du
    # système (macOS : /System/Library/Fonts, /Library/Fonts, ~/Library/Fonts).
    return nom


_SANS_GRAS: set[str] = set()


def _est_gras(style: str) -> bool:
    return any(m in style.lower() for m in ("bold", "black", "heavy"))


def _est_italique(style: str) -> bool:
    return any(m in style.lower() for m in ("italic", "oblique"))


def graisser(police, chemin: str, taille: int, nom: str, variante: str):
    """Renvoie (police grasse, épaississement en pixels).

    On cherche d'abord une vraie graisse : variante "Bold" d'une police variable,
    puis un style gras de la même famille dans le fichier (.ttc). Faute de quoi on
    épaissit le tracé au moment du dessin (gras « synthétique »).
    """
    famille, style = police.getname()
    if _est_gras(style) or _est_gras(variante):  # déjà gras : pas de double graisse
        return police, 0
    if not variante:
        try:
            police.set_variation_by_name("Bold")
            return police, 0
        except Exception:
            pass
    for i in range(16):
        try:
            p = ImageFont.truetype(chemin, taille, index=i)
        except OSError:
            break
        f, s = p.getname()
        if f == famille and _est_gras(s) and _est_italique(s) == _est_italique(style):
            return p, 0
    if nom not in _SANS_GRAS:
        _SANS_GRAS.add(nom)
        print(f"ℹ️  {nom} n'a pas de style gras : le tracé est épaissi à la place.")
    return police, max(1, round(taille * 0.03))


def charger_police(
    nom: str, taille: int, index: int = 0, variante: str = "", gras: bool = False
):
    chemin = resoudre_police(nom)
    try:
        police = ImageFont.truetype(chemin, int(taille), index=int(index))
    except OSError:
        sys.exit(
            f"❌ Police introuvable ou illisible : {nom!r} (index {index}).\n"
            "   Place le fichier .ttf/.otf dans le dossier fonts/ et indique par ex. :\n"
            '   police = "fonts/MaPolice-Bold.ttf"'
        )
    if variante:
        try:
            police.set_variation_by_name(variante)
        except Exception:
            try:
                noms = [
                    n.decode() if isinstance(n, bytes) else n
                    for n in police.get_variation_names()
                ]
            except Exception:
                noms = []
            sys.exit(
                f"❌ Variante {variante!r} indisponible pour {nom!r}. "
                f"Variantes possibles : {', '.join(noms) or 'aucune (police non variable)'}"
            )
    if not gras:
        return police, 0
    return graisser(police, chemin, int(taille), nom, variante)


def lister_styles(nom: str) -> None:
    chemin = resoudre_police(nom)
    print(f"Styles contenus dans {nom} :")
    for i in range(64):
        try:
            p = ImageFont.truetype(chemin, 20, index=i)
        except OSError:
            if i == 0:
                sys.exit("❌ Police introuvable.")
            break
        famille, style = p.getname()
        print(f"  index {i} : {famille} {style}")
        try:
            noms = [
                n.decode() if isinstance(n, bytes) else n
                for n in p.get_variation_names()
            ]
            if noms:
                print(f"            variantes (police variable) : {', '.join(noms)}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rendu d'une image du minuteur
# ---------------------------------------------------------------------------


class Rendu:
    """Dessine le minuteur sur un carré de fond vert, image par image."""

    def __init__(self, cfg: dict):
        c, t, v = cfg["cercle"], cfg["texte"], cfg["video"]
        self.R = float(c["rayon"])
        self.demi = float(c["epaisseur"]) / 2
        self.arrondi = bool(c["bouts_arrondis"])
        self.sens = str(c["sens"]).lower()
        if self.sens not in ("vide", "remplit"):
            sys.exit('❌ [cercle] sens doit valoir "vide" ou "remplit".')
        self.couleurs_arc = {
            "preparation": couleur(c["couleur_preparation"]),
            "travail": couleur(c["couleur_travail"]),
            "pause": couleur(c["couleur_pause"]),
            "pause_longue": couleur(c["couleur_pause_longue"]),
        }

        S = int(math.ceil(2 * (self.R + self.demi) + 12))
        S += S % 2
        self.S, self.cx = S, S / 2
        self.fond = couleur(v["fond"])

        # Géométrie de l'anneau, calculée une seule fois
        yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
        dx, dy = xx + 0.5 - self.cx, yy + 0.5 - self.cx
        r = np.hypot(dx, dy)
        alpha = np.clip(self.demi - np.abs(r - self.R) + 0.5, 0, 1).ravel()
        self.idx = np.flatnonzero(alpha > 0)  # pixels de l'anneau
        self.a_anneau = alpha[self.idx]
        self.r = r.ravel()[self.idx]
        self.px, self.py = dx.ravel()[self.idx], dy.ravel()[self.idx]
        self.theta = np.arctan2(self.px, -self.py) % TAU  # 0 = midi, sens horaire

        # Image de base : fond + piste du cercle
        base = np.empty((S * S, 3), np.float32)
        base[:] = self.fond
        if str(c["couleur_piste"]).strip():
            a = self.a_anneau[:, None]
            base[self.idx] = base[self.idx] * (1 - a) + couleur(c["couleur_piste"]) * a
        self.base = base

        # Textes
        self.police, self.gras = charger_police(
            t["police"],
            t["taille"],
            t["police_index"],
            t["police_variante"],
            t["gras"],
        )
        nom_lib = t["police_libelle"] or t["police"]
        idx_lib = t["libelle_index"] if t["police_libelle"] else t["police_index"]
        var_lib = t["libelle_variante"] if t["police_libelle"] else t["police_variante"]
        self.police_lib, self.gras_lib = charger_police(
            nom_lib, t["taille_libelle"], idx_lib, var_lib, t["gras_libelle"]
        )
        self.coul_texte = couleur(t["couleur"])
        self.coul_lib = couleur(t["couleur_libelle"])
        self.fixes = bool(t["chiffres_fixes"])
        self.libelles = {
            "preparation": t["libelle_preparation"],
            "travail": t["libelle_travail"],
            "pause": t["libelle_pause"],
            "pause_longue": t["libelle_pause_longue"],
            "fin": t["libelle_fin"],
        }
        self.larg_chiffre = max(self.police.getlength(ch) for ch in "0123456789")

        # Mise en page verticale stable (ne bouge pas d'une seconde à l'autre)
        bb = self.police.getbbox("0123456789:", anchor="ls")
        haut_chiffres = bb[3] - bb[1] + 2 * self.gras
        a_lib, d_lib = self.police_lib.getmetrics()
        avec_lib = any(str(x).strip() for x in self.libelles.values())
        total = haut_chiffres + (
            (t["ecart_libelle"] + a_lib + d_lib + 2 * self.gras_lib) if avec_lib else 0
        )
        haut = self.cx - total / 2
        self.base_chiffres = haut - bb[1] + self.gras
        self.base_lib = (
            haut + haut_chiffres + t["ecart_libelle"] + a_lib + self.gras_lib
        )

        largeur = self._largeur("00:00") + 2 * self.gras
        dispo = 2 * (self.R - self.demi)
        if largeur > dispo * 0.9:
            print(
                f"⚠️  Les chiffres ({largeur:.0f} px) risquent de toucher le cercle "
                f"(intérieur : {dispo:.0f} px). Réduis [texte] taille ou augmente [cercle] rayon."
            )

        self._cache: dict[tuple, tuple | None] = {}

    # -- texte ---------------------------------------------------------------

    def _largeur(self, texte: str) -> float:
        if not self.fixes:
            return self.police.getlength(texte)
        return sum(
            self.larg_chiffre if ch.isdigit() else self.police.getlength(ch)
            for ch in texte
        )

    def _masque(self, texte: str, police, base_y: float, fixes: bool, gras: int = 0):
        cle = (texte, id(police), base_y, gras)
        if cle in self._cache:
            return self._cache[cle]
        if len(self._cache) > 32:
            self._cache.clear()
        img = Image.new("L", (self.S, self.S), 0)
        d = ImageDraw.Draw(img)
        if fixes:
            # Chaque chiffre occupe une case de même largeur : le compteur ne tremble pas
            largeurs = [
                self.larg_chiffre if ch.isdigit() else police.getlength(ch)
                for ch in texte
            ]
            x = self.cx - sum(largeurs) / 2
            for ch, w in zip(texte, largeurs):
                d.text(
                    (x + (w - police.getlength(ch)) / 2, base_y),
                    ch,
                    font=police,
                    fill=255,
                    anchor="ls",
                    stroke_width=gras,
                    stroke_fill=255,
                )
                x += w
        else:
            d.text(
                (self.cx, base_y),
                texte,
                font=police,
                fill=255,
                anchor="ms",
                stroke_width=gras,
                stroke_fill=255,
            )
        bbox = img.getbbox()
        res = None
        if bbox:
            res = (np.asarray(img.crop(bbox), np.float32)[..., None] / 255.0, bbox)
        self._cache[cle] = res
        return res

    # -- arc de progression --------------------------------------------------

    def _alpha_arc(self, etendue: float):
        if etendue <= 0:
            return None
        if etendue >= TAU - 1e-6:
            return self.a_anneau
        corps = np.clip((etendue - self.theta) * self.r + 0.5, 0, 1) * np.clip(
            self.theta * self.r + 0.5, 0, 1
        )
        a = corps * self.a_anneau
        if self.arrondi:
            for ang in (0.0, etendue):
                bx, by = self.R * math.sin(ang), -self.R * math.cos(ang)
                disque = np.clip(
                    self.demi - np.hypot(self.px - bx, self.py - by) + 0.5, 0, 1
                )
                a = np.maximum(a, disque)
        return a

    # -- image complète --------------------------------------------------------

    def image(
        self,
        secondes: int,
        fraction_restante: float,
        type_: str,
        numero: int,
        total: int,
    ) -> np.ndarray:
        f = self.base.copy()
        frac = fraction_restante if self.sens == "vide" else 1 - fraction_restante
        a = self._alpha_arc(frac * TAU)
        if a is not None:
            coul = self.couleurs_arc.get(type_, self.couleurs_arc["travail"])
            aa = a[:, None]
            f[self.idx] = f[self.idx] * (1 - aa) + coul * aa
        f = f.reshape(self.S, self.S, 3)

        textes = [
            (
                self._masque(
                    format_temps(secondes),
                    self.police,
                    self.base_chiffres,
                    self.fixes,
                    self.gras,
                ),
                self.coul_texte,
            )
        ]
        lib = str(self.libelles.get(type_, "")).format(n=numero, total=total)
        if lib.strip():
            textes.append(
                (
                    self._masque(
                        lib, self.police_lib, self.base_lib, False, self.gras_lib
                    ),
                    self.coul_lib,
                )
            )
        for masque, coul in textes:
            if masque is None:
                continue
            arr, (x0, y0, x1, y1) = masque
            zone = f[y0:y1, x0:x1]
            zone *= 1 - arr
            zone += coul * arr
        return (f + 0.5).astype(np.uint8)


def etat_a(t: float, sessions: list[Session], debuts: list[int]):
    """Renvoie (secondes affichées, fraction restante, type, numéro) à l'instant t."""
    i = bisect.bisect_right(debuts, t) - 1
    s = sessions[i]
    restant = s.duree - (t - s.debut)
    if restant <= 0:  # après la dernière session : écran de fin
        return 0, 0.0, "fin", s.numero
    return math.ceil(restant - 1e-9), restant / s.duree, s.type, s.numero


# ---------------------------------------------------------------------------
# Sons
# ---------------------------------------------------------------------------


def note(freq: float, duree: float = 0.9) -> np.ndarray:
    t = np.arange(int(duree * TAUX_AUDIO)) / TAUX_AUDIO
    env = np.exp(-t * 6) * np.minimum(1, t / 0.005)
    onde = (
        np.sin(TAU * freq * t)
        + 0.25 * np.sin(TAU * 2 * freq * t)
        + 0.08 * np.sin(TAU * 3 * freq * t)
    )
    return (onde * env / 1.33).astype(np.float32)


SONS_INTEGRES = {
    "preparation": [(523, 0.0)],  # note douce : ça va commencer
    "travail": [(660, 0.0), (990, 0.16)],  # montant : on s'y remet
    "pause": [(990, 0.0), (660, 0.16)],  # descendant : on souffle
    "fin": [(784, 0.0), (988, 0.16), (1319, 0.32)],  # arpège final
}


def charger_son(spec: str):
    spec = str(spec).strip()
    if not spec:
        return None
    if spec.startswith("integre:"):
        nom = spec.split(":", 1)[1]
        if nom not in SONS_INTEGRES:
            sys.exit(
                f"❌ Son intégré inconnu : {nom!r} (choix : {', '.join(SONS_INTEGRES)})"
            )
        notes = SONS_INTEGRES[nom]
        buf = np.zeros(int((max(d for _, d in notes) + 0.9) * TAUX_AUDIO), np.float32)
        for freq, decal in notes:
            n = note(freq)
            i = int(decal * TAUX_AUDIO)
            buf[i : i + len(n)] += n
        return buf / max(1.0, float(np.abs(buf).max()))
    chemin = Path(spec).expanduser()
    if not chemin.is_absolute():
        chemin = RACINE / chemin
    if not chemin.exists():
        sys.exit(f"❌ Fichier son introuvable : {chemin}")
    brut = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(chemin),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(TAUX_AUDIO),
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(brut, np.float32).copy()


def construire_audio(
    cfg: dict, sessions: list[Session], total: int, chemin: Path
) -> None:
    s = cfg["sons"]
    vol = float(s["volume"])
    piste = np.zeros(int(total * TAUX_AUDIO) + 1, np.float32)
    cles = ("debut_preparation", "debut_travail", "debut_pause", "fin")
    sons = {k: charger_son(s[k]) for k in cles}
    son_de = {
        "preparation": "debut_preparation",
        "travail": "debut_travail",
        "pause": "debut_pause",
        "pause_longue": "debut_pause",
    }

    def placer(son, instant):
        if son is None:
            return
        i = int(instant * TAUX_AUDIO)
        n = min(len(son), len(piste) - i)
        if n > 0:
            piste[i : i + n] += son[:n] * vol

    for k, sess in enumerate(sessions):
        if k == 0 and not s["au_demarrage"]:
            continue
        placer(sons[son_de[sess.type]], sess.debut)
    placer(sons["fin"], sessions[-1].debut + sessions[-1].duree)

    pcm = (np.clip(piste, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(chemin), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAUX_AUDIO)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# Programmes
# ---------------------------------------------------------------------------


def sur_canevas(img: np.ndarray, cfg: dict) -> Image.Image:
    v = cfg["video"]
    W, H = int(v["largeur"]), int(v["hauteur"])
    canevas = np.empty((H, W, 3), np.uint8)
    canevas[:] = couleur(v["fond"]).astype(np.uint8)
    S = img.shape[0]
    x, y = (W - S) // 2, (H - S) // 2
    canevas[y : y + S, x : x + S] = img
    return Image.fromarray(canevas)


def apercu(cfg: dict, rendu: Rendu, sessions: list[Session]) -> None:
    dossier = RACINE / "apercus"
    dossier.mkdir(exist_ok=True)
    total_travail = sum(1 for s in sessions if s.type == "travail")
    faits = set()
    for s in sessions:
        if s.type in faits:
            continue
        faits.add(s.type)
        restant = s.duree * 0.6
        img = rendu.image(
            math.ceil(restant), restant / s.duree, s.type, s.numero, total_travail
        )
        chemin = dossier / f"apercu_{s.type}.png"
        sur_canevas(img, cfg).save(chemin)
        print(f"🖼️  {chemin.relative_to(RACINE)}")
    img = rendu.image(0, 0.0, "fin", sessions[-1].numero, total_travail)
    sur_canevas(img, cfg).save(dossier / "apercu_fin.png")
    print(f"🖼️  apercus/apercu_fin.png")


def generer(cfg: dict, rendu: Rendu, sessions: list[Session], sortie: Path) -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("❌ ffmpeg est introuvable. Installe-le avec : brew install ffmpeg")
    v = cfg["video"]
    W, H = int(v["largeur"]), int(v["hauteur"])
    if rendu.S > min(W, H):
        sys.exit(
            f"❌ Le minuteur ({rendu.S} px) est plus grand que la vidéo ({W}x{H}). "
            "Réduis [cercle] rayon."
        )
    fps_rendu, fps = int(v["fps_rendu"]), int(v["fps"])
    codec = str(v["codec"]).lower()
    if codec == "prores":
        sortie = sortie.with_suffix(".mov")
        args_codec = ["-c:v", "prores_ks", "-profile:v", "1", "-c:a", "pcm_s16le"]
        pixfmt = "yuv422p10le"
    elif codec == "h264":
        args_codec = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "16",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]
        pixfmt = "yuv420p"
    else:
        sys.exit('❌ [video] codec doit valoir "h264" ou "prores".')

    fin_sessions = sessions[-1].debut + sessions[-1].duree
    total = fin_sessions + int(cfg["sessions"]["duree_fin"])
    nb_images = total * fps_rendu
    debuts = [s.debut for s in sessions]
    total_travail = sum(1 for s in sessions if s.type == "travail")
    sortie.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "bips.wav"
        construire_audio(cfg, sessions, total, wav)
        fond = str(v["fond"]).lstrip("#")
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{rendu.S}x{rendu.S}",
            "-r",
            str(fps_rendu),
            "-i",
            "-",
            "-i",
            str(wav),
            "-filter_complex",
            f"[0:v]pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x{fond},fps={fps},format={pixfmt}[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            *args_codec,
            "-t",
            str(total),
            str(sortie),
        ]
        print(
            f"🎬 {format_temps(total)} de vidéo, {len(sessions)} sessions → {sortie.name}"
        )
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        depart, dernier = time.time(), -1
        try:
            for i in range(nb_images):
                t = i / fps_rendu
                sec, frac, type_, num = etat_a(t, sessions, debuts)
                proc.stdin.write(
                    rendu.image(sec, frac, type_, num, total_travail).tobytes()
                )
                pct = (i + 1) * 100 // nb_images
                if pct != dernier:
                    dernier = pct
                    ecoule = time.time() - depart
                    reste = ecoule / (i + 1) * (nb_images - i - 1)
                    print(
                        f"\r   {pct:3d} %  (encore ~{format_temps(int(reste))})",
                        end="",
                        flush=True,
                    )
            proc.stdin.close()
        except BrokenPipeError:
            pass
        code = proc.wait()
    print()
    if code != 0:
        sys.exit("❌ ffmpeg a rencontré une erreur (voir le message ci-dessus).")
    print(f"✅ Terminé en {format_temps(int(time.time() - depart))} : {sortie}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Générateur de minuteur Pomodoro sur fond vert."
    )
    ap.add_argument(
        "-c", "--config", default="config.toml", help="fichier de configuration"
    )
    ap.add_argument("-o", "--sortie", help="fichier vidéo de sortie")
    ap.add_argument(
        "--preparation",
        type=float,
        help="compte à rebours de départ (secondes, 0 = aucun)",
    )
    ap.add_argument(
        "--travail", type=float, help="durée d'une session de travail (minutes)"
    )
    ap.add_argument("--pause", type=float, help="durée d'une pause (minutes)")
    ap.add_argument("--cycles", type=int, help="nombre de sessions de travail")
    ap.add_argument(
        "--apercu", action="store_true", help="génère seulement des images PNG d'aperçu"
    )
    ap.add_argument(
        "--test",
        action="store_true",
        help="vidéo courte : chaque minute devient une seconde",
    )
    ap.add_argument(
        "--lister-styles",
        metavar="POLICE",
        help="affiche les styles (index) contenus dans un fichier de police",
    )
    args = ap.parse_args()

    if args.lister_styles:
        lister_styles(args.lister_styles)
        return

    chemin_cfg = Path(args.config)
    if not chemin_cfg.is_absolute() and not chemin_cfg.exists():
        chemin_cfg = RACINE / chemin_cfg
    cfg = charger_config(chemin_cfg)
    s = cfg["sessions"]
    for cle in ("preparation", "travail", "pause", "cycles"):
        if getattr(args, cle) is not None:
            s[cle] = getattr(args, cle)

    sessions = construire_sessions(s, 1 if args.test else 60)
    rendu = Rendu(cfg)

    if args.apercu:
        apercu(cfg, rendu, sessions)
        return

    if args.sortie:
        sortie = Path(args.sortie)
    else:
        nom = f"pomodoro_{s['travail']:g}-{s['pause']:g}_x{int(s['cycles'])}"
        sortie = RACINE / "videos" / f"{nom}{'_test' if args.test else ''}.mp4"
    generer(cfg, rendu, sessions, sortie)


if __name__ == "__main__":
    main()
