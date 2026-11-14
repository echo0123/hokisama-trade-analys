import base64
import http.cookiejar
import json
import os
import random
import re
import socket
import ssl
import string
import struct
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

Window.clearcolor = (0.025, 0.045, 0.08, 1)

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b"


class TradingViewNativeClient:
    """Klien Real-Time WebSocket TradingView & Auth tanpa dependensi eksternal"""

    def __init__(self, config_path):
        self.config_path = config_path
        self.cfg = self._load_cfg()
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126.0.0.0 Mobile Safari/537.36"),
            ("Origin", "https://www.tradingview.com"),
            ("Referer", "https://www.tradingview.com/"),
        ]
        self.auth_token = "unauthorized_user_token"

    def _load_cfg(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"sessionid": "", "sessionid_sign": "", "username": "Guest Feed", "model": DEFAULT_MODEL, "last_symbol": "OANDA:XAUUSD"}

    def save_cfg(self):
        with open(self.config_path, "w") as f:
            json.dump(self.cfg, f, indent=2)

    def login_credentials(self, username, password):
        url = "https://www.tradingview.com/accounts/signin/"
        data = urllib.parse.urlencode({"username": username, "password": password, "remember": "on"}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with self.opener.open(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if "user" in res:
                    sid, sign = "", ""
                    for c in self.cj:
                        if c.name == "sessionid":
                            sid = c.value
                        elif c.name == "sessionid_sign":
                            sign = c.value
                    self.auth_token = res["user"].get("auth_token", "unauthorized_user_token")
                    uname = res["user"].get("username", username)
                    self.cfg["sessionid"] = sid
                    self.cfg["sessionid_sign"] = sign
                    self.cfg["username"] = uname
                    self.save_cfg()
                    return True, f"Terhubung sebagai {uname}"
                return False, res.get("error", "Login gagal. Gunakan Session ID jika akun Google.")
        except Exception as e:
            return False, f"Error koneksi: {str(e)}"

    def login_session(self, sessionid, sessionid_sign=""):
        if not sessionid.strip():
            return False, "Session ID kosong."
        cookie_header = f"sessionid={sessionid.strip()}"
        if sessionid_sign.strip():
            cookie_header += f"; sessionid_sign={sessionid_sign.strip()}"
        try:
            req = urllib.request.Request("https://www.tradingview.com/", headers={"Cookie": cookie_header})
            with self.opener.open(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                t_match = re.search(r'"auth_token":"([^"]+)"', html)
                u_match = re.search(r'"username":"([^"]+)"', html)
                if t_match:
                    self.auth_token = t_match.group(1)
                    uname = u_match.group(1) if u_match else "TV_Pro_User"
                    self.cfg["sessionid"] = sessionid.strip()
                    self.cfg["sessionid_sign"] = sessionid_sign.strip()
                    self.cfg["username"] = uname
                    self.save_cfg()
                    return True, f"Terhubung ke TradingView: {uname}"
                return False, "Session ID tidak valid / kadaluarsa."
        except Exception as e:
            return False, f"Gagal verifikasi: {str(e)}"

    def _refresh_token(self):
        sid = self.cfg.get("sessionid", "")
        if not sid:
            return
        cookie_header = f"sessionid={sid}"
        if self.cfg.get("sessionid_sign"):
            cookie_header += f"; sessionid_sign={self.cfg['sessionid_sign']}"
        try:
            req = urllib.request.Request("https://www.tradingview.com/", headers={"Cookie": cookie_header})
            with self.opener.open(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                t_match = re.search(r'"auth_token":"([^"]+)"', html)
                if t_match:
                    self.auth_token = t_match.group(1)
        except Exception:
            pass

    @staticmethod
    def _ws_send(sock, text):
        payload = text.encode("utf-8")
        length = len(payload)
        mask = os.urandom(4)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = bytearray(payload[i] ^ mask[i % 4] for i in range(length))
        sock.sendall(header + masked)

    @staticmethod
    def _ws_recv(sock):
        hdr = sock.recv(2)
        if len(hdr) < 2:
            return None
        opcode = hdr[0] & 0x0F
        if opcode == 8:
            return None
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", sock.recv(8))[0]
        data = bytearray()
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if opcode == 9:
            sock.sendall(bytearray([0x8A, 0x80]) + os.urandom(4))
            return ""
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _pack_tv(func, args):
        msg = json.dumps({"m": func, "p": args}, separators=(",", ":"))
        return f"~m~{len(msg)}~m~{msg}"

    def fetch_ohlcv(self, symbol="OANDA:XAUUSD", timeframe="15", bars=120):
        self._refresh_token()
        host = "data.tradingview.com"
        path = "/socket.io/websocket?from=chart%2F"
        try:
            raw_sock = socket.create_connection((host, 443), timeout=12)
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        except Exception as e:
            return None, None, f"Koneksi internet/TV gagal: {str(e)}"

        ws_key = base64.b64encode(os.urandom(16)).decode("utf-8")
        cookie_line = ""
        if self.cfg.get("sessionid"):
            cookie_line = f"Cookie: sessionid={self.cfg['sessionid']}"
            if self.cfg.get("sessionid_sign"):
                cookie_line += f"; sessionid_sign={self.cfg['sessionid_sign']}"
            cookie_line += "\r\n"

        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: https://www.tradingview.com\r\n"
            f"User-Agent: Mozilla/5.0 (Linux; Android 14)\r\n"
            f"{cookie_line}\r\n"
        )
        sock.sendall(handshake.encode("utf-8"))
        resp_hdr = b""
        while b"\r\n\r\n" not in resp_hdr:
            resp_hdr += sock.recv(1024)

        cs = "cs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        qs = "qs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))

        self._ws_send(sock, self._pack_tv("set_auth_token", [self.auth_token]))
        self._ws_send(sock, self._pack_tv("chart_create_session", [cs, ""]))
        self._ws_send(sock, self._pack_tv("quote_create_session", [qs]))
        self._ws_send(sock, self._pack_tv("quote_add_symbols", [qs, symbol]))
        sym_cfg = json.dumps({"symbol": symbol, "adjustment": "splits"})
        self._ws_send(sock, self._pack_tv("resolve_symbol", [cs, "sds_sym_1", f"={sym_cfg}"]))
        self._ws_send(sock, self._pack_tv("create_series", [cs, "sds_1", "s1", "sds_sym_1", timeframe, bars, ""]))

        candles = []
        live_price = None
        start_time = time.time()

        try:
            while time.time() - start_time < 9:
                frame = self._ws_recv(sock)
                if frame is None:
                    break
                if "~h~" in frame:
                    for ping in re.findall(r"~m~\d+~m~(~h~\d+)", frame):
                        self._ws_send(sock, f"~m~{len(ping)}~m~{ping}")
                    continue

                for pkt in re.split(r"~m~\d+~m~", frame):
                    if not pkt.startswith("{"):
                        continue
                    data = json.loads(pkt)
                    m_type = data.get("m")
                    if m_type == "qsd":
                        v = data["p"][1].get("v", {})
                        if "lp" in v:
                            live_price = float(v["lp"])
                    elif m_type == "timescale_update":
                        series = data["p"][1].get("sds_1", {}).get("s", [])
                        for item in series:
                            v = item.get("v", [])
                            if len(v) >= 6:
                                candles.append({
                                    "time": v[0],
                                    "open": float(v[1]),
                                    "high": float(v[2]),
                                    "low": float(v[3]),
                                    "close": float(v[4]),
                                    "volume": float(v[5]),
                                })
                if len(candles) >= min(bars, 50):
                    break
        finally:
            sock.close()

        if not candles:
            return None, None, "Simbol tidak valid. Gunakan format BROKER:SIMBOL (misal OANDA:XAUUSD / BINANCE:BTCUSDT)."
        if live_price is None:
            live_price = candles[-1]["close"]
        return candles, live_price, "OK"


def calculate_quant_metrics(candles, live_price):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    def calc_ema(vals, period):
        alpha = 2 / (period + 1)
        res = [vals[0]]
        for x in vals[1:]:
            res.append(alpha * x + (1 - alpha) * res[-1])
        return res[-1]

    ema20 = round(calc_ema(closes, 20), 4)
    ema50 = round(calc_ema(closes, 50), 4)
    ema200 = round(calc_ema(closes, min(200, len(closes))), 4)

    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-14:]) / max(len(gains[-14:]), 1)
    avg_l = sum(losses[-14:]) / max(len(losses[-14:]), 1)
    rsi14 = 100.0 if avg_l == 0 else round(100 - (100 / (1 + (avg_g / avg_l))), 2)

    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr14 = round(sum(trs[-14:]) / max(len(trs[-14:]), 1), 4)

    swing_high = round(max(highs[-30:]), 4)
    swing_low = round(min(lows[-30:]), 4)

    if live_price > ema20 > ema50:
        bias = "BUY"
        structure = "BULLISH BOS (Higher High Structure)"
        entry = live_price
        sl = round(entry - 1.5 * atr14, 4)
        tp1 = round(entry + 1.5 * atr14, 4)
        tp2 = round(entry + 3.0 * atr14, 4)
        tp3 = round(entry + 4.5 * atr14, 4)
    elif live_price < ema20 < ema50:
        bias = "SELL"
        structure = "BEARISH BOS (Lower Low Structure)"
        entry = live_price
        sl = round(entry + 1.5 * atr14, 4)
        tp1 = round(entry - 1.5 * atr14, 4)
        tp2 = round(entry - 3.0 * atr14, 4)
        tp3 = round(entry - 4.5 * atr14, 4)
    else:
        bias = "WAIT"
        structure = "CONSOLIDATION / LIQUIDITY SWEEP"
        entry = live_price
        sl = round(swing_low - 0.5 * atr14, 4)
        tp1 = swing_high
        tp2 = round(swing_high + atr14, 4)
        tp3 = round(swing_high + 2 * atr14, 4)

    return {
        "price": live_price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi14,
        "atr14": atr14,
        "bias": bias,
        "structure": structure,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }


