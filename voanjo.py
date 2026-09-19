#!/usr/bin/env python3
"""
voanjo.py – Claim des faucet avec cookies (Camoufox + Turnstile)
Version finale : multi-langue + détection login robuste + timer 5min en échec
+ PATCH : fermeture automatique des pop-ups (contest/leaderboard, etc.)
"""

import os, sys, json, time, random, base64, subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from dotenv import load_dotenv
load_dotenv()

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend
from github import Github, GithubException, Auth
from camoufox import Camoufox
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import ananana

# ---------- Variables d'environnement ----------
GH_TOKEN = os.getenv("GH_TOKEN")
GH_USERNAME = os.getenv("GH_USERNAME")
GH_REPO = os.getenv("GH_REPO")
GH_BRANCH = os.getenv("GH_BRANCH", "main")
USER_ID = os.getenv("USER_ID")
CLAIM_EMAIL = os.getenv("CLAIM_EMAIL")
CLAIM_PLATFORM = os.getenv("CLAIM_PLATFORM")
CRYPTO_SECRET = os.getenv("CRYPTO_SECRET")
JP_PROXY_LIST = [p.strip() for p in os.getenv("JP_PROXY_LIST", "").split(",") if p.strip()]

if not all([CRYPTO_SECRET, USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, JP_PROXY_LIST]):
    print("❌ Variables d'environnement manquantes")
    sys.exit(1)

USER_FILE = f"account_{USER_ID}_{CLAIM_PLATFORM}_{CLAIM_EMAIL}.json"

VIDEOS_DIR = Path(__file__).parent / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)

# PATCH : dossier pour les captures de debug (bouton présent mais masqué, etc.)
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# --- Chiffrement ---
def derive_key(secret: str, salt: bytes = b"salt") -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1, backend=default_backend())
    return kdf.derive(secret.encode())

KEY = derive_key(CRYPTO_SECRET)

