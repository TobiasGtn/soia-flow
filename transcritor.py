#!/usr/bin/env python3
"""SOIA Flow — ditado por voz com Whisper na nuvem (Groq).

Uma barrinha preta discreta fica no centro-inferior da tela, logo acima da
barra de tarefas. Ao passar o mouse, ela expande mostrando o microfone e o
atalho global. Segure o atalho para gravar; ao soltar, o áudio é transcrito
pelo Groq e o texto vai para a área de transferência.
"""

import os
import sys
import traceback

# ── Pasta de dados do app (config + log) ───────────────────────────────────────
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "TranscritorDesktop")
os.makedirs(APP_DIR, exist_ok=True)
LOG_PATH = os.path.join(APP_DIR, "transcritor.log")

# pythonw.exe roda sem console: stdout/stderr = None quebra libs que escrevem
# neles. Redireciona para o log ANTES de qualquer import pesado.
class _LogWriter:
    def __init__(self, path):
        self._f = open(path, "a", encoding="utf-8", buffering=1)
    def write(self, s):
        try: self._f.write(s)
        except Exception: pass
    def flush(self):
        try: self._f.flush()
        except Exception: pass
    def fileno(self): return -1
    def isatty(self): return False

_lw = _LogWriter(LOG_PATH)
if sys.stdout is None: sys.stdout = _lw
if sys.stderr is None: sys.stderr = _lw

import logging
_root_log = logging.getLogger()
if not any(isinstance(h, logging.FileHandler) for h in _root_log.handlers):
    _fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s", "%H:%M:%S"))
    _root_log.addHandler(_fh)
_root_log.setLevel(logging.INFO)
log = logging.getLogger("app")

log.info("=== SOIA Flow v2.2 — iniciando ===")

# ── Imports ────────────────────────────────────────────────────────────────────
import ctypes
import io
import json
import math
import queue
import tempfile
import threading
import time
import wave
import winsound
from array import array

# DPI awareness ANTES do Tk: garante que coordenadas de tela sejam reais
# (sem isso, com escala 125%/150% do Windows a barrinha sai do lugar).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Instância única: se já houver uma rodando, sinaliza e sai.
_SHOW_EVENT = ctypes.windll.kernel32.CreateEventW(
    None, False, False, "TranscritorDesktop_Show_v1")