class HokisamaTradeApp(App):
    def build(self):
        self.title = "Hokisama Trade Analys"
        cfg_file = os.path.join(self.user_data_dir, "hokisama_config.json")
        self.tv = TradingViewNativeClient(cfg_file)
        self.last_context = ""

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # HEADER
        header = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        title_box = BoxLayout(orientation="vertical")
        title_lbl = Label(
            text="[b][color=#00f3ff]HOKISAMA TRADE ANALYS[/color][/b]",
            markup=True,
            font_size=dp(17),
            halign="left",
            valign="middle",
        )
        title_lbl.bind(size=title_lbl.setter("text_size"))

        self.status_lbl = Label(
            text=f"[color=#ffaa00]TV: {self.tv.cfg.get('username')} | AI: Checking...[/color]",
            markup=True,
            font_size=dp(11),
            halign="left",
            valign="middle",
        )
        self.status_lbl.bind(size=self.status_lbl.setter("text_size"))
        title_box.add_widget(title_lbl)
        title_box.add_widget(self.status_lbl)

        btn_login = Button(
            text="TV LOGIN",
            size_hint_x=None,
            width=dp(90),
            background_color=(0.0, 0.55, 0.75, 1),
            font_size=dp(12),
            bold=True,
        )
        btn_login.bind(on_release=self.open_login_popup)
        header.add_widget(title_box)
        header.add_widget(btn_login)
        root.add_widget(header)

        # WATCHLIST QUICK BAR
        quick_bar = BoxLayout(size_hint_y=None, height=dp(32), spacing=dp(4))
        for sym in ["OANDA:XAUUSD", "BINANCE:BTCUSDT", "OANDA:EURUSD", "FOREXCOM:NSXUSD"]:
            b = Button(text=sym.split(":")[1], background_color=(0.08, 0.14, 0.22, 1), font_size=dp(11))
            b.bind(on_release=lambda inst, s=sym: setattr(self.symbol_input, "text", s))
            quick_bar.add_widget(b)
        root.add_widget(quick_bar)

        # INPUT & SCAN CONTROLS
        ctrl = GridLayout(cols=3, size_hint_y=None, height=dp(44), spacing=dp(6))
        self.symbol_input = TextInput(
            text=self.tv.cfg.get("last_symbol", "OANDA:XAUUSD"),
            multiline=False,
            background_color=(0.07, 0.11, 0.17, 1),
            foreground_color=(0, 1, 0.85, 1),
            font_size=dp(13),
            padding=[dp(8), dp(10)],
        )
        self.tf_spinner = Spinner(
            text="15",
            values=("1", "5", "15", "60", "240", "D"),
            size_hint_x=None,
            width=dp(64),
            background_color=(0.08, 0.16, 0.25, 1),
        )
        self.btn_scan = Button(
            text="SCAN AI",
            size_hint_x=None,
            width=dp(92),
            background_color=(0.0, 0.72, 0.48, 1),
            font_size=dp(13),
            bold=True,
        )
        self.btn_scan.bind(on_release=self.start_scan)
        ctrl.add_widget(self.symbol_input)
        ctrl.add_widget(self.tf_spinner)
        ctrl.add_widget(self.btn_scan)
        root.add_widget(ctrl)

        # TERMINAL OUTPUT
        scroll = ScrollView(size_hint=(1, 1))
        self.output_lbl = Label(
            text=(
                "[color=#00f3ff]>>> HOKISAMA NEURAL TERMINAL READY[/color]\n"
                "1. Pastikan Ollama aktif di Termux ([color=#00ff99]ollama serve[/color])\n"
                "2. Tekan [b]TV LOGIN[/b] untuk sinkronisasi akun TradingView\n"
                "3. Pilih aset lalu tekan [b]SCAN AI[/b] untuk analisis real-time.\n\n"
                "[color=#667788]created by echogaul corp[/color]"
            ),
            markup=True,
            font_size=dp(13),
            size_hint_y=None,
            halign="left",
            valign="top",
            padding=(dp(8), dp(8)),
        )
        self.output_lbl.bind(texture_size=self.output_lbl.setter("size"))
        self.output_lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w - dp(16), None)))
        scroll.add_widget(self.output_lbl)
        root.add_widget(scroll)

        # AI CHAT BAR
        chat_row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        self.chat_input = TextInput(
            hint_text="Tanya AI soal setup market ini...",
            multiline=False,
            background_color=(0.07, 0.11, 0.17, 1),
            foreground_color=(1, 1, 1, 1),
            font_size=dp(12),
            padding=[dp(8), dp(10)],
        )
        btn_ask = Button(text="TANYA", size_hint_x=None, width=dp(75), background_color=(0.15, 0.35, 0.65, 1), bold=True)
        btn_ask.bind(on_release=self.ask_chat)
        chat_row.add_widget(self.chat_input)
        chat_row.add_widget(btn_ask)
        root.add_widget(chat_row)

        footer = Label(
            text="[color=#4f6d7a]Hokisama Trade Analys v1.0 | created by echogaul corp[/color]",
            markup=True,
            size_hint_y=None,
            height=dp(20),
            font_size=dp(10),
        )
        root.add_widget(footer)

        Clock.schedule_once(lambda dt: threading.Thread(target=self.check_ai_status, daemon=True).start(), 1)
        return root

    def check_ai_status(self):
        ai_txt = "OFFLINE (Buka Termux)"
        ok = False
        try:
            with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
                tags = json.loads(r.read().decode("utf-8")).get("models", [])
                if tags:
                    self.tv.cfg["model"] = tags[0]["name"]
                    ai_txt = f"ONLINE ({tags[0]['name']})"
                    ok = True
        except Exception:
            pass
        color = "#00ff99" if ok else "#ffaa00"
        Clock.schedule_once(
            lambda dt: setattr(
                self.status_lbl,
                "text",
                f"[color={color}]TV: {self.tv.cfg.get('username')} | AI: {ai_txt}[/color]",
            )
        )

    def open_login_popup(self, instance):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        box.add_widget(Label(text="Login Username/Email & Password TV:", size_hint_y=None, height=dp(22), font_size=dp(12)))
        u_in = TextInput(hint_text="Username / Email", multiline=False, size_hint_y=None, height=dp(38))
        p_in = TextInput(hint_text="Password", password=True, multiline=False, size_hint_y=None, height=dp(38))
        box.add_widget(u_in)
        box.add_widget(p_in)

        box.add_widget(Label(text="Atau Cookie sessionid (jika akun Google):", size_hint_y=None, height=dp(22), font_size=dp(12)))
        sid_in = TextInput(text=self.tv.cfg.get("sessionid", ""), hint_text="sessionid", multiline=False, size_hint_y=None, height=dp(38))
        sign_in = TextInput(text=self.tv.cfg.get("sessionid_sign", ""), hint_text="sessionid_sign (opsional)", multiline=False, size_hint_y=None, height=dp(38))
        box.add_widget(sid_in)
        box.add_widget(sign_in)

        msg_lbl = Label(text="", markup=True, size_hint_y=None, height=dp(28), font_size=dp(11))
        box.add_widget(msg_lbl)

        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        b_ok = Button(text="HUBUNGKAN", background_color=(0, 0.65, 0.45, 1), bold=True)
        b_close = Button(text="TUTUP", background_color=(0.45, 0.15, 0.15, 1))
        row.add_widget(b_ok)
        row.add_widget(b_close)
        box.add_widget(row)

        pop = Popup(title="TradingView Auth", content=box, size_hint=(0.92, 0.72))
        b_close.bind(on_release=pop.dismiss)

        def do_connect(btn):
            msg_lbl.text = "[color=#00f3ff]Menghubungkan...[/color]"

            def worker():
                if sid_in.text.strip():
                    ok, info = self.tv.login_session(sid_in.text, sign_in.text)
                else:
                    ok, info = self.tv.login_credentials(u_in.text.strip(), p_in.text.strip())

                def ui(dt):
                    c = "#00ff99" if ok else "#ff5555"
                    msg_lbl.text = f"[color={c}]{info}[/color]"
                    if ok:
                        self.check_ai_status()

                Clock.schedule_once(ui)

            threading.Thread(target=worker, daemon=True).start()

        b_ok.bind(on_release=do_connect)
        pop.open()

    def start_scan(self, instance):
        sym = self.symbol_input.text.strip().upper()
        if ":" not in sym:
            sym = "OANDA:" + sym
            self.symbol_input.text = sym
        tf = self.tf_spinner.text
        self.tv.cfg["last_symbol"] = sym
        self.tv.save_cfg()
        self.btn_scan.disabled = True
        self.output_lbl.text = f"[color=#00f3ff]>>> Mengambil data real-time {sym} (TF {tf}) dari TradingView...[/color]"
        threading.Thread(target=self._run_pipeline, args=(sym, tf), daemon=True).start()

    def _run_pipeline(self, sym, tf):
        candles, live_price, status = self.tv.fetch_ohlcv(sym, tf, 120)
        if not candles:
            Clock.schedule_once(lambda dt: self._done(f"[color=#ff5555]GAGAL:[/color] {status}"))
            return

        q = calculate_quant_metrics(candles, live_price)
        ctx = (
            f"ASSET: {sym} | TF: {tf} | WAKTU: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"LIVE PRICE: {q['price']} | STRUKTUR: {q['structure']}\n"
            f"EMA20: {q['ema20']} | EMA50: {q['ema50']} | EMA200: {q['ema200']}\n"
            f"RSI(14): {q['rsi14']} | ATR(14): {q['atr14']}\n"
            f"QUANT SIGNAL: {q['bias']} | ENTRY: {q['entry']} | SL: {q['sl']} | "
            f"TP1: {q['tp1']} | TP2: {q['tp2']} | TP3: {q['tp3']}"
        )
        self.last_context = ctx

        Clock.schedule_once(
            lambda dt: setattr(
                self.output_lbl,
                "text",
                f"[color=#00ff99]Harga Live: {live_price}[/color]\n[color=#00f3ff]>>> Menganalisis dengan Local AI Ollama...[/color]",
            )
        )

        prompt = (
            f"Kamu adalah Senior Institutional Market Analyst untuk 'Hokisama Trade Analys'. "
            f"Analisa data market REAL-TIME berikut secara tajam dan presisi:\n\n{ctx}\n\n"
            f"Format output Bahasa Indonesia:\n"
            f"1. KEPUTUSAN: [BUY / SELL / WAIT] + Confidence (%)\n"
            f"2. SETUP: Entry {q['entry']}, SL {q['sl']}, TP1 {q['tp1']}, TP2 {q['tp2']}, TP3 {q['tp3']}\n"
            f"3. ANALISIS SMC & INDIKATOR: Konfluensi EMA, RSI, BOS/Liquidity, dan ATR.\n"
            f"4. INVALIDASI SETUP."
        )
        ai_reply = self._query_ollama(prompt)
        b_col = "#00ff99" if q["bias"] == "BUY" else ("#ff4466" if q["bias"] == "SELL" else "#ffaa00")
        report = (
            f"[b][color=#00f3ff]=== HOKISAMA AI REPORT ===[/color][/b]\n"
            f"[b]Asset:[/b] {sym} (TF {tf}) | [b]Live:[/b] [color=#00ff99]{live_price}[/color]\n"
            f"[b]Bias:[/b] [color={b_col}][b]{q['bias']}[/b][/color] | [b]RSI:[/b] {q['rsi14']} | [b]ATR:[/b] {q['atr14']}\n"
            f"----------------------------------------\n"
            f"{ai_reply}\n"
            f"----------------------------------------\n"
            f"[color=#667788]created by echogaul corp[/color]"
        )
        Clock.schedule_once(lambda dt: self._done(report))

    def _query_ollama(self, prompt):
        model = self.tv.cfg.get("model", DEFAULT_MODEL)
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 550},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8")).get("response", "Tidak ada respon AI.")
        except Exception as e:
            return f"[Koneksi Ollama Gagal: {str(e)}]\nPastikan 'ollama serve' berjalan di Termux."

    def ask_chat(self, instance):
        q = self.chat_input.text.strip()
        if not q:
            return
        self.chat_input.text = ""
        prev = self.output_lbl.text
        self.output_lbl.text = prev + f"\n\n[color=#00f3ff][b]ANDA:[/b] {q}[/color]\n[color=#ffaa00]AI sedang menganalisis...[/color]"

        def worker():
            ans = self._query_ollama(f"Data market:\n{self.last_context}\n\nPertanyaan: {q}\nJawab singkat & tajam.")
            Clock.schedule_once(
                lambda dt: setattr(
                    self.output_lbl,
                    "text",
                    prev + f"\n\n[color=#00f3ff][b]ANDA:[/b] {q}[/color]\n[color=#00ff99][b]HOKISAMA AI:[/b][/color]\n{ans}",
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, txt):
        self.output_lbl.text = txt
        self.btn_scan.disabled = False


if __name__ == "__main__":
    HokisamaTradeApp().run()