def decrypt(encrypted_text: str) -> str:
    if not isinstance(encrypted_text, str):
        return encrypted_text
    try:
        json.loads(encrypted_text)
        return encrypted_text
    except:
        pass
    parts = encrypted_text.split(":")
    if len(parts) != 2:
        return encrypted_text
    try:
        iv = bytes.fromhex(parts[0])
        encrypted = parts[1]
        cipher = Cipher(algorithms.AES(KEY), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(bytes.fromhex(encrypted)) + decryptor.finalize()
        pad_len = padded[-1]
        return padded[:-pad_len].decode('utf-8')
    except:
        return encrypted_text

def decrypt_cookies(encrypted_cookies: str) -> list:
    dec = decrypt(encrypted_cookies)
    try:
        return json.loads(dec)
    except:
        return []

def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    if not proxy_url:
        return None
    proxy_url = proxy_url.strip()
    if proxy_url.startswith("socks5://") or proxy_url.startswith("socks://"):
        protocol = "socks5"
    else:
        protocol = "http"
    if "://" in proxy_url:
        proxy_url = proxy_url.split("://", 1)[1]
    parts = proxy_url.split("@")
    if len(parts) == 2:
        auth, server = parts
        user, pwd = auth.split(":", 1)
        host, port = server.split(":")
        return {
            "server": f"{protocol}://{host}:{port}",
            "username": user,
            "password": pwd,
        }
    else:
        host, port = proxy_url.split(":")
        return {"server": f"{protocol}://{host}:{port}", "username": None, "password": None}

def start_ffmpeg(video_path: str):
    display = os.environ.get("DISPLAY", ":99")
    args = [
        "ffmpeg", "-f", "x11grab", "-video_size", "1280x720",
        "-i", display, "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-y", video_path,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"🎥 FFmpeg démarré → {video_path}")
    return proc

def stop_ffmpeg(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    print("🎥 FFmpeg arrêté")

def get_github_client():
    return Github(auth=Auth.Token(GH_TOKEN))

def save_account(account_data: dict) -> None:
    g = get_github_client()
    repo = g.get_repo(f"{GH_USERNAME}/{GH_REPO}")
    content = json.dumps(account_data, indent=2)
    max_retries = 30
    for attempt in range(1, max_retries + 1):
        try:
            sha = None
            try:
                contents = repo.get_contents(USER_FILE, ref=GH_BRANCH)
                sha = contents.sha
            except GithubException as e:
                if e.status != 404:
                    raise
            if sha:
                repo.update_file(path=USER_FILE, message=f"Mise à jour compte {CLAIM_EMAIL}",
                                 content=content, branch=GH_BRANCH, sha=sha)
            else:
                repo.create_file(path=USER_FILE, message=f"Mise à jour compte {CLAIM_EMAIL}",
                                 content=content, branch=GH_BRANCH)
            print("💾 Sauvegarde réussie")
            return
        except GithubException as e:
            if e.status == 409:
                print(f"⚠️ Conflit 409, tentative {attempt}/{max_retries}")
                time.sleep(attempt + random.uniform(0, 3))
            else:
                raise

def add_history_entry(user_id, email, platform, success, bonus=0):
    g = get_github_client()
    repo = g.get_repo(f"{GH_USERNAME}/{GH_REPO}")
    history_file = f"history_{user_id}.json"
    entry = {
        "email": email, "platform": platform,
        "timestamp": int(time.time() * 1000),
        "success": success, "bonus": bonus
    }
    try:
        sha = None
        history = []
        try:
            contents = repo.get_contents(history_file, ref=GH_BRANCH)
            sha = contents.sha
            history = json.loads(base64.b64decode(contents.content).decode())
        except GithubException as e:
            if e.status != 404:
                raise
        history.append(entry)
        content = json.dumps(history, indent=2)
        if sha:
            repo.update_file(history_file, f"Historique claim {email}", content, sha, branch=GH_BRANCH)
        else:
            repo.create_file(history_file, f"Historique claim {email}", content, branch=GH_BRANCH)
        print("📜 Historique sauvegardé.")
    except Exception as e:
        print(f"⚠️ Impossible d'enregistrer l'historique : {e}")

def extract_timer(page):
    try:
        return page.evaluate("""() => {
            const timerEl = document.querySelector('#next_claim_timer, .countdown, [id*="timer"], [class*="timer"]');
            if (timerEl) {
                const txt = timerEl.textContent.trim();
                const mmss = txt.match(/(\\d+):(\\d+)/);
                if (mmss) return parseInt(mmss[1]) + parseInt(mmss[2]) / 60;
            }
            const cells = document.querySelectorAll('td, th');
            for (const cell of cells) {
                const txt = cell.textContent.trim();
                const mmss = txt.match(/(\\d+):(\\d+)/);
                if (mmss && txt.length <= 8) return parseInt(mmss[1]) + parseInt(mmss[2]) / 60;
            }
            const errorMsg = document.querySelector('.alert-danger, .error, [class*="error"]');
            if (errorMsg) {
                const msg = errorMsg.textContent.trim();
                const match = msg.match(/(\\d+)\\s*(minutes?|mins?)/i);
                if (match) return parseInt(match[1]);
            }
            return null;
        }""")
    except:
        return None

def is_login_page(page) -> bool:
    """Détection robuste de la page de login (multi-sites)"""
    try:
        url = page.url.lower()
        if "login" in url or "signin" in url or "auth" in url:
            return True
        # Présence d'un champ password visible
        pwd = page.query_selector('input[type="password"]')
        if pwd:
            box = pwd.bounding_box()
            if box and box["width"] > 0:
                return True
        # Texte typique de login
        body_text = page.inner_text("body").lower()
        if any(x in body_text for x in ["sign in", "log in", "login", "connexion", "se connecter"]):
            if page.query_selector('input[type="email"], input[name*="email"], input[name*="user"]'):
                return True
    except:
        pass
    return False


def dismiss_popups(page, max_attempts: int = 3) -> bool:
    """
    PATCH : détecte et ferme les pop-ups/modales qui recouvrent la page
    (ex: "You're Still in the Game!" sur freetron.in — contest/leaderboard,
    newsletter, cookie banner, etc.) AVANT de chercher le bouton claim.
    Retourne True si au moins une popup a été fermée.
    """
    closed_any = False

    dismiss_selectors = [
        # Textes génériques multi-langue
        "text=/^Dismiss$/i",
        "text=/^Close$/i",
        "text=/^Skip$/i",
        "text=/^No thanks$/i",
        "text=/^Maybe later$/i",
        "text=/^Fermer$/i",
        "text=/^Ignorer$/i",
        "text=/^Plus tard$/i",

        # Boutons/icônes de fermeture classiques
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "[class*='modal'] [class*='close']",
        "[class*='modal'] button.close",
        "[class*='popup'] [class*='close']",
        ".modal-close",
        ".btn-close",
        ".close-modal",
        "[data-dismiss='modal']",

        # Croix "×" isolée
        "text=/^×$/",
        "text=/^✕$/",
    ]

    for attempt in range(max_attempts):
        found = False
        for sel in dismiss_selectors:
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                box = el.bounding_box()
                if not box or box["width"] < 3 or box["height"] < 3:
                    continue
                el.click(timeout=3000)
                print(f"🗙 Pop-up fermée via sélecteur → {sel}")
                closed_any = True
                found = True
                time.sleep(1)
                break
            except Exception:
                continue

        if not found:
            break  # plus aucune popup détectée, on arrête la boucle

    # Sécurité supplémentaire : si un overlay/backdrop reste visible et bloque
    # les clics, on tente un Escape clavier en dernier recours.
    try:
        overlay = page.query_selector("[class*='overlay'], [class*='backdrop'], .modal-backdrop")
        if overlay:
            box = overlay.bounding_box()
            if box and box["width"] > 200 and box["height"] > 200:
                print("⌨️ Overlay encore présent → tentative Escape")
                page.keyboard.press("Escape")
                time.sleep(1)
    except Exception:
        pass

    return closed_any


def extract_claim_confirmation(page):
    """
    PATCH : détecte une modale de confirmation de claim au texte (ex: freetron.in
    affiche "🎉 Réclamé  0.005 TRX" dans une popup custom SANS les classes
    .alert-success/.success habituelles). Recherche par motif texte, insensible
    aux classes CSS utilisées par chaque site.
    Retourne (success: bool, amount: float|None, raw_text: str)
    """
    try:
        result = page.evaluate(r"""() => {
            const bodyText = document.body.innerText || "";
            const patterns = [
                /R[ée]clam[ée][^\d]*([\d]+\.?[\d]*)\s*([A-Za-z]{2,6})/i,
                /Claimed[^\d]*([\d]+\.?[\d]*)\s*([A-Za-z]{2,6})/i,
                /You\s+(?:received|earned|got)[^\d]*([\d]+\.?[\d]*)\s*([A-Za-z]{2,6})/i,
                /Success(?:ful)?[^\d]{0,40}([\d]+\.?[\d]*)\s*([A-Za-z]{2,6})/i,
            ];
            for (const re of patterns) {
                const m = bodyText.match(re);
                if (m) return { matched: true, amount: m[1], unit: m[2] || '', raw: m[0].replace(/\s+/g, ' ').trim() };
            }
            return { matched: false };
        }""")
        if result and result.get("matched"):
            try:
                amount = float(result["amount"])
            except (TypeError, ValueError):
                amount = None
            return True, amount, result.get("raw", "")
    except Exception:
        pass
    return False, None, ""


def find_claim_button(page, selectors):
    """
    PATCH : cherche un bouton claim visible/actif parmi la liste de sélecteurs.
    Retourne (element, sélecteur_trouvé) ou (None, None).
    """
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            box = el.bounding_box()
            if not box or box["width"] < 5 or box["height"] < 5:
                continue
            is_disabled = page.evaluate(
                "(el) => el.disabled || el.getAttribute('disabled') !== null", el
            )
            if is_disabled:
                continue
            return el, sel
        except Exception:
            continue
    return None, None


def find_claim_button_any(page, selectors):
    """
    PATCH : comme find_claim_button, mais sans exiger que le bouton soit
    visible (bounding_box > 0). Retourne le premier élément non-disabled
    trouvé dans le DOM, visible ou pas. Utilisé pour ne jamais abandonner
    quand le bouton existe mais est simplement masqué (display:none, etc.).
    """
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            is_disabled = page.evaluate(
                "(el) => el.disabled || el.getAttribute('disabled') !== null", el
            )
            if is_disabled:
                continue
            return el, sel
        except Exception:
            continue
    return None, None


def run_autologin(account: dict) -> bool:
    """
    PATCH : quand les cookies sont expirés, relance test_login_workflow.py
    pour refaire un login complet. Email/password sont déjà stockés (chiffrés)
    dans le fichier compte — on réutilise decrypt() pour récupérer le
    mot de passe en clair, et on force un timer de 01:00 (1 min) pour
    retenter le claim très vite après le login.
    Retourne True si l'autologin s'est terminé avec succès (exit code 0).
    """
    raw_password = account.get("password")
    if not raw_password:
        print("⚠️ Pas de mot de passe stocké dans le compte → autologin impossible")
        return False

    try:
        plain_password = decrypt(raw_password)
    except Exception as e:
        print(f"⚠️ Impossible de déchiffrer le mot de passe : {e}")
        return False

    script_path = Path(__file__).parent / "test_login_workflow.py"
    if not script_path.exists():
        print(f"⚠️ {script_path} introuvable → autologin impossible")
        return False

    env = os.environ.copy()
    env["TEST_EMAIL"] = CLAIM_EMAIL
    env["TEST_PASSWORD"] = plain_password
    env["TEST_PLATFORM"] = CLAIM_PLATFORM
    env["TEST_PROXY_INDEX"] = str(account.get("proxyIndex", 0))
    env["TEST_INITIAL_TIMER"] = "01:00"   # ← 1 min avant le prochain claim

    print("🔐 Cookies expirés → lancement de l'autologin (test_login_workflow.py)...")
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            env=env,
            cwd=str(script_path.parent),
        )
    except Exception as e:
        print(f"⚠️ Erreur pendant le lancement de l'autologin : {e}")
        return False

    if result.returncode == 0:
        print("✅ Autologin réussi, cookies rafraîchis (timer 01:00)")
        return True
    else:
        print(f"❌ Autologin échoué (code retour {result.returncode})")
        return False


def claim_with_cookies(account: dict):
    faucet_urls = {
        "tronpick": "https://tronpick.io/faucet.php",
        "litepick": "https://litepick.io/faucet.php",
        "dogepick": "https://dogepick.io/faucet.php",
        "solpick": "https://solpick.io/faucet.php",
        "bnbpick": "https://bnbpick.io/faucet.php",
        "tonpick": "https://tonpick.game/faucet.php",
        "suipick": "https://suipick.io/faucet.php",
        "polpick": "https://polpick.io/faucet.php",
        "freetron": "https://freetron.in/faucet",
    }
    faucet_url = faucet_urls.get(CLAIM_PLATFORM, "https://tronpick.io/faucet.php")

    proxy_index = account.get("proxyIndex", 0)
    proxy_url = JP_PROXY_LIST[proxy_index] if proxy_index < len(JP_PROXY_LIST) else JP_PROXY_LIST[0]
    proxy_dict = parse_proxy_url(proxy_url)
    if not proxy_dict:
        raise ValueError("Proxy invalide")

    safe_email = CLAIM_EMAIL.replace("@", "_").replace(".", "_")
    video_path = str(VIDEOS_DIR / f"claim_{CLAIM_PLATFORM}_{safe_email}_{int(time.time())}.mp4")
    ffmpeg_proc = None

    for attempt in range(1, 4):
        try:
            print(f"--- Tentative claim {attempt}/3 ---")
            ffmpeg_proc = start_ffmpeg(video_path)
            time.sleep(1.5)

            with Camoufox(headless=False, humanize=True, geoip=True, proxy=proxy_dict) as browser:
                # PATCH : viewport fixe (desktop) pour éviter qu'un fingerprint
                # mobile/petit écran ne déclenche un layout responsive qui
                # masque des éléments (ex: bouton claim en display:none).
                page = browser.new_page(viewport={"width": 1366, "height": 768})

                # Injection cookies
                cookies_data = account.get("cookies")
                if cookies_data:
                    decrypted = decrypt_cookies(cookies_data)
                    if isinstance(decrypted, list) and len(decrypted) > 0:
                        valid_cookies = [c for c in decrypted if c.get("name") and c.get("value")]
                        if valid_cookies:
                            page.context.add_cookies(valid_cookies)
                            print(f"🍪 {len(valid_cookies)} cookies injectés")

                page.goto(faucet_url, wait_until="networkidle", timeout=90000)
                time.sleep(12)

                # ── PATCH : fermer les pop-ups (contest/leaderboard, etc.) ──
                # AVANT toute autre détection, sinon elles masquent login/claim
                dismiss_popups(page)

                # ── Détection login robuste ──
                if is_login_page(page):
                    print("❌ Cookies expirés ou page de login détectée")
                    account["cookiesStatus"] = "expired"

                    # Ferme le navigateur courant avant de lancer l'autologin
                    # (évite d'avoir 2 instances Camoufox ouvertes en même temps)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    stop_ffmpeg(ffmpeg_proc)
                    ffmpeg_proc = None

                    autologin_ok = run_autologin(account)

                    if autologin_ok:
                        # test_login_workflow.py a déjà sauvegardé le compte
                        # (nouvelles cookies + timer=01:00) → rien à réécrire ici.
                        add_history_entry(USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, False, 0)
                        print(f"🎥 Vidéo sauvegardée : {video_path}")
                        return {"success": False, "message": "Cookies expirés → autologin réussi, réessai dans 1 min"}
                    else:
                        account["lastClaim"] = int(time.time() * 1000)
                        account["timer"] = 5          # ← 5 min si l'autologin échoue aussi
                        save_account(account)
                        add_history_entry(USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, False, 0)
                        print(f"🎥 Vidéo sauvegardée : {video_path}")
                        return {"success": False, "message": "Cookies expirés / autologin échoué"}

                print("✅ Session valide")
                account["cookiesStatus"] = "valid"

                # ── Sélecteurs multi-langue (ID en premier = indépendant de la langue) ──
                claim_btn_selectors = [
                    # === Priorité absolue : ID (ne change jamais avec la langue) ===
                    "#process_claim_hourly_faucet",
                    "button#process_claim_hourly_faucet",
                    "input#process_claim_hourly_faucet",

                    # === Classes communes ===
                    ".btn-claim",
                    "[onclick*='claim']",
                    "[onclick*='Claim']",

                    # === Textes multi-langue ===
                    "button:has-text('Claim')",
                    "button:has-text('CLAIM')",
                    "button:has-text('claim')",
                    "button:has-text('Claim Now')",
                    "button:has-text('CLAIM NOW')",
                    "button:has-text('Réclamer')",          # Français
                    "button:has-text('réclamer')",
                    "button:has-text('Reclamar')",          # Espagnol
                    "button:has-text('Beanspruchen')",      # Allemand
                    "button:has-text('Претендовать')",      # Russe
                    "button:has-text('Claim Reward')",
                    "button:has-text('Get Reward')",

                    # === Fallbacks plus larges mais encore ciblés ===
                    "button.btn-primary:has-text('Claim')",
                    "button[type='submit']:has-text('Claim')",
                    "button[type='submit']:has-text('Réclamer')",
                ]

                # ── PATCH : recherche du bouton Claim avec retry jusqu'à 15s ──
                # (le bouton peut mettre du temps à s'afficher, ou une popup
                # tardive peut encore le masquer entre deux tentatives)
                claim_btn_wait_seconds = 15
                poll_interval = 1.0
                wait_start = time.time()
                claim_btn = None
                found_sel = None

                while (time.time() - wait_start) < claim_btn_wait_seconds and claim_btn is None:
                    claim_btn, found_sel = find_claim_button(page, claim_btn_selectors)
                    if claim_btn:
                        elapsed = time.time() - wait_start
                        print(f"✅ Bouton Claim trouvé → {found_sel} (après {elapsed:.1f}s)")
                        break
                    # Une popup peut apparaître avec un léger délai de rendu → on retente ici.
                    dismiss_popups(page, max_attempts=1)
                    time.sleep(poll_interval)

                # ── PATCH : le bouton n'est pas apparu VISIBLE en 15s → on
                # cherche s'il existe quand même dans le DOM (masqué) et on le
                # force à s'afficher, plutôt que d'abandonner directement.
                if not claim_btn:
                    forced_el, forced_sel = find_claim_button_any(page, claim_btn_selectors)
                    if forced_el:
                        print(f"⚠️ Bouton claim présent mais masqué → {forced_sel} : on force sa visibilité et on continue")
                        try:
                            page.evaluate("""(el) => {
                                el.style.display = 'block';
                                el.style.visibility = 'visible';
                                el.style.opacity = '1';
                                el.style.pointerEvents = 'auto';
                                el.removeAttribute('hidden');
                                // Certains sites masquent via un parent — on remonte
                                // quelques niveaux pour lever un display:none hérité.
                                let parent = el.parentElement;
                                for (let i = 0; i < 4 && parent; i++) {
                                    const style = window.getComputedStyle(parent);
                                    if (style.display === 'none') parent.style.display = 'block';
                                    if (style.visibility === 'hidden') parent.style.visibility = 'visible';
                                    parent = parent.parentElement;
                                }
                                el.scrollIntoView({ behavior: 'instant', block: 'center' });
                            }""", forced_el)
                            time.sleep(1.5)
                        except Exception as e:
                            print(f"⚠️ Impossible de forcer la visibilité : {e}")
                        claim_btn = forced_el
                        found_sel = forced_sel

                if not claim_btn:
                    print(f"⏳ Aucun bouton Claim trouvé après {claim_btn_wait_seconds}s → Inspection...")
                    try:
                        buttons_info = page.evaluate("""() => {
                            const buttons = Array.from(document.querySelectorAll(
                                'button, input[type="submit"], input[type="button"], a.btn, .btn, [role="button"]'
                            ));
                            return buttons.map(btn => {
                                const rect = btn.getBoundingClientRect();
                                const style = window.getComputedStyle(btn);
                                return {
                                    tag: btn.tagName,
                                    id: btn.id || null,
                                    class: btn.className || null,
                                    text: (btn.innerText || btn.value || '').trim().substring(0, 80),
                                    disabled: btn.disabled || false,
                                    visible: !!(rect.width > 0 && rect.height > 0 && style.display !== 'none'),
                                    width: Math.round(rect.width),
                                    height: Math.round(rect.height),
                                    x: Math.round(rect.x),
                                    y: Math.round(rect.y)
                                };
                            });
                        }""")
                        print(f"🔍 {len(buttons_info)} boutons trouvés :")
                        claim_btn_present_but_hidden = False
                        for i, b in enumerate(buttons_info, 1):
                            status = []
                            if b["disabled"]: status.append("DISABLED")
                            if not b["visible"]: status.append("HIDDEN")
                            status_str = f" [{', '.join(status)}]" if status else ""
                            print(f"  {i:2d}. <{b['tag']}> id={b['id']} class=\"{b['class']}\" "
                                  f"text=\"{b['text']}\" size={b['width']}x{b['height']}{status_str}")
                            # PATCH : repère explicitement le cas "bouton claim présent
                            # dans le DOM mais masqué" (différent de "bouton absent")
                            if b["id"] == "process_claim_hourly_faucet" and not b["disabled"] and not b["visible"]:
                                claim_btn_present_but_hidden = True

                        if claim_btn_present_but_hidden:
                            print("⚠️ Le bouton claim EXISTE dans le DOM mais reste masqué (probablement display:none) → capture de debug")
                    except Exception as e:
                        print(f"⚠️ Inspection échouée : {e}")
                        claim_btn_present_but_hidden = False

                    # PATCH : capture d'écran de debug quand le bouton est
                    # présent-mais-caché, pour diagnostiquer visuellement
                    # (viewport figé, popup résiduelle, tab non actif, etc.)
                    if claim_btn_present_but_hidden:
                        try:
                            safe_ts = int(time.time())
                            screenshot_path = str(
                                SCREENSHOTS_DIR / f"hidden_btn_{CLAIM_PLATFORM}_{safe_email}_{safe_ts}.png"
                            )
                            page.screenshot(path=screenshot_path, full_page=True)
                            print(f"📸 Capture de debug sauvegardée : {screenshot_path}")
                        except Exception as e:
                            print(f"⚠️ Capture de debug échouée : {e}")

                    minutes_left = extract_timer(page)
                    if minutes_left is not None and minutes_left < 5:
                        minutes_left = 5
                    wait_time = minutes_left if minutes_left is not None else 5
                    print(f"⏱️ Timer restant : {wait_time:.1f} minutes")

                    account["timer"] = wait_time
                    account["lastClaim"] = int(time.time() * 1000)
                    save_account(account)
                    add_history_entry(USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, False, 0)
                    stop_ffmpeg(ffmpeg_proc)
                    print(f"🎥 Vidéo sauvegardée : {video_path}")
                    return {"success": False, "message": f"Claim déjà fait, dispo dans {wait_time:.1f} min"}

                # Scroll
                page.evaluate("""(el) => {
                    el.style.display = 'inline-block';
                    el.style.visibility = 'visible';
                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }""", claim_btn)
                time.sleep(2.5)

                # ── PATCH : Turnstile en 3 tentatives ──
                max_turnstile_attempts = 3
                token_found = False
                for turnstile_attempt in range(1, max_turnstile_attempts + 1):
                    print(f"🔍 Résolution Turnstile (tentative {turnstile_attempt}/{max_turnstile_attempts})...")
                    select = page.query_selector("select")
                    if select:
                        options = page.eval_on_selector_all(
                            "select option",
                            "opts => opts.map(o => ({text: o.textContent.trim(), value: o.value}))"
                        )
                        turnstile_opt = next((o for o in options if "Turnstile" in o.get("text", "")), None)
                        if turnstile_opt:
                            page.select_option("select", turnstile_opt["value"])
                            print("🔁 Turnstile sélectionné")
                            time.sleep(2)

                    token_found = ananana.solve_turnstile(page, timeout=35)
                    if token_found:
                        break
                    print(f"⚠️ Turnstile non résolu (tentative {turnstile_attempt}/{max_turnstile_attempts})")
                    if turnstile_attempt < max_turnstile_attempts:
                        time.sleep(3)

                if not token_found:
                    print("❌ Token Turnstile non résolu après plusieurs tentatives")
                    account["lastClaim"] = int(time.time() * 1000)
                    account["timer"] = 5
                    save_account(account)
                    add_history_entry(USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, False, 0)
                    stop_ffmpeg(ffmpeg_proc)
                    print(f"🎥 Vidéo sauvegardée : {video_path}")
                    return {"success": False, "message": "Échec Turnstile"}

                print("✅ Turnstile résolu")
                time.sleep(2)

                # ── Clic fiable ──
                print("🖱️ Clic sur le bouton Claim")
                try:
                    claim_btn.click(timeout=8000)
                    print("✅ Clic élément réussi")
                except Exception as e:
                    print(f"⚠️ Clic élément échoué ({e}), tentative clic forcé...")
                    try:
                        claim_btn.click(timeout=5000, force=True)
                        print("✅ Clic forcé réussi")
                    except Exception as e2:
                        print(f"⚠️ Clic forcé échoué ({e2}), fallback souris...")
                        box = claim_btn.bounding_box()
                        if box and box["width"] > 5:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            ananana.move_mouse_to(page, x, y)
                            page.mouse.click(x, y)
                        else:
                            # PATCH : même sans bounding box exploitable, on ne
                            # renonce plus — dernier recours via dispatch JS.
                            print("⚠️ Pas de bounding box exploitable, tentative de clic JS direct...")
                            try:
                                page.evaluate("(el) => el.click()", claim_btn)
                                print("✅ Clic JS direct effectué")
                            except Exception as e3:
                                raise RuntimeError(f"Impossible de cliquer sur le bouton Claim : {e3}")

                # Attente résultat
                try:
                    page.wait_for_function("""
                        () => {
                            const btn = document.querySelector('#process_claim_hourly_faucet');
                            if (btn && btn.disabled) return true;
                            const msgs = document.querySelectorAll('.alert-success, .alert-danger, .error, [class*="error"], .success, [class*="success"]');
                            for (const msg of msgs) if (msg.textContent.trim().length > 0) return true;
                            return false;
                        }
                    """, timeout=20000)
                    claim_result = 'success'
                except PlaywrightTimeout:
                    claim_result = 'timeout'

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass
                time.sleep(3)

                messages = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('.alert-success, .alert-danger, .success, [class*="success"], .error, [class*="error"]'))
                        .map(el => el.textContent.trim()).filter(t => t);
                }""")

                # ── PATCH : détection de modale de confirmation par texte ──
                # (ex: "🎉 Réclamé  0.005 TRX" sur freetron.in, sans classe .alert-success)
                confirmed, confirmed_amount, confirmed_text = extract_claim_confirmation(page)
                if confirmed:
                    print(f"🎉 Modale de confirmation détectée : {confirmed_text}")

                result_message = messages[0] if messages else confirmed_text
                if result_message:
                    print(f"📢 Message du site : {result_message}")

                is_error = any(w in result_message.lower() for w in ["error", "something went wrong", "try again", "failed"]) if result_message else False
                btn_disabled_now = page.evaluate("""() => {
                    const btn = document.querySelector('#process_claim_hourly_faucet');
                    return btn ? btn.disabled : false;
                }""")

                success = confirmed or ((not is_error) and (claim_result != 'timeout' or btn_disabled_now))

                if success:
                    print("✅ Claim réussi")
                else:
                    print("❌ Claim échoué")

                # Si une modale de confirmation est affichée, on la ferme
                # (bouton × / "Plus Tard") pour ne pas bloquer la suite/prochain run.
                if confirmed:
                    dismiss_popups(page)

                balance = 0.0
                try:
                    bal_el = page.query_selector('[class*="balance"]')
                    if bal_el:
                        balance_text = bal_el.inner_text()
                        balance = float("".join(c for c in balance_text if c.isdigit() or c == "."))
                        print(f"💰 Solde après claim : {balance}")
                except:
                    pass

                # Si pas de sélecteur de solde global mais un montant lu dans la
                # modale de confirmation, on s'en sert pour l'historique.
                if success and balance == 0.0 and confirmed_amount is not None:
                    balance = confirmed_amount

                if success:
                    account["totalClaims"] = account.get("totalClaims", 0) + 1
                    new_timer = extract_timer(page)
                    account["timer"] = max(new_timer or 60, 60)
                else:
                    # ← En cas d'échec on met seulement 5 minutes
                    account["timer"] = 5

                account["finalBalance"] = balance
                account["lastClaim"] = int(time.time() * 1000)
                save_account(account)
                add_history_entry(USER_ID, CLAIM_EMAIL, CLAIM_PLATFORM, success, balance)

                stop_ffmpeg(ffmpeg_proc)
                print(f"🎥 Vidéo sauvegardée : {video_path}")
                return {"success": success, "message": result_message or ("Claim OK" if success else "Échec")}

        except Exception as e:
            print(f"❌ Erreur tentative {attempt}: {e}")
            stop_ffmpeg(ffmpeg_proc)
            print(f"🎥 Vidéo sauvegardée (erreur) : {video_path}")
            if attempt == 3:
                raise
            time.sleep(3)

    raise RuntimeError("Échec du claim après plusieurs tentatives")

def main():
    try:
        g = get_github_client()
        repo = g.get_repo(f"{GH_USERNAME}/{GH_REPO}")
        try:
            contents = repo.get_contents(USER_FILE, ref=GH_BRANCH)
            account = json.loads(base64.b64decode(contents.content).decode())
        except GithubException as e:
            if e.status == 404:
                print("❌ Compte introuvable")
                sys.exit(1)
            raise

        account.setdefault("totalClaims", 0)
        account.setdefault("initialBalance", 0)
        account.setdefault("finalBalance", 0)

        print(f"📋 Compte chargé : {account['email']} ({account['platform']})")
        result = claim_with_cookies(account)
        print(f"🏁 Terminé. Succès: {result['success']} - {result['message']}")
    except Exception as e:
        print(f"❌ Erreur fatale : {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
