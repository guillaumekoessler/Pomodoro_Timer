# Minuteur Pomodoro sur fond vert

Génère une vidéo avec un compte à rebours, un cercle de progression et des bips aux
changements de session, sur fond vert, pour l'incruster dans CapCut par-dessus ta boucle.

## Installation (une seule fois)

```bash
brew install python ffmpeg          # Python 3.11 minimum
cd pomodoro-timer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

À chaque nouvelle session de Terminal : `cd pomodoro-timer && source .venv/bin/activate`.

## Utilisation

```bash
python pomodoro.py --apercu                     # PNG dans apercus/ (1 seconde) : pour régler le design
python pomodoro.py --test                       # vidéo courte (1 min → 1 s) : vérifier transitions et bips
python pomodoro.py                              # vidéo complète selon config.toml → dossier videos/
python pomodoro.py --travail 50 --pause 10 --cycles 3
python pomodoro.py --preparation 10             # 10 s de compte à rebours avant la 1re session
python pomodoro.py -o ~/Desktop/pomodoro.mp4    # choisir le fichier de sortie
python pomodoro.py -c config-50-10.toml         # utiliser une autre config
```

Astuce : duplique `config.toml` pour chaque style de vidéo (ex. `config-50-10.toml`).

## Compte à rebours de départ

`preparation` (section `[sessions]`) ajoute un premier chrono avant la première session,
en **secondes** (`0` = aucun) :

```toml
[sessions]
preparation = 10
```

Il a son propre libellé (`libelle_preparation`), sa couleur d'arc (`couleur_preparation`)
et son bip (`debut_preparation`). Le bip de début de travail sonne alors à la fin de ce
compte à rebours. `--test` ne le raccourcit pas : des secondes restent des secondes.

## Changer de police

1. Télécharge une police (ex. sur fonts.google.com) et mets le `.ttf` ou `.otf` dans `fonts/`.
   Dans les archives Google Fonts, prends de préférence les fichiers du dossier `static/`
   (ex. `Poppins-Bold.ttf`).
2. Dans `config.toml` : `police = "fonts/Poppins-Bold.ttf"`
3. `python pomodoro.py --apercu` pour vérifier.

Les polices du Mac marchent aussi par leur nom de fichier (`"Menlo.ttc"`, `"Avenir Next.ttc"`…).
Un `.ttc` contient plusieurs styles : `python pomodoro.py --lister-styles "Avenir Next.ttc"`
affiche les index à mettre dans `police_index`.

Avec `chiffres_fixes = true`, n'importe quelle police donne un compteur stable (pas de
tremblement quand les chiffres changent).

### Gras

`gras = true` (chiffres) et `gras_libelle = true` (libellé), dans `[texte]` :

```toml
[texte]
gras = true
gras_libelle = false
```

Le vrai style gras de la police est utilisé s'il existe (`Menlo Bold` pour `Menlo.ttc`,
variante `Bold` d'une police variable). Sinon le tracé est épaissi au dessin, et le
script te le signale. Si `police_index` / `police_variante` désignent déjà un style gras,
`gras` ne change rien : pas de double graisse.

## Sons personnalisés

Mets un fichier (mp3, wav, m4a…) dans `sons/` puis, dans `config.toml` :
`debut_travail = "sons/cloche.mp3"`. Mettre `""` pour désactiver un son.

## Dans CapCut

1. Place ta boucle de fond sur la piste principale, la vidéo du minuteur au-dessus.
2. Sélectionne le minuteur → Vidéo → Supprimer l'arrière-plan → Incrustation chromatique (Chroma key).
3. Pipette sur le vert, puis ajuste Intensité / Ombre jusqu'à des bords propres.
4. Redimensionne et place le minuteur où tu veux. Ajoute ta musique lo-fi sur une piste audio.

Les bips sont déjà dans le fichier du minuteur.

## Conseils

- Évite toute couleur verte ou vert-jaune dans le design : l'incrustation la ferait disparaître.
- Si les bords verdissent à l'incrustation, essaie `codec = "prores"` (fichier beaucoup plus lourd).
- Génère le minuteur un peu trop grand et réduis-le dans CapCut plutôt que l'inverse.