_mutex = ctypes.windll.kernel32.CreateMutexW(
    None, False, "TranscritorDesktop_v1_mutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    ctypes.windll.kernel32.SetEvent(_SHOW_EVENT)
    log.info("Já em execução — sinalizando e saindo.")
    sys.exit(0)

import tkinter as tk
from tkinter import font as tkfont
import requests
import sounddevice as sd
import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk

log.info("Imports OK")

# ── Configuração persistente ───────────────────────────────────────────────────
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
KEYRING_SVC = "TranscritorDesktop"

CONFIG_PADRAO = {
    "atalho":       "ctrl+shift+space",
    "modelo":       "whisper-large-v3-turbo",
    "idioma":       "pt",
    "fechar_apos":  True,    # True = só copia e recolhe; False = caixa editável
    "autostart":    False,
    "autopaste":    True,    # cola automaticamente onde o cursor estiver
    "dicionario":   "",      # palavras/nomes que enviesam a transcrição
}

def _load_config() -> dict:
    cfg = dict(CONFIG_PADRAO)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg

def _save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error("_save_config: %s", e)

# ── Constantes ─────────────────────────────────────────────────────────────────
TAXA        = 16000            # Hz — o que o Whisper espera
GROQ_URL    = "https://api.groq.com/openai/v1"
MAX_SEG     = 900              # auto-para após 15 min de gravação
VERSAO      = "v 2.2"

# Design — balões pretos com borda cinza sutil (estilo Wispr).
# TRANSPARENT é a cor-chave da janela: os balões são renderizados em PIL com
# antialiasing fundindo a borda para essa cor (quase preta), o que elimina o
# serrilhado sem deixar halo visível.
TRANSPARENT = "#000001"
PANEL_FILL  = "#101010"        # fundo dos balões
BORDER      = "#5c5c5c"        # borda dos balões
PILL_BORDER = "#8c8c8c"        # borda da barrinha (um pouco mais visível)
DOT_RED     = "#e53935"
WHITE       = "#ffffff"
MUTED       = "#8a8a8a"

# Paleta SOIA CRC (de dev/soia-nova-interface/src/index.css)
TINTA       = "#0e0f11"        # texto principal
TINTA_3     = "#6b717a"        # texto secundário
VERDE       = "#0e7a5f"        # o único acento: verde-esmeralda profundo
VERDE_ESC   = "#0a5c47"
VERDE_VIVO  = "#13a87b"
VERDE_SUAVE = "#e9f4f0"
VERMELHO    = "#dc2626"
FIO         = "#e5e7eb"
GREEN       = VERDE_VIVO       # sinalizações positivas nos balões escuros
ACCENT      = VERDE
ACCENT_HOV  = VERDE_ESC
# Tela de configurações — superfícies claras do SOIA
SET_BG      = "#ffffff"
FIELD_BG    = "#e9ebef"        # o cinza-fundo do SOIA: contraste visível
FIELD_HOV   = "#dde0e5"
SEP         = FIO

SPINNER     = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
ICO_MIC     = "\uE720"         # microfone — fonte Segoe MDL2 Assets (Win10/11)


# ── Sons suaves (senoide com fade, sem os bipes estridentes do Windows) ────────
def _tom_wav(freq: float, ms: int, vol: float) -> bytes:
    """Gera um WAV em memória: senoide com fade-in/fade-out, volume baixo."""
    sr = 22050
    n = int(sr * ms / 1000)
    fade = max(1, int(sr * 0.025))   # 25 ms de fade
    arr = array("h")
    for i in range(n):
        a = 1.0
        if i < fade:
            a = i / fade
        if i > n - fade:
            a = max(0.0, (n - i) / fade)
        arr.append(int(vol * a * 32767 *
                       math.sin(2 * math.pi * freq * i / sr)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(arr.tobytes())
    return buf.getvalue()

# winsound não toca da MEMÓRIA em modo assíncrono (RuntimeError silencioso),
# então os sons são gravados em arquivo uma vez e tocados de lá.
SOM_INICIO = os.path.join(APP_DIR, "som_inicio.wav")
SOM_ERRO   = os.path.join(APP_DIR, "som_erro.wav")
try:
    with open(SOM_INICIO, "wb") as _f:
        _f.write(_tom_wav(660, 130, 0.16))   # blip curto e delicado
    with open(SOM_ERRO, "wb") as _f:
        _f.write(_tom_wav(250, 200, 0.12))   # tom grave discreto
except Exception as _e:
    log.warning("sons: %s", _e)

def tocar(caminho: str):
    try:
        winsound.PlaySound(caminho, winsound.SND_FILENAME |
                           winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def pretty_hotkey(combo: str) -> str:
    return " + ".join(p.strip().capitalize() for p in combo.split("+") if p.strip())


def _logo_soia(size: int = 64) -> Image.Image:
    """Logo do SOIA CRC: funil branco sobre o tile verde vitrificado
    (mesmo desenho do favicon.svg do soia-nova-interface)."""
    k = 4
    s = size * k
    # Tile com degradê vertical #149a6f → #0c6b50
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    topo, base = (20, 154, 111), (12, 107, 80)
    for y in range(s):
        t = y / (s - 1)
        cor = tuple(int(topo[i] + (base[i] - topo[i]) * t) for i in range(3))
        gd.line([(0, y), (s, y)], fill=cor + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, s - 1, s - 1], radius=int(s * 7 / 32), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    # Funil: três barras brancas decrescentes
    d = ImageDraw.Draw(img)
    e = s / 32
    for wbar, y in ((24.0, 8.0), (15.6, 14.4), (7.6, 20.8)):
        x0 = (32 - wbar) / 2 * e
        d.rounded_rectangle([x0, y * e, x0 + wbar * e, (y + 3.2) * e],
                            radius=1.4 * e, fill="white")
    return img.resize((size, size), Image.LANCZOS)


def _area_trabalho_bottom() -> int:
    """Borda inferior da área útil da tela (acima da barra de tarefas)."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    r = RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
        return r.bottom
    return ctypes.windll.user32.GetSystemMetrics(1) - 48


class App:

    def __init__(self):
        self.cfg       = _load_config()
        self.gravando  = False
        self._estado   = ""
        self._chunks   = []
        self._last_chunk = b""
        self.stream    = None
        self._t0       = 0.0
        self._tid      = None
        self._spin_i   = 0
        self._queue    = queue.Queue()
        self._canvas   = None
        self._cv_bars  = None
        self._bars_dim = (90, 18)
        self._n_bars   = 17
        self._amp_history = [0.0] * self._n_bars
        self._tray     = None
        self._hk_main  = None
        self._hk_esc   = None
        self._win_cfg  = None
        self._capturando = False
        self._hk_cv    = None
        self._hk_item  = None
        self._lbl_teste = None
        self._novo_atalho = self.cfg.get("atalho", CONFIG_PADRAO["atalho"])
        self._inicio_por_tecla = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.configure(bg=TRANSPARENT)
        self.root.title("SOIA Flow")

        # Fator de escala da tela (1.0 = 100%, 1.25 = 125%…)
        fator = self.root.winfo_fpixels("1i") / 96.0
        self.S = lambda px: int(round(px * fator))

        # Fonte da marca: Inter se instalada; senão, Segoe UI
        try:
            self._fam = "Inter" if "Inter" in tkfont.families() else "Segoe UI"
        except Exception:
            self._fam = "Segoe UI"

        self._sw     = self.root.winfo_screenwidth()
        self._bottom = _area_trabalho_bottom() - self.S(8)

        self._setup_tray()
        self._registrar_atalho()
        self._state("pill")

        self.root.after(80, self._drain)
        self.root.after(500, self._check_show_event)
        if not self.obter_token():
            self.root.after(600, self._abrir_config)

    # ── Token (Cofre de Credenciais do Windows, com fallback no config) ────────

    def obter_token(self) -> str:
        try:
            import keyring
            t = keyring.get_password(KEYRING_SVC, "groq_api_key")
            if t:
                return t
        except Exception as e:
            log.warning("keyring get: %s", e)
        return self.cfg.get("groq_api_key", "")

    def salvar_token(self, t: str):
        try:
            import keyring
            keyring.set_password(KEYRING_SVC, "groq_api_key", t)
            self.cfg.pop("groq_api_key", None)
            log.info("Token salvo no Cofre de Credenciais")
            return
        except Exception as e:
            log.warning("keyring set: %s — usando config.json", e)
        self.cfg["groq_api_key"] = t
        _save_config(self.cfg)

    # ── Fila thread-safe — único canal entre threads e UI ─────────────────────

    def _drain(self):
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "done":
                    self._clipboard_set(item[1])
                    if self.cfg.get("autopaste", True):
                        # Cola onde o cursor estiver (a barrinha nunca rouba o foco)
                        self.root.after(150, self._colar_no_cursor)
                    if self.cfg.get("fechar_apos", True):
                        self._state("flash")
                    else:
                        self._state("caixa", item[1])
                elif kind == "error":
                    tocar(SOM_ERRO)
                    self._state("error", item[1])
                elif kind == "nada":
                    # Sem fala captada: recolhe em silêncio, sem aviso
                    self._state("pill")
                elif kind == "tecla":
                    # Pressionou o atalho global
                    if self.gravando:
                        # Repetição de tecla enquanto segura: ignora.
                        # Se a gravação começou por clique, o atalho encerra.
                        if not self._inicio_por_tecla:
                            self._confirm()
                    elif self._estado != "processing":
                        self._start(por_tecla=True)
                elif kind == "toggle":
                    self._toggle()
                elif kind == "descartar":
                    if self.gravando:
                        self._discard()
                elif kind == "config":
                    self._abrir_config()
                elif kind == "captura":
                    self._fim_captura(item[1])
                elif kind == "teste":
                    self._mostrar_teste(item[1], item[2], item[3])
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _check_show_event(self):
        if ctypes.windll.kernel32.WaitForSingleObject(_SHOW_EVENT, 0) == 0:
            self._abrir_config()
        self.root.after(500, self._check_show_event)

    # ── Bandeja ────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Configurações",
                             lambda: self._queue.put(("config",)),
                             default=True),   # clique esquerdo no ícone
            pystray.MenuItem("Gravar / Parar",
                             lambda: self._queue.put(("toggle",))),
            pystray.MenuItem("Fechar", self._quit),
        )
        self._tray = pystray.Icon("soiaflow", _logo_soia(),
                                  "SOIA Flow", menu)
        threading.Thread(target=self._tray_run, daemon=True).start()

    def _tray_run(self):
        try:
            self._tray.run()
        except Exception:
            log.error("Bandeja:\n%s", traceback.format_exc())

    def _quit(self, *_):
        try: keyboard.unhook_all()
        except Exception: pass
        try: self._tray.stop()
        except Exception: pass
        _save_config(self.cfg)
        self.root.after(0, self.root.quit)

    # ── Atalho global ──────────────────────────────────────────────────────────

    def _registrar_atalho(self):
        self._remover_atalho()
        combo = self.cfg.get("atalho") or CONFIG_PADRAO["atalho"]
        try:
            self._hk_main = keyboard.add_hotkey(
                combo, lambda: self._queue.put(("tecla",)))
            log.info("Atalho global: %s", combo)
        except Exception as e:
            log.error("Atalho '%s' inválido (%s) — voltando ao padrão", combo, e)
            self.cfg["atalho"] = CONFIG_PADRAO["atalho"]
            self._hk_main = keyboard.add_hotkey(
                self.cfg["atalho"], lambda: self._queue.put(("tecla",)))

    def _remover_atalho(self):
        if self._hk_main is not None:
            try: keyboard.remove_hotkey(self._hk_main)
            except Exception: pass
            self._hk_main = None

    def _combo_pressionado(self) -> bool:
        """True se todas as teclas do atalho ainda estão pressionadas."""
        try:
            partes = [p.strip() for p in
                      self.cfg.get("atalho", "").split("+") if p.strip()]
            return bool(partes) and all(keyboard.is_pressed(p) for p in partes)
        except Exception:
            return True   # na dúvida, não interrompe a gravação

    # ── Área de transferência ──────────────────────────────────────────────────

    def _clipboard_set(self, txt: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
        except Exception as e:
            log.error("clipboard: %s", e)

    def _colar_no_cursor(self):
        """Simula Ctrl+V para colar no campo que estiver com o foco."""
        try:
            keyboard.send("ctrl+v")
            log.info("Autopaste enviado")
        except Exception as e:
            log.warning("autopaste: %s", e)

    def _set_noactivate(self, ativo: bool):
        """Impede (ou permite) que a janela da barrinha receba o foco.
        Com WS_EX_NOACTIVATE, clicar nela não tira o foco do app onde o
        usuário está digitando — essencial para o autopaste funcionar."""
        try:
            GWL_EXSTYLE      = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = (ctypes.windll.user32.GetParent(self.root.winfo_id())
                    or self.root.winfo_id())
            est = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ativo:
                est |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            else:
                est &= ~WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, est)
        except Exception as e:
            log.warning("noactivate: %s", e)

    # ── Painéis renderizados em PIL (bordas suaves, sem serrilhado) ────────────

    def _painel(self, w, h, r=None, fill=PANEL_FILL, border=BORDER):
        """Retângulo arredondado suavizado (supersampling 4×) com borda de 1 px.
        Borda por preenchimento duplo (nunca quebra nos cantos); o fundo é a
        cor-chave transparente, então o antialias funde para ela."""
        if r is None:
            r = h // 2
        k = 4
        tr = tuple(int(TRANSPARENT[i:i+2], 16) for i in (1, 3, 5))
        img = Image.new("RGBA", (w * k, h * k), tr + (255,))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w * k - 1, h * k - 1],
                            radius=r * k, fill=border)
        d.rounded_rectangle([k, k, w * k - 1 - k, h * k - 1 - k],
                            radius=max(1, (r - 1) * k), fill=fill)
        img = img.resize((w, h), Image.LANCZOS)
        return ImageTk.PhotoImage(img)

    def _icone_mic(self, dia: int, circulo: str) -> ImageTk.PhotoImage:
        """Botão de microfone em alta resolução: círculo + mic branco (PIL 4×)."""
        k = 4
        s = dia * k
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cr = tuple(int(circulo[i:i+2], 16) for i in (1, 3, 5))
        d.ellipse([0, 0, s - 1, s - 1], fill=cr + (255,))
        br = (255, 255, 255, 255)
        cx = s / 2
        lw = max(k, int(s * 0.05))
        bw = s * 0.11
        d.rounded_rectangle([cx - bw, s * 0.20, cx + bw, s * 0.52],
                            radius=bw, fill=br)
        aw = s * 0.20
        d.arc([cx - aw, s * 0.32, cx + aw, s * 0.62],
              start=0, end=180, fill=br, width=lw)
        d.line([cx, s * 0.62, cx, s * 0.70], fill=br, width=lw)
        d.line([cx - s * 0.11, s * 0.735, cx + s * 0.11, s * 0.735],
               fill=br, width=lw)
        return ImageTk.PhotoImage(img.resize((dia, dia), Image.LANCZOS))

    def _reset_canvas(self, w, h, r=None, fill=PANEL_FILL, border=BORDER):
        if self._tid:
            self.root.after_cancel(self._tid)
            self._tid = None
        x = (self._sw - w) // 2
        y = self._bottom - h
        first = self._canvas is None
        if first:
            cv = tk.Canvas(self.root, width=w, height=h,
                           bg=TRANSPARENT, highlightthickness=0)
            cv.pack()
            self._canvas = cv
        else:
            cv = self._canvas
            for ch in cv.winfo_children():
                ch.destroy()
            cv.delete("all")
            cv.config(width=w, height=h)
        for ev in ("<Button-1>", "<Enter>", "<Leave>"):
            cv.unbind(ev)
        cv.configure(cursor="")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        painel = self._painel(w, h, r, fill, border)
        cv.create_image(0, 0, anchor="nw", image=painel)
        cv._painel = painel   # evita coleta de lixo da imagem
        if first:
            self.root.update_idletasks()
            self.root.deiconify()
        return cv

    def _texto_btn(self, cv, x, y, txt, cor_hover, cmd, size=13):
        item = cv.create_text(x, y, text=txt, fill=MUTED,
                              font=("Segoe UI", size))
        cv.tag_bind(item, "<Enter>", lambda _: cv.itemconfig(item, fill=cor_hover))
        cv.tag_bind(item, "<Leave>", lambda _: cv.itemconfig(item, fill=MUTED))
        cv.tag_bind(item, "<Button-1>", lambda _: cmd())
        return item

    # ── Estados ────────────────────────────────────────────────────────────────

    def _state(self, name, *args):
        log.info("→ estado: %s", name)
        self._estado = name
        getattr(self, f"_s_{name}")(*args)
        # A caixa editável precisa de foco para digitar; os demais estados
        # nunca podem roubar o foco do app onde o usuário está.
        self._set_noactivate(name != "caixa")

    def _s_pill(self):
        """Barrinha preta discreta com borda cinza — repouso."""
        W, H = self.S(60), self.S(9)
        cv = self._reset_canvas(W, H, border=PILL_BORDER)
        cv.configure(cursor="hand2")
        cv.bind("<Enter>", lambda _: self._state("hover"))
        cv.bind("<Button-1>", lambda _: self._toggle())

    def _s_hover(self):
        """Balão expandido ao passar o mouse: microfone + lembrete do atalho."""
        S = self.S
        atalho = pretty_hotkey(self.cfg.get("atalho", CONFIG_PADRAO["atalho"]))
        f_ditar  = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_atalho = tkfont.Font(family="Segoe UI", size=9)
        x_txt  = S(52)
        w_dit  = f_ditar.measure("Ditar")
        w_atl  = f_atalho.measure(atalho)
        W = x_txt + w_dit + S(10) + w_atl + S(20)
        H = S(42)
        cv = self._reset_canvas(W, H)
        cv.configure(cursor="hand2")
        cy = H // 2
        # Botão do microfone — preto e branco, renderizado em PIL
        dia = S(28)
        self._mic_n = self._icone_mic(dia, "#2e2e2e")
        self._mic_h = self._icone_mic(dia, "#3f3f3f")
        cv._mics = (self._mic_n, self._mic_h)
        mic = cv.create_image(S(28), cy, image=self._mic_n)
        cv.create_text(x_txt, cy, text="Ditar", fill=WHITE, anchor="w",
                       font=f_ditar)
        cv.create_text(x_txt + w_dit + S(10), cy, text=atalho, fill=MUTED,
                       anchor="w", font=f_atalho)
        cv.bind("<Button-1>", lambda _: self._toggle())
        cv.bind("<Enter>", lambda _: cv.itemconfig(mic, image=self._mic_h))
        cv.bind("<Leave>", lambda _: cv.itemconfig(mic, image=self._mic_n))
        self._vigiar_hover()

    def _vigiar_hover(self):
        """Recolhe o balão quando o mouse sai de cima dele."""
        if self._estado != "hover":
            return
        x, y = self.root.winfo_pointerxy()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        dentro = (rx - 4 <= x <= rx + self.root.winfo_width() + 4 and
                  ry - 4 <= y <= ry + self.root.winfo_height() + 4)
        if dentro:
            self._tid = self.root.after(150, self._vigiar_hover)
        else:
            self._state("pill")

    def _s_recording(self):
        """Balão mini e delicado: só as ondas da voz.
        Iniciado por clique, ganha um ✕ para encerrar; por atalho
        (segurar/soltar), fica só o gráfico."""
        S = self.S
        self._n_bars = 7
        por_clique = not self._inicio_por_tecla
        W = S(86) if por_clique else S(62)
        H = S(30)
        cv = self._reset_canvas(W, H)
        cv.configure(cursor="hand2")
        bw, bh = S(52), S(22)
        self._bars_dim = (bw, bh)
        self._cv_bars = tk.Canvas(cv, width=bw, height=bh,
                                  bg=PANEL_FILL, highlightthickness=0)
        self._bars_item = self._cv_bars.create_image(0, 0, anchor="nw")
        x_bars = S(32) if por_clique else W // 2
        cv.create_window(x_bars, H // 2, window=self._cv_bars)
        self._amp_history = [0.0] * self._n_bars
        self._draw_bars()
        # Clique encerra e transcreve (Esc descarta)
        cv.bind("<Button-1>", lambda _: self._confirm())
        self._cv_bars.bind("<Button-1>", lambda _: self._confirm())
        if por_clique:
            self._texto_btn(cv, W - S(16), H // 2, "✕", WHITE,
                            self._confirm, size=11)
        self._tick()

    def _s_processing(self):
        S = self.S
        W, H = S(150), S(28)
        cv = self._reset_canvas(W, H)
        self._spin_item = cv.create_text(W // 2, H // 2,
                                         text=f"{SPINNER[0]}  Transcrevendo…",
                                         fill=MUTED, font=("Segoe UI", 9))
        self._cv_spin = cv
        self._spin_i = 0
        self._spin()

    def _s_flash(self):
        S = self.S
        W, H = S(110), S(28)
        cv = self._reset_canvas(W, H)
        cv.create_text(W // 2, H // 2, text="✓  Copiado", fill="#e8e8e8",
                       font=("Segoe UI", 9, "bold"))
        self._tid = self.root.after(900, lambda: self._state("pill"))

    def _s_error(self, msg="Erro"):
        S = self.S
        msg_s = (msg[:46] + "…") if len(msg) > 46 else msg
        W, H = S(310), S(30)
        cv = self._reset_canvas(W, H)
        cv.create_text(W // 2, H // 2, text=f"✕  {msg_s}", fill="#e8e8e8",
                       font=("Segoe UI", 9))
        self._tid = self.root.after(3000, lambda: self._state("pill"))

    def _s_caixa(self, txt=""):
        S = self.S
        W, H = S(380), S(262)
        cv = self._reset_canvas(W, H, r=S(14))
        self._texto_btn(cv, W - S(22), S(20), "✕", WHITE,
                        lambda: self._state("pill"), size=12)
        cv.create_text(W // 2, S(22),
                       text="✓  Copiado para a área de transferência",
                       fill="#e8e8e8", font=("Segoe UI", 9, "bold"))
        borda = tk.Frame(cv, bg="#3d3d3d", padx=1, pady=1)
        interno = tk.Frame(borda, bg="#242424")
        interno.pack()
        sb = tk.Scrollbar(interno, bg="#242424", troughcolor=PANEL_FILL,
                          relief="flat", bd=0, width=10)
        tb = tk.Text(interno, bg="#242424", fg=WHITE, insertbackground=WHITE,
                     font=("Segoe UI", 10), wrap="word", relief="flat",
                     bd=0, padx=8, pady=6, height=7, width=40,
                     yscrollcommand=sb.set)
        sb.config(command=tb.yview)
        tb.pack(side=tk.LEFT)
        sb.pack(side=tk.RIGHT, fill="y")
        tb.insert("1.0", txt)
        cv.create_window(W // 2, S(134), window=borda)
        def _recopiar():
            self._clipboard_set(tb.get("1.0", "end-1c"))
            cv.itemconfig(btn_copiar, text="✓ Copiado", fill="#e8e8e8")
            self.root.after(1200, lambda: cv.itemconfig(
                btn_copiar, text="⧉  Copiar", fill=MUTED))
        btn_copiar = cv.create_text(W // 2, H - S(24), text="⧉  Copiar",
                                    fill=MUTED, font=("Segoe UI", 10))
        cv.tag_bind(btn_copiar, "<Enter>",
                    lambda _: cv.itemconfig(btn_copiar, fill=WHITE))
        cv.tag_bind(btn_copiar, "<Leave>",
                    lambda _: cv.itemconfig(btn_copiar, fill=MUTED))
        cv.tag_bind(btn_copiar, "<Button-1>", lambda _: _recopiar())

    # ── Animações ──────────────────────────────────────────────────────────────

    def _tick(self):
        if not self.gravando:
            return
        decorrido = time.time() - self._t0
        if decorrido >= MAX_SEG:
            self._confirm()
            return
        # Modo segurar-para-falar: soltou o atalho → para e transcreve
        if self._inicio_por_tecla and decorrido > 0.25 \
                and not self._combo_pressionado():
            if decorrido < 0.6:
                self._discard(silencioso=True)   # toque rápido demais
            else:
                self._confirm()
            return
        # Amplitude do último bloco de áudio
        amp = 0.0
        if self._last_chunk:
            try:
                arr = array("h")
                arr.frombytes(self._last_chunk)
                pico = max(max(arr), -min(arr)) / 32768.0
                # Raiz quadrada: fala normal (pico ~0,05-0,2) já enche as barras
                amp = min(1.0, math.sqrt(pico * 4.0))
            except Exception:
                pass
        self._amp_history.pop(0)
        self._amp_history.append(amp)
        self._draw_bars()
        self._tid = self.root.after(80, self._tick)

    def _draw_bars(self):
        """Barras de voz em pílula (pontas redondas), suavizadas em PIL 4×.
        Em repouso viram bolinhas — o visual delicado do Wispr."""
        cv = self._cv_bars
        if cv is None or not cv.winfo_exists():
            return
        W, H = self._bars_dim
        n = self._n_bars
        k = 4
        pf = tuple(int(PANEL_FILL[i:i+2], 16) for i in (1, 3, 5))
        img = Image.new("RGB", (W * k, H * k), pf)
        d = ImageDraw.Draw(img)
        bw  = max(4, self.S(4)) * k
        gap = max(3, self.S(3)) * k
        x0  = max(0, (W * k - (n * bw + (n - 1) * gap)) // 2)
        mid = H * k // 2
        maxh = mid - k
        for i, amp in enumerate(self._amp_history):
            half = max(bw // 2, int(amp * maxh))
            x = x0 + i * (bw + gap)
            d.rounded_rectangle([x, mid - half, x + bw - 1, mid + half],
                                radius=bw // 2, fill=(232, 232, 232))
        foto = ImageTk.PhotoImage(img.resize((W, H), Image.LANCZOS))
        cv._foto = foto   # evita coleta de lixo
        cv.itemconfig(self._bars_item, image=foto)

    def _spin(self):
        if self._estado != "processing":
            return
        self._spin_i = (self._spin_i + 1) % len(SPINNER)
        self._cv_spin.itemconfig(
            self._spin_item, text=f"{SPINNER[self._spin_i]}  Transcrevendo…")
        self._tid = self.root.after(80, self._spin)

    # ── Gravação ───────────────────────────────────────────────────────────────

    def _cb(self, indata, frames, t, status):
        if self.gravando:
            b = bytes(indata)
            self._chunks.append(b)
            self._last_chunk = b

    def _toggle(self):
        """Início/parada por clique (barrinha, balão ou bandeja)."""
        if self._estado == "processing":
            return
        if self.gravando:
            self._confirm()
        else:
            self._start(por_tecla=False)

    def _start(self, por_tecla=False):
        if not self.obter_token():
            self._state("error", "Configure o token do Groq")
            self._abrir_config()
            return
        log.info("Iniciando gravação (%s)",
                 "segurar tecla" if por_tecla else "clique")
        self._chunks = []
        self._last_chunk = b""
        try:
            self.stream = sd.RawInputStream(
                samplerate=TAXA, channels=1, dtype="int16",
                blocksize=1600, callback=self._cb)
            self.stream.start()
        except Exception as e:
            log.error("Microfone: %s", e)
            self._state("error", "Microfone indisponível")
            return
        self.gravando = True
        self._inicio_por_tecla = por_tecla
        self._t0 = time.time()
        tocar(SOM_INICIO)
        # Esc descarta enquanto grava
        try:
            self._hk_esc = keyboard.add_hotkey(
                "esc", lambda: self._queue.put(("descartar",)))
        except Exception:
            self._hk_esc = None
        self._state("recording")

    def _stop_stream(self):
        self.gravando = False
        if self._hk_esc is not None:
            try: keyboard.remove_hotkey(self._hk_esc)
            except Exception: pass
            self._hk_esc = None
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _discard(self, silencioso=False):
        log.info("Gravação descartada%s", " (toque rápido)" if silencioso else "")
        self._stop_stream()
        self._chunks = []
        self._state("pill")

    def _confirm(self):
        log.info("Confirmado — %d blocos", len(self._chunks))
        self._stop_stream()
        self._state("processing")
        threading.Thread(target=self._transcrever, daemon=True).start()

    # ── Transcrição via Groq ───────────────────────────────────────────────────

    # Frases que o Whisper "inventa" sobre silêncio/ruído de fim de gravação
    ALUCINACOES = {
        "obrigado", "obrigada", "muito obrigado", "muito obrigada",
        "ok", "okay", "e aí", "tchau", "valeu", "até mais", "até logo",
        "legendas pela comunidade amara.org", "amara.org", "obrigado por assistir",
    }

    def _transcrever(self):
        try:
            # Corta silêncio do início e do fim: o "clique" de abrir/fechar o
            # microfone e a respiração final são o que vira "obrigado"/"ok".
            LIMIAR = 400   # pico int16 (~1,2% do fundo de escala)
            picos = []
            for c in self._chunks:
                try:
                    a = array("h")
                    a.frombytes(c)
                    picos.append(max(max(a), -min(a)))
                except Exception:
                    picos.append(0)
            com_voz = [i for i, p in enumerate(picos) if p > LIMIAR]
            if not com_voz:
                # Nada falado — nem chama o Groq (evita alucinação e gasto)
                log.info("Sem voz — recolhendo em silêncio")
                self._queue.put(("nada",))
                return
            i0 = max(0, com_voz[0] - 2)              # ~0,2 s antes da voz
            i1 = min(len(self._chunks), com_voz[-1] + 4)   # ~0,4 s depois
            dados = b"".join(self._chunks[i0:i1])
            fala_dur = (com_voz[-1] - com_voz[0] + 1) * 0.1
            dur = len(dados) / (TAXA * 2)
            log.info("Áudio: %.1f s enviados (%.1f s de voz, %.1f KB)",
                     dur, fala_dur, len(dados) / 1024)
            if dur < 0.4:
                self._queue.put(("nada",))
                return
            if len(dados) > 24 * 1024 * 1024:
                self._queue.put(("error", "Gravação longa demais (máx ~12 min)"))
                return
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_path = tmp.name
            tmp.close()
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(TAXA)
                wf.writeframes(dados)
            try:
                txt = self._chamar_groq(tmp_path)
            finally:
                try: os.unlink(tmp_path)
                except Exception: pass
            # Fala curtíssima que virou só uma palavra típica de alucinação
            norm = txt.strip().strip(".!?…,").lower()
            if fala_dur < 1.2 and norm in self.ALUCINACOES:
                log.info("Alucinação descartada: '%s'", txt)
                txt = ""
            if txt:
                self._queue.put(("done", txt))
            else:
                self._queue.put(("nada",))
        except requests.exceptions.ConnectionError:
            self._queue.put(("error", "Sem conexão com a internet"))
        except requests.exceptions.Timeout:
            self._queue.put(("error", "Groq demorou demais — tente de novo"))
        except _ErroAPI as e:
            self._queue.put(("error", str(e)))
        except Exception as e:
            log.error("Transcrição:\n%s", traceback.format_exc())
            self._queue.put(("error", str(e)[:60]))

    def _chamar_groq(self, caminho: str) -> str:
        token  = self.obter_token()
        modelo = self.cfg.get("modelo", CONFIG_PADRAO["modelo"])
        idioma = (self.cfg.get("idioma") or "pt").strip().lower()
        data = {"model": modelo, "response_format": "json", "temperature": "0"}
        if idioma and idioma != "auto":
            data["language"] = idioma
        # Dicionário personalizado: o Whisper aceita um "prompt" de contexto
        # que aumenta muito a chance de grafar certo nomes e termos próprios.
        dicio = " ".join((self.cfg.get("dicionario") or "").split())
        if dicio:
            data["prompt"] = f"Vocabulário: {dicio}"[:600]
        t0 = time.time()
        with open(caminho, "rb") as f:
            r = requests.post(
                f"{GROQ_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files={"file": ("audio.wav", f, "audio/wav")},
                timeout=180,
            )
        log.info("Groq: HTTP %d em %.1f s", r.status_code, time.time() - t0)
        if r.status_code == 401:
            raise _ErroAPI("Token do Groq inválido")
        if r.status_code == 429:
            raise _ErroAPI("Limite do Groq atingido — aguarde um pouco")
        if r.status_code != 200:
            log.error("Groq %d: %s", r.status_code, r.text[:500])
            raise _ErroAPI(f"Erro do Groq ({r.status_code})")
        txt = (r.json().get("text") or "").strip()
        log.info("OK — %d caracteres: '%s'", len(txt), txt[:80])
        return txt

    # ── Janela de configurações ────────────────────────────────────────────────

    def _painel_claro(self, w, h, r, fill, border=None):
        """Retângulo arredondado suavizado para a tela clara (fundo SET_BG)."""
        k = 4
        bgc = tuple(int(SET_BG[i:i+2], 16) for i in (1, 3, 5))
        img = Image.new("RGB", (w * k, h * k), bgc)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=r * k,
                            fill=fill, outline=border or fill, width=k)
        return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))

    def _botao(self, parent, txt, cmd, primario=False, w_min=0):
        """Botão de cantos arredondados (raio SOIA), desenhado em PIL."""
        f = tkfont.Font(family=self._fam, size=10 if primario else 9,
                        weight="bold" if primario else "normal")
        W = max(w_min, f.measure(txt) + self.S(34))
        H = self.S(34) if primario else self.S(30)
        R = self.S(9)
        bg = ACCENT if primario else FIELD_BG
        hv = ACCENT_HOV if primario else FIELD_HOV
        fg = WHITE if primario else TINTA
        cv = tk.Canvas(parent, width=W, height=H, bg=SET_BG,
                       highlightthickness=0, cursor="hand2")
        img_n = self._painel_claro(W, H, R, bg)
        img_h = self._painel_claro(W, H, R, hv)
        cv._imgs = (img_n, img_h)
        iid = cv.create_image(0, 0, anchor="nw", image=img_n)
        cv.create_text(W // 2, H // 2, text=txt, fill=fg, font=f)
        cv.bind("<Enter>", lambda e: cv.itemconfig(iid, image=img_h))
        cv.bind("<Leave>", lambda e: cv.itemconfig(iid, image=img_n))
        cv.bind("<Button-1>", lambda e: cmd())
        return cv

    def _caixa_redonda(self, parent, w, h, fill=None, r=None):
        """Canvas com painel arredondado de fundo — abriga campos de texto."""
        cv = tk.Canvas(parent, width=w, height=h, bg=SET_BG,
                       highlightthickness=0)
        img = self._painel_claro(w, h, r or self.S(9), fill or FIELD_BG)
        cv._img = img
        cv.create_image(0, 0, anchor="nw", image=img)
        return cv

    def _abrir_config(self):
        if self._win_cfg is not None and self._win_cfg.winfo_exists():
            self._win_cfg.lift()
            self._win_cfg.focus_force()
            return
        w = tk.Toplevel(self.root)
        self._win_cfg = w
        w.title("SOIA Flow — Configurações")
        w.configure(bg=SET_BG)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.withdraw()   # mostra só depois de montar (evita salto de tamanho)

        try:
            self._logo_tk = ImageTk.PhotoImage(_logo_soia(40))
            w.iconphoto(False, ImageTk.PhotoImage(_logo_soia(32)))
        except Exception:
            self._logo_tk = None

        S = self.S
        LARG  = S(440)
        PADX  = S(26)
        INNER = LARG - 2 * PADX

        def lbl(parent, txt, **kw):
            base = dict(bg=SET_BG, fg=TINTA, font=(self._fam, 10), anchor="w")
            base.update(kw)
            return tk.Label(parent, text=txt, **base)

        def secao(txt, topo=18):
            lbl(w, txt, font=(self._fam, 10, "bold")).pack(
                padx=PADX, pady=(topo, 6), anchor="w")

        def separador():
            tk.Frame(w, bg=SEP, height=1).pack(fill="x", padx=PADX,
                                               pady=(18, 0))

        # ── Cabeçalho com a marca ──
        cab = tk.Frame(w, bg=SET_BG)
        cab.pack(fill="x", padx=PADX, pady=(20, 0))
        if self._logo_tk is not None:
            tk.Label(cab, image=self._logo_tk, bg=SET_BG).pack(side=tk.LEFT)
        cab_txt = tk.Frame(cab, bg=SET_BG)
        cab_txt.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(cab_txt, text="SOIA Flow", bg=SET_BG, fg=TINTA,
                 font=(self._fam, 14, "bold"), anchor="w").pack(anchor="w")
        tk.Label(cab_txt, text="Ditado por voz · SOIA CRC", bg=SET_BG,
                 fg=TINTA_3, font=(self._fam, 9), anchor="w").pack(anchor="w")

        # ── Token ──
        secao("Token do Groq", topo=20)
        cx_tok = self._caixa_redonda(w, INNER, S(36))
        cx_tok.pack(padx=PADX, anchor="w")
        ent_token = tk.Entry(cx_tok, bg=FIELD_BG, fg=TINTA,
                             insertbackground=TINTA, relief="flat",
                             font=("Consolas", 10), show="•",
                             highlightthickness=0, bd=0)
        cx_tok.create_window(S(14), S(18), window=ent_token, anchor="w",
                             width=INNER - S(54))
        ent_token.insert(0, self.obter_token())
        def _toggle_ver(_=None):
            ent_token.config(show="" if ent_token.cget("show") else "•")
        olho = cx_tok.create_text(INNER - S(22), S(18), text="👁",
                                  fill=TINTA_3, font=(self._fam, 10))
        cx_tok.tag_bind(olho, "<Button-1>", _toggle_ver)
        cx_tok.tag_bind(olho, "<Enter>",
                        lambda _: cx_tok.itemconfig(olho, fill=TINTA))
        cx_tok.tag_bind(olho, "<Leave>",
                        lambda _: cx_tok.itemconfig(olho, fill=TINTA_3))
        lbl(w, "Crie o seu em console.groq.com/keys — é gratuito.",
            fg=TINTA_3, font=(self._fam, 8)).pack(padx=PADX, pady=(4, 0),
                                                  anchor="w")

        linha_teste = tk.Frame(w, bg=SET_BG)
        linha_teste.pack(fill="x", padx=PADX, pady=(10, 0))
        lbl_teste = tk.Label(linha_teste, text="", bg=SET_BG, fg=TINTA_3,
                             font=(self._fam, 9))
        def _testar():
            tok = ent_token.get().strip()
            if not tok:
                lbl_teste.config(text="Informe o token primeiro", fg=VERMELHO)
                return
            lbl_teste.config(text="Testando…", fg=TINTA_3)
            def _th():
                try:
                    r = requests.get(f"{GROQ_URL}/models",
                                     headers={"Authorization": f"Bearer {tok}"},
                                     timeout=15)
                    if r.status_code == 200:
                        self._queue.put(("teste",
                                         "✓ Conexão OK — token salvo",
                                         VERDE, tok))
                    elif r.status_code == 401:
                        self._queue.put(("teste", "✕ Token inválido",
                                         VERMELHO, None))
                    else:
                        self._queue.put(("teste", f"✕ Erro {r.status_code}",
                                         VERMELHO, None))
                except Exception:
                    self._queue.put(("teste", "✕ Sem conexão", VERMELHO, None))
            threading.Thread(target=_th, daemon=True).start()
        self._botao(linha_teste, "Testar conexão", _testar).pack(side=tk.LEFT)
        lbl_teste.pack(side=tk.LEFT, padx=(12, 0))
        self._lbl_teste = lbl_teste

        separador()

        # ── Modelo e idioma ──
        secao("Modelo")
        var_modelo = tk.StringVar(value=self.cfg.get("modelo",
                                                     CONFIG_PADRAO["modelo"]))
        for texto, valor in (("Rápido (whisper-large-v3-turbo) — recomendado",
                              "whisper-large-v3-turbo"),
                             ("Máxima qualidade (whisper-large-v3)",
                              "whisper-large-v3")):
            tk.Radiobutton(w, text=texto, variable=var_modelo, value=valor,
                           bg=SET_BG, fg=TINTA, selectcolor=SET_BG,
                           activebackground=SET_BG, activeforeground=TINTA,
                           highlightthickness=0,
                           font=(self._fam, 9)).pack(padx=PADX - 4, anchor="w")

        linha_idi = tk.Frame(w, bg=SET_BG)
        linha_idi.pack(fill="x", padx=PADX, pady=(10, 0))
        tk.Label(linha_idi, text="Idioma:", bg=SET_BG, fg=TINTA,
                 font=(self._fam, 10, "bold")).pack(side=tk.LEFT)
        cx_idi = self._caixa_redonda(linha_idi, S(64), S(30))
        cx_idi.pack(side=tk.LEFT, padx=(10, 8))
        ent_idioma = tk.Entry(cx_idi, bg=FIELD_BG, fg=TINTA,
                              insertbackground=TINTA, relief="flat",
                              font=("Consolas", 10), justify="center",
                              highlightthickness=0, bd=0)
        cx_idi.create_window(S(32), S(15), window=ent_idioma, width=S(48))
        ent_idioma.insert(0, self.cfg.get("idioma", "pt"))
        tk.Label(linha_idi, text="(pt, en, es… ou auto)", bg=SET_BG,
                 fg=TINTA_3, font=(self._fam, 8)).pack(side=tk.LEFT)

        separador()

        # ── Dicionário personalizado ──
        secao("Dicionário personalizado")
        lbl(w, "Nomes e termos do seu dia a dia, separados por vírgula\n"
               "(ex.: SOIA, CRC, Grazziotin). Ajuda o Groq a grafar certo.",
            fg=TINTA_3, font=(self._fam, 8), justify="left").pack(
            padx=PADX, pady=(0, 6), anchor="w")
        cx_dic = self._caixa_redonda(w, INNER, S(78), r=S(10))
        cx_dic.pack(padx=PADX, anchor="w")
        txt_dicio = tk.Text(cx_dic, bg=FIELD_BG, fg=TINTA,
                            insertbackground=TINTA, relief="flat",
                            font=(self._fam, 9), wrap="word",
                            highlightthickness=0, bd=0)
        cx_dic.create_window(INNER // 2, S(39), window=txt_dicio,
                             width=INNER - S(26), height=S(60))
        txt_dicio.insert("1.0", self.cfg.get("dicionario", ""))

        separador()

        # ── Atalho ──
        secao("Atalho global  (segure para falar, solte para transcrever)")
        linha_hk = tk.Frame(w, bg=SET_BG)
        linha_hk.pack(fill="x", padx=PADX)
        self._novo_atalho = self.cfg.get("atalho", CONFIG_PADRAO["atalho"])
        cx_hk = self._caixa_redonda(linha_hk, INNER - S(104), S(32))
        cx_hk.pack(side=tk.LEFT)
        self._hk_cv = cx_hk
        self._hk_item = cx_hk.create_text(S(14), S(16),
                                          text=self._novo_atalho,
                                          fill=TINTA, anchor="w",
                                          font=("Consolas", 10))
        def _capturar():
            if self._capturando:
                return
            self._capturando = True
            cx_hk.itemconfig(self._hk_item,
                             text="Pressione a nova combinação…",
                             fill=TINTA_3)
            self._remover_atalho()   # não disparar gravação durante a captura
            def _th():
                try:
                    combo = keyboard.read_hotkey(suppress=False)
                except Exception:
                    combo = None
                self._queue.put(("captura", combo))
            threading.Thread(target=_th, daemon=True).start()
        self._botao(linha_hk, "Alterar…", _capturar).pack(
            side=tk.LEFT, padx=(S(10), 0))

        separador()

        # ── Comportamento ──
        var_fechar = tk.BooleanVar(value=self.cfg.get("fechar_apos", True))
        var_auto   = tk.BooleanVar(value=self.cfg.get("autostart", False))
        var_paste  = tk.BooleanVar(value=self.cfg.get("autopaste", True))
        estilo_chk = dict(bg=SET_BG, fg=TINTA, selectcolor=SET_BG,
                          activebackground=SET_BG, activeforeground=TINTA,
                          highlightthickness=0, font=(self._fam, 9))
        tk.Checkbutton(w, text="Colar automaticamente onde o cursor estiver",
                       variable=var_paste, **estilo_chk).pack(
            padx=PADX - 4, pady=(16, 0), anchor="w")
        tk.Checkbutton(w, text="Ao terminar, apenas copiar e recolher\n"
                               "(desmarcado: abre a caixa com texto editável)",
                       variable=var_fechar, justify="left",
                       **estilo_chk).pack(padx=PADX - 4, pady=(4, 0),
                                          anchor="w")
        tk.Checkbutton(w, text="Iniciar junto com o Windows",
                       variable=var_auto, **estilo_chk).pack(
            padx=PADX - 4, pady=(4, 0), anchor="w")

        # ── Salvar ──
        def _salvar():
            tok = ent_token.get().strip()
            if tok:
                self.salvar_token(tok)
            self.cfg["modelo"] = var_modelo.get()
            self.cfg["idioma"] = ent_idioma.get().strip().lower() or "pt"
            self.cfg["atalho"] = self._novo_atalho
            self.cfg["fechar_apos"] = var_fechar.get()
            self.cfg["autostart"] = var_auto.get()
            self.cfg["autopaste"] = var_paste.get()
            self.cfg["dicionario"] = txt_dicio.get("1.0", "end-1c").strip()
            _save_config(self.cfg)
            try:
                _aplicar_autostart(var_auto.get())
            except Exception as e:
                log.error("autostart: %s", e)
            self._registrar_atalho()
            w.destroy()
        rodape = tk.Frame(w, bg=SET_BG)
        rodape.pack(fill="x", padx=PADX, pady=(24, 10))
        self._botao(rodape, "Salvar", _salvar, primario=True,
                    w_min=INNER).pack()
        tk.Label(w, text=f"SOIA Flow {VERSAO}", bg=SET_BG,
                 fg="#a6acb5", font=(self._fam, 8)).pack(pady=(0, 10))

        def _fechar():
            # Se fechou sem salvar durante uma captura, restaura o atalho atual
            if self._hk_main is None and not self._capturando:
                self._registrar_atalho()
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", _fechar)

        # Altura ajustada ao conteúdo (nunca corta o Salvar) e centraliza
        w.update_idletasks()
        altura = w.winfo_reqheight()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"{LARG}x{altura}+{(sw-LARG)//2}+{(sh-altura)//2}")
        w.deiconify()

    def _fim_captura(self, combo):
        self._capturando = False
        if combo:
            self._novo_atalho = combo
        try:
            if self._hk_cv is not None and self._hk_cv.winfo_exists():
                self._hk_cv.itemconfig(self._hk_item,
                                       text=self._novo_atalho, fill=TINTA)
        except Exception:
            pass
        # Reativa o atalho vigente (o novo só vale após Salvar)
        self._registrar_atalho()

    def _mostrar_teste(self, msg, cor, token_ok):
        if token_ok:
            # Teste passou → já persiste o token, sem depender do Salvar
            self.salvar_token(token_ok)
        if self._lbl_teste is not None and self._lbl_teste.winfo_exists():
            self._lbl_teste.config(text=msg, fg=cor)

    def run(self):
        self.root.mainloop()


class _ErroAPI(Exception):
    pass


def _aplicar_autostart(ativo: bool):
    """Cria/remove o .bat na pasta Inicializar do Windows."""
    startup = os.path.join(os.environ["APPDATA"],
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
    bat = os.path.join(startup, "SOIAFlow.bat")
    # Remove o nome antigo, de antes do rebrand
    legado = os.path.join(startup, "TranscritorDesktop.bat")
    if os.path.exists(legado):
        try: os.remove(legado)
        except Exception: pass
    if ativo:
        if getattr(sys, "frozen", False):
            # Empacotado com PyInstaller: o executável é o próprio app
            comando = f'start "" "{sys.executable}"'
        else:
            py  = sys.executable
            pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = py
            script = os.path.abspath(__file__)
            comando = f'start "" "{pyw}" "{script}"'
        with open(bat, "w", encoding="ascii", errors="replace") as f:
            f.write(f"@echo off\n{comando}\n")
        log.info("Autostart criado: %s", bat)
    elif os.path.exists(bat):
        os.remove(bat)
        log.info("Autostart removido")


if __name__ == "__main__":
    try:
        App().run()
    except Exception:
        log.error("Fatal:\n%s", traceback.format_exc())
        raise
